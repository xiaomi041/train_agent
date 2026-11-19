"""统一通知出口：盯票命中时把结果推到手机（ntfy / 群机器人），可选投给提醒服务。

默认**只走 ntfy**，不依赖任何别的项目。以前默认投给 PersonalAI 的提醒服务，两个项目
就绑在一起了：它重启的几秒里通知投不进去，它界面上的问题也会牵连过来。现在
`REMIND_URL` 留空即完全不走提醒服务，要联动时显式配一行即可。

设计要点：
1. 失败不抛异常：通知是副产物，不能因为它挂了把盯票主流程带崩；
2. 失败进本地队列（`notifications.jsonl`），下次 `drain()` 补发，进程重启也不丢；
3. 默认目标是 ntfy；要投提醒服务需显式配置 `REMIND_URL`；
4. 若对端配置了口令（`APP_PASSWORD`），用同名环境变量带上 token，未配置则不校验。
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

# 留空 = 不启用提醒服务通道（默认）。要联动个人事务项目时再显式配置。
REMIND_URL = os.getenv("REMIND_URL", "").strip()
TIMEOUT = float(os.getenv("REMIND_TIMEOUT", "4"))
QUEUE_FILE = Path(os.getenv("NOTIFY_QUEUE", str(Path(__file__).parent / "notifications.jsonl")))
WEBHOOK = os.getenv("NOTIFY_WEBHOOK", "").strip()
WEBHOOK_TIMEOUT = float(os.getenv("NOTIFY_WEBHOOK_TIMEOUT", "5"))
# ntfy：不依赖任何管理员审批的推送通道（手机装个 App 订阅 topic 即可）
NTFY_TOPIC = os.getenv("NOTIFY_NTFY", "").strip()
NTFY_SERVER = os.getenv("NOTIFY_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TIMEOUT = float(os.getenv("NOTIFY_NTFY_TIMEOUT", "5"))
# 同一件事多久内只通知一次（秒）。0 表示关闭去重。
DEDUPE_SECONDS = float(os.getenv("NOTIFY_DEDUPE_SECONDS", "600"))


def _post(payload: dict, url: str) -> dict:
    import requests

    r = requests.post(url, json=payload, timeout=TIMEOUT)
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": "对端返回非 JSON"}
    return data if isinstance(data, dict) else {"ok": True, "raw": data}


def _queue(payload: dict) -> None:
    try:
        with QUEUE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _webhook_payload(text: str, url: str = "") -> dict:
    """按域名猜是哪家机器人 —— 三家都是「POST 一个 JSON」，但字段名不一样。"""
    u = (url or WEBHOOK).lower()
    if "feishu" in u or "larksuite" in u:
        return {"msg_type": "text", "content": {"text": text}}
    if "dingtalk" in u:
        return {"msgtype": "text", "text": {"content": text}}
    if "weixin" in u or "wecom" in u or "qyapi" in u:
        return {"msgtype": "text", "text": {"content": text}}
    return {"msgtype": "text", "text": {"content": text}}


def webhook_push(text: str, url: str = "") -> dict:
    """推一条到群机器人（飞书 / 钉钉 / 企业微信）。没配置就是 no-op，不算失败。"""
    target = (url or WEBHOOK).strip()
    if not target:
        return {"ok": False, "skipped": True, "reason": "未配置 NOTIFY_WEBHOOK"}
    if not text.strip():
        return {"ok": False, "error": "空内容"}
    import requests

    payload = _webhook_payload(text, target)
    try:
        r = requests.post(target, json=payload, timeout=WEBHOOK_TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
    # 三家成功时都回 errcode/StatusCode 之类的字段，且 200 也可能带错误码
    try:
        data = r.json()
    except Exception:
        return {"ok": True, "provider": "unknown"}
    if isinstance(data, dict):
        code = data.get("code", data.get("errcode", data.get("StatusCode", 0)))
        try:
            bad = int(code) not in (0, 200)
        except Exception:
            bad = False
        if bad:
            return {"ok": False, "error": f"机器人返回 {code}: {str(data)[:120]}"}
    return {"ok": True, "provider": "webhook"}


def ntfy_push(text: str, topic: str = "", server: str = "") -> dict:
    """推一条到 ntfy。没配 topic 就是 no-op。

    ntfy 的协议很朴素：往 `https://ntfy.sh/<topic>` POST 一段纯文本就是一条消息，
    标题走 ASCII 头（HTTP 头不能放中文），正文是 UTF-8 的 body。
    """
    t = (topic or NTFY_TOPIC).strip()
    if not t:
        return {"ok": False, "skipped": True, "reason": "未配置 NOTIFY_NTFY"}
    if not text.strip():
        return {"ok": False, "error": "空内容"}
    import requests

    base = (server or NTFY_SERVER).rstrip("/")
    url = f"{base}/{t}"
    try:
        r = requests.post(
            url,
            data=text.encode("utf-8"),
            headers={"Title": "trainAI", "Tags": "train", "Priority": "default"},
            timeout=NTFY_TIMEOUT,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
    return {"ok": True, "provider": "ntfy"}


def _sent_file() -> Path:
    return QUEUE_FILE.with_name("notify_sent.jsonl")


def _seen_recently(key: str, window: float) -> bool:
    """同一个 key 在 window 秒内是否已经发过（跨进程，所以落文件）。"""
    p = _sent_file()
    if not p.exists():
        return False
    now = time.time()
    keep = []
    seen = False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if now - float(item.get("t", 0)) > window:
            continue          # 过期的顺手清掉，文件不会无限长
        keep.append(item)
        if item.get("k") == key:
            seen = True
    try:
        p.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in keep),
                     encoding="utf-8")
    except OSError:
        pass
    return seen


def _mark_sent(key: str) -> None:
    try:
        with _sent_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps({"k": key, "t": time.time()}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def send(content: str, at: str = "", repeat: str = "once", url: str = None,
         dedupe_key: str = "") -> dict:
    """发一条通知。at 为空表示「马上提醒」（1 分钟后，避免立刻过期）。

    三个通道（提醒服务 / 群机器人 / ntfy）各自独立，**哪条失败了就只补哪条**：
    payload 里的 `channels` 记下失败通道，`drain()` 只重试它们，成功的不重发，
    否则「提醒服务挂了但 ntfy 通了」这种情况下手机会收到两遍同样的消息。

    `dedupe_key`：同一件事在 `NOTIFY_DEDUPE_SECONDS`（默认 600）秒内只发一次。
    起因是真实踩到的——同一次命中被两个 tick 各通知一遍，界面上弹出两条一样的提醒。
    """
    if not content.strip():
        return {"ok": False, "error": "空内容"}

    # 先算出这一条**实际会走几个通道**：一个都没配就别去碰去重表、更别写队列，
    # 否则队列里会堆一堆永远发不出去的东西，`pending` 数字变成噪音。
    # 显式传 url 也算「要投提醒服务」——测试和临时脚本靠这个注入失败端点，
    # 只认环境变量会让 send(url=...) 静默变成「只发 ntfy」，排查时非常误导。
    remind_url = (url or REMIND_URL or '').strip()
    enabled = []
    if remind_url:
        enabled.append('remind')
    if WEBHOOK:
        enabled.append("webhook")
    if NTFY_TOPIC:
        enabled.append("ntfy")
    if not enabled:
        return {"ok": False, "skipped": True,
                "reason": "没有任何通知通道（NOTIFY_NTFY / NOTIFY_WEBHOOK / REMIND_URL 都为空）"}

    key = dedupe_key or content.strip()[:200]
    if DEDUPE_SECONDS > 0 and _seen_recently(key, DEDUPE_SECONDS):
        return {"ok": True, "deduped": True, "key": key}
    _mark_sent(key)

    when = at or (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    payload = {
        "content": content.strip()[:500],
        "at": when,
        "repeat": repeat,
        "token": os.getenv("REMIND_TOKEN") or os.getenv("APP_PASSWORD", ""),
        "source": "trainAI",
    }
    failed = []
    res = {"ok": True, "channels": enabled, "target": remind_url or NTFY_SERVER}

    if "remind" in enabled:
        try:
            r = _post(payload, remind_url)
        except Exception as e:  # 连接失败/超时：进队列，不打断主流程
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}
        res["remind"] = r
        if not r.get("ok"):
            failed.append("remind")

    # ntfy / 群机器人（配置了才发；各自失败不影响另外两条）
    if "webhook" in enabled:
        r = webhook_push(content)
        res["webhook"] = r
        if not r.get("ok"):
            failed.append("webhook")
    if "ntfy" in enabled:
        r = ntfy_push(content)
        res["ntfy"] = r
        if not r.get("ok"):
            failed.append("ntfy")

    if failed:
        payload["channels"] = failed
        _queue(payload)
        res["queued"] = True
        res["failed_channels"] = failed
        res["ok"] = False
    return res


def pending() -> int:
    """队列里还有几条没发出去。"""
    if not QUEUE_FILE.exists():
        return 0
    try:
        return sum(1 for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def _retry(payload: dict, url: str = None) -> list:
    """重试一个积压 payload 里失败过的通道，返回仍然失败的通道名。"""
    # 老队列没有 channels 字段：那时默认走提醒服务；现在若提醒服务已关闭，
    # 就按当前唯一启用的通道重试，否则这些积压会永远发不出去。
    chans = payload.get("channels") or (["remind"] if REMIND_URL else ["ntfy"])
    text = payload.get("content", "")
    still = []
    for ch in chans:
        if ch == "remind":
            body = {k: v for k, v in payload.items() if k != "channels"}
            try:
                res = _post(body, url or REMIND_URL)
            except Exception:
                res = {"ok": False}
            if not res.get("ok"):
                still.append("remind")
        elif ch == "webhook":
            if not webhook_push(text).get("ok"):
                still.append("webhook")
        elif ch == "ntfy":
            if not ntfy_push(text).get("ok"):
                still.append("ntfy")
    return still


def drain(url: str = None) -> dict:
    """补发队列；成功的移出，失败的保留（只重试失败过的通道）。返回 {sent, left}。"""
    if not QUEUE_FILE.exists():
        return {"sent": 0, "left": 0}
    sent, left = 0, []
    for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        still = _retry(payload, url)
        if still:
            payload["channels"] = still
            left.append(payload)
        else:
            sent += 1
    try:
        QUEUE_FILE.write_text(
            "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in left), encoding="utf-8"
        )
    except OSError:
        pass
    return {"sent": sent, "left": len(left)}


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if args and args[0] in ("--drain", "-d"):
        # 提醒服务恢复后补发积压：成功的出队、失败的保留
        print(drain())
    elif args and args[0] in ("--pending", "-p"):
        print({"pending": pending()})
    elif args and args[0] == "--push":
        # 只推群机器人，不写提醒表：用来单独验证 webhook 通不通
        print(webhook_push(" ".join(args[1:]) or "测试：来自 trainAI 的 webhook 推送"))
    elif args and args[0] == "--ntfy":
        # 只推 ntfy：用来单独验证手机能不能收到
        print(ntfy_push(" ".join(args[1:]) or "测试：来自 trainAI 的 ntfy 推送"))
    else:
        print(send(" ".join(args) or "测试通知：来自 trainAI"))
