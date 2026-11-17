"""盯票 Agent：把「查一次最优路线」变成「持续盯着，直到买到或明确买不到」。

设计上的三条硬约束（不是口号，是代码结构）：

1. 目标可验证 —— Goal 是可断言的约束集，`check()` 返回违规项；任务成功与否由代码判定。
2. 多步自主推进 —— `decide()` 根据本轮观察决定下一步（下手 / 降级 / 继续等），
   而不是固定跑完「查直达→查中转→推荐」。
3. 失败自恢复 —— `tick()` 区分四类失败：暂时性错误退避重试、数据缺失换查法、
   目标不可达逐级放宽、内部错误兜底并留痕。

与 a.py 的关系：a.py 是 Streamlit 页面，不能 import；本模块只依赖 ticket_api。
"""
from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import geo
import ticket_api

log = logging.getLogger("watch")

# 每轮最多查几个中转枢纽（每个枢纽两次查询，太多会拖慢单轮并增加被限流的风险）
HUB_QUERY_LIMIT = int(__import__("os").getenv("WATCH_HUB_LIMIT", "3"))

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.getenv("WATCH_DB", str(BASE_DIR / "watch.db")))

# ---------------------------------------------------------------- 目标

SEAT_ORDER = ["二等座", "硬卧", "硬座", "一等座", "软卧", "无座", "商务座", "高级软卧", "软座", "特等座"]

VIOLATION_PRICE = "超出预算"
VIOLATION_DURATION = "耗时超限"
VIOLATION_ARRIVE = "到达时段不合适"
VIOLATION_NO_SEAT = "没有可买席别"
VIOLATION_TRANSFER = "不接受中转"
VIOLATION_DATE = "日期不符"
VIOLATION_DEPART = "出发时段不合适"
VIOLATION_SEAT = "席别不在偏好内"
VIOLATION_TRANSFER_COUNT = "换乘次数超限"


@dataclass
class Goal:
    """一次盯票任务。所有字段都是可判定的约束，不是自然语言。"""

    origin: str
    dest: str
    date: str                                  # YYYY-MM-DD
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    flex_days: int = 1                         # 允许前后浮动几天
    budget: Optional[float] = None             # 单人总价上限（元）
    max_minutes: Optional[int] = None          # 总耗时上限
    arrive_after: str = "06:00"                # 到达时段起点
    arrive_before: str = "23:59"               # 到达时段终点
    seat_prefs: list = field(default_factory=lambda: ["二等座", "硬卧", "硬座"])
    allow_transfer: bool = True
    depart_after: str = "00:00"                # 出发时段起点
    depart_before: str = "23:59"               # 出发时段终点
    max_transfers: int = 1                     # 最多换乘几次（0=只要直达）
    strict_seat: bool = False                  # 席别必须落在 seat_prefs 内
    interval_sec: int = 300
    max_attempts: int = 288                    # 默认 288 次 ≈ 24h @5min
    status: str = "active"                     # active / done / failed / stopped
    relax_level: int = 0                       # 0 严格 → 4 最宽
    attempts: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = ""
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(s: str) -> "Goal":
        return Goal(**json.loads(s))

    # 各级放宽会打开哪些开关
    def relaxed(self, level: int) -> "Goal":
        g = Goal.from_json(self.to_json())
        g.relax_level = level
        if level >= 2:
            g.flex_days = max(g.flex_days, 2)
        return g

    def date_window(self) -> list:
        base = datetime.strptime(self.date, "%Y-%m-%d").date()
        if self.relax_level < 2:
            return [base.isoformat()]
        out = []
        for d in range(-self.flex_days, self.flex_days + 1):
            out.append((base + timedelta(days=d)).isoformat())
        return out


@dataclass
class Scheme:
    """一个可执行的乘车方案（直达或单点中转）。"""

    train_no: str
    dep_station: str
    arr_station: str
    dep_time: str
    arr_time: str
    duration_min: int
    price: float
    seat: str
    date: str
    kind: str = "直达"           # 直达 / 中转
    via: str = ""

    def label(self) -> str:
        base = f"{self.date} {self.train_no} {self.dep_station}→{self.arr_station} {self.dep_time}-{self.arr_time}"
        if self.kind == "中转":
            base += f"（经 {self.via} 中转）"
        return f"{base} {self.seat} ¥{self.price:.0f}"


