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
MULTI_HUB_LIMIT = int(__import__("os").getenv("WATCH_MULTI_HUB_LIMIT", "3"))

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

# 可以放宽的维度。规则版按固定顺序一级一级开，模型版由模型挑「下一个开哪个」。
RELAX_ORDER = ["transfer", "date", "seat", "money", "time", "arrive"]
RELAX_LABEL = {
    "transfer": "允许中转",
    "date": "日期前后浮动",
    "seat": "不限席别",
    "money": "预算上浮 20%",
    "time": "耗时上浮 25%",
    "arrive": "不限到达时段",
}
# 老的「等级」语义保留（0..4 对应固定的开关组合），只用于展示与兼容
LEVEL_DIMS = {
    0: [],
    1: ["transfer"],
    2: ["transfer", "date"],
    3: ["transfer", "date", "seat"],
    4: ["transfer", "date", "seat", "money", "time", "arrive"],
}


def relax_dims(goal: "Goal") -> set:
    """任务当前已放宽的维度集合。

    `relax_dims` 为空时按老语义从 `relax_level` 推——这样老数据、老测试都不用改。
    """
    if goal.relax_dims:
        return set(goal.relax_dims)
    return set(LEVEL_DIMS.get(min(goal.relax_level, 4), []))


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
    relax_level: int = 0                       # 0 严格 → 4 最宽（展示用）
    relax_dims: list = field(default_factory=list)   # 实际放宽了哪些维度（模型可决定）
    attempts: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = ""
    note: str = ""
    owner: str = "default"                     # 谁的任务：多用户时按它隔离与限量

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(s: str) -> "Goal":
        return Goal(**json.loads(s))

    # 各级放宽会打开哪些开关
    def relaxed(self, level: int) -> "Goal":
        g = Goal.from_json(self.to_json())
        g.relax_level = level
        g.relax_dims = list(LEVEL_DIMS.get(level, RELAX_ORDER))
        if level >= 2:
            g.flex_days = max(g.flex_days, 2)
        return g

    def date_window(self) -> list:
        base = datetime.strptime(self.date, "%Y-%m-%d").date()
        if "date" not in relax_dims(self):
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
    legs: list = field(default_factory=list)   # 多段中转时逐段的车票（route.Leg）

    @property
    def transfers(self) -> int:
        """换乘次数：有 legs 时按段数算，否则看 kind。"""
        if self.legs:
            return max(0, len(self.legs) - 1)
        return 1 if self.kind == "中转" else 0

    def label(self) -> str:
        if self.legs and len(self.legs) > 2:
            segs = " ｜ ".join(l.label() for l in self.legs)
            return f"{self.date} {self.dep_time}-{self.arr_time} {self.seat} ¥{self.price:.0f}（换乘 {self.transfers} 次）{segs}"
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
    relax_dims: list = field(default_factory=list)   # 本次决定放宽的维度（最多一个）

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
    dims = relax_dims(goal)

    if scheme.price > 0:
        budget = goal.budget
        if "money" in dims and budget is not None:
            budget = budget * 1.2
        if budget is not None and scheme.price > budget:
            bad.append(VIOLATION_PRICE)

    limit = goal.max_minutes
    if "time" in dims and limit:
        limit = int(limit * 1.25)
    if limit and scheme.duration_min > limit:
        bad.append(VIOLATION_DURATION)

    if "arrive" not in dims:
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

    n_transfer = scheme.transfers
    if n_transfer > 0:
        if not goal.allow_transfer:
            bad.append(VIOLATION_TRANSFER)
        elif n_transfer > goal.max_transfers:
            bad.append(VIOLATION_TRANSFER_COUNT)

    # 席别约束：严格模式下只接受偏好列表内的席别（放宽「席别」后不再校验）
    if goal.strict_seat and "seat" not in dims and scheme.seat not in goal.seat_prefs:
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
    """真实抓取：直达；第 1 级起允许少量枢纽中转（控制请求数）。

    注意：**不要**把城市展开成同城车站逐个查。12306 的余票接口本身把站名归一到城市
    （查「北京南→上海虹桥」返回的仍是北京/北京丰台/北京南 三个站的全部车次），
    展开成 9×5=45 个组合会拿到 45 份一模一样的数据：请求数翻 45 倍、耗时翻倍，
    候选里还全是重复项（实测北京→上海 一次 tick 135 次请求 / 54 秒）。
    传城市名或站名给接口都对——它自己会归一到城市。
    """
    out: list = []
    dates = goal.date_window()
    seen: set = set()

    for d in dates:
        data = ticket_api.query_ticket_dict(goal.origin, goal.dest, d)
        time.sleep(sleep_between)
        if "error" in data:
            raise FetchError(data["error"])
        for t in data.get("trains", []):
            for s in _direct_schemes(t, d):
                key = (s.train_no, s.seat, s.date)
                if key in seen:
                    continue
                seen.add(key)
                out.append(s)

    if "transfer" in relax_dims(goal) and goal.allow_transfer and hubs:
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

    # 两段换乘（A→h1→h2→D）：只有明确允许 2 次换乘、且放宽到第 2 级才试，
    # 否则请求数会翻几倍，而绝大多数行程一次换乘就够。
    if len(relax_dims(goal)) >= 2 and goal.allow_transfer and goal.max_transfers >= 2 and hubs:
        try:
            import route as _route
            picked2 = geo.pick_hubs(goal.origin, goal.dest, hubs, k=MULTI_HUB_LIMIT)
            if len(picked2) >= 2:
                log.info("尝试两段换乘，候选枢纽：%s", "、".join(picked2))
                d = dates[0]
                fetched = {"n": 0}

                def _fetch(a, b, day, _d=d):
                    fetched["n"] += 1
                    data = ticket_api.query_ticket_dict(a, b, day)
                    if "error" in data:
                        raise RuntimeError(data["error"])
                    return data

                for chain in _route.multi_hub_chains(_fetch, goal.origin, goal.dest, picked2, d):
                    out.append(_route.to_scheme(chain, d))
                log.info("两段换乘：查了 %d 个区段，得到 %d 条链", fetched["n"], len(out))
        except Exception as exc:                      # 多段失败不影响直达/单点结果
            log.warning("两段换乘失败（已忽略）：%r", exc)

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
    seen: set = set()
    for t1 in seg1.get("trains", []):
        for t2 in seg2.get("trains", []):
            s1 = _direct_schemes(t1, d)
            s2 = _direct_schemes(t2, d)
            if not s1 or not s2:
                continue
            a, b = s1[0], s2[0]
            key = (a.train_no, b.train_no, a.seat, b.seat)
            if key in seen:
                continue
            seen.add(key)
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


