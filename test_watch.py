"""盯票 Agent 的三条判据测试（全部离线，不联网、不调模型）。

run: python test_watch.py
"""
import sys
import traceback
from pathlib import Path

import watch
from watch import (Goal, Scheme, Store, Watcher, FetchError, DataError,
                   check, decide, VIOLATION_PRICE, VIOLATION_DURATION,
                   VIOLATION_ARRIVE, VIOLATION_TRANSFER)

PASS, FAIL = 0, 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def make_goal(**kw):
    base = dict(origin="十堰", dest="哈尔滨", date="2026-11-20", budget=500,
                max_minutes=840, arrive_after="06:00", arrive_before="22:00",
                seat_prefs=["硬卧", "二等座"], max_attempts=10, interval_sec=60)
    base.update(kw)
    return Goal(**base)


def scheme(**kw):
    base = dict(train_no="K1264", dep_station="十堰", arr_station="哈尔滨",
                dep_time="10:00", arr_time="20:00", duration_min=600,
                price=420, seat="硬卧", date="2026-11-20")
    base.update(kw)
    return Scheme(**base)


# ---------------------------------------------------------------- 1. 目标可验证
def test_goal_verifiable():
    print("\n[1] 目标可验证：约束能由代码判定")
    g = make_goal()
    ok("完全符合 → 无违规", check(scheme(), g) == [])

    ok("超预算被判出", VIOLATION_PRICE in check(scheme(price=650), g))
    ok("耗时超限被判出", VIOLATION_DURATION in check(scheme(duration_min=900), g))
    ok("到达太晚被判出", VIOLATION_ARRIVE in check(scheme(arr_time="23:50"), g))

    g_no_transfer = make_goal(allow_transfer=False)
    ok("不接受中转被判出",
       VIOLATION_TRANSFER in check(scheme(kind="中转", via="郑州"), g_no_transfer))

    # 第 4 级放宽后，预算与到达窗放松
    g4 = make_goal()
    g4.relax_level = 4
    ok("第 4 级放宽后 620 元可接受", VIOLATION_PRICE not in check(scheme(price=590), g4))
    ok("第 4 级放宽后深夜到达可接受",
       VIOLATION_ARRIVE not in check(scheme(arr_time="23:50"), g4))

    # 排序：满足约束的方案排在不满足之前
    bad = scheme(train_no="G1", price=900)
    good = scheme(train_no="K1", price=300)
    ranked = watch.rank([bad, good], g)
    ok("排序把满足约束的排前面", ranked[0].train_no == "K1", f"got {ranked[0].train_no}")


# ---------------------------------------------------------------- 2. 命中即停
def test_hit_and_stop():
    print("\n[2] 第 0 级就有票 → 一次决策命中，不再瞎查")
    store = Store(Path(":memory:"))
    calls = {"n": 0}

    def fetcher(g):
        calls["n"] += 1
        return [scheme()]

    g = make_goal()
    store.save_goal(g)
    w = Watcher(g, fetcher, store, sleep=lambda s: None)
    d = w.tick()

    ok("决策 = 命中", d.action == "命中", d.action)
    ok("任务状态 = done", g.status == "done", g.status)
    ok("放宽层级没变", g.relax_level == 0, g.relax_level)
    ok("只查询了 1 次（命中即停）", calls["n"] == 1, calls["n"])
    ok("留下了可执行卡片（车次+席别+价格）", "K1264" in (g.note or "") and "¥420" in (g.note or ""), g.note)


# ---------------------------------------------------------------- 3. 无票即降级
def test_escalate_then_hit():
    print("\n[3] 第 0 级无票 → 自主降级 → 第 1 级命中")
    store = Store(Path(":memory:"))
    seen_levels = []

    def fetcher(g):
        seen_levels.append(g.relax_level)
        if g.relax_level == 0:
            return []
        return [scheme(train_no="K998", kind="中转", via="郑州", price=470)]

    g = make_goal()
    store.save_goal(g)
    w = Watcher(g, fetcher, store, sleep=lambda s: None)
    d = w.tick()

    ok("路径先到第 0 级再升到第 1 级", seen_levels[0] == 0 and max(seen_levels) >= 1, seen_levels)
    ok("最终命中", d.action == "命中", d.action)
    ok("命中后状态 done", g.status == "done", g.status)
    ok("记录里写着放行到什么层级", g.relax_level == 1, g.relax_level)
    ok("卡片含中转信息", "经 郑州 中转" in (g.note or ""), g.note)