@dataclass
class Decision:
    """一次决策记录：既给用户看，也给评测断言用。"""

    action: str            # 命中 / 降级 / 等待 / 放弃
    level: int
    reason: str
    goal_id: str = ""
    best: Optional[Scheme] = None
    closest: Optional[Scheme] = None
    candidates: int = 0
    violations: list = field(default_factory=list)

    def summary(self) -> str:
        if self.action == "命中" and self.best:
            return f"命中：{self.best.label()}"
        if self.action == "降级":
            return f"降级到第 {self.level} 级：{self.reason}"
        if self.action == "放弃":
            extra = f"；最接近的是 {self.closest.label()}" if self.closest else ""
            return f"放弃：{self.reason}{extra}"
        return f"等待：{self.reason}"


# ---------------------------------------------------------------- 约束判定

def _minutes(duration: str) -> int:
    try:
        h, m = duration.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _arrive_dt(scheme: Scheme) -> datetime:
    """把到达时间还原成带日期的 datetime（跨日按 +1 天算）。"""
    d = datetime.strptime(scheme.date, "%Y-%m-%d")
    t = datetime.strptime(scheme.arr_time, "%H:%M")
    arr = d.replace(hour=t.hour, minute=t.minute)
    dep = d.replace(hour=int(scheme.dep_time[:2]), minute=int(scheme.dep_time[3:5]))
    if arr < dep:
        arr += timedelta(days=1)
    return arr


def check(scheme: Scheme, goal: Goal) -> list:
    """返回违规项列表；空列表 = 满足全部约束。"""
    bad = []
    level = goal.relax_level

    if scheme.price > 0:
        budget = goal.budget
        if level >= 4 and budget is not None:
            budget = budget * 1.2
        if budget is not None and scheme.price > budget:
            bad.append(VIOLATION_PRICE)

    limit = goal.max_minutes
    if level >= 4 and limit:
        limit = int(limit * 1.25)
    if limit and scheme.duration_min > limit:
        bad.append(VIOLATION_DURATION)

    if level < 4:
        arr = _arrive_dt(scheme)
        after = arr.replace(hour=int(goal.arrive_after[:2]), minute=int(goal.arrive_after[3:5]))
        before = arr.replace(hour=int(goal.arrive_before[:2]), minute=int(goal.arrive_before[3:5]))
        if not (after <= arr <= before):
            bad.append(VIOLATION_ARRIVE)

    # 出发时段（始终生效：错过末班车这类约束与"放宽"无关）
    if goal.depart_after != "00:00" or goal.depart_before != "23:59":
        dep = datetime.strptime(
            f"{scheme.date} {scheme.dep_time}", "%Y-%m-%d %H:%M"
        )
        d_after = dep.replace(hour=int(goal.depart_after[:2]), minute=int(goal.depart_after[3:5]))
        d_before = dep.replace(hour=int(goal.depart_before[:2]), minute=int(goal.depart_before[3:5]))
        if not (d_after <= dep <= d_before):
            bad.append(VIOLATION_DEPART)

    if scheme.kind == "中转":
        if not goal.allow_transfer:
            bad.append(VIOLATION_TRANSFER)
        elif 1 > goal.max_transfers:
            bad.append(VIOLATION_TRANSFER_COUNT)

    # 席别约束：严格模式下只接受偏好列表内的席别（level>=3 放宽）
    if goal.strict_seat and level < 3 and scheme.seat not in goal.seat_prefs:
        bad.append(VIOLATION_SEAT)

    return bad


def rank(schemes: list, goal: Goal) -> list:
    """按「满足约束优先 → 席别偏好 → 价格 → 耗时」排序。"""
    def seat_rank(s: Scheme) -> int:
        try:
            return goal.seat_prefs.index(s.seat)
        except ValueError:
            return len(goal.seat_prefs) + SEAT_ORDER.index(s.seat) if s.seat in SEAT_ORDER else 99

    return sorted(
        schemes,
        key=lambda s: (len(check(s, goal)), seat_rank(s), s.price, s.duration_min),
    )


# ---------------------------------------------------------------- 抓取端口

class FetchError(Exception):
    """暂时性失败（网络、限流），可重试。"""


class DataError(Exception):
    """数据问题（站名不存在、该日无车次），换查法而不是重试。"""


