"""长时间盯票的存储演化：跑 30 天，看数据库会不会失控。

这是"提前考虑"的量化：不是猜，是按真实写入模式跑一遍。

    python bench_longrun.py            # 10 个任务 × 30 天
    python bench_longrun.py --days 7 --goals 5

对照组：
  A. 不裁剪 —— 每 5 分钟一条事件，全部留着
  B. 每 30 分钟裁剪一次，每任务保留最近 200 条
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import watch

TMP = Path(__file__).parent / ".tmp-longrun"


def simulate(goals: int, days: int, prune: bool) -> dict:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    st = watch.Store(TMP / "w.db")
    for i in range(goals):
        st.save_goal(watch.Goal(origin="北京", dest="上海", date="2026-12-01", id=f"g{i:03d}"))

    # 每 5 分钟一次 tick，一天 288 次
    ticks = days * 288
    d = watch.Decision("等待", 0, "本轮无票（搜索范围已放开），继续盯")
    t0 = time.perf_counter()
    for t in range(ticks):
        for i in range(goals):
            st.log(f"g{i:03d}", d, {"candidates": 0})
        if prune and t % 6 == 0:          # 每 30 分钟裁一次
            st.prune_events(keep_per_goal=200, keep_days=30)
    elapsed = time.perf_counter() - t0

    n = st.event_count()
    size = st.db_bytes()
    tq = time.perf_counter()
    st.events("g000", 50)
    q_ms = (time.perf_counter() - tq) * 1000
    tl = time.perf_counter()
    st.list_goals("active")
    l_ms = (time.perf_counter() - tl) * 1000
    st._conn.close()   # Windows 上不关连接，rmtree 删不掉文件，下一次会复用旧库
    out = {
        "prune": prune, "goals": goals, "days": days, "ticks": ticks,
        "events": n, "db_kb": size / 1024, "write_s": elapsed,
        "events_query_ms": q_ms, "list_ms": l_ms,
    }
    shutil.rmtree(TMP, ignore_errors=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--goals", type=int, default=10)
    args = ap.parse_args(argv)

    rows = [simulate(args.goals, args.days, prune=False),
            simulate(args.goals, args.days, prune=True)]

    print("=" * 72)
    print(f"{args.goals} 个任务 × {args.days} 天（每 5 分钟一次 = {args.days * 288} 轮）")
    print("=" * 72)
    print(f"{'策略':<22}{'事件行':>10}{'库大小':>12}{'写入耗时':>12}{'查50条':>10}")
    for r in rows:
        name = "不裁剪" if not r["prune"] else "每任务留200条"
        print(f"{name:<22}{r['events']:>10}{r['db_kb']:>10.1f}KB"
              f"{r['write_s']:>11.1f}s{r['events_query_ms']:>9.2f}ms")

    a, b = rows[0], rows[1]
    if b["db_kb"] > 0:
        print()
        print(f"裁剪把库压到 1/{a['db_kb'] / b['db_kb']:.0f}："
              f"{a['db_kb'] / 1024:.1f} MB → {b['db_kb']:.1f} KB")
        print(f"事件行数 {a['events']} → {b['events']}"
              f"（{a['events'] / max(1, b['events']):.0f}×）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
