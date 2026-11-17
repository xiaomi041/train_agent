"""多约束规划 + 通知队列的离线测试（不联网、不调模型）。

跑法：python test_planner.py
"""

import os
import tempfile

os.environ.setdefault("NOTIFY_QUEUE", os.path.join(tempfile.gettempdir(), "notify_test.jsonl"))

import notify  # noqa: E402
import planner  # noqa: E402
import watch  # noqa: E402

PASS, FAIL = 0, 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def S(train, price, minutes, dep="08:00", arr="12:00", seat="二等座",
      kind="直达", via="", date="2026-11-20"):
    return watch.Scheme(train_no=train, dep_station="A", arr_station="B",
                        dep_time=dep, arr_time=arr, duration_min=minutes,
                        price=price, seat=seat, date=date, kind=kind, via=via)


def base_cons(**kw):
    return planner.Constraints(**kw)


print("== 1. 六类约束各自生效 ==")
schemes = [
    S("G1", 500, 240),                                    # 基准：全部满足
    S("G2", 900, 200),                                    # 超预算
    S("G3", 400, 600),                                    # 超耗时
    S("G4", 500, 240, dep="23:30"),                       # 出发时段外
    S("G5", 500, 240, arr="02:10", dep="22:00"),          # 到达时段外
    S("D1", 300, 300, kind="中转", via="C"),               # 中转
    S("G6", 500, 240, seat="商务座"),                      # 席别不符
]
cons = base_cons(budget=600, max_minutes=300,
                 depart_after="06:00", depart_before="22:00",
                 arrive_after="06:00", arrive_before="23:00",
                 max_transfers=0, strict_seat=True,
                 seat_prefs=["二等座"])
res = planner.evaluate(schemes, cons, "A", "B", "2026-11-20")
feasible_trains = {s.train_no for s in res.feasible}
ok("只放行满足全部约束的那条", feasible_trains == {"G1"}, feasible_trains)
ok("超预算被统计", res.excluded.get(watch.VIOLATION_PRICE) == 1, res.excluded)   # G2 + G3(?) 见下
ok("超耗时被统计", res.excluded.get(watch.VIOLATION_DURATION) == 1, res.excluded)
ok("出发时段被统计", res.excluded.get(watch.VIOLATION_DEPART) == 1, res.excluded)
ok("到达时段被统计", res.excluded.get(watch.VIOLATION_ARRIVE) == 1, res.excluded)
ok("中转被拒（拒绝原因=不接受中转/换乘超限）",
   (res.excluded.get(watch.VIOLATION_TRANSFER) or 0) + (res.excluded.get(watch.VIOLATION_TRANSFER_COUNT) or 0) == 1,
   res.excluded)
ok("席别被拒", res.excluded.get(watch.VIOLATION_SEAT) == 1, res.excluded)

print("== 2. 放宽 max_transfers 后中转进入可行集 ==")
cons2 = base_cons(budget=600, max_minutes=300, max_transfers=1, allow_transfer=True)
res2 = planner.evaluate(schemes, cons2, "A", "B", "2026-11-20")
ok("中转方案可行", any(s.kind == "中转" for s in res2.feasible), [s.train_no for s in res2.feasible])

print("== 3. 推荐三选一与前沿 ==")
cheap = S("C1", 280, 600, dep="07:10", arr="17:10")
fast = S("F1", 780, 210, dep="09:00", arr="12:30")
mid = S("M1", 460, 280, dep="10:00", arr="14:40")
res3 = planner.evaluate([cheap, fast, mid], base_cons(), "A", "B", "2026-11-20")
names = {p.name: p.scheme.train_no for p in res3.picks if p.scheme}
ok("最便宜指向 C1", names.get("💰 最便宜") == "C1", names)
ok("最快指向 F1", names.get("⚡ 最快") == "F1", names)
ok("有取舍解释", any("省" in p.why for p in res3.picks), [p.why for p in res3.picks])
ok("前沿是三点（无支配关系）", len(res3.frontier) == 3, len(res3.frontier))

res3b = planner.evaluate([cheap, fast, mid,
                          S("X1", 300, 620, dep="07:00", arr="17:20")],
                         base_cons(), "A", "B", "2026-11-20")
ok("被支配方案不进前沿", all(s.train_no != "X1" for s in res3b.frontier),
   [s.train_no for s in res3b.frontier])

print("== 4. 无解时明确说无解，不硬凑 ==")
res4 = planner.evaluate([S("G9", 900, 700)], base_cons(budget=100, max_minutes=60),
                        "A", "B", "2026-11-20")
ok("可行集为空", res4.feasible == [])
ok("推荐改为结论", res4.picks[0].name == "结论" and res4.picks[0].scheme is None)
ok("表格文案明确", "没有满足" in res4.table())

print("== 5. plan() 可注入 fetcher（离线） ==")
called = {}


def fake_fetcher(goal):
    called["origin"] = goal.origin
    called["budget"] = goal.budget
    called["max_transfers"] = goal.max_transfers
    return [S("G1", 500, 240), S("G2", 900, 200)]

r5 = planner.plan("北京", "上海", "2026-11-20", base_cons(budget=600), fetcher=fake_fetcher)
ok("fetcher 收到约束", called.get("budget") == 600 and called.get("max_transfers") == 1, called)
ok("只剩一条可行", len(r5.feasible) == 1, len(r5.feasible))
ok("渲染表格包含车次", "G1" in r5.table(), r5.table()[:80])

print("== 6. 通知失败进队列、不抛异常 ==")
notify.QUEUE_FILE = __import__("pathlib").Path(os.environ["NOTIFY_QUEUE"])
if notify.QUEUE_FILE.exists():
    notify.QUEUE_FILE.unlink()
r6 = notify.send("测试：成都东→重庆北", url="http://127.0.0.1:9/remind")
ok("连接失败不抛异常且标记 queued", r6.get("ok") is False and r6.get("queued") is True, r6)
ok("队列里有 1 条", notify.pending() == 1, notify.pending())
r6b = notify.drain(url="http://127.0.0.1:9/remind")
ok("补发失败仍保留", r6b == {"sent": 0, "left": 1}, r6b)
ok("空内容被拒", notify.send("   ").get("ok") is False)

print("== 7. 命中/放弃会触发通知，且通知异常不影响决策 ==")
store = watch.Store(":memory:")
sent = []


def fake_notifier(text):
    sent.append(text)


g = watch.Goal(origin="A", dest="B", date="2026-11-20", budget=600)
w = watch.Watcher(g, fetcher=lambda goal: [S("G1", 500, 240)], store=store,
                  notifier=fake_notifier)
d = w.tick()
ok("判定命中", d.action == "命中", d.summary())
ok("通知被触发且含车次", sent and "G1" in sent[0], sent)


def boom(_):
    raise RuntimeError("通知服务挂了")


g2 = watch.Goal(origin="A", dest="B", date="2026-11-20", budget=600)
w2 = watch.Watcher(g2, fetcher=lambda goal: [S("G1", 500, 240)], store=store, notifier=boom)
d2 = w2.tick()
ok("通知抛异常但决策仍是命中", d2.action == "命中", d2.summary())

print("\n" + "=" * 52)
print(f"结果：{PASS} 通过，{FAIL} 失败")
print("=" * 52)
raise SystemExit(1 if FAIL else 0)

