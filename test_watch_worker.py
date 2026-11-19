"""watch_worker.py 的离线测试：验证"到期才跑、退避、命中复位、异常不崩"。

run: python test_watch_worker.py
"""
from __future__ import annotations

import sys
import shutil
import tempfile
import traceback
from pathlib import Path

import watch
import watch_worker

PASS, FAIL = 0, 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def make_goal(**kw):
    base = dict(origin="十堰", dest="哈尔滨", date="2026-11-20", budget=500,
                max_minutes=840, arrive_after="06:00", arrive_before="22:00",
                seat_prefs=["硬卧"], max_attempts=50, interval_sec=60)
    base.update(kw)
    return watch.Goal(**base)


def make_scheme(**kw):
    base = dict(train_no="K1264", dep_station="十堰", arr_station="哈尔滨",
                dep_time="10:00", arr_time="20:00", duration_min=600,
                price=420, seat="硬卧", date="2026-11-20")
    base.update(kw)
    return watch.Scheme(**base)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


TMP_DIRS: list = []


def temp_store() -> watch.Store:
    d = tempfile.mkdtemp(prefix="watchworker_")
    TMP_DIRS.append(d)
    return watch.Store(Path(d) / "watch.db")


# ---------------------------------------------------------------- 1
def test_only_due_goals():
    print("\n[1] 到期才执行：间隔未到不会重复打接口")
    store = temp_store()
    clock = Clock()
    calls = {"n": 0}

    def fetcher(g):
        calls["n"] += 1
        # 有票但超预算：任务不会立刻结束，才能验证"到期再执行"
        return [make_scheme(price=1500)]

    g = make_goal()
    store.save_goal(g)
    w = watch_worker.WatchWorker(store=store, fetcher=fetcher, notifier=lambda t: {}, clock=clock)

    w.run_once()
    first = calls["n"]
    ok("第一轮确实查了接口", first >= 1, first)
    w.run_once()
    ok("间隔未到不重复查（调用次数不变）", calls["n"] == first, f"{first} -> {calls['n']}")

    clock.advance(61)
    w.run_once()
    ok("到期后再次查询", calls["n"] > first, f"{first} -> {calls['n']}")


# ---------------------------------------------------------------- 2
def test_backoff_while_waiting():
    print("\n[2] 持续没票时退避：间隔逐步放大，最多 4 倍")
    store = temp_store()
    clock = Clock()
    fetcher = lambda g: []  # noqa: E731  没票

    g = make_goal()
    store.save_goal(g)
    w = watch_worker.WatchWorker(store=store, fetcher=fetcher, notifier=lambda t: {}, clock=clock)

    intervals = []
    for _ in range(7):
        brief = w.run_once()
        if brief:
            intervals.append(brief[0]["interval"])
        clock.advance(max(intervals[-1] if intervals else 60, 1) + 1)

    wait_part = intervals[3:]  # 前三级在扩搜（降级，不算"没信息"）
    ok("间隔单调不减", all(b >= a for a, b in zip(wait_part, wait_part[1:])),
       intervals)
    ok("确实放大了", wait_part and wait_part[-1] > 60, intervals)
    ok("不超过 4 倍上限", max(intervals) <= 60 * watch_worker.BACKOFF_MAX + 1, intervals)


# ---------------------------------------------------------------- 3
def test_backoff_reset_on_hit():
    print("\n[3] 命中后退避复位：立即恢复成配置的间隔")
    store = temp_store()
    clock = Clock()
    state = {"ticket": False}
    fetcher = lambda g: [make_scheme()] if state["ticket"] else []  # noqa: E731

    g = make_goal()
    store.save_goal(g)
    w = watch_worker.WatchWorker(store=store, fetcher=fetcher, notifier=lambda t: {}, clock=clock)

    for _ in range(5):
        w.run_once()
        clock.advance(1000)

    state["ticket"] = True
    brief = w.run_once()
    ok("本轮命中", brief and brief[0]["action"] == "命中", brief)
    ok("命中后间隔回到 60s", brief and brief[0]["interval"] <= 61, brief)


# ---------------------------------------------------------------- 4
def test_exception_does_not_kill():
    print("\n[4] 抓取异常不影响后续轮询，任务保留")
    store = temp_store()
    clock = Clock()

    def fetcher(g):
        raise watch.FetchError("网络抖了")

    g = make_goal()
    store.save_goal(g)
    w = watch_worker.WatchWorker(store=store, fetcher=fetcher, notifier=lambda t: {}, clock=clock)

    brief = w.run_once()
    ok("没有抛出异常", brief is not None, brief)
    ok("任务仍在 active", g.status == "active", g.status)
    ok("任务状态仍是 active", store.load_goal(g.id).status == "active",
       store.load_goal(g.id).status)
    clock.advance(120)
    again = w.run_once()
    ok("下一轮仍会尝试", bool(again), again)