# ---------------------------------------------------------------- 改约束

# 允许在运行中修改的字段。刻意做成白名单：`id`/`owner`/`status` 这些不该被界面顺手改掉。
EDITABLE = {
    "origin", "dest", "date", "budget", "max_minutes",
    "arrive_after", "arrive_before", "seat_prefs", "strict_seat",
    "max_transfers", "allow_transfer", "flex_days", "interval_sec", "note",
}


def edit_goal(goal: Goal, **changes) -> Goal:
    """改约束：改完把「放宽状态」清零，否则旧的放宽会继续生效。

    为什么要清零：放宽记录是「为了让这次搜索命中而降级过什么」。用户一旦换了预算
    或日期，那些放宽理由可能已经失效（比如原来超预算 20 元，现在预算调高就不超了），
    继续沿用会让判定偏松 —— 买到一趟其实不满足新约束的车。

    改完如果是 done/failed，顺手复活成 active：用户改约束的意思通常是"再盯一次"。
    """
    unknown = set(changes) - EDITABLE
    if unknown:
        raise KeyError(f"不可修改的字段：{', '.join(sorted(unknown))}")
    for k, v in changes.items():
        setattr(goal, k, v)
    goal.relax_dims = []
    goal.relax_level = 0
    if goal.status in ("done", "failed"):
        goal.status = "active"
        goal.note = ""
    return goal


