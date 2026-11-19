"""长周期观察：把 watch.db 里的事件读成一张"它这两天到底干了什么"的表。

不用外挂采样进程 —— worker 每 600 秒自己打一行心跳（运行时长/轮次/事件数/库大小），
这个脚本负责把 SQLite 里的事实摊开：

    python observe.py                 # 最近 7 天
    python observe.py --days 2        # 最近 2 天
    python observe.py --goal <id>     # 只看某个任务

三个问题它要回答：
1. **还在转吗** —— 事件的时间间隔有没有突然断开（机器睡眠/容器被杀会留空洞）
2. **节奏对不对** —— 间隔是否按退避放大、有没有封顶到 4×
3. **库有没有失控** —— 事件条数、单条字节数、裁剪有没有发生过
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

import watch


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def timeline(store: watch.Store, goal_id: str, since: datetime) -> list[dict]:
    rows = store.events(goal_id, 500)
    out = []
    prev = None
    for ts, lvl, act, reason in reversed(rows):
        t = _dt(ts)
        if t < since:
            continue
        gap = (t - prev).total_seconds() if prev else 0.0
        out.append({"ts": t, "level": lvl, "action": act, "reason": reason, "gap": gap})
        prev = t
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=float(os.getenv("OBSERVE_DAYS", "7")))
    ap.add_argument("--goal", default="")
    args = ap.parse_args(argv)

    store = watch.Store()
    since = datetime.now() - timedelta(days=args.days)

    goals = store.list_goals(owner=None)
    if args.goal:
        goals = [g for g in goals if g.id == args.goal]
    print(f"窗口：最近 {args.days:g} 天｜任务 {len(goals)} 个｜"
          f"事件 {store.event_count()} 条｜库 {store.db_bytes() / 1024:.1f} KB")
    print("=" * 78)

    all_events = store.event_count()
    per_goal = max(1, len(goals))
    print(f"事件/任务 = {all_events / per_goal:.0f} 条"
          f"（每任务保留上限 200，超过说明裁剪还没触发或任务很少）")
    print()

    total_hits = total_give_up = 0
    for g in goals:
        rows = timeline(store, g.id, since)
        if not rows:
            print(f"[{g.id}] {g.origin}→{g.dest} {g.date}｜{g.status}｜窗口内无事件")
            continue
        hits = sum(1 for r in rows if r["action"] == "命中")
        gives = sum(1 for r in rows if r["action"] == "放弃")
        total_hits += hits
        total_give_up += gives
        gaps = [r["gap"] for r in rows if r["gap"] > 0]
        big = [r for r in rows if r["gap"] > 3600]
        levels = [r["level"] for r in rows if r["level"] is not None]
        print(f"[{g.id}] {g.origin}→{g.dest} {g.date}｜{g.status}｜"
              f"attempts={g.attempts}｜事件 {len(rows)} 条｜命中 {hits}｜放弃 {gives}")
        if gaps:
            print(f"    间隔：中位 {sorted(gaps)[len(gaps) // 2]:.0f}s｜"
                  f"最大 {max(gaps):.0f}s｜平均 {sum(gaps) / len(gaps):.0f}s")
        if levels:
            print(f"    放宽档位：最高 {max(levels)}｜轨迹 "
                  + "".join(str(x) for x in levels[-20:])
                  + ("…" if len(levels) > 20 else ""))
        if big:
            print(f"    ⚠ 有 {len(big)} 段超过 1 小时没有事件（机器睡眠/容器停止）：")
            for r in big[:3]:
                print(f"       {r['ts']:%m-%d %H:%M} 距上一条 {r['gap'] / 3600:.1f} 小时")
        print(f"    最近三条：")
        for r in rows[-3:]:
            print(f"       {r['ts']:%m-%d %H:%M:%S} 第 {r['level']} 级 · {r['action']} · "
                  f"{r['reason'][:56]}")

    print("=" * 78)
    print(f"汇总：命中 {total_hits} 次、放弃 {total_give_up} 次")
    st = store.stats(int(args.days) or 7)
    print(f"stats：任务 {st['goals']}｜检查 {st['checks']}｜命中率 {st['hit_rate']:.0%}｜"
          f"平均命中轮数 {st['avg_ticks_to_hit']:.1f}｜误报 {st['wrong']}｜订票 {st['bookings']}")

    # 数据不足时明确说"还不能下结论"，避免把小样本读数当成趋势
    if st["checks"] < 50:
        print("\n⚠ 检查次数不足 50，上面的间隔/退避还不足以判断长期趋势。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
