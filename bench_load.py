"""
盯票存储与调度的压力基准：把"长时间"和"多任务"两件事量出来。

为什么要有这个
--------------
盯票是常驻系统，成本不是"一次查询"，而是"跑三天会变成什么样"：

- SQLite 里 `events` 表每个 tick 都写一行，5 分钟一次 = 288 行/天/任务，
  10 个任务跑一个月 = 8.6 万行。文件多大？查询还快吗？
- worker 每 20 秒扫一遍全部活跃任务，且单轮上限 20 —— 30 个任务时会怎样？

这个脚本不调 12306、不调模型，只测存储与调度本身，所以可以随便跑。

    python bench_load.py            # 默认规模
    python bench_load.py --fast     # 小规模，几秒出结果
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import watch

TMP = Path(__file__).parent / ".tmp-load"


def _goal(i: int, owner: str = "bench") -> watch.Goal:
    return watch.Goal(
        id=f"g-{owner}-{i:04d}",
        origin="北京",
        dest="上海",
        date="2026-12-01",
        budget=800,
        max_minutes=600,
        seat_prefs=["二等座"],
        interval_sec=300,
    )


def _decision(i: int) -> watch.Decision:
    return watch.Decision("等待", level=i % 3, reason=f"第 {i % 3} 级仍无满足方案（耗时超限）")


def size_of(p: Path) -> int:
    return p.stat().st_size if p.exists() else 0


def bench_storage(goals: int, events_per_goal: int, store: watch.Store) -> dict:
    t0 = time.perf_counter()
    for i in range(goals):
        store.save_goal(_goal(i))
    write_goals = time.perf_counter() - t0

    t0 = time.perf_counter()
    for i in range(goals):
        gid = f"g-bench-{i:04d}"
        for j in range(events_per_goal):
            store.log(gid, _decision(j), {"best": {"train_no": f"G{1000 + j}", "price": 553}})
    write_events = time.perf_counter() - t0

    n_events = goals * events_per_goal
    db = size_of(store.path)
    wal = size_of(Path(str(store.path) + "-wal"))

    t0 = time.perf_counter()
    got = store.list_goals("active")
    t_list = time.perf_counter() - t0

    t0 = time.perf_counter()
    rows = store.events("g-bench-0000", 50)
    t_events = time.perf_counter() - t0

    t0 = time.perf_counter()
    store.last_scheme("g-bench-0000")
    t_last = time.perf_counter() - t0

    return {
        "goals": len(got),
        "events": n_events,
        "db_bytes": db,
        "wal_bytes": wal,
        "bytes_per_event": db / n_events if n_events else 0,
        "write_goals_ms": write_goals * 1000,
        "write_events_s": write_events,
        "list_goals_ms": t_list * 1000,
        "events50_ms": t_events * 1000,
        "last_scheme_ms": t_last * 1000,
        "rows": len(rows),
    }


def bench_round(goals: int, store: watch.Store, latency: float = 0.0,
                concurrency: int = 1, fresh: bool = True) -> dict:
    """模拟 worker 扫一轮：fetcher 不联网，只按 latency 模拟一次请求的耗时。"""
    calls = {"n": 0}

    def fetcher(g):
        calls["n"] += 1
        if latency:
            time.sleep(latency)
        return []

    import watch_worker as ww

    saved = ww.CONCURRENCY
    ww.CONCURRENCY = concurrency
    try:
        w = ww.WatchWorker(store=store, fetcher=fetcher, notifier=lambda t: {"ok": True})
        t0 = time.perf_counter()
        briefs = w.run_once()
        elapsed = time.perf_counter() - t0
    finally:
        ww.CONCURRENCY = saved
    return {
        "handled": len(briefs),
        "fetches": calls["n"],
        "seconds": elapsed,
        "per_goal_ms": (elapsed * 1000 / len(briefs)) if briefs else 0,
        "concurrency": concurrency,
    }


def watch_worker_cls():
    import watch_worker

    return watch_worker.WatchWorker


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="小规模快速跑")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args(argv)

    goals = 10 if args.fast else 30
    events_per_goal = 288 if args.fast else 2880  # 288 = 一天（5 分钟一次）

    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    store = watch.Store(TMP / "watch.db")

    out = {"storage": bench_storage(goals, events_per_goal, store)}
    out["round_no_latency"] = bench_round(goals, store, latency=0.0)
    shutil.rmtree(TMP, ignore_errors=True)

    store2 = watch.Store(TMP / "watch2.db")
    for i in range(goals):
        store2.save_goal(_goal(i))
    out["round_with_latency"] = bench_round(goals, store2, latency=0.05)
    shutil.rmtree(TMP, ignore_errors=True)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    s = out["storage"]
    print("=" * 62)
    print(f"存储（{s['goals']} 个任务 × {events_per_goal} 条事件 = {s['events']} 行）")
    print("=" * 62)
    print(f"  db 文件        {s['db_bytes'] / 1024:8.1f} KB   (wal {s['wal_bytes'] / 1024:.1f} KB)")
    print(f"  每条事件       {s['bytes_per_event']:8.0f} B")
    print(f"  写 {s['events']} 条事件   {s['write_events_s']:8.2f} s")
    print(f"  list_goals     {s['list_goals_ms']:8.2f} ms   ({s['goals']} 个任务)")
    print(f"  events(50)     {s['events50_ms']:8.2f} ms")
    print(f"  last_scheme    {s['last_scheme_ms']:8.2f} ms")

    r = out["round_no_latency"]
    print()
    print("=" * 62)
    print("一轮调度（fetcher 无延迟，只测调度开销）")
    print("=" * 62)
    print(f"  处理 {r['handled']} 个任务，耗时 {r['seconds'] * 1000:.0f} ms，每条 {r['per_goal_ms']:.1f} ms")

    r2 = out["round_with_latency"]
    print()
    print("=" * 62)
    print("一轮调度（每次查询模拟 50 ms）")
    print("=" * 62)
    print(f"  处理 {r2['handled']} 个任务，耗时 {r2['seconds']:.2f} s，每条 {r2['per_goal_ms']:.0f} ms")

    # 外推：一天/一个月
    per_day = 288
    print()
    print("=" * 62)
    print("外推（每任务每 5 分钟一次，一天 288 条事件）")
    print("=" * 62)
    for n in (1, 5, 10, 50):
        for days in (1, 30):
            rows = n * per_day * days
            mb = rows * s["bytes_per_event"] / 1024 / 1024
            print(f"  {n:2d} 个任务 × {days:2d} 天 → {rows:8d} 行 / {mb:6.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