Fetcher = Callable[[Goal], list]


def real_fetcher(goal: Goal, hubs: Optional[list] = None, sleep_between: float = 0.2) -> list:
    """真实抓取：直达；第 1 级起允许少量枢纽中转（控制请求数）。"""
    out: list = []
    dates = goal.date_window()
    starts = ticket_api.get_city_stations(goal.origin) if len(goal.origin) <= 3 else [goal.origin]
    ends = ticket_api.get_city_stations(goal.dest) if len(goal.dest) <= 3 else [goal.dest]

    for d in dates:
        for s0 in starts:
            for s1 in ends:
                data = ticket_api.query_ticket_dict(s0, s1, d)
                time.sleep(sleep_between)
                if "error" in data:
                    raise FetchError(data["error"])
                for t in data.get("trains", []):
                    out.extend(_direct_schemes(t, d))

    if goal.relax_level >= 1 and goal.allow_transfer and hubs:
        # 按"是否顺路"挑枢纽，而不是盲取列表前三个：
        # 北京→上海应该试南京/济南/徐州，而不是天津/石家庄。
        picked = geo.pick_hubs(goal.origin, goal.dest, hubs, k=HUB_QUERY_LIMIT)
        if picked:
            log.info("中转枢纽（按绕行比挑选）：%s", "、".join(picked))
        for d in dates[:1]:
            for hub in picked:
                try:
                    out.extend(_transfer_schemes(goal, hub, d, sleep_between))
                except FetchError:
                    continue
    return out


def _direct_schemes(train: dict, d: str) -> list:
    seats = train.get("seats", {})
    res = []
    for seat, info in seats.items():
        remain = str(info.get("remain", ""))
        price = info.get("price")
        if not price or price <= 0:
            continue
        if remain in ("", "--", "无", "0"):
            continue
        res.append(Scheme(
            train_no=train["train_no"],
            dep_station=train["departure_station"],
            arr_station=train["arrival_station"],
            dep_time=train["departure_time"],
            arr_time=train["arrival_time"],
            duration_min=_minutes(train.get("duration", "00:00")),
            price=float(price),
            seat=seat,
            date=d,
        ))
    return res


def _transfer_schemes(goal: Goal, hub: str, d: str, sleep_between: float) -> list:
    seg1 = ticket_api.query_ticket_dict(goal.origin, hub, d)
    time.sleep(sleep_between)
    seg2 = ticket_api.query_ticket_dict(hub, goal.dest, d)
    time.sleep(sleep_between)
    if "error" in seg1 or "error" in seg2:
        raise FetchError("中转段查询失败")
    res = []
    for t1 in seg1.get("trains", []):
        for t2 in seg2.get("trains", []):
            s1 = _direct_schemes(t1, d)
            s2 = _direct_schemes(t2, d)
            if not s1 or not s2:
                continue
            a, b = s1[0], s2[0]
            total = a.duration_min + b.duration_min
            if total <= 0:
                continue
            if _minutes(b.dep_time) < _minutes(a.arr_time):
                total += 24 * 60  # 次日换乘
            res.append(Scheme(
                train_no=f"{a.train_no}+{b.train_no}",
                dep_station=a.dep_station,
                arr_station=b.arr_station,
                dep_time=a.dep_time,
                arr_time=b.arr_time,
                duration_min=total,
                price=a.price + b.price,
                seat=f"{a.seat}/{b.seat}",
                date=d,
                kind="中转",
                via=hub,
            ))
    return res


# ---------------------------------------------------------------- 决策

MAX_LEVEL = 4


