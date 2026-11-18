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

    print("-" * 72)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
