"""统一通知出口：盯票命中时把结果推到「个人事务委托系统」的提醒服务。

为什么不让两个项目各写各的通知：
- 提醒的**落库、去重、弹窗、确认**在 PersonalAI 里已经做好了（`reminders` 表 + 8600 服务）；
- 再实现一遍只会出现"两个地方都能提醒，但哪个都不准"。

设计要点：
1. 失败不抛异常：通知是副产物，不能因为它挂了把盯票主流程带崩；
2. 失败进本地队列（`notifications.jsonl`），下次 `drain()` 补发，进程重启也不丢；
3. 目标是容器网络里的 `http://personal-ai:8600/remind`，可用 `REMIND_URL` 覆盖；
4. 若对端配置了口令（`APP_PASSWORD`），用同名环境变量带上 token，未配置则不校验。
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

REMIND_URL = os.getenv("REMIND_URL", "http://personal-ai:8600/remind")
TIMEOUT = float(os.getenv("REMIND_TIMEOUT", "4"))
QUEUE_FILE = Path(os.getenv("NOTIFY_QUEUE", str(Path(__file__).parent / "notifications.jsonl")))


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


def send(content: str, at: str = "", repeat: str = "once", url: str = None) -> dict:
    """发一条通知。at 为空表示「马上提醒」（1 分钟后，避免立刻过期）。"""
    if not content.strip():
        return {"ok": False, "error": "空内容"}
    when = at or (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    payload = {
        "content": content.strip()[:500],
        "at": when,
        "repeat": repeat,
        "token": os.getenv("REMIND_TOKEN") or os.getenv("APP_PASSWORD", ""),
        "source": "trainAI",
    }
    target = url or REMIND_URL
    try:
        res = _post(payload, target)
    except Exception as e:  # 连接失败/超时：进队列，不打断主流程
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}
    if not res.get("ok"):
        _queue(payload)
        res["queued"] = True
        res["target"] = target
    return res


def pending() -> int:
    """队列里还有几条没发出去。"""
    if not QUEUE_FILE.exists():
        return 0
    try:
        return sum(1 for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def drain(url: str = None) -> dict:
    """补发队列；成功的移除，失败的保留。返回 {sent, left}。"""
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
        try:
            res = _post(payload, url or REMIND_URL)
        except Exception:
            res = {"ok": False}
        if res.get("ok"):
            sent += 1
        else:
            left.append(payload)
    QUEUE_FILE.write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in left), encoding="utf-8"
    )
    return {"sent": sent, "left": len(left)}


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if args and args[0] in ("--drain", "-d"):
        # 提醒服务恢复后补发积压：成功的出队、失败的保留
        print(drain())
    elif args and args[0] in ("--pending", "-p"):
        print({"pending": pending()})
    else:
        print(send(" ".join(args) or "测试通知：来自 trainAI"))
