"""反馈闭环：误报两次之后，策略要自己变（全部离线）。

测的是「闭环被消费」这件事本身：
- 一次误报不收，两次误报收（阈值）；
- 只收被标错的那个席别，同线路其他席别不动；
- 别的线路不受影响；
- 全部席别都被拦时不清空偏好（否则等于把任务搞死）；
- 提示文案能说清"依据是哪几次"。

run: python test_policy.py
"""
import shutil
import tempfile
from pathlib import Path

import policy
import watch
from watch import Decision, Goal, Scheme, Store

PASS, FAIL = 0, 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


DATE = "2026-12-01"


class Tmp:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="pol-test-")
        self.store = Store(Path(self.dir) / "w.db")

    def close(self):
        self.store._conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def scheme(seat="二等座", price=553.0, train_no="G1"):
    return Scheme(train_no=train_no, dep_station="北京南", arr_station="上海虹桥",
                  dep_time="08:00", arr_time="14:30", duration_min=390,
                  price=price, seat=seat, date=DATE)


def make_goal(origin="北京", dest="上海", seats=("二等座", "硬卧")):
    return Goal(origin=origin, dest=dest, date=DATE, budget=600.0,
                max_minutes=600, seat_prefs=list(seats), interval_sec=1)


def hit_then_wrong(t: Tmp, seat="二等座", times=1, origin="北京", dest="上海"):
    """造一条"命中 → 被标成误报"的历史。"""
    for i in range(times):
        g = make_goal(origin, dest)
        t.store.save_goal(g)
        s = scheme(seat=seat, train_no=f"G{i}")
        t.store.log(g.id, Decision("命中", 0, "约束全部满足", goal_id=g.id, best=s),
                    {"best": {"seat": seat, "train_no": s.train_no, "date": DATE}})
        t.store.feedback(g.id, "wrong")


def test_threshold():
    print("\n[1] 阈值：误报 1 次不收紧，2 次才收紧")
    t = Tmp()
    hit_then_wrong(t, seat="二等座", times=1)
    tight = policy.for_route(t.store, "北京", "上海")
    ok("1 次误报 → 不收紧", not tight.active, tight.blocked)
    ok("但总计里能看到 1 次", tight.total_wrong == 1, tight.total_wrong)

    hit_then_wrong(t, seat="二等座", times=1)
    tight = policy.for_route(t.store, "北京", "上海")
    ok("2 次误报 → 收紧二等座", tight.active and "二等座" in tight.blocked, tight.blocked)
    ok("次数被记下来", tight.blocked.get("二等座") == 2, tight.blocked)
    t.close()


def test_scope():
    print("\n[2] 只收被标错的席别，不牵连同线路其他席别与别的线路")
    t = Tmp()
    hit_then_wrong(t, seat="硬卧", times=3)
    hit_then_wrong(t, seat="二等座", times=1)
    tight = policy.for_route(t.store, "北京", "上海")
    ok("硬卧被收", "硬卧" in tight.blocked, tight.blocked)
    ok("二等座只有 1 次，不收", "二等座" not in tight.blocked, tight.blocked)
    other = policy.for_route(t.store, "北京", "广州")
    ok("别的线路不受影响", not other.active, other.blocked)
    t.close()


def test_apply():
    print("\n[3] 落到任务上：剔除席别 + 打开 strict_seat")
    t = Tmp()
    hit_then_wrong(t, seat="二等座", times=2)
    tight = policy.for_route(t.store, "北京", "上海")
    g = make_goal(seats=("二等座", "硬卧"))
    removed = policy.apply(g, tight)
    ok("二等座被剔除", removed == ["二等座"], removed)
    ok("偏好里只剩硬卧", g.seat_prefs == ["硬卧"], g.seat_prefs)
    ok("strict_seat 被打开", g.strict_seat is True, g.strict_seat)

    g2 = make_goal(seats=("一等座",))       # 偏好里没有它
    ok("偏好里不含被拦席别 → 什么也不做", policy.apply(g2, tight) == [], g2.seat_prefs)
    t.close()


def test_never_empty():
    print("\n[4] 全部席别都被拦时不清空偏好（否则等于把任务搞死）")
    t = Tmp()
    hit_then_wrong(t, seat="二等座", times=2)
    tight = policy.for_route(t.store, "北京", "上海")
    g = make_goal(seats=("二等座",))
    removed = policy.apply(g, tight)
    ok("返回被拦的席别", removed == ["二等座"], removed)
    ok("偏好没有被清空", g.seat_prefs == ["二等座"], g.seat_prefs)
    ok("但 strict_seat 仍然打开（让判定更严）", g.strict_seat is True)
    t.close()


def test_advice_and_empty():
    print("\n[5] 提示文案与空结论")
    t = Tmp()
    ok("没有误报时 advice 为空", policy.for_route(t.store, "北京", "上海").advice() == "")
    hit_then_wrong(t, seat="二等座", times=2)
    text = policy.for_route(t.store, "北京", "上海").advice()
    ok("提示里点了席别", "二等座" in text, text)
    ok("提示里写了次数", "2 次" in text, text)
    ok("提示里给了改回来的入口", "改约束" in text, text)
    t.close()


def test_days_window():
    print("\n[6] 只看窗口内的误报")
    t = Tmp()
    hit_then_wrong(t, seat="二等座", times=2)
    ok("窗口内能统计到", policy.for_route(t.store, "北京", "上海", days=90).active)

    old = "2026-01-01T00:00:00"
    with t.store._lock:                      # 把这两条反馈挪到 300 天前
        t.store._conn.execute(
            "UPDATE events SET ts=? WHERE action='反馈' AND reason='wrong'", (old,))
        t.store._conn.commit()
    ok("90 天窗口看不到 300 天前的误报",
       not policy.for_route(t.store, "北京", "上海", days=90).active)
    ok("把窗口放大到 365 天又能看到",
       policy.for_route(t.store, "北京", "上海", days=365).active)
    t.close()


def main():
    test_threshold()
    test_scope()
    test_apply()
    test_never_empty()
    test_advice_and_empty()
    test_days_window()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
