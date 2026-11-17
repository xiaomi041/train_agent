"""多约束行程规划：把「最便宜 / 最快」升级成「在约束内可执行」。

原来的 `find_best_route()` 只做两件事：算最低价、算最短耗时，然后把两个答案
并排丢给用户。真实买票时的约束不止这两条——预算、总耗时、出发时段、到达时段、
换乘次数、席别，任何一条不满足的方案都不可执行。

这里的做法：
1. 约束用 `Constraints` 结构化表达，映射成 `watch.Goal` 后复用同一套 `check()`；
2. 所有候选按「违规条数 → 席别偏好 → 价格 → 耗时」排序，可行方案进入 `feasible`；
3. 不可行的不丢掉，按原因计数放进 `excluded`，并在输出里解释「为什么砍掉它」；
4. 给出三个不同取向的推荐（最便宜 / 最快 / 最省心）与价格-耗时帕累托前沿，
   让用户在「多花 80 块省 3 小时」这种取舍上自己拍板。

纯函数 `evaluate()` 不碰网络，可离线测试；`plan()` 只是「抓数据 + evaluate」。
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

import watch

# 六类可判定约束（与 watch.check 一一对应）
CONSTRAINT_FIELDS = ["预算", "总耗时", "出发时段", "到达时段", "换乘次数", "席别"]

REASON_HINT = {
    watch.VIOLATION_PRICE: "票价超过预算，等降价或换席别",
    watch.VIOLATION_DURATION: "路上太久，超过你能接受的总耗时",
    watch.VIOLATION_DEPART: "出发时间不在你能出发的时段内",
    watch.VIOLATION_ARRIVE: "到达时间不在你能接站的时段内",
    watch.VIOLATION_TRANSFER: "你不接受中转，但这条需要换乘",
    watch.VIOLATION_TRANSFER_COUNT: "换乘次数超过你设的上限",
    watch.VIOLATION_SEAT: "席别不在你的偏好里",
    watch.VIOLATION_NO_SEAT: "该车次对应席别没有余票",
}


@dataclass
class Constraints:
    """用户的六类约束。全部留空 = 只按价格/耗时排序，不做任何过滤。"""

    budget: Optional[float] = None
    max_minutes: Optional[int] = None
    depart_after: str = "00:00"
    depart_before: str = "23:59"
    arrive_after: str = "00:00"
    arrive_before: str = "23:59"
    max_transfers: int = 1
    seat_prefs: list = field(default_factory=lambda: ["二等座", "硬卧", "硬座"])
    strict_seat: bool = False
    allow_transfer: bool = True

    def to_goal(self, origin: str, dest: str, date: str) -> watch.Goal:
        return watch.Goal(
            origin=origin,
            dest=dest,
            date=date,
            budget=self.budget,
            max_minutes=self.max_minutes,
            depart_after=self.depart_after or "00:00",
            depart_before=self.depart_before or "23:59",
            arrive_after=self.arrive_after or "00:00",
            arrive_before=self.arrive_before or "23:59",
            max_transfers=self.max_transfers,
            seat_prefs=list(self.seat_prefs) or ["二等座"],
            strict_seat=self.strict_seat,
            allow_transfer=self.allow_transfer,
        )

    def describe(self) -> list:
        out = []
        if self.budget is not None:
            out.append(f"预算 ≤ ¥{self.budget:.0f}")
        if self.max_minutes:
            out.append(f"总耗时 ≤ {self.max_minutes // 60} 小时 {self.max_minutes % 60} 分")
        if self.depart_after != "00:00" or self.depart_before != "23:59":
            out.append(f"出发 {self.depart_after}–{self.depart_before}")
        if self.arrive_after != "00:00" or self.arrive_before != "23:59":
            out.append(f"到达 {self.arrive_after}–{self.arrive_before}")
        if not self.allow_transfer:
            out.append("只接受直达")
        elif self.max_transfers == 0:
            out.append("只接受直达")
        else:
            out.append(f"最多换乘 {self.max_transfers} 次")
        out.append("席别：" + "/".join(self.seat_prefs) + ("（严格）" if self.strict_seat else ""))
        return out


@dataclass
class Pick:
    name: str
    scheme: Optional[watch.Scheme]
    why: str


@dataclass
class PlanResult:
    origin: str
    dest: str
    date: str
    total: int
    feasible: list = field(default_factory=list)
    excluded: dict = field(default_factory=dict)
    picks: list = field(default_factory=list)
    frontier: list = field(default_factory=list)
    constraints: Optional[Constraints] = None

    # ---------------------------------------------------------------- 渲染

    def head_line(self) -> str:
        return (f"{self.origin} → {self.dest}　{self.date}　"
                f"候选 {self.total} 条，可行 {len(self.feasible)} 条")

    @staticmethod
    def _row(i: int, s: watch.Scheme) -> str:
        via = f"（经 {s.via}）" if s.kind == "中转" else ""
        h, m = divmod(s.duration_min, 60)
        return (f"| {i} | {s.train_no} | {s.dep_time} → {s.arr_time} | "
                f"{h}h{m:02d}m | {s.seat} | ¥{s.price:.0f} | {s.kind}{via} |")

    def table(self, limit: int = 12) -> str:
        if not self.feasible:
            return "没有满足全部约束的方案。"
        head = ("| # | 车次 | 出发 → 到达 | 历时 | 席别 | 价格 | 类型 |\n"
                "|---|---|---|---|---|---|---|")
        rows = [self._row(i + 1, s) for i, s in enumerate(self.feasible[:limit])]
        more = ""
        if len(self.feasible) > limit:
            more = f"\n\n（还有 {len(self.feasible) - limit} 条未显示，按同样规则排序）"
        return head + "\n" + "\n".join(rows) + more

    def picks_table(self) -> str:
        if not self.picks:
            return ""
        head = ("| 取向 | 方案 | 为什么 |\n|---|---|---|")
        rows = []
        for p in self.picks:
            if p.scheme is None:
                rows.append(f"| {p.name} | — | {p.why} |")
            else:
                rows.append(f"| {p.name} | {p.scheme.label()} | {p.why} |")
        return head + "\n" + "\n".join(rows)

    def excluded_text(self) -> str:
        if not self.excluded:
            return "没有被排除的方案。"
        lines = []
        for reason, n in sorted(self.excluded.items(), key=lambda kv: -kv[1]):
            lines.append(f"- **{reason}**：{n} 条 —— {REASON_HINT.get(reason, '不满足该约束')}")
        return "\n".join(lines)

    def frontier_text(self) -> str:
        if len(self.frontier) < 2:
            return ""
        lines = []
        for s in self.frontier:
            h, m = divmod(s.duration_min, 60)
            lines.append(f"- {s.train_no} {s.seat}　¥{s.price:.0f}　{h}h{m:02d}m")
        return "\n".join(lines)


# ---------------------------------------------------------------- 纯计算

def _pareto(schemes: list) -> list:
    """价格-耗时双目标前沿：没有别的方案比它既便宜又更快。"""
    out = []
    for s in schemes:
        dominated = any(
            o is not s and o.price <= s.price and o.duration_min <= s.duration_min
            and (o.price < s.price or o.duration_min < s.duration_min)
            for o in schemes
        )
        if not dominated:
            out.append(s)
    return sorted(out, key=lambda s: s.price)


def _picks(feasible: list, cons: Constraints) -> list:
    if not feasible:
        return [Pick("结论", None, "当前约束下无解——建议放宽其中一条（预算 / 耗时 / 时段）")]
    cheapest = min(feasible, key=lambda s: (s.price, s.duration_min))
    fastest = min(feasible, key=lambda s: (s.duration_min, s.price))
    easy = min(feasible, key=lambda s: (1 if s.kind == "中转" else 0, s.price, s.duration_min))

    # 平衡：价格与耗时各自归一到 0~1 后相加
    pmin, pmax = min(s.price for s in feasible), max(s.price for s in feasible)
    dmin, dmax = min(s.duration_min for s in feasible), max(s.duration_min for s in feasible)

    def norm(s):
        p = 0 if pmax == pmin else (s.price - pmin) / (pmax - pmin)
        d = 0 if dmax == dmin else (s.duration_min - dmin) / (dmax - dmin)
        return p + d

    balanced = min(feasible, key=norm)
    save_price = fastest.price - cheapest.price
    save_min = cheapest.duration_min - fastest.duration_min
    out = [
        Pick("💰 最便宜", cheapest,
             f"¥{cheapest.price:.0f}，{cheapest.kind}；比最快省 ¥{save_price:.0f}"),
        Pick("⚡ 最快", fastest,
             f"{fastest.duration_min // 60}h{fastest.duration_min % 60:02d}m；比最便宜省 "
             f"{save_min // 60}h{save_min % 60:02d}m" if save_min > 0 else
             f"{fastest.duration_min // 60}h{fastest.duration_min % 60:02d}m"),
    ]
    if easy.train_no != cheapest.train_no or easy.train_no != fastest.train_no:
        out.append(Pick("🧘 最省心", easy, "全程直达或换乘最少，扛行李更轻松"))
    if balanced.train_no not in {cheapest.train_no, fastest.train_no}:
        out.append(Pick("⚖️ 折中", balanced, "价格与耗时各让一点，综合分最低"))
    return out


def evaluate(schemes: list, cons: Constraints, origin: str = "", dest: str = "", date: str = "") -> PlanResult:
    """把候选方案按约束分类，产出可行集、排除统计、推荐与前沿。纯函数。"""
    goal = cons.to_goal(origin, dest, date or (schemes[0].date if schemes else ""))
    feasible, excluded = [], {}
    for s in schemes:
        bad = watch.check(s, goal)
        if bad:
            for r in bad:
                excluded[r] = excluded.get(r, 0) + 1
        else:
            feasible.append(s)

    feasible = watch.rank(feasible, goal)
    return PlanResult(
        origin=origin, dest=dest, date=date,
        total=len(schemes), feasible=feasible, excluded=excluded,
        picks=_picks(feasible, cons), frontier=_pareto(feasible), constraints=cons,
    )


def plan(origin: str, dest: str, date: str, cons: Constraints,
         hubs: Optional[list] = None,
         fetcher: Callable = None,
         sleep_between: float = 0.2) -> PlanResult:
    """抓取候选 → evaluate。fetcher 可注入，便于离线测试与替换数据源。"""
    goal = cons.to_goal(origin, dest, date)
    # 允许中转时把放宽级别抬到 1：real_fetcher 靠这个开关才会去查枢纽，
    # 否则「多约束规划」永远只能看到直达车（这是设计上的默认，而非漏查）。
    if cons.allow_transfer and cons.max_transfers >= 1:
        goal.relax_level = max(goal.relax_level, 1)
    if fetcher is None:
        schemes = watch.real_fetcher(goal, hubs=hubs, sleep_between=sleep_between)
    else:
        schemes = fetcher(goal)
    return evaluate(schemes, cons, origin=origin, dest=dest, date=date)
