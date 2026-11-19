"""「放宽哪一项」由谁决定：规则版与模型版的行为契约（全部离线，不联网）。

这里测的不是模型选得好不好（那是 bench_decide.py 的事），而是：
- 决策器返回什么，任务就必须照做（含非法返回值、抛异常、说"等等"）
- 决策器挂了，盯票不能跟着挂

run: python test_decide.py
"""
import shutil
import tempfile
from pathlib import Path

import decider
import watch
from watch import Goal, Scheme, Store, Watcher, VIOLATION_PRICE

PASS, FAIL = 0, 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


DATE = "2026-11-20"


def make_goal(**kw):
    base = dict(origin="北京", dest="上海", date=DATE, budget=600.0,
                max_minutes=600, arrive_after="06:00", arrive_before="23:59",
                seat_prefs=["二等座"], strict_seat=True, allow_transfer=False,
                max_transfers=0, flex_days=2, interval_sec=1)
    base.update(kw)
    return Goal(**base)


def scheme(**kw):
    base = dict(train_no="G1", dep_station="北京南", arr_station="上海虹桥",
                dep_time="08:00", arr_time="14:30", duration_min=390,
                price=553.0, seat="二等座", date=DATE)
    base.update(kw)
    return Scheme(**base)


class Tmp:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="dec-test-")
        self.store = Store(Path(self.dir) / "w.db")

    def close(self):
        self.store._conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------- 1. 规则版
def test_rule():
    print("\n[1] 规则版：按固定顺序，永不返回 None")
    g = make_goal()
    ok("第一个维度是 transfer", decider.rule_decider(g, [], list(watch.RELAX_ORDER)) == "transfer")
    ok("已放开 transfer 后轮到 date",
       decider.rule_decider(g, ["transfer"], ["date", "seat"]) == "date")
    ok("没得放了返回 None", decider.rule_decider(g, list(watch.RELAX_ORDER), []) is None)


# ---------------------------------------------------------------- 2. pick_dim
def test_pick_dim():
    print("\n[2] pick_dim：模型说什么就听什么，但只接受合法值")
    g = make_goal()

    def fixed(choice):
        return lambda *a, **kw: choice

    ok("模型选 money → money",
       watch.pick_dim(g, fixed("money")) == "money")
    ok("模型选一个不在候选里的 → 退回顺序第一位",
       watch.pick_dim(g, fixed("bogus")) == "transfer")
    ok("模型选 None（先不放宽）→ None",
       watch.pick_dim(g, fixed(None)) is None)

    def boom(*a, **kw):
        raise RuntimeError("模型挂了")

    ok("决策器抛异常 → 退回规则，不抛出去", watch.pick_dim(g, boom) == "transfer")

    # 已经放开 transfer，剩余列表里没有它
    g2 = make_goal(relax_dims=["transfer"])
    ok("重复选择已放开的维度 → 退回顺序（date）",
       watch.pick_dim(g2, fixed("transfer")) == "date")


# ---------------------------------------------------------------- 3. check 认维度
def test_check_uses_dims():
    print("\n[3] 约束判定认「维度」而不是「等级」")
    g = make_goal()                       # 预算 600
    s = scheme(price=620.0)               # 超 20 元
    ok("不放宽时预算不够", VIOLATION_PRICE in watch.check(s, g))
    g2 = make_goal(relax_dims=["money"])  # 放宽后 600*1.2=720
    ok("放宽 money 后够了", watch.check(s, g2) == [])

    g3 = make_goal(relax_dims=["arrive"])
    s3 = scheme(dep_time="19:00", arr_time="00:30", duration_min=330)
    ok("放宽 arrive 后不再校验到达时段", watch.check(s3, g3) == [])

    ok("老语义仍然成立：relax_level=4 等价于放开 money",
       "money" in watch.relax_dims(make_goal(relax_level=4)))
    ok("relaxed(2) 会写进 relax_dims",
       set(make_goal().relaxed(2).relax_dims) == {"transfer", "date"})


# ---------------------------------------------------------------- 4. decide 听决策器
def test_decide():
    print("\n[4] decide：把决策器的选择写进决策记录")
    g = make_goal()
    s = scheme(price=620.0)
    d = watch.decide([s], g, decider=lambda *a, **kw: "money")
    ok("动作是降级", d.action == "降级", d.action)
    ok("放宽项是 money", d.relax_dims == ["money"], d.relax_dims)
    ok("理由里写了放宽哪一项", "预算上浮" in d.reason, d.reason)

    d2 = watch.decide([s], g, decider=lambda *a, **kw: None)
    ok("模型选等等 → 动作是等待", d2.action == "等待", d2.action)
    ok("等待时保留最接近的候选", d2.closest is not None)


# ---------------------------------------------------------------- 5. Watcher 落地
def test_watcher_applies():
    print("\n[5] Watcher：把维度写回任务，下一轮就按新约束判定")
    t = Tmp()
    g = make_goal()
    w = Watcher(g, fetcher=lambda gg: [scheme(price=620.0)], store=t.store,
                sleep=lambda s: None, decider=lambda *a, **kw: "money")
    d = w.tick()
    ok("同一轮内放宽 money 后立刻重试并命中", d.action == "命中", f"{d.action} {d.relax_dims}")
    ok("任务上记录了 relax_dims", g.relax_dims == ["money"], g.relax_dims)
    ok("relax_level 跟着维度数走", g.relax_level == 1, g.relax_level)
    d2 = w.tick()
    ok("第二轮直接命中", d2.action == "命中", d2.action)
    t.close()


