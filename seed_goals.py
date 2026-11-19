"""给长周期观察撒几个任务（只在本地/容器里手动跑一次，不进产品路径）。

    python seed_goals.py          # 建 3 个任务
    python seed_goals.py --list   # 只看现有任务

设计意图：三个任务覆盖三种"命运"，观察时才能同时看到退避、降级、命中：
1. 紧约束（容易触发逐级放宽）
2. 中等约束（正常命中）
3. 极宽松（第一轮就命中，验证通知与订票记录）
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

import watch


def _days(n: int) -> str:
    return (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")


OWNER = os.getenv("SEED_OWNER", "default")

GOALS = [
    # 紧约束：预算压到明显不够，只能靠"放宽预算"才能买到 -> 观察逐级放宽
    dict(origin="深圳", dest="乌鲁木齐", date=_days(5), flex_days=1,
         budget=600, max_minutes=1200, arrive_after="00:00", arrive_before="23:59",
         seat_prefs=["硬卧"], allow_transfer=True, max_transfers=1,
         interval_sec=300, note="长跑样本 A：紧约束，观察逐级放宽"),
    # 中等约束：常见线路 + 合理预算 -> 大概率在第 1–2 级命中
    dict(origin="北京", dest="上海", date=_days(3), flex_days=0,
         budget=700, max_minutes=600, arrive_after="06:00", arrive_before="23:00",
         seat_prefs=["二等座"], allow_transfer=True, max_transfers=1,
         interval_sec=300, note="长跑样本 B：中等约束，观察命中与通知"),
    # 极宽松：几乎必然第一轮命中 -> 验证命中->通知->订票记录全链路
    dict(origin="广州", dest="长沙", date=_days(2), flex_days=0,
         budget=2000, max_minutes=2000, arrive_after="00:00", arrive_before="23:59",
         seat_prefs=["二等座", "一等座", "硬座", "硬卧"], allow_transfer=True,
         max_transfers=1, interval_sec=300, note="长跑样本 C：极宽松，验证全链路"),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    store = watch.Store()
    if args.list:
        for g in store.list_goals(owner=None):
            print(f"{g.id}  {g.status:8s}  {g.origin}→{g.dest} {g.date}  "
                  f"L{g.relax_level} attempts={g.attempts}  {g.note}")
        return 0

    for spec in GOALS:
        note = spec.pop("note")
        g = watch.Goal(owner=OWNER, **spec)
        g.note = note
        store.save_goal(g)
        print(f"已建 {g.id}  {g.origin}→{g.dest} {g.date}  owner={OWNER}  {note}")
    print(f"\n当前共 {len(store.list_goals(owner=None))} 个任务，"
          f"事件 {store.event_count()} 条，库 {store.db_bytes() / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
