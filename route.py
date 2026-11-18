"""多段中转：把「A→B→C→D」这种两段换乘也算出来。

原来 `watch.real_fetcher` 只会拼两段（直达 + 一个枢纽）。这里把它推广成 k 段：

    节点链 = [起点, 枢纽1, 枢纽2, 终点]

限制与取舍：

1. **不在车站级别拼**。12306 的余票接口把城市名归一到城市，所以"北京南→上海虹桥"
   返回的其实是北京三个站 → 上海三个站的全部车次。在车站级别拼会立刻爆炸（9×5），
   而且拼出来的换乘在现实中未必同站。这里一律按**城市**拼。
2. **换乘间隔用固定窗口**（默认 30 分钟 ~ 6 小时）。真实最短换乘时间取决于站台和
   是否同站换乘，接口不给这个信息，所以取一个保守值，并在结果里标注"换乘 1 次"。
3. **每段只保留前若干个候选**（按到达时间）。三段全排列是 O(n³)，不剪枝会有几万个
   组合；剪枝后一次两段换乘约 10~20 个请求、1~3 秒。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

MIN_GAP_MIN = int(os.getenv("TRANSFER_MIN_GAP_MIN", "30"))     # 换乘最短间隔
MAX_GAP_MIN = int(os.getenv("TRANSFER_MAX_GAP_MIN", "360"))    # 换乘最长间隔（再久不如换方案）
PER_SEG_KEEP = int(os.getenv("TRANSFER_SEG_KEEP", "25"))       # 每段保留多少个候选
CHAIN_KEEP = int(os.getenv("TRANSFER_CHAIN_KEEP", "40"))       # 拼到中间层时保留多少条


@dataclass
class Leg:
    """一段车票。"""

    train_no: str
    dep_station: str
    arr_station: str
    dep_time: str
    arr_time: str
    duration_min: int
    price: float
    seat: str

    def label(self) -> str:
        return f"{self.train_no} {self.dep_station}→{self.arr_station} {self.dep_time}-{self.arr_time}"


def _dt(date: str, hhmm: str) -> datetime:
    t = datetime.strptime(hhmm, "%H:%M")
    return datetime.strptime(date, "%Y-%m-%d").replace(hour=t.hour, minute=t.minute)


def _hour_min(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def segment(fetch: Callable[[str, str, str], dict], a: str, b: str, date: str) -> list:
    """查一段：返回按「最早到达」排序的 Leg 列表（每种席别各一条，取该车次最便宜的可买席别）。"""
    data = fetch(a, b, date)
    if "error" in data:
        raise RuntimeError(data["error"])
    out = []
    for train in data.get("trains", []):
        seats = train.get("seats", {}) or {}
        best = None
        for seat, info in seats.items():
            remain = str(info.get("remain", ""))
            price = info.get("price")
            if not price or price <= 0:
                continue
            if remain in ("", "--", "无", "0"):
                continue
            if best is None or float(price) < best[1]:
                best = (seat, float(price))
        if not best:
            continue
        out.append(Leg(
            train_no=train["train_no"],
            dep_station=train["departure_station"],
            arr_station=train["arrival_station"],
            dep_time=train["departure_time"],
            arr_time=train["arrival_time"],
            duration_min=_minutes(train.get("duration", "00:00")),
            price=best[1],
            seat=best[0],
        ))
    out.sort(key=lambda l: _dt(date, l.arr_time))
    return out[:PER_SEG_KEEP]


def _minutes(duration: str) -> int:
    try:
        h, m = duration.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _next_dep(leg: Leg, date: str, not_before: datetime) -> datetime:
    """把「HH:MM 发车」落到具体日期：取不早于 not_before 的那一趟（最多往后推 2 天）。"""
    dep = _dt(date, leg.dep_time)
    for _ in range(3):
        if dep >= not_before:
            return dep
        dep += timedelta(days=1)
    return dep


def _chain_times(chain: list, date: str) -> tuple:
    """沿链推时间：返回 (出发时刻, 到达时刻)。跨日、跨两天都按绝对时间累加。"""
    dep0 = _dt(date, chain[0].dep_time)
    cur = dep0
    arr = cur
    for leg in chain:
        dep = _next_dep(leg, date, cur)
        arr = dep + timedelta(minutes=leg.duration_min)
        cur = arr
    return dep0, arr


def _fits(prev_arr: datetime, leg: Leg, date: str) -> tuple:
    """给定上一段的到站时刻，判断这一段是否落在换乘窗口内；返回 (是否可行, 发车时刻)。"""
    dep = _next_dep(leg, date, prev_arr)
    gap = (dep - prev_arr).total_seconds() / 60
    return MIN_GAP_MIN <= gap <= MAX_GAP_MIN, dep


def chains(fetch: Callable[[str, str, str], dict], nodes: list, date: str) -> list:
    """把 nodes 依次连起来，返回可行的 Leg 链（已按到达时间剪枝）。"""
    if len(nodes) < 2:
        return []
    # 逐段推进：acc 里放 (链, 当前到站时刻)，每一步丢掉接不上的链
    first_legs = segment(fetch, nodes[0], nodes[1], date)
    acc = []
    for leg in first_legs:
        dep = _dt(date, leg.dep_time)
        acc.append(([leg], dep + timedelta(minutes=leg.duration_min)))

    for i in range(1, len(nodes) - 1):
        nxt_legs = segment(fetch, nodes[i], nodes[i + 1], date)
        if not nxt_legs:
            return []
        merged = []
        for chain, end in acc:
            for leg in nxt_legs:
                good, dep = _fits(end, leg, date)
                if good:
                    merged.append((chain + [leg], dep + timedelta(minutes=leg.duration_min)))
        if not merged:
            return []
        merged.sort(key=lambda item: (item[1], sum(x.price for x in item[0])))
        acc = merged[:CHAIN_KEEP]
    return [chain for chain, _ in acc]


def to_scheme(chain: list, date: str, kind: str = "中转"):
    """把 Leg 链折叠成 watch.Scheme（保持既有字段，另带 legs）。"""
    import watch  # 延迟导入：watch 反过来会用到本模块

    first, last = chain[0], chain[-1]
    dep, arr = _chain_times(chain, date)
    total = int((arr - dep).total_seconds() // 60)
    return watch.Scheme(
        train_no="+".join(l.train_no for l in chain),
        dep_station=first.dep_station,
        arr_station=last.arr_station,
        dep_time=first.dep_time,
        arr_time=f"{arr.hour:02d}:{arr.minute:02d}",
        duration_min=total,
        price=sum(l.price for l in chain),
        seat="/".join(l.seat for l in chain),
        date=date,
        kind=kind,
        via="、".join(l.arr_station for l in chain[:-1]),
        legs=list(chain),
    )


def multi_hub_chains(
    fetch: Callable[[str, str, str], dict],
    origin: str,
    dest: str,
    hubs: list,
    date: str,
    pairs: Optional[list] = None,
) -> list:
    """两段换乘：origin → h1 → h2 → dest。

    pairs 为空时按传入顺序两两组合（调用方负责按"顺路"排序，见 geo.pick_hubs）。
    """
    if pairs is None:
        pairs = [(hubs[i], hubs[j]) for i in range(len(hubs)) for j in range(i + 1, len(hubs))]
    out = []
    for h1, h2 in pairs:
        try:
            out.extend(chains(fetch, [origin, h1, h2, dest], date))
        except RuntimeError:
            continue
    out.sort(key=lambda c: (c[-1].arr_time, sum(x.price for x in c)))
    return out[:CHAIN_KEEP]
