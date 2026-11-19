"""反馈闭环的对照实验：同一个错误会犯几次？

设定：用户其实只要硬卧，但偏好里写的是「二等座、硬卧」。排序会把二等座排在前面，
所以每次命中都是二等座 —— 用户点「🙅 不是我要的」，系统记一笔误报。

对照组（A，`--no-policy`）：误报只是被记下来，不影响下一次。
实验组（B，默认）：同一线路同一席别误报 ≥ 2 次后，新建任务自动剔除该席别。

衡量的是「同一个错误犯几次」，不是"命中率越高越好"——命中率两组一样（都命中了），
差别在命中的东西是不是用户要的。

run: python bench_loop.py [--rounds 10]
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import policy
import watch
from watch import Goal, Scheme, Store, Watcher

DATE = "2026-12-01"
ROUNDS = 10


def scheme(seat, price, train_no):
    return Scheme(train_no=train_no, dep_station="北京南", arr_station="上海虹桥",
                  dep_time="08:00", arr_time="14:30", duration_min=390,
                  price=price, seat=seat, date=DATE)


def fetcher(goal):
    """两趟车都能买：二等座更便宜，所以会被排在前面。"""
    return [scheme("二等座", 553.0, "G101"), scheme("硬卧", 620.0, "Z21")]


def run(rounds: int, use_policy: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="loop-bench-"))
    store = Store(tmp / "w.db")
    wrong = 0
    booked = []          # 每轮最终"买到"的席别
    ticks = 0
    try:
        for _ in range(rounds):
            g = Goal(origin="北京", dest="上海", date=DATE, budget=800.0,
                     max_minutes=600, seat_prefs=["二等座", "硬卧"], interval_sec=1)
            if use_policy:
                policy.apply(g, policy.for_route(store, g.origin, g.dest))
            store.save_goal(g)
            w = Watcher(g, fetcher=fetcher, store=store, sleep=lambda s: None)
            d = w.tick()
            ticks += g.attempts
            if d.action != "命中":
                booked.append("-")
                continue
            seat = d.best.seat
            booked.append(seat)
            if seat == "二等座":        # 用户其实只要硬卧 → 这次算误报
                store.feedback(g.id, "wrong")
                wrong += 1
            else:
                store.feedback(g.id, "good")
        stats = store.stats(days=1)
    finally:
        store._conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return {"wrong": wrong, "booked": booked, "checks": ticks,
            "hit_rate": stats["hit_rate"], "rounds": rounds}


def main() -> int:
    rounds = ROUNDS
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    a = run(rounds, use_policy=False)
    b = run(rounds, use_policy=True)

    print("=" * 62)
    print(f"反馈闭环对照（{rounds} 轮，线路 北京→上海，用户偏好实际是硬卧）")
    print("=" * 62)
    print(f"{'':<26}{'误报次数':>10}{'命中率':>10}{'总检查':>10}")
    print(f"{'A 无闭环（只记不改）':<26}{a['wrong']:>10}{a['hit_rate']:>9.0%}{a['checks']:>10}")
    print(f"{'B 有闭环（≥2 次收紧）':<26}{b['wrong']:>10}{b['hit_rate']:>9.0%}{b['checks']:>10}")
    print()
    print(f"A 每轮买到的席别：{' '.join(a['booked'])}")
    print(f"B 每轮买到的席别：{' '.join(b['booked'])}")

    saved = a["wrong"] - b["wrong"]
    if a["wrong"]:
        print(f"\n同一错误从 {a['wrong']} 次降到 {b['wrong']} 次"
              f"（少 {saved} 次，{(saved / a['wrong']):.0%}）；"
              f"命中率不变说明闭环只改「买什么」，不改「能不能买到」。")
    else:
        print("\n这组数据里没有误报，闭环无从生效（检查 fetcher 与偏好设置）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