# ---------------------------------------------------------------- 决策
# 可以放宽的维度总数就是「最多能升几级」；老代码里写死的 4 已经不够用了
# 模型版连续说「先等等」的上限，用完退回规则顺序，避免任务卡在原地
MAX_MODEL_WAITS = int(__import__('os').getenv('DECIDE_MAX_WAITS', '3'))
MAX_LEVEL = len(RELAX_ORDER)

# 决策器：决定「下一个放宽哪一项」。签名见 pick_dim()，返回维度名或 None（=先不放宽）。
Decider = Callable[[Goal, list, list, Optional[Scheme], list], Optional[str]]


def pick_dim(goal: Goal, decider: Optional[Decider] = None,
             best: Optional[Scheme] = None, bad: Optional[list] = None) -> Optional[str]:
    """挑「下一个该放宽的维度」。

    没有 decider 或 decider 抛异常时退回固定顺序（=规则版行为），
    这样模型挂了也不会让盯票停摆。
    """
    dims = relax_dims(goal)
    remaining = [d for d in RELAX_ORDER if d not in dims]
    if not remaining:
        return None
    if decider is not None:
        try:
            choice = decider(goal, sorted(dims), remaining, best, bad or [])
        except Exception as e:  # noqa: BLE001 — 决策器出错退回规则，不中断盯票
            log.warning("决策器异常，退回固定顺序：%r", e)
            choice = None
        else:
            if choice in remaining:
                return choice
            if choice is None:
                return None
    return remaining[0]


def decide(schemes: list, goal: Goal, decider: Optional[Decider] = None) -> Decision:
    """根据本轮观察决定下一步。这是「自主推进」的落点，也是评测的断言对象。"""
    goal_id = goal.id
    dims = relax_dims(goal)
    if not schemes:
        # 一条都没有 = 现在没票（还没放票 / 已售罄 / 该日期已过）。
        # 这种情况**不能**判定放弃：昨天没有今天可能有，盯票的价值就在于等。
        # 前两级仍然要扩搜（放开中转、放开日期弹性），因为"没票"也可能是搜得太窄。
        if len(dims) < 2:
            nxt = pick_dim(goal, decider)
            if nxt:
                return Decision("降级", len(dims) + 1,
                                f"本轮没有任何可买方案，放宽「{RELAX_LABEL[nxt]}」再搜",
                                goal_id=goal_id, relax_dims=[nxt])
        return Decision("等待", goal.relax_level, "本轮无票（搜索范围已放开），继续盯", goal_id=goal_id)

    ordered = rank(schemes, goal)
    best = ordered[0]
    bad = check(best, goal)
    if not bad:
        return Decision("命中", goal.relax_level, "约束全部满足", goal_id=goal_id,
                        best=best, candidates=len(schemes))

    if len(dims) < len(RELAX_ORDER):
        nxt = pick_dim(goal, decider, best, bad)
        if nxt:
            return Decision(
                "降级", len(dims) + 1,
                f"最优候选「{best.train_no}」{'；'.join(bad)} → 放宽「{RELAX_LABEL[nxt]}」",
                goal_id=goal_id, closest=best, candidates=len(schemes), violations=bad,
                relax_dims=[nxt],
            )
        return Decision("等待", goal.relax_level,
                        f"最优候选「{best.train_no}」{'；'.join(bad)}，但先不放宽（等新票）",
                        goal_id=goal_id, closest=best, candidates=len(schemes), violations=bad)
    return Decision("放弃", goal.relax_level, f"最宽条件下仍不满足：{'；'.join(bad)}",
                    goal_id=goal_id, closest=best, candidates=len(schemes), violations=bad)


