"""规则版 vs 模型版：放宽哪一项，谁选得准。

六个场景，每个场景里只有一项放宽能救这趟车（或者根本不该放宽）。
判分不看最终有没有命中（两组都能命中），看**代价**：

- 放宽次数：开了几项约束才买到（越少越好，用户的容忍度不该被白白消耗）
- 抓取次数：多开一项就多查一轮接口（12306 会限流）
- 模型调用：模型版自己的成本

用法：
    python bench_decide.py            # 两组都跑
    python bench_decide.py --only money
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import decider
import watch

DATE = "2026-11-25"
NEXT = (datetime.strptime(DATE, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def S(**kw):
    base = dict(train_no="G1", dep_station="北京南", arr_station="上海虹桥",
                dep_time="08:00", arr_time="14:30", duration_min=390,
                price=500.0, seat="二等座", date=DATE, kind="直达")
    base.update(kw)
    return watch.Scheme(**base)


# 每个场景：goal + 「世界」（不管放不放宽都返回这些候选，由 check() 判定）
def _goal(**kw):
    base = dict(origin="北京", dest="上海", date=DATE, budget=600.0,
                max_minutes=600, arrive_after="06:00", arrive_before="23:59",
                seat_prefs=["二等座"], strict_seat=True, allow_transfer=False,
                max_transfers=0, flex_days=2, interval_sec=1)
    base.update(kw)
    return watch.Goal(**base)


SCENARIOS = {
    # 只差预算：620 > 600，放宽 money（×1.2=720）后即可
    "money": (_goal(), [S(price=620.0)]),
    # 只差席别：一等座不在偏好里（strict），放宽 seat 后即可
    "seat": (_goal(), [S(seat="一等座", price=553.0)]),
    # 只差到达时段：00:30 到，超出 06:00-23:59，放宽 arrive 后即可
    "arrive": (_goal(arrive_before="23:59"), [S(dep_time="19:00", arr_time="00:30",
                                                duration_min=330, price=480.0)]),
    # 只差日期：当天没票，放宽 date 后有次日车
    "date": (_goal(), []),          # 见 fetcher 里的特判
    # 全部放宽也没用（价格 900 远超 600×1.2），应该放弃而不是一路放宽
    "hopeless": (_goal(), [S(price=900.0)]),
    # 只是还没放票：前两轮空，第三轮来一趟好票 —— 正确动作是等，不是放宽
    "wait": (_goal(), []),
}


def make_fetcher(name):
    goal, schemes = SCENARIOS[name]
    calls = {"n": 0}

    def fetcher(g: watch.Goal) -> list:
        calls["n"] += 1
        dims = watch.relax_dims(g)
        if name == "date":
            # 当天没有，放宽日期后才有次日车
            return [S(date=NEXT, price=520.0)] if "date" in dims else []
        if name == "wait":
            return [S(price=520.0)] if g.attempts >= 3 else []
        return list(schemes)

    fetcher.calls = calls
    return fetcher


def run_one(name: str, kind: str, max_ticks: int = 8) -> dict:
    goal, _ = SCENARIOS[name]
    goal = watch.Goal.from_json(goal.to_json())      # 每次跑用干净任务
    fetcher = make_fetcher(name)
    tmp = Path(tempfile.mkdtemp(prefix="dec-"))
    store = watch.Store(tmp / "w.db")
    calls = {"n": 0, "in": 0, "out": 0}

    # 这里只比较「规则 vs 模型」的决策质量，所以把小时配额放开，
    # 否则一个场景跑 8 轮就会撞上限额，把成本对比变成"被限额截断"的对比。
    real_quota = decider.QUOTA_PER_HOUR
    decider.QUOTA_PER_HOUR = 999
    decider.clear_cache()
    decider.reset_stats()
    real_ask = decider._ask_model

    def counting_ask(text):
        """记真实模型调用与 token（缓存命中不该算钱）。"""
        calls["n"] += 1
        out = real_ask(text)
        u = getattr(decider, "LAST_USAGE", {}) or {}
        calls["in"] += int(u.get("in", 0) or 0)
        calls["out"] += int(u.get("out", 0) or 0)
        return out

    dec = decider.llm_decider if kind == "llm" else decider.rule_decider
    w = watch.Watcher(goal, fetcher=fetcher, store=store, sleep=lambda s: None, decider=dec)

    decision = None
    try:
        if kind == "llm":
            decider._ask_model = counting_ask
        for _ in range(max_ticks):
            decision = w.tick()
            if decision.action in ("命中", "放弃"):
                break
        dstats = decider.stats()
    finally:
        decider._ask_model = real_ask
        decider.QUOTA_PER_HOUR = real_quota
        decider.clear_cache()

    out = {
        "scenario": name,
        "kind": kind,
        "action": decision.action,
        "ticks": goal.attempts,
        "relaxed": list(goal.relax_dims),
        "dims_opened": len(goal.relax_dims),
        "fetches": fetcher.calls["n"],
        "llm_calls": calls["n"],
        "cache_hits": dstats["cache_hits"] if kind == "llm" else 0,
        "tokens": calls["in"] + calls["out"],
    }
    store._conn.close()
    # 容器里 TMPDIR 可能指向 /app，不清理会在镜像工作目录里堆一堆 dec-* 目录
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--ticks", type=int, default=8)
    args = ap.parse_args()

    names = [n for n in SCENARIOS if not args.only or args.only in n]
    rows = []
    for name in names:
        for kind in ("rule", "llm"):
            rows.append(run_one(name, kind, args.ticks))

    head = (f"{'场景':<10}{'决策器':<6}{'结论':<6}{'轮数':>4}"
            f"{'放宽':>5}{'请求':>5}{'模型调用':>7}{'缓存':>5}{'token':>7}")
    print(head)
    print("-" * 60)
    for r in rows:
        print(f"{r['scenario']:<10}{r['kind']:<6}{r['action']:<6}{r['ticks']:>4}"
              f"{r['dims_opened']:>5}{r['fetches']:>5}{r['llm_calls']:>7}"
              f"{r['cache_hits']:>5}{r['tokens']:>7}")

    print()
    for kind in ("rule", "llm"):
        sel = [r for r in rows if r["kind"] == kind]
        tok_in = sum(r["tokens"] for r in sel)          # 只在模型版有意义
        cost = tok_in / 1_000_000 * 2 + 0               # 粗略：按 ¥2/M 计
        print(f"{kind:<5} 放宽 {sum(r['dims_opened'] for r in sel):>2} 项 | "
              f"请求 {sum(r['fetches'] for r in sel):>2} 次 | "
              f"命中 {sum(1 for r in sel if r['action'] == '命中')}/{len(sel)} | "
              f"模型调用 {sum(r['llm_calls'] for r in sel)} 次 | "
              f"缓存命中 {sum(r['cache_hits'] for r in sel)} 次 | "
              f"约 ¥{cost:.4f}")


if __name__ == "__main__":
    main()