# ---------------------------------------------------------------- 6. 等等预算
def test_wait_budget():
    print("\n[6] 模型一直说「等等」时，用完预算退回规则，任务不会卡死")
    t = Tmp()
    g = make_goal()
    w = Watcher(g, fetcher=lambda gg: [scheme(price=620.0)], store=t.store,
                sleep=lambda s: None, decider=lambda *a, **kw: None)
    actions = [w.tick().action for _ in range(watch.MAX_MODEL_WAITS + 2)]
    ok(f"前 {watch.MAX_MODEL_WAITS} 轮都在等",
       actions[:watch.MAX_MODEL_WAITS] == ["等待"] * watch.MAX_MODEL_WAITS, actions)
    ok("用完之后不再干等，开始放宽（维度数增加）", len(g.relax_dims) > 0, g.relax_dims)
    t.close()


# ---------------------------------------------------------------- 7. 全部放完
def test_exhausted():
    print("\n[7] 六个维度全放完仍不满足 → 放弃，不再无限降级")
    t = Tmp()
    g = make_goal(budget=100.0)          # 放到最宽也不够
    w = Watcher(g, fetcher=lambda gg: [scheme(price=9999.0)], store=t.store,
                sleep=lambda s: None)
    d = None
    for _ in range(len(watch.RELAX_ORDER) + 2):
        d = w.tick()
        if d.action in ("放弃", "命中"):
            break
    ok("最终放弃", d.action == "放弃", d.summary())
    ok("放弃时已经把能放开的都放开了", len(g.relax_dims) == len(watch.RELAX_ORDER), g.relax_dims)
    t.close()


# ---------------------------------------------------------------- 8. 缓存与配额
def test_cache_and_quota():
    print("\n[8] 模型版默认开启：缓存省调用，配额防烧钱，异常一律退回规则")
    calls = {"n": 0}

    def fake_ask(text):
        calls["n"] += 1
        return '{"relax": "money"}'

    real_ask = decider._ask_model
    real_quota = decider.QUOTA_PER_HOUR
    decider._ask_model = fake_ask
    try:
        decider.clear_cache()
        decider.reset_stats()
        g = make_goal()

        r1 = decider.llm_decider(g, [], list(watch.RELAX_ORDER), None, [])
        r2 = decider.llm_decider(g, [], list(watch.RELAX_ORDER), None, [])
        ok("模型选了 money", r1 == "money", r1)
        ok("同一局面第二次直接命中缓存（只调 1 次模型）", calls["n"] == 1, calls["n"])
        ok("缓存命中计数为 1", decider.stats()["cache_hits"] == 1, decider.stats())

        g2 = make_goal(budget=800.0)   # 约束变了 = 新局面
        decider.llm_decider(g2, [], list(watch.RELAX_ORDER), None, [])
        ok("换约束后要重新问模型（累计 2 次）", calls["n"] == 2, calls["n"])

        # 配额：同一任务每小时只允许 N 次
        decider.clear_cache()
        decider.QUOTA_PER_HOUR = 2
        calls["n"] = 0
        gid_goal = make_goal()
        for i in range(3):
            gg = make_goal(id=gid_goal.id, budget=600.0 + i)   # 三个不同局面，同一任务
            decider.llm_decider(gg, [], list(watch.RELAX_ORDER), None, [])
        ok("配额用完：第 3 次不再调模型（共 2 次）", calls["n"] == 2, calls["n"])
        ok("配额拦截计入配额计数", decider.stats()["quota_blocks"] == 1, decider.stats())

        # 坏 JSON：不抛异常，按「等」处理，且计入 bad_json
        decider.clear_cache()
        decider.QUOTA_PER_HOUR = real_quota
        decider.reset_stats()
        decider._ask_model = lambda text: "我觉得应该放宽预算"
        r = decider.llm_decider(make_goal(), [], list(watch.RELAX_ORDER), None, [])
        ok("模型答非 JSON → 返回 None（让上层用规则）", r is None, r)
        ok("坏 JSON 计入统计", decider.stats()["bad_json"] == 1, decider.stats())

        # 模型异常：返回 None，且不写缓存（下次还会问）
        decider.clear_cache()
        boom_calls = {"n": 0}

        def boom(text):
            boom_calls["n"] += 1
            raise RuntimeError("网络挂了")

        decider._ask_model = boom
        r = decider.llm_decider(make_goal(), [], list(watch.RELAX_ORDER), None, [])
        ok("模型异常 → 返回 None 而不是抛出去", r is None, r)
        decider.llm_decider(make_goal(id="same-goal"), [], list(watch.RELAX_ORDER), None, [])
        ok("异常结果不写缓存（两次都真的调了）", boom_calls["n"] == 2, boom_calls["n"])
    finally:
        decider._ask_model = real_ask
        decider.QUOTA_PER_HOUR = real_quota
        decider.clear_cache()
        decider.reset_stats()

    ok("make_decider() 默认是模型版", decider.make_decider() is decider.llm_decider)
    ok("配置写错（bogus）也不会让盯票停摆，退回规则",
       decider.make_decider("bogus") is decider.rule_decider)
    ok("显式 rule 仍是规则版", decider.make_decider("rule") is decider.rule_decider)


def main():
    test_rule()
    test_pick_dim()
    test_check_uses_dims()
    test_decide()
    test_watcher_applies()
    test_wait_budget()
    test_exhausted()
    test_cache_and_quota()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