# ---------------------------------------------------------------- 持久化

class Store:
    """SQLite 持久化：任务 / 事件 / 订票记录。

    性能相关的三条设置不是随手加的，见 `_tune()`：
    - `journal_mode=WAL` + `synchronous=NORMAL`：每个 tick 都要写事件，默认
      rollback journal + FULL 同步会让每次 insert 变成一次 fsync。实测 2880 条
      事件从 19.3 s 降到 0.6 s（约 32×）。
    - `events(goal_id, id)` 与 `events(ts)` 索引：事件表是唯一会长到十万行级的表。
    - `prune_events()`：长跑时按「每任务保留 N 条 + 超过 D 天丢弃」裁剪。
    """

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._tune()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
                relax_level INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
                created_at TEXT, updated_at TEXT, note TEXT, owner TEXT DEFAULT 'default'
            )""")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL,
                ts TEXT NOT NULL, level INTEGER, action TEXT, reason TEXT, snapshot TEXT
            )""")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                goal_id TEXT, train_no TEXT, date TEXT, dep_station TEXT, arr_station TEXT,
                dep_time TEXT, arr_time TEXT, seat TEXT, price REAL,
                status TEXT DEFAULT 'booked',
                created_at TEXT
            )""")
        self._migrate()
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_goal ON events(goal_id, id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_owner ON goals(owner, status)")
        self._conn.commit()
        self._ensure_autovacuum()

    def _tune(self) -> None:
        # WAL 允许读写并发，也把 fsync 从"每条 insert"挪到 checkpointer
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")


    def _ensure_autovacuum(self) -> None:
        """让删除真的能释放磁盘。

        默认 `auto_vacuum=0`：删了行只是把页标成空闲，文件不会变小 —— 实测
        10 个任务跑 30 天后裁掉 42 倍的行，文件仍是 13 MB。改成 INCREMENTAL
        后，`prune_events()` 里的 `incremental_vacuum` 才有意义。

        注意：这个模式只能在建表前设定，已有表的库需要一次 VACUUM 才会重排。
        库小于 50 MB 时顺手做一次；更大就跳过（那是异常情况，交给人工处理）。
        """
        try:
            if self._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
                return
            if self.db_bytes() > 50 * 1024 * 1024:
                log.warning("库超过 50 MB，跳过一次性的 VACUUM（auto_vacuum 仍为 0）")
                return
            self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._conn.execute("VACUUM")
        except sqlite3.Error as e:
            log.warning("设置 auto_vacuum 失败（不影响功能）：%r", e)

    def _migrate(self) -> None:
        """老库升级：加 owner 列（SQLite 的 ADD COLUMN 是 O(1)，不需要重建表）。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(goals)").fetchall()}
        if "owner" not in cols:
            self._conn.execute("ALTER TABLE goals ADD COLUMN owner TEXT DEFAULT 'default'")
            self._conn.commit()

    def save_goal(self, goal: Goal) -> None:
        goal.updated_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO goals(id,spec,status,relax_level,attempts,created_at,updated_at,note,owner) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "spec=excluded.spec,status=excluded.status,relax_level=excluded.relax_level,"
                "attempts=excluded.attempts,updated_at=excluded.updated_at,note=excluded.note,"
                "owner=excluded.owner",
                (goal.id, goal.to_json(), goal.status, goal.relax_level,
                 goal.attempts, goal.created_at, goal.updated_at, goal.note, goal.owner),
            )
            self._conn.commit()

    def load_goal(self, goal_id: str) -> Optional[Goal]:
        with self._lock:
            row = self._conn.execute("SELECT spec FROM goals WHERE id=?", (goal_id,)).fetchone()
        return Goal.from_json(row[0]) if row else None

    def list_goals(self, status: Optional[str] = None, owner: Optional[str] = None) -> list:
        q = "SELECT spec FROM goals"
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if owner:
            where.append("owner=?")
            args.append(owner)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(q, tuple(args)).fetchall()
        return [Goal.from_json(r[0]) for r in rows]

    def count_active(self, owner: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM goals WHERE status='active' AND owner=?", (owner,)
            ).fetchone()
        return int(row[0]) if row else 0

    def log(self, goal_id: str, decision: Decision, snapshot: Optional[dict] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(goal_id,ts,level,action,reason,snapshot) VALUES(?,?,?,?,?,?)",
                (goal_id, datetime.now().isoformat(timespec="seconds"),
                 decision.level, decision.action, decision.reason,
                 json.dumps(snapshot or {}, ensure_ascii=False)),
            )
            self._conn.commit()

    def last_scheme(self, goal_id: str) -> dict:
        """最近一次「命中」事件里记录的方案字段；没有就返回空 dict。

        界面用它重建出票卡片 —— 不重用事件里的 label 字符串，
        因为 label 是给人看的，链接需要结构化字段。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot FROM events WHERE goal_id=? AND action='命中' ORDER BY id DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
        if not row or not row[0]:
            return {}
        try:
            data = json.loads(row[0])
        except Exception:
            return {}
        best = data.get("best") if isinstance(data, dict) else None
        return best if isinstance(best, dict) else {}

    def events(self, goal_id: str, limit: int = 50) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,level,action,reason FROM events WHERE goal_id=? ORDER BY id DESC LIMIT ?",
                (goal_id, limit),
            ).fetchall()
        return rows

    # ---------- 长跑维护 ----------
    def prune_events(self, keep_per_goal: int = 200, keep_days: int = 30) -> int:
        """裁掉事件表里"没人会看"的行，返回删除条数。

        两条规则（任一满足就删）：
        - 每个任务只留最近 `keep_per_goal` 条（界面只展示 50 条，留 200 已有余量）
        - 早于 `keep_days` 天的全删（用于清理长期停用的任务）

        实现上刻意用「先求出阈值 id，再按 id 删」而不是 `NOT IN (子查询)`：
        后者每轮都要全表扫描一次，实测跑 30 天时把总写入耗时从 8 s 拖到 81 s。
        """
        deleted = 0
        with self._lock:
            goal_ids = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT goal_id FROM events").fetchall()]
            for gid in goal_ids:
                row = self._conn.execute(
                    "SELECT id FROM events WHERE goal_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
                    (gid, max(0, keep_per_goal)),
                ).fetchone()
                if row:
                    cur = self._conn.execute(
                        "DELETE FROM events WHERE goal_id=? AND id<=?", (gid, row[0]))
                    deleted += max(0, cur.rowcount or 0)
            cur = self._conn.execute(
                "DELETE FROM events WHERE ts < ?",
                ((datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds"),),
            )
            deleted += max(0, cur.rowcount or 0)
            self._conn.commit()
            # 把删掉的页还回去：incremental_vacuum 是增量的，不会像 VACUUM 那样长时间独占
            try:
                self._conn.execute("PRAGMA incremental_vacuum(200)")
                # WAL 自己也会长：只 checkpoint 不 truncate 时文件不回收，
                # 实测 30 天跑到 30 MB，truncate 之后回到几百 KB。
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        return deleted

    def event_count(self, goal_id: Optional[str] = None) -> int:
        q = "SELECT COUNT(*) FROM events"
        args: tuple = ()
        if goal_id:
            q += " WHERE goal_id=?"
            args = (goal_id,)
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return int(row[0]) if row else 0

    def db_bytes(self, suffix: str = "") -> int:
        """库文件大小。默认只看主库；`-wal` 单独看（它需要 checkpoint 才会缩）。"""
        p = Path(str(self.path) + suffix)
        return p.stat().st_size if p.exists() else 0

    def vacuum(self) -> None:
        """裁剪之后回收空间。会短暂独占数据库，只在维护时调用。"""
        with self._lock:
            self._conn.commit()
            self._conn.execute("VACUUM")

    # ---------- 订票记录（长记忆） ----------
    def add_booking(self, owner: str, scheme: Optional[dict] = None, goal_id: str = "",
                    status: str = "booked") -> int:
        # 允许直接传 Scheme（界面上手边正好有），也允许传事件快照 dict
        if scheme is not None and not isinstance(scheme, dict):
            scheme = {f: getattr(scheme, f, None) for f in (
                "train_no", "date", "dep_station", "arr_station",
                "dep_time", "arr_time", "seat", "price")}
        scheme = scheme or {}
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bookings(owner,goal_id,train_no,date,dep_station,arr_station,"
                "dep_time,arr_time,seat,price,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, goal_id, scheme.get("train_no", ""), scheme.get("date", ""),
                 scheme.get("dep_station", ""), scheme.get("arr_station", ""),
                 scheme.get("dep_time", ""), scheme.get("arr_time", ""),
                 scheme.get("seat", ""), scheme.get("price"),
                 status, datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def bookings(self, owner: str, limit: int = 20) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT train_no,date,dep_station,arr_station,dep_time,arr_time,seat,price,status,created_at "
                "FROM bookings WHERE owner=? ORDER BY id DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        keys = ("train_no", "date", "dep_station", "arr_station", "dep_time",
                "arr_time", "seat", "price", "status", "created_at")
        return [dict(zip(keys, r)) for r in rows]

    def recent_context(self, owner: str, limit: int = 3) -> str:
        """把最近的订票记录压成一句给模型看的话（长记忆的入口）。"""
        rows = self.bookings(owner, limit)
        if not rows:
            return ""
        parts = []
        for r in rows:
            when = f"{r['date']} {r['dep_time']}" if r["date"] else r["created_at"][:10]
            parts.append(f"{when} {r['dep_station']}→{r['arr_station']} {r['train_no']} {r['seat']}")
        return "；".join(parts)

    # ---------- 真实使用的度量 ----------

    def feedback(self, goal_id: str, verdict: str = "wrong", note: str = "") -> None:
        """用户对一次命中的评价：wrong（不是我要的）/ good（就是要这趟）。

        写进 events 表而不是新表：它本来就是"这条任务上发生过的事"，
        且能被 prune_events 一起裁剪、被「决策记录」一起展示。
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (goal_id, ts, level, action, reason, snapshot) "
                "VALUES (?,?,?,?,?,?)",
                (goal_id, datetime.now().isoformat(timespec="seconds"), None,
                 "反馈", verdict, note or ""),
            )
            self._conn.commit()

    def stats(self, days: int = 7, owner: Optional[str] = None) -> dict:
        """这段时间里到底发生了什么——给「真的有人用」准备的数字。

        只数事实，不算奖励分：任务数、检查次数、命中/放弃、命中时放宽了几项、
        平均多少轮才命中、被标成误报几次、订了几张票。
        """
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        where = "created_at >= ?"
        args: list = [since]
        if owner:
            where += " AND owner = ?"
            args.append(owner)

        with self._lock:
            rows = self._conn.execute(
                f"SELECT spec, status, attempts, relax_level FROM goals WHERE {where}", args
            ).fetchall()
            fb = self._conn.execute(
                "SELECT reason, COUNT(*) FROM events WHERE action='反馈' AND ts >= ? "
                + ("AND goal_id IN (SELECT id FROM goals WHERE owner=? )" if owner else "")
                + " GROUP BY reason",
                ([since, owner] if owner else [since]),
            ).fetchall()
            ev = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE ts >= ?", (since,)
            ).fetchone()[0]
            bk = self._conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE created_at >= ?"
                + (" AND owner=?" if owner else ""),
                ([since, owner] if owner else [since]),
            ).fetchone()[0]

        goals = [Goal.from_json(s) for s, *_ in rows]
        status = {"active": 0, "done": 0, "failed": 0, "stopped": 0}
        for g in goals:
            status[g.status] = status.get(g.status, 0) + 1

        done = [g for g in goals if g.status == "done"]
        finished = len(done) + status.get("failed", 0)
        fb_map = {k: v for k, v in fb}
        return {
            "days": days,
            "goals": len(goals),
            "status": status,
            "checks": sum(g.attempts for g in goals),
            "events": ev,
            "hit_rate": (len(done) / finished) if finished else 0.0,
            "avg_ticks_to_hit": (sum(g.attempts for g in done) / len(done)) if done else 0.0,
            "avg_dims_at_hit": (sum(len(relax_dims(g)) for g in done) / len(done)) if done else 0.0,
            "wrong": fb_map.get("wrong", 0),
            "good": fb_map.get("good", 0),
            "bookings": bk,
            "owners": len({g.owner for g in goals}),
        }