# ---------------------------------------------------------------- 4. 暂时性失败 → 重试
def test_retry_on_transient():
    print("\n[4] 暂时性失败 → 不降级、退避重试，恢复后照常命中")
    store = Store(Path(":memory:"))
    state = {"n": 0}

    def fetcher(g):
        state["n"] += 1
        if state["n"] <= 2:
            raise FetchError("12306 返回 429")
        return [scheme()]

    g = make_goal()
    store.save_goal(g)
    w = Watcher(g, fetcher, store, sleep=lambda s: None)

    d1 = w.tick()
    ok("第 1 次失败 → 等待（不降级）", d1.action == "等待" and g.relax_level == 0, d1.action)
    w.tick()
    d3 = w.tick()
    ok("第 3 次恢复 → 命中", d3.action == "命中", d3.action)
    ok("重试期间层级未变", g.relax_level == 0, g.relax_level)


def test_retry_then_escalate():
    print("\n[5] 连续 3 次抓取失败 → 判为查法不行，降级换查法")
    store = Store(Path(":memory:"))

    def fetcher(g):
        if g.relax_level == 0:
            raise FetchError("连接超时")
        return [scheme()]

    g = make_goal()
    store.save_goal(g)
    w = Watcher(g, fetcher, store, sleep=lambda s: None)
    w.tick()
    w.tick()
    d = w.tick()
    ok("第 3 次失败后降级并命中", d.action == "命中" and g.relax_level == 1,
       f"{d.action}/{g.relax_level}")


# ---------------------------------------------------------------- 5. 数据缺失
def test_data_error_escalates():
    print("\n[6] 数据缺失 → 换查法（不消耗 3 次重试）")
    store = Store(Path(":memory:"))

    def fetcher(g):
        if g.relax_level == 0:
            raise DataError("找不到出发站「十堰东」")
        return [scheme()]

    g = make_goal()
    store.save_goal(g)
    w = Watcher(g, fetcher, store, sleep=lambda s: None)
    d = w.tick()
    ok("一次 tick 内完成降级并命中", d.action == "命中" and g.relax_level == 1,
       f"{d.action}/{g.relax_level}")


# ---------------------------------------------------------------- 6. 内部错误不编造
def test_internal_error_no_fabrication():
    print("\n[7] 内部错误 → 不采信任何方案，绝不编造车次")
    store = Store(Path(":memory:"))

    def fetcher(g):
        raise ValueError("解析崩了")

    g = make_goal()
    store.save_goal(g)
    w = Watcher(g, fetcher, store, sleep=lambda s: None)
    d = w.tick()
    ok("决策 = 等待", d.action == "等待", d.action)
    ok("没有产出任何方案", d.best is None and g.status == "active")
    ok("原因里写明未采信", "内部错误" in d.reason, d.reason)


# ---------------------------------------------------------------- 7. 全局放弃
def test_give_up_with_closest():
    print("\n[8] 有票但怎么放宽都不满足 → 放弃，并给出最接近方案")
    store = Store(Path(":memory:"))

    def fetcher(g):
        # 关键：始终"有票"，只是这票永远违反约束（超预算 + 超耗时）。
        # 这才是该放弃的情形——需求本身不现实。而"一条都没有"是没票，见 [10]。
        return [scheme(price=1500, duration_min=1400, arr_time="23:50")]

    g = make_goal()
    store.save_goal(g)
    w = Watcher(g, fetcher, store, sleep=lambda s: None)
    d = None
    for _ in range(8):
        d = w.tick()
        if g.status != "active":
            break

    ok("最终放弃", d.action == "放弃", f"{d.action} level={g.relax_level}")
    ok("状态 failed", g.status == "failed", g.status)
    ok("给出了最接近方案", d.closest is not None, "closest=None")
    ok("说明差在哪", bool(d.violations), d.violations)