# ---------------------------------------------------------------- 5
def test_only_active_goals():
    print("\n[5] 只处理 active 任务（done/stopped 不打扰）")
    store = temp_store()
    clock = Clock()
    seen = []

    def fetcher(g):
        seen.append(g.id)
        return [make_scheme()]

    a = make_goal()
    b = make_goal()
    b.status = "stopped"
    store.save_goal(a)
    store.save_goal(b)
    w = watch_worker.WatchWorker(store=store, fetcher=fetcher, notifier=lambda t: {}, clock=clock)
    w.run_once()

    ok("只查了 active 那条", seen == [a.id], seen)


# ---------------------------------------------------------------- 6
def test_notifier_failure_is_swallowed():
    print("\n[6] 通知失败不影响决策与状态")
    store = temp_store()
    clock = Clock()

    def boom(_text):
        raise RuntimeError("提醒服务挂了")

    fetcher = lambda g: [make_scheme()]  # noqa: E731
    g = make_goal()
    store.save_goal(g)
    w = watch_worker.WatchWorker(store=store, fetcher=fetcher, notifier=boom, clock=clock)
    brief = w.run_once()
    ok("仍然是命中", brief and brief[0]["action"] == "命中", brief)
    ok("任务已结束为 done", store.load_goal(g.id).status == "done",
       store.load_goal(g.id).status)


# ---------------------------------------------------------------- 7
def test_drain_runs_on_heartbeat():
    print("\n[7] 通知队列按间隔自动补发（以前只有手动 --drain 才会发）")
    store = temp_store()
    clock = Clock()
    calls = []

    def fake_drain():
        calls.append(clock.t)
        return {"sent": 1, "left": 0}

    fetcher = lambda g: []  # noqa: E731
    g = make_goal()
    store.save_goal(g)
    w = watch_worker.WatchWorker(store=store, fetcher=fetcher,
                                 notifier=lambda _t: {"ok": True},
                                 clock=clock, drainer=fake_drain)
    w.run_once()
    ok("第一轮就补发了一次（启动即补）", len(calls) == 1, calls)

    clock.t += 10  # 还没到 300s
    w.run_once()
    ok("间隔没到不重复补发", len(calls) == 1, calls)

    clock.t += watch_worker.DRAIN_EVERY + 1
    w.run_once()
    ok("到间隔后再次补发", len(calls) == 2, calls)

    def boom():
        raise RuntimeError("队列文件坏了")

    w2 = watch_worker.WatchWorker(store=store, fetcher=fetcher,
                                  notifier=lambda _t: {"ok": True},
                                  clock=clock, drainer=boom)
    try:
        w2.run_once()
        ok("补发异常不打断轮询", True)
    except Exception as e:
        ok("补发异常不打断轮询", False, repr(e))


# ---------------------------------------------------------------- 8
def test_cli_once():
    print("\n[7] --once 正常退出（调试入口可用）")
    code = watch_worker.main(["--once"])
    ok("返回码为 0", code == 0, code)


# ---------------------------------------------------------------- 9
def test_heartbeat_reports_progress():
    print("\n[9] 心跳：定期把「还在转吗 / 库有没有失控」打进日志")
    import logging

    store = temp_store()
    clock = Clock()
    g = make_goal()
    store.save_goal(g)

    lines = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("watch_worker")
    h = Collect()
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    try:
        w = watch_worker.WatchWorker(store=store, fetcher=lambda _g: [],
                                     notifier=lambda _t: {"ok": True}, clock=clock)
        w.run_once()
        beats = [x for x in lines if x.startswith("心跳：")]
        ok("第一轮就报一次心跳（启动即报）", len(beats) == 1, lines)
        ok("心跳里有盯票中/事件/库大小",
           all(k in beats[0] for k in ("盯票中", "事件", "库")), beats[:1])

        clock.t += 10
        w.run_once()
        ok("间隔没到不重复报", len([x for x in lines if x.startswith("心跳：")]) == 1)

        clock.t += watch_worker.HEARTBEAT_EVERY
        w.run_once()
        beats = [x for x in lines if x.startswith("心跳：")]
        ok("到间隔后再次报心跳", len(beats) == 2, beats)
        ok("轮次计数随 run_once 增长", w._rounds >= 3, w._rounds)
    finally:
        logger.removeHandler(h)

    store._conn.close()


def main() -> int:
    tests = [
        test_only_due_goals,
        test_backoff_while_waiting,
        test_backoff_reset_on_hit,
        test_exception_does_not_kill,
        test_only_active_goals,
        test_notifier_failure_is_swallowed,
        test_drain_runs_on_heartbeat,
        test_heartbeat_reports_progress,
        test_cli_once,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            global FAIL
            FAIL += 1
            print(f"  ERROR {t.__name__}")
            traceback.print_exc()
    for d in TMP_DIRS:                      # 容器里 TMPDIR 可能是 /app，别留下垃圾
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{'='*52}\n结果：{PASS} 通过，{FAIL} 失败\n{'='*52}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
