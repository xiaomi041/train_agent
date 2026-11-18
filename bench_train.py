"""trainAI 量化基准：把「看起来像优化」换成「测出来的数字」。

四组测量（全部打真实 12306 接口，需在容器内跑）：
  A 单次车站查询耗时（HTTP 往返 + JSON 解析）
  B 规划耗时随枢纽数的增长（请求数、候选数、端到端耗时）
  C 盯票一次 tick 的耗时与请求数
  D 枢纽筛选把「无效枢纽」挡掉了多少（省下的请求）

用法：python bench_train.py
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

import geo
import planner
import ticket_api
import watch

ORIGIN = "北京"
DEST = "上海"
DATE = "2026-11-25"

# ---- 请求计数：包一层 ticket_api.query_ticket_dict，所有路径都会经过它 ----
_CALLS = {"n": 0}
_orig_query = ticket_api.query_ticket_dict


def _counted_query(*args, **kwargs):
    _CALLS["n"] += 1
    return _orig_query(*args, **kwargs)


ticket_api.query_ticket_dict = _counted_query


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    k = max(0, min(len(data) - 1, int(round((p / 100) * (len(data) - 1)))))
    return data[k]


def ms(x: float) -> str:
    return f"{x * 1000:.0f} ms"


@dataclass
class Run:
    label: str
    seconds: float
    calls: int
    schemes: int = 0
    note: str = ""


def part_a() -> list[Run]:
    """A：单次车站查询耗时，取 5 次。"""
    out: list[Run] = []
    samples: list[float] = []
    for _ in range(5):
        _CALLS["n"] = 0
        t0 = time.perf_counter()
        res = ticket_api.query_ticket_dict(ORIGIN, DEST, DATE)
        dt = time.perf_counter() - t0
        samples.append(dt)
        out.append(Run("单次车站查询", dt, _CALLS["n"], len(res), "北京→上海"))
        time.sleep(0.3)
    print(
        f"[A] 单次查询 5 次：P50 {ms(pct(samples, 50))} / P95 {ms(pct(samples, 95))} "
        f"/ 最大 {ms(max(samples))}"
    )
    return out


def _run_plan(hub_list: list[str], label: str) -> Run:
    cons = planner.Constraints(budget=900, max_minutes=900, max_transfers=1)
    _CALLS["n"] = 0
    t0 = time.perf_counter()
    res = planner.plan(ORIGIN, DEST, DATE, cons, hubs=hub_list, sleep_between=0.2)
    dt = time.perf_counter() - t0
    print(
        f"[B] {label:<12} 枢纽 {len(hub_list):>2} 个 → 请求 {_CALLS['n']:>2} 次 / "
        f"候选 {res.total:>5} / 可行 {len(res.feasible):>4} / 耗时 {dt:.2f} s"
    )
    return Run(label, dt, _CALLS["n"], res.total, f"hubs={len(hub_list)}")


def part_b(hubs_all: list[str]) -> list[Run]:
    """B：规划耗时随枢纽数增长（3 / 6 / 12）。"""
    return [
        _run_plan(hubs_all[:3], "plan-3"),
        _run_plan(hubs_all[:6], "plan-6"),
        _run_plan(hubs_all[:12], "plan-12"),
    ]


def part_c() -> Run:
    """C：盯票一次 tick（含中转移交）。"""
    goal = watch.Goal(
        origin=ORIGIN,
        dest=DEST,
        date=DATE,
        budget=900,
        max_minutes=900,
        allow_transfer=True,
        relax_level=2,
        interval_sec=20,
    )
    store = watch.Store(":memory:")
    store.save_goal(goal)
    w = watch.Watcher(goal, fetcher=watch.real_fetcher, store=store)
    _CALLS["n"] = 0
    t0 = time.perf_counter()
    d = w.tick()
    dt = time.perf_counter() - t0
    print(f"[C] 一次 tick：请求 {_CALLS['n']} 次 / 耗时 {dt:.2f} s / 决策 {d.action}（{d.reason}）")
    return Run("tick", dt, _CALLS["n"], 0, d.action)


def part_d(hubs_all: list[str]) -> None:
    """D：筛选挡掉了多少枢纽（用请求数换算成省下的时间）。"""
    picked = geo.pick_hubs(ORIGIN, DEST, hubs_all, k=3)
    single = pct([r.seconds for r in PART_A], 50)
    print(
        f"[D] 全部枢纽 {len(hubs_all)} 个 → 筛出 {len(picked)} 个 {picked}；"
        f"若逐个查需 ≈ {len(hubs_all)} × {ms(single)} ≈ "
        f"{len(hubs_all) * single:.1f} s，实际 {len(picked)} × {ms(single)} ≈ {len(picked) * single:.1f} s"
    )


def load_hubs() -> list[str]:
    """从 a.py 里按 AST 取 HUB_STATIONS_BY_REGION，避免 import Streamlit 页面。"""
    import ast
    from pathlib import Path

    src = Path(__file__).with_name("a.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "HUB_STATIONS_BY_REGION" for t in node.targets
        ):
            regions = ast.literal_eval(node.value)
            return [s for group in regions.values() for s in group]
    return []


if __name__ == "__main__":
    print("=" * 64)
    hubs_all = load_hubs()
    print(f"枢纽池：{len(hubs_all)} 个｜场景：{ORIGIN}→{DEST} {DATE}")
    print("=" * 64)

    PART_A = part_a()
    runs = list(PART_A)
    runs += part_b(hubs_all)
    runs.append(part_c())
    print("-" * 64)
    part_d(hubs_all)
    print("-" * 64)
    print("汇总：")
    for r in runs:
        print(f"  {r.label:<14} {r.seconds:6.2f} s  请求 {r.calls:>3}  候选 {r.schemes:>6}  {r.note}")
    total_calls = sum(r.calls for r in runs)
    total_s = sum(r.seconds for r in runs)
    print(f"  合计 {total_calls} 次请求 / {total_s:.1f} s；平均每次请求 {total_s / max(1, total_calls) * 1000:.0f} ms")
    print(f"  请求耗时 P50 {ms(statistics.median([r.seconds / max(1, r.calls) for r in runs]))}")
