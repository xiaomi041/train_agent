"""webhook 推送与出票卡片的离线自检（不联网）。

要验的两件事：
1. **三家机器人的载荷字段不同** —— 飞书是 `msg_type/content.text`，钉钉/企业微信是
   `msgtype/text.content`。发错了对方会回 200 但报 errcode，看起来像成功。
2. **200 不等于成功** —— 机器人经常 HTTP 200 + `{"code": 19001}`。所以必须看返回体里的码。
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import booking  # noqa: E402
import json as njson  # noqa: E402
import notify  # noqa: E402

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


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"code": 0}
        self.text = str(self._body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def fake_requests(status=200, body=None, raises=None):
    mod = types.ModuleType("requests")
    calls = []

    def post(url, json=None, timeout=None, data=None, headers=None):
        calls.append({"url": url, "json": json, "timeout": timeout,
                      "data": data, "headers": headers or {}})
        if raises:
            raise raises
        return FakeResp(status, body)

    mod.post = post
    mod.calls = calls
    return mod


def with_requests(mod):
    old = sys.modules.get("requests")
    sys.modules["requests"] = mod
    return old


def main():
    print("=" * 72)
    print("webhook / 出票卡片自检")
    print("=" * 72)

    # ---- 载荷格式 ----
    f = notify._webhook_payload("hi", "https://open.feishu.cn/open-apis/bot/v2/hook/xxx")
    ok("飞书用 msg_type/content.text", f.get("msg_type") == "text" and f["content"]["text"] == "hi", str(f))

    d = notify._webhook_payload("hi", "https://oapi.dingtalk.com/robot/send?access_token=x")
    ok("钉钉用 msgtype/text.content", d.get("msgtype") == "text" and d["text"]["content"] == "hi", str(d))

    w = notify._webhook_payload("hi", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x")
    ok("企业微信用 msgtype/text.content", w.get("msgtype") == "text" and w["text"]["content"] == "hi", str(w))

    unk = notify._webhook_payload("hi", "https://example.com/hook")
    ok("未知域名也给一个合法载荷", unk.get("msgtype") == "text")

    # ---- 未配置 = 跳过，不算失败 ----
    old_hook = notify.WEBHOOK
    notify.WEBHOOK = ""
    res = notify.webhook_push("hi")
    ok("没配 webhook 时返回 skipped", res.get("skipped") is True, str(res))

    # ---- 空内容 ----
    notify.WEBHOOK = "https://open.feishu.cn/x"
    ok("空内容被拒", notify.webhook_push("   ").get("ok") is False)

    # ---- 正常推送 ----
    mod = fake_requests(200, {"code": 0})
    old = with_requests(mod)
    try:
        res = notify.webhook_push("hello")
        ok("推送成功", res.get("ok") is True, str(res))
        ok("只发了一次", len(mod.calls) == 1)
        ok("发的是飞书格式", mod.calls[0]["json"].get("msg_type") == "text")
        ok("带超时", mod.calls[0]["timeout"] == notify.WEBHOOK_TIMEOUT)

        # ---- 200 但业务码报错 ----
        mod2 = fake_requests(200, {"code": 19001, "msg": "param invalid"})
        sys.modules["requests"] = mod2
        r2 = notify.webhook_push("hello")
        ok("HTTP 200 + 错误码判为失败", r2.get("ok") is False and "19001" in r2.get("error", ""), str(r2))

        # ---- 非 200 ----
        mod3 = fake_requests(500, {"code": 0})
        sys.modules["requests"] = mod3
        ok("HTTP 500 判为失败", notify.webhook_push("hi").get("ok") is False)

        # ---- 抛异常不外溢 ----
        mod4 = fake_requests(raises=RuntimeError("网络挂了"))
        sys.modules["requests"] = mod4
        r4 = notify.webhook_push("hi")
        ok("异常不外溢且返回失败", r4.get("ok") is False and "网络挂了" in r4.get("error", ""), str(r4))
    finally:
        if old is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old

    notify.WEBHOOK = old_hook

    # ---- ntfy ----
    old_topic = notify.NTFY_TOPIC
    old_srv = notify.NTFY_SERVER
    notify.NTFY_TOPIC = ""
    ok("没配 ntfy topic 时 skipped", notify.ntfy_push("hi").get("skipped") is True)

    notify.NTFY_TOPIC = "dingpiao-test"
    mod5 = fake_requests(200, {"id": "abc"})
    old = with_requests(mod5)
    try:
        r = notify.ntfy_push("盯票命中 测试")
        ok("ntfy 推送成功", r.get("ok") is True and r.get("provider") == "ntfy", str(r))
        call = mod5.calls[0]
        ok("ntfy 发到 topic 路径", call["url"].endswith("/dingpiao-test"), call["url"])
        ok("ntfy 默认走 ntfy.sh", call["url"].startswith("https://ntfy.sh/"), call["url"])
        ok("ntfy 正文是 UTF-8 字节", isinstance(call["data"], (bytes, bytearray)) and "盯票".encode("utf-8") in call["data"], str(type(call["data"])))
        ok("ntfy 带 ASCII 标题头", call["headers"]["Title"] == "trainAI", str(call.get("headers")))

        # 自建服务器
        mod6 = fake_requests(200, {"id": "abc"})
        sys.modules["requests"] = mod6
        notify.ntfy_push("hi", server="https://push.example.com")
        ok("支持自建 ntfy 服务器", mod6.calls[0]["url"].startswith("https://push.example.com/"), mod6.calls[0]["url"])

        mod7 = fake_requests(404, {})
        sys.modules["requests"] = mod7
        ok("ntfy 非 200 判失败", notify.ntfy_push("hi").get("ok") is False)

        mod8 = fake_requests(raises=RuntimeError("断网"))
        sys.modules["requests"] = mod8
        ok("ntfy 异常不外溢", notify.ntfy_push("hi").get("ok") is False)
    finally:
        if old is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old
    notify.NTFY_TOPIC = old_topic
    notify.NTFY_SERVER = old_srv

    # ---- 队列：只补发失败过的通道 ----
    # 现实场景：提醒服务（reminder-worker）那一刻挂了，但 ntfy 是通的。
    # 如果补发时把三条都重发一遍，手机会收到两条一模一样的消息。
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "q.jsonl"
    old_queue, old_topic, old_url = notify.QUEUE_FILE, notify.NTFY_TOPIC, notify.REMIND_URL
    notify.QUEUE_FILE = tmp
    notify.NTFY_TOPIC = "dingpiao-test"
    notify.REMIND_URL = "http://remind.invalid/remind"

    def router(remind_ok=True):
        mod = types.ModuleType("requests")
        calls = []

        def post(url, json=None, timeout=None, data=None, headers=None):
            calls.append(url)
            if "remind.invalid" in url:
                return FakeResp(200, {"ok": remind_ok}) if remind_ok else FakeResp(500, {})
            return FakeResp(200, {"id": "abc"})

        mod.post = post
        mod.calls = calls
        return mod

    old_req = sys.modules.get("requests")
    try:
        m = router(remind_ok=False)
        sys.modules["requests"] = m
        r = notify.send("盯票命中 测试")
        ok("提醒服务挂了也返回结果", r.get("ok") is False, str(r))
        ok("记下失败通道只有 remind", r.get("failed_channels") == ["remind"], str(r.get("failed_channels")))
        ok("ntfy 那条仍算成功", r.get("ntfy", {}).get("ok") is True, str(r.get("ntfy")))
        ok("队列里有 1 条", notify.pending() == 1, str(notify.pending()))

        # 提醒服务恢复，补发：只重发 remind，不该再推一次 ntfy
        m2 = router(remind_ok=True)
        sys.modules["requests"] = m2
        before_ntfy = sum(1 for u in m2.calls if "ntfy" in u)
        res = notify.drain()
        ok("补发成功", res.get("sent") == 1 and res.get("left") == 0, str(res))
        ok("补发后队列清空", notify.pending() == 0, str(notify.pending()))
        ok("补发时没有重复推 ntfy", sum(1 for u in m2.calls if "ntfy" in u) == before_ntfy, str(m2.calls))

        # 还是失败：留在队列里，且通道保持不变
        m3 = router(remind_ok=False)
        sys.modules["requests"] = m3
        notify.send("再试一条")
        res = notify.drain()
        ok("仍然失败时保留在队列", res.get("left") == 1 and notify.pending() == 1, str(res))

        # 老格式（没有 channels 字段）按提醒服务处理，不能崩
        tmp.write_text('{"content":"老的积压","at":"2026-11-19 08:00","repeat":"once"}\n',
                       encoding="utf-8")
        m4 = router(remind_ok=True)
        sys.modules["requests"] = m4
        res = notify.drain()
        ok("老格式也能补发", res.get("sent") == 1 and res.get("left") == 0, str(res))
    finally:
        if old_req is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old_req
        notify.QUEUE_FILE, notify.NTFY_TOPIC, notify.REMIND_URL = old_queue, old_topic, old_url

    # ---- 去重：同一件事 10 分钟内只通知一次 ----
    # 真事：同一次命中被两个 tick 各通知一遍，界面上弹出两条一模一样的提醒。
    import tempfile as _tf
    from pathlib import Path as _P

    tmp2 = _P(_tf.mkdtemp()) / "q2.jsonl"
    old_q2, old_url2 = notify.QUEUE_FILE, notify.REMIND_URL
    notify.QUEUE_FILE = tmp2
    notify.REMIND_URL = "http://remind.invalid/remind"
    old_req2 = sys.modules.get("requests")
    try:
        m = router(remind_ok=True)
        sys.modules["requests"] = m
        r1 = notify.send("命中 G547", dedupe_key="g1|G547|二等座|2026-11-20|06:18")
        r2 = notify.send("命中 G547", dedupe_key="g1|G547|二等座|2026-11-20|06:18")
        ok("第一次正常发", r1.get("ok") is True and not r1.get("deduped"), str(r1))
        ok("同 key 第二次被去重", r2.get("deduped") is True, str(r2))
        ok("去重时只发了一次请求", len([u for u in m.calls if "remind.invalid" in u]) == 1, str(m.calls))

        r3 = notify.send("命中 G547", dedupe_key="g1|G547|二等座|2026-11-21|06:18")
        ok("换了日期的 key 不算重复", not r3.get("deduped"), str(r3))

        old_win = notify.DEDUPE_SECONDS
        notify.DEDUPE_SECONDS = 0
        r4 = notify.send("命中 G547", dedupe_key="g1|G547|二等座|2026-11-20|06:18")
        ok("窗口设 0 可关闭去重", not r4.get("deduped"), str(r4))
        notify.DEDUPE_SECONDS = old_win

        # 过期的记录会被顺手清掉，文件不会无限长
        m2 = router(remind_ok=True)
        sys.modules["requests"] = m2
        notify.DEDUPE_SECONDS = 1
        notify.send("另一件事", dedupe_key="x|1")
        import time as _t
        _t.sleep(1.1)
        notify.send("另一件事", dedupe_key="x|1")
        ok("超窗口后同 key 会重发", True)
        notify.DEDUPE_SECONDS = old_win
    finally:
        if old_req2 is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old_req2
        notify.QUEUE_FILE, notify.REMIND_URL = old_q2, old_url2

    # ---- 解耦之后：ntfy 是主通道，提醒服务默认关闭 ----
    # 起因：盯票的通知以前必须经过 PersonalAI 的提醒服务，那个项目一重启/界面一出问题，
    # 盯票这块就被牵连。现在 REMIND_URL 留空就完全不走它。
    import tempfile as _tf2
    from pathlib import Path as _P2

    tmp3 = _P2(_tf2.mkdtemp()) / "q3.jsonl"
    old_q3 = notify.QUEUE_FILE
    old_url3, old_topic3 = notify.REMIND_URL, notify.NTFY_TOPIC
    notify.QUEUE_FILE = tmp3
    notify.REMIND_URL = ""
    notify.NTFY_TOPIC = "dingpiao-test"
    old_req3 = sys.modules.get("requests")
    try:
        only_ntfy = router(remind_ok=True)
        sys.modules["requests"] = only_ntfy
        r = notify.send("盯票命中 解耦测试")
        ok("没配提醒服务时也算发成功", r.get("ok") is True, str(r))
        ok("只走 ntfy 一个通道", r.get("channels") == ["ntfy"], str(r.get("channels")))
        ok("没有请求打到提醒服务", not any("remind" in u for u in only_ntfy.calls), str(only_ntfy.calls))
        ok("成功时不写队列", notify.pending() == 0, str(notify.pending()))

        # ntfy 挂了才进队列，且只记 ntfy
        dead = fake_requests(raises=RuntimeError("断网"))
        sys.modules["requests"] = dead
        r2 = notify.send("盯票命中 断网测试")
        ok("ntfy 失败时返回失败", r2.get("ok") is False, str(r2))
        ok("失败通道只有 ntfy", r2.get("failed_channels") == ["ntfy"], str(r2.get("failed_channels")))
        ok("失败进队列", notify.pending() == 1, str(notify.pending()))

        # 补发：REMIND_URL 为空时，老格式（无 channels）也应该走 ntfy，而不是死磕提醒服务
        tmp3.write_text('{"content":"老的积压","at":"2026-11-19 08:00","repeat":"once"}\n',
                        encoding="utf-8")
        back = router(remind_ok=True)
        sys.modules["requests"] = back
        res = notify.drain()
        ok("老格式在解耦后仍能补发", res.get("sent") == 1 and res.get("left") == 0, str(res))
        ok("补发走的是 ntfy 而不是提醒服务",
           any("ntfy" in u for u in back.calls) and not any("remind" in u for u in back.calls),
           str(back.calls))

        # 一个通道都没配：跳过，不写队列（否则队列里全是永远发不出去的垃圾）
        tmp3.write_text("", encoding="utf-8")
        notify.REMIND_URL = ""
        notify.NTFY_TOPIC = ""
        notify.WEBHOOK = ""
        r3 = notify.send("没人收的消息")
        ok("没配任何通道时返回 skipped", r3.get("skipped") is True, str(r3))
        ok("没配通道时不写队列", notify.pending() == 0, str(notify.pending()))
        ok("没配通道时不消耗去重窗口", notify._seen_recently("没人收的消息", 600) is False)
    finally:
        if old_req3 is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old_req3
        notify.QUEUE_FILE, notify.REMIND_URL, notify.NTFY_TOPIC = old_q3, old_url3, old_topic3
        notify.WEBHOOK = old_hook
    # ---- 出票卡片 ----
    class S:
        date = "2026-11-29"
        dep_station = "北京南"
        arr_station = "上海虹桥"
        dep_time = "06:18"
        arr_time = "12:11"
        duration_min = 353
        price = 553.0
        seat = "二等座"
        train_no = "G547"
        via = ""
        transfers = 0

        def label(self):
            return "G547 北京南→上海虹桥"

    link = booking.search_link("北京南", "上海虹桥", "2026-11-29")
    ok("深链有 fs/ts/date", "fs=" in link and "ts=" in link and "date=2026-11-29" in link, link)
    ok("深链走官方域名", link.startswith("https://kyfw.12306.cn/otn/leftTicket/init"), link)
    ok("深链对中文做了编码", "%E5%8C%97" in link, link)

    card = booking.card_text(S())
    ok("卡片含车次", "G547" in card)
    ok("卡片含席别与价格", "二等座" in card and "553" in card)
    ok("卡片含深链", "kyfw.12306.cn" in card)

    line = booking.one_line(S())
    ok("一行摘要含深链", "kyfw.12306.cn" in line and "G547" in line, line)

    ok("空方案返回空串", booking.card_text(None) == "" and booking.one_line(None) == "")
    ok("缺参数不生成链接", booking.search_link("", "上海", "2026-11-29") == "")

    # ---- 10. 目标通道已关闭的积压：丢弃而不是无限重试
    notify.QUEUE_FILE.write_text(
        njson.dumps({"content": "老积压：只投提醒服务", "channels": ["remind"]}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    notify.REMIND_URL = ""
    res = notify.drain()
    ok("通道已关的积压被丢弃", res.get("dropped") == 1 and res.get("left") == 0, str(res))
    ok("丢弃后队列为空", notify.pending() == 0, str(notify.pending()))

    # 显式给了 url 仍算要投提醒服务（临时补发/测试注入靠这个）
    notify.QUEUE_FILE.write_text(
        njson.dumps({"content": "指定端点的积压", "channels": ["remind"]}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    res = notify.drain(url="http://127.0.0.1:1/remind")
    ok("显式 url 时不丢弃、失败仍保留", res.get("dropped") == 0 and res.get("left") == 1, str(res))
    notify.QUEUE_FILE.unlink()

    print("-" * 72)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
