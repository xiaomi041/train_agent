"""把「误报」变成下一次的做法 —— agent 的第三步：从经验里改策略。

前两步是「这一轮该放宽哪一项」（decider.py）和「人中途改主意要能接住」（edit_goal）。
这一步是：**同一个错误犯两次之后，系统自己要改**。

数据从哪儿来：`events` 表里 `action='反馈'` 且 `reason='wrong'` 的行 —— 用户点了
「🙅 不是我要的」。但一条反馈只说"这次不对"，没说"哪里不对"，所以这里用同一任务
最近一次「命中」事件里记录的席别来归因。

规则（只收紧，不放松）：
- 同一线路（起点→终点）同一席别的误报累计 ≥ `POLICY_WRONG_THRESHOLD`（默认 2）；
- 新建任务时默认不再收这个席别，并把 `strict_seat` 打开；
- 界面上给一句提示，说明"是依据你在哪几次标错"，人可以再改回来。

为什么不自动放宽：自动放宽会掩盖问题（结果更差但没人发现），而且会让"策略"变成
黑箱。收紧是可验证的：下次同类误报应该不再出现，这正是 `test_policy.py` 断言的东西。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

THRESHOLD = int(os.getenv("POLICY_WRONG_THRESHOLD", "2"))
LOOKBACK_DAYS = int(os.getenv("POLICY_LOOKBACK_DAYS", "90"))


@dataclass
class Tightening:
    """一条线路上"该收紧什么"的结论。"""

    origin: str
    dest: str
    blocked: dict = field(default_factory=dict)   # 席别 -> 误报次数
    total_wrong: int = 0

    @property
    def active(self) -> bool:
        return bool(self.blocked)

    def advice(self) -> str:
        if not self.active:
            return ""
        parts = [f"{seat}（{n} 次）" for seat, n in
                 sorted(self.blocked.items(), key=lambda kv: -kv[1])]
        return (f"这条线路近 {LOOKBACK_DAYS} 天有误报：{'、'.join(parts)}被标成「不是我要的」，"
                f"已默认不再收这些席别。想改回来在任务卡片的「✏️ 改约束」里调。")


def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def wrong_seat_counts(store, days: int = LOOKBACK_DAYS) -> dict:
    """按 (起点, 终点) 汇总每个席别被标成误报的次数。"""
    with store._lock:
        rows = store._conn.execute(
            "SELECT goal_id FROM events WHERE action='反馈' AND reason='wrong' AND ts >= ?",
            (_since(days),),
        ).fetchall()

    out: dict = {}
    for (goal_id,) in rows:
        goal = store.load_goal(goal_id)       # spec 里才有起终点
        if goal is None:
            continue
        seat = (store.last_scheme(goal_id) or {}).get("seat") or ""
        key = (goal.origin, goal.dest)
        bucket = out.setdefault(key, {"total": 0, "seats": {}})
        bucket["total"] += 1
        if seat:
            bucket["seats"][seat] = bucket["seats"].get(seat, 0) + 1
    return out


def for_route(store, origin: str, dest: str, days: int = LOOKBACK_DAYS,
              threshold: int = THRESHOLD) -> Tightening:
    """这条线路上哪些席别该收紧（误报 ≥ threshold 次）。"""
    counts = wrong_seat_counts(store, days).get((origin, dest), {"total": 0, "seats": {}})
    blocked = {s: n for s, n in counts["seats"].items() if n >= threshold}
    return Tightening(origin=origin, dest=dest, blocked=blocked,
                      total_wrong=counts["total"])


def apply(goal, tight: Tightening) -> list:
    """把收紧结论落到一个新任务上，返回真正被剔除的席别。

    偏好里所有席别都被拦时保留原样（不能把用户的偏好清空到"什么都不要"），
    这种情况下只把 strict_seat 打开，让人自己决定。
    """
    if not tight.active:
        return []
    blocked = [s for s in goal.seat_prefs if s in tight.blocked]
    if not blocked:
        return []
    remaining = [s for s in goal.seat_prefs if s not in tight.blocked]
    if remaining:
        goal.seat_prefs = remaining
    goal.strict_seat = True
    return blocked
