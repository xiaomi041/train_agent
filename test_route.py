"""多段中转（route.py）的离线自检：不联网，用假数据把换乘窗口、剪枝、折叠都验一遍。

关键要验的不是「能连通」，而是**换乘窗口**：
  - 间隔太短（10 分钟）不能算，现实中赶不上；
  - 间隔太长（8 小时）不该算，那等于在中间城市过夜，不如换个方案；
  - 跨日（23:50 到、次日 00:30 发）要按 +1 天算。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import route  # noqa: E402
import watch  # noqa: E402

PASS = 0
FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def train(no, dep_st, arr_st, dep, arr, dur, price, seat="二等座"):
    return {
        "train_no": no,
        "departure_station": dep_st,
        "arrival_station": arr_st,
        "departure_time": dep,
        "arrival_time": arr,
        "duration": dur,
        "seats": {seat: {"remain": "有", "price": price}},
    }


FLIGHTS = {
    ("北京", "济南"): [
        train("G101", "北京南", "济南西", "08:00", "09:30", "01:30", 180),
        train("G103", "北京南", "济南西", "09:00", "10:30", "01:30", 190),
    ],
    ("济南", "徐州"): [
        train("G201", "济南西", "徐州东", "09:40", "11:10", "01:30", 150),   # 与 G101 只差 10 分钟：太赶
        train("G205", "济南西", "徐州东", "10:40", "12:10", "01:30", 160),   # 与 G101 差 70 分钟：可以
        train("G209", "济南西", "徐州东", "20:00", "21:30", "01:30", 150),   # 与 G101 差 10.5 小时：太久
    ],
    ("徐州", "上海"): [
        train("G301", "徐州东", "上海虹桥", "13:00", "15:30", "02:30", 280),
        train("G305", "徐州东", "上海虹桥", "23:50", "02:20", "02:30", 300),  # 跨日
    ],
    ("济南", "上海"): [
        train("G401", "济南西", "上海虹桥", "10:40", "15:10", "04:30", 430),
    ],
}


def fake_fetch(a, b, date):
    key = (a, b)
    if key not in FLIGHTS:
        return {"trains": []}
    return {"trains": FLIGHTS[key]}


def main():
    print("=" * 72)
    print("多段中转自检")
    print("=" * 72)
    d = "2026-11-25"

    # ---- segment ----
    legs = route.segment(fake_fetch, "北京", "济南", d)
    ok("区段解析出 2 班车", len(legs) == 2, f"got {len(legs)}")
    ok("按到达时间排序", legs[0].arr_time == "09:30")
    ok("取最便宜的可买席别", legs[0].price == 180.0 and legs[0].seat == "二等座")

    # ---- 换乘窗口 ----
    chain_ok = route.chains(fake_fetch, ["北京", "济南", "徐州"], d)
    ok("70 分钟换乘被接受", any(c[-1].train_no == "G205" for c in chain_ok),
       str([c[-1].train_no for c in chain_ok]))
    ok("10 分钟换乘被拒绝", all(c[-1].train_no != "G201" for c in chain_ok))
    ok("10 小时换乘被拒绝", all(c[-1].train_no != "G209" for c in chain_ok))

    # ---- 三段 ----
    chains3 = route.chains(fake_fetch, ["北京", "济南", "徐州", "上海"], d)
    ok("三段链能拼出来", len(chains3) >= 1, f"got {len(chains3)}")
    if chains3:
        c = chains3[0]
        ok("三段链共 3 段", len(c) == 3, f"got {len(c)}")
        ok("三段链顺序正确",
           [l.train_no for l in c][:1] in (["G101"], ["G103"]) and c[1].train_no == "G205")

    # ---- 折叠成 Scheme ----
    if chains3:
        s = route.to_scheme(chains3[0], d)
        ok("价格是各段之和", abs(s.price - sum(l.price for l in chains3[0])) < 1e-6, f"{s.price}")
        ok("换乘次数 = 2", s.transfers == 2, f"{s.transfers}")
        ok("车次号含三段", s.train_no.count("+") == 2, s.train_no)
        ok("label 里能看到三段", all(l.train_no in s.label() for l in chains3[0]))
        ok("总耗时按首末站算", s.duration_min == 7 * 60 + 30, f"{s.duration_min}")

        # ---- 约束判定 ----
        g1 = watch.Goal(origin="北京", dest="上海", date=d, max_transfers=1)
        ok("只允许 1 次换乘时被判超限",
           watch.VIOLATION_TRANSFER_COUNT in watch.check(s, g1))
        g2 = watch.Goal(origin="北京", dest="上海", date=d, max_transfers=2)
        ok("允许 2 次换乘时通过换乘校验",
           watch.VIOLATION_TRANSFER_COUNT not in watch.check(s, g2))
        g3 = watch.Goal(origin="北京", dest="上海", date=d, allow_transfer=False)
        ok("不允许中转时被判违规", watch.VIOLATION_TRANSFER in watch.check(s, g3))

    # ---- 跨日 ----
    night = route.chains(fake_fetch, ["北京", "济南", "徐州"], d)
    cross = [c for c in night if c[-1].train_no == "G305"]
    # G305 23:50 发车接在 12:10 到达之后，间隔 11 小时 > 上限，本来就不该出现
    ok("跨日车次按间隔规则被筛掉", not cross)

    # ---- 跨两天的链：总耗时不能被「同一天」假设算少 ----
    LONG = {
        ("太原", "合肥"): [train("V1398", "太原", "合肥", "21:48", "12:55", "15:07", 300)],
        ("合肥", "南昌"): [train("N221", "合肥", "南昌", "18:50", "01:21", "06:31", 180)],
        ("南昌", "厦门"): [train("D2361", "南昌西", "厦门北", "07:20", "11:57", "04:37", 160)],
    }

    def long_fetch(a, b, date):
        return {"trains": LONG.get((a, b), [])}

    long_chains = route.chains(long_fetch, ["太原", "合肥", "南昌", "厦门"], d)
    ok("跨两天三段链能拼出来", len(long_chains) == 1, f"got {len(long_chains)}")
    if long_chains:
        s2 = route.to_scheme(long_chains[0], d)
        real = (route._chain_times(long_chains[0], d)[1] - route._chain_times(long_chains[0], d)[0]).total_seconds() / 60
        ok("跨两天总耗时 > 24 小时", s2.duration_min > 24 * 60, f"{s2.duration_min} 分钟")
        ok("总耗时与绝对时间一致", abs(s2.duration_min - real) <= 1, f"{s2.duration_min} vs {real}")

    # ---- 枢纽组合 ----
    got = route.multi_hub_chains(fake_fetch, "北京", "上海", ["济南", "徐州"], d)
    ok("两段换乘入口能返回结果", len(got) >= 1, f"got {len(got)}")

    # ---- 失败不冒泡 ----
    def boom(a, b, date):
        raise RuntimeError("接口挂了")

    ok("某段查询失败时返回空而不是抛异常", route.multi_hub_chains(boom, "北京", "上海", ["济南", "徐州"], d) == [])

    print("-" * 72)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