# ---------------------------------------------------------------- 8. 自主性
def test_path_depends_on_observation():
    print("\n[9] 路径随观察改变：同样输入、不同余票 → 不同决策路径")
    def run(has_ticket):
        store = Store(Path(":memory:"))
        levels = []

        def fetcher(g):
            levels.append(g.relax_level)
            return [scheme()] if has_ticket else []

        g = make_goal()
        store.save_goal(g)
        w = Watcher(g, fetcher, store, sleep=lambda s: None)
        w.tick()
        return levels, g.status, g.relax_level

    l_hit, s_hit, lv_hit = run(True)
    l_miss, s_miss, lv_miss = run(False)

    ok("有票：停在第 0 级且 done", lv_hit == 0 and s_hit == "done", f"{lv_hit}/{s_hit}")
    ok("无票：升到第 1 级且未结束", lv_miss == 1 and s_miss == "active", f"{lv_miss}/{s_miss}")
    ok("两条路径不同（说明在决策而非跑脚本）", l_hit != l_miss, f"{l_hit} vs {l_miss}")



def test_no_ticket_keeps_waiting():
    print("\n[10] 持续无票 → 不放弃，扩搜到第 2 级后转为等待")
    store = Store(Path(":memory:"))

    def fetcher(g):
        return []   # 没票：还没放票 / 已售罄

    g = make_goal()
    store.save_goal(g)
    w = Watcher(g, fetcher, store, sleep=lambda s: None)
    actions = []
    for _ in range(6):
        d = w.tick()
        actions.append(d.action)
        if g.status != "active":
            break

    ok("没有判定放弃", "放弃" not in actions, actions)
    ok("任务仍在 active", g.status == "active", g.status)
    ok("扩搜级别停在第 2 级（不再无意义地往上加）", g.relax_level == 2, g.relax_level)
    ok("后续动作是等待", actions[-1] == "等待", actions)


def test_fetcher_no_station_expansion():
    """real_fetcher 不能把城市展开成同城车站逐个查。

    12306 的余票接口本身把站名归一到城市：查「北京南→上海虹桥」返回的仍是北京三个站
    的全部车次。扩成 9×5 组合会拿到 45 份完全相同的数据——请求数翻 45 倍、耗时翻倍、
    候选里全是重复项（修复前一次 tick 135 次请求 / 54 秒）。
    """
    import ticket_api

    sent = []
    orig = ticket_api.query_ticket_dict
    ticket_api.query_ticket_dict = lambda a, b, d: (
        sent.append((a, b, d)) or {"trains": [
            {"train_no": "G1", "departure_station": "北京南", "arrival_station": "上海虹桥",
             "departure_time": "08:00", "arrival_time": "12:00", "duration": "04:00",
             "seats": {"二等座": {"remain": "有", "price": 500}}},
        ]}
    )
    try:
        g = Goal(origin="北京", dest="上海", date="2026-12-01", allow_transfer=False)
        schemes = watch.real_fetcher(g, hubs=[], sleep_between=0)
    finally:
        ticket_api.query_ticket_dict = orig

    ok("单日只发 1 次请求（不展开同城车站）", len(sent) == 1, sent)
    ok("请求用的是城市名而非站名", sent[0][0] == "北京" and sent[0][1] == "上海", sent[0])
    ok("返回方案不含重复项", len(schemes) == 1, len(schemes))


def test_fetcher_dedupes_same_train(monkeypatch_target=None):
    """同一车次/席别在多份响应里重复出现时只保留一条。"""
    import ticket_api

    train = {"train_no": "G2", "departure_station": "北京南", "arrival_station": "上海虹桥",
             "departure_time": "09:00", "arrival_time": "13:00", "duration": "04:00",
             "seats": {"二等座": {"remain": "有", "price": 550}}}
    orig = ticket_api.query_ticket_dict
    ticket_api.query_ticket_dict = lambda a, b, d: {"trains": [train, dict(train)]}
    try:
        g = Goal(origin="北京", dest="上海", date="2026-12-01", allow_transfer=False)
        schemes = watch.real_fetcher(g, hubs=[], sleep_between=0)
    finally:
        ticket_api.query_ticket_dict = orig

    ok("重复车次只留一条", len(schemes) == 1, len(schemes))


def main():
    tests = [
        test_goal_verifiable,
        test_hit_and_stop,
        test_escalate_then_hit,
        test_retry_on_transient,
        test_retry_then_escalate,
        test_data_error_escalates,
        test_internal_error_no_fabrication,
        test_give_up_with_closest,
        test_path_depends_on_observation,
        test_no_ticket_keeps_waiting,
        test_fetcher_no_station_expansion,
        test_fetcher_dedupes_same_train,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            global FAIL
            FAIL += 1
            print(f"  ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{'='*52}\n结果：{PASS} 通过，{FAIL} 失败\n{'='*52}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