def decide(schemes: list, goal: Goal) -> Decision:
    """根据本轮观察决定下一步。这是「自主推进」的落点，也是评测的断言对象。"""
    goal_id = goal.id
    if not schemes:
        # 一条都没有 = 现在没票（还没放票 / 已售罄 / 该日期已过）。
        # 这种情况**不能**判定放弃：昨天没有今天可能有，盯票的价值就在于等。
        # 前两级仍然要扩搜（放开中转、放开日期弹性），因为"没票"也可能是搜得太窄。
        if goal.relax_level < 2:
            return Decision("降级", goal.relax_level + 1, "本轮没有任何可买方案，先扩大搜索范围",
                            goal_id=goal_id)
        return Decision("等待", goal.relax_level, "本轮无票（搜索范围已放开），继续盯", goal_id=goal_id)

    ordered = rank(schemes, goal)
    best = ordered[0]
    bad = check(best, goal)
    if not bad:
        return Decision("命中", goal.relax_level, "约束全部满足", goal_id=goal_id,
                        best=best, candidates=len(schemes))

    if goal.relax_level < MAX_LEVEL:
        return Decision(
            "降级", goal.relax_level + 1,
            f"最优候选「{best.train_no}」{'；'.join(bad)}",
            goal_id=goal_id, closest=best, candidates=len(schemes), violations=bad,
        )
    return Decision("放弃", goal.relax_level, f"最宽条件下仍不满足：{'；'.join(bad)}",
                    goal_id=goal_id, closest=best, candidates=len(schemes), violations=bad)


# ---------------------------------------------------------------- 持久化

class Store:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
                relax_level INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
                created_at TEXT, updated_at TEXT, note TEXT
            )""")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL,
                ts TEXT NOT NULL, level INTEGER, action TEXT, reason TEXT, snapshot TEXT
            )""")
        self._conn.commit()

    def save_goal(self, goal: Goal) -> None:
        goal.updated_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO goals(id,spec,status,relax_level,attempts,created_at,updated_at,note) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "spec=excluded.spec,status=excluded.status,relax_level=excluded.relax_level,"
                "attempts=excluded.attempts,updated_at=excluded.updated_at,note=excluded.note",
                (goal.id, goal.to_json(), goal.status, goal.relax_level,
                 goal.attempts, goal.created_at, goal.updated_at, goal.note),
            )
            self._conn.commit()

    def load_goal(self, goal_id: str) -> Optional[Goal]:
        with self._lock:
            row = self._conn.execute("SELECT spec FROM goals WHERE id=?", (goal_id,)).fetchone()
        return Goal.from_json(row[0]) if row else None

    def list_goals(self, status: Optional[str] = None) -> list:
        q = "SELECT spec FROM goals"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [Goal.from_json(r[0]) for r in rows]

    def log(self, goal_id: str, decision: Decision, snapshot: Optional[dict] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(goal_id,ts,level,action,reason,snapshot) VALUES(?,?,?,?,?,?)",
                (goal_id, datetime.now().isoformat(timespec="seconds"),
                 decision.level, decision.action, decision.reason,
                 json.dumps(snapshot or {}, ensure_ascii=False)),
            )
            self._conn.commit()

    def events(self, goal_id: str, limit: int = 50) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,level,action,reason FROM events WHERE goal_id=? ORDER BY id DESC LIMIT ?",
                (goal_id, limit),
            ).fetchall()
        return rows


# ---------------------------------------------------------------- 轮询

