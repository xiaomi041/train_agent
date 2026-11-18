"""
盯票常驻 worker：不依赖浏览器，按间隔轮询所有活跃盯票任务。

为什么需要它
------------
`watch.Watcher.start()` 把轮询线程挂在 Streamlit 的 session_state 上，
**关掉网页线程就没了** —— 这不是"后台盯着"，只是"开着页面时盯着"。
本进程独立于 Web，与 reminder-worker 同构：

    python watch_worker.py            # 常驻
    python watch_worker.py --once     # 只跑一轮（调试用）

设计要点
--------
1. 每 `WATCH_TICK_SECONDS` 秒扫一次活跃任务（默认 20s，任务自身的 interval_sec 才是真正间隔）。
2. **自适应退避**：同一个任务连续「等待」时把间隔放大到 1.5×、2.25×…最多 `WATCH_BACKOFF_MAX` 倍；
   一旦命中或升档立即复位。理由见 `_next_interval()` 的注释 —— 没票的时候每分钟查一次
   纯属浪费 12306 的接口额度，也更容易被限流。
3. 决策与事件全部落 SQLite（`watch.Store`），Web 端刷新即可看到 worker 写下的记录。
4. 通知走 `notify.send`，失败只记日志（不阻断轮询）。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import notify
import watch

LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format=LOG_FORMAT,
    stream=sys.stderr,
)
for _noisy in ("httpx", "httpx2", "httpcore", "urllib3", "requests", "openai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("watch_worker")

TICK_SECONDS = float(os.getenv("WATCH_TICK_SECONDS", "20"))
BACKOFF_MAX = float(os.getenv("WATCH_BACKOFF_MAX", "4"))
# 单轮最多处理多少个任务：防止任务堆积时一次 tick 打满接口
MAX_PER_ROUND = int(os.getenv("WATCH_MAX_PER_ROUND", "20"))
# 一轮里并行查几个任务。每个任务的一次 tick 要发 1~9 个 HTTP 请求（直连/中转/多段），
# 串行时一个任务就可能吃掉 1 秒以上，30 个任务根本赶不上 20 秒的节拍。
CONCURRENCY = max(1, int(os.getenv("WATCH_CONCURRENCY", "4")))
# 每隔多久裁一次事件表（秒）。裁剪本身很快，但没必要每轮都做。
PRUNE_EVERY = int(os.getenv("WATCH_PRUNE_SECONDS", "1800"))
PRUNE_KEEP_PER_GOAL = int(os.getenv("WATCH_KEEP_EVENTS", "200"))
PRUNE_KEEP_DAYS = int(os.getenv("WATCH_KEEP_DAYS", "30"))

# 「等待」意味着"现在没票/暂时失败"，继续按原频率查收益很低
_CALM_ACTIONS = {"等待"}


def _next_interval(base: int, factor: float) -> float:
    """实际间隔 = 任务配置的间隔 × 退避系数。

    为什么退避：`interval_sec` 是用户期望的"最短检查间隔"，但在两个小时的没票窗口里
    每 5 分钟查一次，等于给 12306 发 24 次无效请求。命中/升档是"有信息量"的事件，
    此时复位到 1×；连续无信息就逐步拉长到 4×，最坏情况下也只是把响应延迟到 20 分钟。
    """
    return max(30.0, float(base) * max(1.0, factor))


class WatchWorker:
    def __init__(
        self,
        store: Optional[watch.Store] = None,
        fetcher: Optional[watch.Fetcher] = None,
        notifier: Optional[Callable[[str], dict]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store or watch.Store()
        self.fetcher = fetcher or watch.real_fetcher
        self.notifier = notifier if notifier is not None else notify.send
        self.clock = clock
        self._due: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self._pool: Optional[ThreadPoolExecutor] = None
        self._stop = False

    # ---------- 单轮 ----------
    def run_once(self) -> list[dict]:
        """扫一轮到期的活跃任务，返回每条的简报（便于测试与日志）。"""
        results: list[dict] = []
        try:
            goals = self.store.list_goals("active")
        except Exception as e:  # 数据库被锁/文件损坏都不能让进程退出
            log.warning("读取盯票任务失败（本轮跳过）：%r", e)
            return results

        now = self.clock()
        due = [g for g in goals if self._due.get(g.id, 0.0) <= now]
        # 按等待时间排序，保证先建的先查，不会因为任务多而饿死后面的
        due.sort(key=lambda g: self._due.get(g.id, 0.0))
        batch = due[:MAX_PER_ROUND]
        if len(due) > MAX_PER_ROUND:
            log.warning("本轮有 %d 个任务到期，只处理前 %d 个", len(due), MAX_PER_ROUND)

        # 并发执行：每个任务内部仍是串行的 HTTP，但多个任务之间并行。
        # 数据库有锁、SQLite 开了 WAL，多个线程写事件不会互相阻塞。
        if CONCURRENCY > 1 and len(batch) > 1:
            # 复用线程池：每轮新建线程池在 Windows 上要 60 ms 左右，
            # 而任务少的时候一轮本来就只要几毫秒 —— 建池比干活还贵。
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=CONCURRENCY,
                                                thread_name_prefix="watch")
            for brief in self._pool.map(lambda g: self._run_one(g, now), batch):
                results.append(brief)
        else:
            for goal in batch:
                results.append(self._run_one(goal, now))

        self._maybe_prune()
        return results

    def _maybe_prune(self) -> None:
        """定期裁剪事件表。长时间盯票时这是唯一会无限增长的表。"""
        if PRUNE_EVERY <= 0:
            return
        now = time.time()
        if now - self._last_prune < PRUNE_EVERY:
            return
        self._last_prune = now
        try:
            removed = self.store.prune_events(PRUNE_KEEP_PER_GOAL, PRUNE_KEEP_DAYS)
            if removed:
                log.info("裁剪事件表：删除 %d 条，当前 %d 条 / %.1f KB",
                         removed, self.store.event_count(), self.store.db_bytes() / 1024)
        except Exception as e:
            log.warning("裁剪事件表失败（不影响轮询）：%r", e)

    def _run_one(self, goal: watch.Goal, now: float) -> dict:
        before_level = goal.relax_level
        try:
            d = watch.Watcher(goal, self.fetcher, self.store, notifier=self.notifier).tick()
        except Exception as e:
            log.warning("任务 %s 执行异常（保留任务，下轮重试）：%r", goal.id, e)
            with self._lock:
                self._due[goal.id] = now + _next_interval(
                    goal.interval_sec, self._backoff.get(goal.id, 1.0))
            return {"id": goal.id, "action": "异常", "reason": repr(e)}

        # 有信息量的变化（命中 / 升档）复位退避；纯等待则放大
        informative = d.action in ("命中", "降级", "放弃") or goal.relax_level != before_level
        with self._lock:
            factor = 1.0 if informative else min(BACKOFF_MAX, self._backoff.get(goal.id, 1.0) * 1.5)
            self._backoff[goal.id] = factor
            interval = _next_interval(goal.interval_sec, factor)
            self._due[goal.id] = now + interval

        brief = {
            "id": goal.id,
            "action": d.action,
            "level": d.level,
            "reason": d.reason,
            "interval": round(interval, 1),
        }
        if d.action == "命中":
            log.info("🎯 命中 %s | %s", goal.id, d.summary())
            with self._lock:
                self._backoff[goal.id] = 1.0
        elif d.action == "放弃":
            log.info("⛔ 放弃 %s | %s", goal.id, d.reason)
            with self._lock:
                self._due.pop(goal.id, None)
        else:
            log.info("⏳ %s | 第 %d 级 | %s | 下次 %ss", goal.id, d.level, d.reason, round(interval))
        return brief

    # ---------- 常驻 ----------
    def start(self) -> None:
        log.info(
            "盯票 worker 启动 | 活跃任务轮询间隔 ≤ %ss | 退避上限 %sx | 单轮上限 %d",
            TICK_SECONDS, BACKOFF_MAX, MAX_PER_ROUND,
        )
        while not self._stop:
            self.run_once()
            self._sleep(TICK_SECONDS)
        log.info("盯票 worker 已停止")

    def _sleep(self, seconds: float) -> None:
        """可被信号打断的睡眠：分成 1s 片，停止指令最多 1s 生效。"""
        end = time.time() + seconds
        while not self._stop and time.time() < end:
            time.sleep(min(1.0, max(0.0, end - time.time())))

    def stop(self) -> None:
        self._stop = True
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="盯票常驻 worker")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出（调试/CI 用）")
    args = parser.parse_args(argv)

    worker = WatchWorker()

    def _shutdown(signum, _frame):
        log.info("收到信号 %s，准备退出", signum)
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    if args.once:
        briefs = worker.run_once()
        if not briefs:
            print("本轮没有到期的活跃任务")
        for b in briefs:
            print(f"{b['id']} | {b['action']} | 第 {b.get('level')} 级 | {b['reason']} | 下次 {b.get('interval')}s")
        return 0

    worker.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