# ---------------------------------------------------------------- 轮询

class Watcher:
    """单个任务的后台轮询器。失败分四类处理，见 tick()。"""

    def __init__(self, goal: Goal, fetcher: Fetcher, store: Store,
                 sleep: Callable[[float], None] = time.sleep, jitter: float = 0.2,
                 notifier: Optional[Callable[[str], dict]] = None,
                 decider: Optional[Decider] = None):
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
        # 决策器：None = 规则版（固定顺序），传入模型版即可让模型挑放宽哪一项
        self.decider = decider
        # 模型版的「先等等」预算：用完之后退回规则顺序，避免一直等下去不动
        self._model_waits = 0

    def _apply_relax(self, d: Decision) -> None:
        """把「这次要放宽的维度」写回任务，并让 relax_level 跟着它走。"""
        dims = set(self.goal.relax_dims) or relax_dims(self.goal)
        for dim in (d.relax_dims or []):
            dims.add(dim)
        self.goal.relax_dims = [x for x in RELAX_ORDER if x in dims]
        self.goal.relax_level = min(len(RELAX_ORDER), len(self.goal.relax_dims))

    def _notify(self, text: str, key: str = "") -> None:
        if not self.notifier:
            return
        try:
            try:
                self.notifier(text, dedupe_key=key)   # 支持去重的通知实现（notify.send）
            except TypeError:
                self.notifier(text)                   # 测试里的单参数假通知器
        except Exception as e:  # noqa: BLE001 — 通知失败不改变任何决策
            log.warning("通知发送失败（已忽略）：%r", e)

    # -- 单轮：观察 → 判定 → 决策（可离线测试）
    def tick(self) -> Decision:
        """一次 tick = 一个决策周期；若本轮判定要降级，会带着新层级立刻重试一次。"""
        g = self.goal
        g.attempts += 1
        candidates = 0
        escalated = False
        # 模型说「等等」的次数用完后就退回规则，否则任务会一直等下去不动。
        # 这里的 use_decider 在整个 tick 内固定，避免"升了一级又立刻被模型劝住"。
        use_decider = None if self._model_waits >= MAX_MODEL_WAITS else self.decider
        d = Decision("等待", g.relax_level, "本轮未执行", goal_id=g.id)

        for _ in range(2):  # 本轮 + 降级后立即重试一次
            try:
                schemes = self.fetcher(g)
                self._err_streak = 0
            except FetchError as e:
                # 第 1 类：暂时性错误 → 前两次只等，第 3 次判定「这条查法不行」转降级
                self._err_streak = getattr(self, "_err_streak", 0) + 1
                if self._err_streak >= 3 and len(relax_dims(g)) < len(RELAX_ORDER) and not escalated:
                    nxt = pick_dim(g, self.decider)
                    if nxt:
                        d = Decision("降级", g.relax_level + 1,
                                     f"连续 {self._err_streak} 次抓取失败：{e} → 放宽「{RELAX_LABEL[nxt]}」",
                                     goal_id=g.id, relax_dims=[nxt])
                        self._apply_relax(d)
                        self._err_streak = 0
                        escalated = True
                        continue
                d = Decision("等待", g.relax_level, f"抓取失败将重试：{e}", goal_id=g.id)
                break
            except DataError as e:
                # 第 2 类：数据缺失 → 立刻换查法，不浪费重试次数
                self._err_streak = 0
                if len(relax_dims(g)) < len(RELAX_ORDER) and not escalated:
                    nxt = pick_dim(g, self.decider)
                    if nxt:
                        d = Decision("降级", g.relax_level + 1,
                                     f"数据不可用：{e} → 放宽「{RELAX_LABEL[nxt]}」",
                                     goal_id=g.id, relax_dims=[nxt])
                        self._apply_relax(d)
                        escalated = True
                        continue
                d = Decision("放弃", g.relax_level, f"数据不可用且已无退路：{e}", goal_id=g.id)
                break
            except Exception as e:
                # 第 4 类：内部错误 → 兜底留痕，绝不采信/编造任何方案
                d = Decision("等待", g.relax_level, f"内部错误（未采信任何方案）：{e!r}", goal_id=g.id)
                break

            candidates = len(schemes)
            d = decide(schemes, g, use_decider)
            if d.action == "等待" and use_decider is not None and d.closest is not None:
                self._model_waits += 1
            if d.action == "降级":
                if escalated:  # 一个 tick 只升一级，避免一次 tick 跳过中间档位
                    d = Decision("等待", g.relax_level,
                                 f"第 {g.relax_level} 级仍无满足方案（{d.reason}），下一轮继续",
                                 goal_id=g.id, closest=d.closest, candidates=candidates)
                    break
                self._apply_relax(d)
                escalated = True
                continue  # 第 3 类：目标不可达 → 放宽后立刻用新层级再看一次
            break

        if d.action == "命中":
            self._model_waits = 0
            g.status = "done"
            g.note = d.best.label() if d.best else ""
            if d.best:
                # 通知里带一条 12306 深链：手机上点开就是该日期该起终点的查询页，
                # 只剩「预订」一步 —— 这是「半自动」里最安全的一档（不托管登录态与支付）。
                try:
                    import booking
                    tail = booking.one_line(d.best)
                except Exception:
                    tail = d.best.label()
                b = d.best
                key = "|".join(str(x) for x in (
                    g.id, getattr(b, "train_no", ""), getattr(b, "seat", ""),
                    getattr(b, "date", g.date), getattr(b, "dep_time", "")))
                self._notify(f"✅ 盯票命中 | {g.origin}→{g.dest} {g.date} | {tail}"
                             f"（第 {g.relax_level} 级，检查 {g.attempts} 次）", key=key)
        elif d.action == "放弃":
            g.status = "failed"
            g.note = d.reason
            self._notify(f"⚠️ 盯票放弃 | {g.origin}→{g.dest} {g.date} | {d.reason}"
                         f"（检查 {g.attempts} 次）", key=f"{g.id}|giveup")
        elif g.attempts >= g.max_attempts:
            g.status = "failed"
            d = Decision("放弃", g.relax_level, f"达到最大尝试次数 {g.max_attempts}", goal_id=g.id)

        self.store.save_goal(g)
        snap = {"candidates": candidates}
        if d.best is not None:
            b = d.best
            snap["best"] = {
                "train_no": b.train_no, "dep_station": b.dep_station,
                "arr_station": b.arr_station, "dep_time": b.dep_time,
                "arr_time": b.arr_time, "duration_min": b.duration_min,
                "price": b.price, "seat": b.seat, "date": b.date,
                "kind": b.kind, "via": b.via, "transfers": b.transfers,
            }
        self.store.log(g.id, d, snap)
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
