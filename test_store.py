"""Store 的存储/隔离/记忆测试：多用户、长跑裁剪、订票记录。

全部离线（不碰 12306、不碰模型），跑法：

    python test_store.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

import watch

TMP = Path(__file__).parent / ".tmp-store-test"
passed = failed = 0


def ok(name: str, cond, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [ok]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {extra}")


def fresh() -> watch.Store:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    return watch.Store(TMP / "t.db")


def main() -> int:
    print("1. 基础写入与容量")
    st = fresh()
    g = watch.Goal(origin="北京", dest="上海", date="2026-12-01", id="g1", owner="alice")
    st.save_goal(g)
    ok("任务能写能读", st.load_goal("g1").owner == "alice")
    for i in range(10):
        st.log("g1", watch.Decision("等待", 0, f"第 {i} 次"), {"best": {"train_no": "G1"}})
    ok("事件计数", st.event_count("g1") == 10, str(st.event_count("g1")))
    ok("统计活跃数", st.count_active("alice") == 1)

    print("2. 多用户隔离")
    g2 = watch.Goal(origin="广州", dest="北京", date="2026-12-02", id="g2", owner="bob")
    st.save_goal(g2)
    ok("alice 只看得到自己的", [x.id for x in st.list_goals(owner="alice")] == ["g1"])
    ok("bob 只看得到自己的", [x.id for x in st.list_goals(owner="bob")] == ["g2"])
    ok("不传 owner 时看得到全部", len(st.list_goals()) == 2)
    ok("按人统计活跃数互不影响",
       st.count_active("alice") == 1 and st.count_active("bob") == 1)

    print("3. 长跑裁剪（超过 N 条的旧事件被删，最近 N 条保留）")
    for i in range(500):
        st.log("g1", watch.Decision("等待", 0, f"填充 {i}"))
    ok("裁剪前有 510 条", st.event_count("g1") == 510, str(st.event_count("g1")))
    removed = st.prune_events(keep_per_goal=200, keep_days=30)
    ok("裁剪删掉了多余的行", removed >= 310, f"removed={removed}")
    ok("裁剪后每任务不超过 200 条", st.event_count("g1") <= 200, str(st.event_count("g1")))
    ok("裁剪后事件仍在（不是清空）", st.event_count("g1") >= 190)

    print("4. 裁剪按天数：老事件被清掉")
    old_ts = "2020-01-01T00:00:00"
    st._conn.execute(
        "INSERT INTO events(goal_id,ts,level,action,reason,snapshot) VALUES(?,?,?,?,?,?)",
        ("g1", old_ts, 0, "等待", "很久以前", "{}"),
    )
    st._conn.commit()
    before = st.event_count("g1")
    st.prune_events(keep_per_goal=10_000, keep_days=30)
    ok("超期事件被删", st.event_count("g1") < before, f"{before} -> {st.event_count('g1')}")

    print("5. 订票记录（长记忆）")
    n0 = len(st.bookings("alice"))
    st.add_booking("alice", {"train_no": "G547", "date": "2026-12-01",
                             "dep_station": "北京南", "arr_station": "上海虹桥",
                             "dep_time": "06:18", "arr_time": "12:11",
                             "seat": "二等座", "price": 553}, "g1")
    rows = st.bookings("alice")
    ok("订票记录可写可读", len(rows) == n0 + 1 and rows[0]["train_no"] == "G547")
    ok("别人的订票记录看不到", all(r["train_no"] != "G547" for r in st.bookings("bob")))
    ctx = st.recent_context("alice")
    ok("能给模型一句话上下文", "G547" in ctx and "北京南" in ctx, ctx)
    ok("没记录的人上下文为空", st.recent_context("nobody") == "")
    # 界面手边是 Scheme 对象，不该强制调用方先转 dict（这个坑真踩过）
    sch = watch.Scheme(train_no="D2361", dep_station="南昌西", arr_station="厦门北",
                       dep_time="07:20", arr_time="11:57", duration_min=277,
                       price=305, seat="二等座", date="2026-12-02", kind="中转")
    st.add_booking("alice", sch, "g2")
    ok("直接传 Scheme 也能记", st.bookings("alice")[0]["train_no"] == "D2361")

    print("6. 迁移与设置")
    ok("WAL 已开启", st._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    ok("busy_timeout 已设", st._conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0)
    idx = {r[1] for r in st._conn.execute("PRAGMA index_list(events)").fetchall()}
    ok("事件表有索引", "idx_events_goal" in idx, str(idx))

    # 老库（没有 owner 列）升级后不丢数据
    legacy = TMP / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE goals (id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,"
                 " relax_level INTEGER, attempts INTEGER, created_at TEXT, updated_at TEXT, note TEXT)")
    conn.execute("INSERT INTO goals VALUES('old','{\"origin\":\"a\",\"dest\":\"b\",\"date\":\"2026-01-01\"}',"
                 "'active',0,0,'2026-01-01','','')")
    conn.commit()
    conn.close()
    st2 = watch.Store(legacy)
    got = st2.load_goal("old")
    ok("老库能平滑升级（补 owner 列）", got is not None and got.owner == "default")

    print("7. 软删除语义：stopped 也算" )
    g3 = watch.Goal(origin="a", dest="b", date="2026-12-03", id="g3", owner="alice")
    st.save_goal(g3)
    ok("alice 活跃数变为 2", st.count_active("alice") == 2)
    g3.status = "stopped"
    st.save_goal(g3)
    ok("停止后活跃数回到 1", st.count_active("alice") == 1)

    shutil.rmtree(TMP, ignore_errors=True)
    print("=" * 60)
    print(f"结果：{passed} 通过，{failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