class Watcher:
    """单个任务的后台轮询器。失败分四类处理，见 tick()。"""

    def __init__(self, goal: Goal, fetcher: Fetcher, store: Store,
                 sleep: Callable[[float], None] = time.sleep, jitter: float = 0.2,
                 notifier: Optional[Callable[[str], dict]] = None):
        self.goal = goal
        self.fetcher = fetcher
        self.store = store
        self._sleep = sleep
        self._jitter = jitter
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last: Optional[Decision] = None
        # 通知是副产物：注入进来便于测试，也不让通知失败影响决策
        self.notifier = notifier

    def _notify(self, text: str) -> None:
        if not self.notifier:
            return
        try:
            self.notifier(text)
        except Exception as e:  # noqa: BLE001 — 通知失败不改变任何决策
            log.warning("通知发送失败（已忽略）：%r", e)

    # -- 单轮：观察 → 判定 → 决策（可离线测试）
    def tick(self) -> Decision:
        """一次 tick = 一个决策周期；若本轮判定要降级，会带着新层级立刻重试一次。"""
        g = self.goal
        g.attempts += 1
        candidates = 0
        escalated = False
        d = Decision("等待", g.relax_level, "本轮未执行", goal_id=g.id)

        for _ in range(2):  # 本轮 + 降级后立即重试一次
            try:
                schemes = self.fetcher(g)
                self._err_streak = 0
            except FetchError as e:
                # 第 1 类：暂时性错误 → 前两次只等，第 3 次判定「这条查法不行」转降级
                self._err_streak = getattr(self, "_err_streak", 0) + 1
                if self._err_streak >= 3 and g.relax_level < MAX_LEVEL and not escalated:
                    d = Decision("降级", g.relax_level + 1,
                                 f"连续 {self._err_streak} 次抓取失败：{e}", goal_id=g.id)
                    g.relax_level = d.level
                    self._err_streak = 0
                    escalated = True
                    continue
                d = Decision("等待", g.relax_level, f"抓取失败将重试：{e}", goal_id=g.id)
                break
            except DataError as e:
                # 第 2 类：数据缺失 → 立刻换查法，不浪费重试次数
                self._err_streak = 0
                if g.relax_level < MAX_LEVEL and not escalated:
                    d = Decision("降级", g.relax_level + 1, f"数据不可用：{e}", goal_id=g.id)
                    g.relax_level = d.level
                    escalated = True
                    continue
                d = Decision("放弃", g.relax_level, f"数据不可用且已无退路：{e}", goal_id=g.id)
                break
            except Exception as e:
                # 第 4 类：内部错误 → 兜底留痕，绝不采信/编造任何方案
                d = Decision("等待", g.relax_level, f"内部错误（未采信任何方案）：{e!r}", goal_id=g.id)
                break

            candidates = len(schemes)
            d = decide(schemes, g)
            if d.action == "降级":
                if escalated:  # 一个 tick 只升一级，避免一次 tick 跳过中间档位
                    d = Decision("等待", g.relax_level,
                                 f"第 {g.relax_level} 级仍无满足方案（{d.reason}），下一轮继续",
                                 goal_id=g.id, closest=d.closest, candidates=candidates)
                    break
                g.relax_level = d.level
                escalated = True
                continue  # 第 3 类：目标不可达 → 放宽后立刻用新层级再看一次
            break

        if d.action == "命中":
            g.status = "done"
            g.note = d.best.label() if d.best else ""
            if d.best:
                self._notify(f"✅ 盯票命中 | {g.origin}→{g.dest} {g.date} | {d.best.label()}"
                             f"（第 {g.relax_level} 级，检查 {g.attempts} 次）")
        elif d.action == "放弃":
            g.status = "failed"
            g.note = d.reason
            self._notify(f"⚠️ 盯票放弃 | {g.origin}→{g.dest} {g.date} | {d.reason}"
                         f"（检查 {g.attempts} 次）")
        elif g.attempts >= g.max_attempts:
            g.status = "failed"
            d = Decision("放弃", g.relax_level, f"达到最大尝试次数 {g.max_attempts}", goal_id=g.id)

        self.store.save_goal(g)
        self.store.log(g.id, d, {"candidates": candidates})
        self.last = d
        return d

    # -- 循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.goal.status != "active":
                return
            self.tick()
            if self.goal.status != "active":
                return
            wait = max(30, self.goal.interval_sec)
            self._stop.wait(wait * (1 + random.uniform(-self._jitter, self._jitter)))

    def start(self) -> "Watcher":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"watch-{self.goal.id}")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.goal.status = "stopped"
        self.store.save_goal(self.goal)


# ---------------------------------------------------------------- 演示 / CLI

def demo() -> None:
    """离线演示：不联网，用脚本化数据展示「降级 → 命中」的完整决策链。"""
    store = Store(Path(":memory:"))
    goal = Goal(origin="十堰", dest="哈尔滨", date="2026-11-20", budget=500,
                max_minutes=840, arrive_after="06:00", arrive_before="22:00",
                seat_prefs=["硬卧", "二等座"], interval_sec=60, max_attempts=10)
    store.save_goal(goal)

    scripted = {
        0: [],  # 首轮：没票
        1: [Scheme("K1264", "十堰", "哈尔滨", "10:00", "23:40", 820, 620, "硬卧", "2026-11-20")],
    }

    def fetcher(g: Goal) -> list:
        return scripted.get(g.relax_level, [])

    w = Watcher(goal, fetcher, store, sleep=lambda s: None)
    for _ in range(3):
        d = w.tick()
        print(f"[第 {goal.attempts} 次 / 放宽级 {goal.relax_level}] {d.summary()}")
        if goal.status != "active":
            break
    print("最终状态:", goal.status, "|", goal.note)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        print("用法: python watch.py demo")
