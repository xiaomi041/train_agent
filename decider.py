"""放宽哪一项：规则版 vs 模型版。

盯票遇到「有候选但都不满足」时，得决定下一步放宽哪个约束。规则版按固定顺序开
（先允许中转、再放日期、再放席别……），好处是零成本、可预测，坏处是经常放错地方：
用户明明是被预算卡住，它却先去放开一个无关的维度，于是多跑几轮、多查几次接口，
最后买到的票还不如原本就够用的那趟。

模型版把这一步交给模型：给它当前约束、已放宽的维度、最优候选差在哪，让它挑一项。
两者可以互换，也可以跑 A/B —— `bench_decide.py` 就是干这个的。

2026-11-20 起模型版是默认（`DECIDE_MODE=llm`），为了保证它不会变成"每小时烧钱"，
加了三道闸：

1. **局面缓存**：同一组约束 + 同一批违规项只问一次模型。盯票的常态是"局面几十分钟
   不变"，缓存命中率很高；
2. **单任务配额**：每个任务每小时最多 `DECIDE_MAX_CALLS` 次模型决策，超了退回规则，
   任务不会因为省钱而停摆；
3. **任何异常都退回规则**：模型挂了、返回没法解析、配额用完 —— 一律按规则顺序开。

选择返回：维度名（RELAX_ORDER 之一），或 None 表示「先不放宽，等新票」。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:  # dotenv 缺席时照常工作（容器里靠环境变量）
    pass

import watch

log = logging.getLogger("decider")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.getenv("DECIDE_MODEL", "deepseek-chat")
# 决策这种小事用长上下文没意义，也不希望它自由发挥
TEMPERATURE = 0.0

# ---- 成本控制的三道闸 ----
CACHE_MAX = int(os.getenv("DECIDE_CACHE_MAX", "256"))
QUOTA_PER_HOUR = int(os.getenv("DECIDE_MAX_CALLS", "6"))
QUOTA_WINDOW = float(os.getenv("DECIDE_WINDOW_SECONDS", "3600"))

_CACHE: "OrderedDict[str, object]" = OrderedDict()
_QUOTA: dict = {}
_STATS = {"calls": 0, "cache_hits": 0, "quota_blocks": 0, "bad_json": 0}
_clock = time.monotonic


def reset_stats() -> None:
    """清空统计（bench / 测试用；生产里这些数字只增不减）。"""
    _STATS.update({"calls": 0, "cache_hits": 0, "quota_blocks": 0, "bad_json": 0})


def stats() -> dict:
    """这一进程里模型决策实际花了多少次调用、省了多少次。"""
    return dict(_STATS, cache_size=len(_CACHE), quota_goals=len(_QUOTA))


def clear_cache() -> None:
    _CACHE.clear()
    _QUOTA.clear()


def rule_decider(goal, dims, remaining, best=None, bad=None):
    """规则版：按固定顺序开下一个。返回 None 只在全部开完时。"""
    return remaining[0] if remaining else None


PROMPT = """你是火车票盯票助手。用户设了一组硬约束，现在有候选车次但都不满足。

请从「还没放宽的维度」里挑一个最值得放宽的，或者选择先不放宽（等新票放出来）。

判断原则：
- 优先放宽「最可能挡住这趟车」的那一项，而不是固定顺序；
- 候选只是价格超一点、而预算本来宽裕时，放宽价格通常比放开日期更划算；
- 如果一条候选都没有：当天没票/未放票。用户本来就允许日期浮动（flex_days>0）时，
  「放宽日期」通常最划算；日期完全不可动时，就选 null 等新票；
- 只有「所有能放宽的都放完了还不满足」才该放弃，别急着放弃；
- 一次只挑一项；拿不准就选 null（等），代价是晚一轮，比乱放宽便宜。

只输出 JSON，不要解释：{"relax": "transfer|date|seat|money|time|arrive" 或 null}
"""


def _client():
    from openai import OpenAI
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=BASE_URL)


def describe(goal, dims, remaining, best, bad) -> str:
    """把当前局面压成一段给模型看的事实（不塞长文本，控制成本）。"""
    lines = [
        f"行程：{goal.origin} → {goal.dest}，日期 {goal.date}，已查 {goal.attempts} 次",
        f"约束：预算 {goal.budget if goal.budget is not None else '不限'} 元；"
        f"总耗时上限 {goal.max_minutes or '不限'} 分钟；"
        f"到达时段 {goal.arrive_after}-{goal.arrive_before}；"
        f"席别偏好 {'/'.join(goal.seat_prefs)}；最多换乘 {goal.max_transfers} 次；"
        f"日期可浮动 ±{goal.flex_days} 天",
        f"已放宽：{'、'.join(watch.RELAX_LABEL[d] for d in dims) or '无'}",
        f"还能放宽：{'、'.join(f'{d}({watch.RELAX_LABEL[d]})' for d in remaining)}",
    ]
    if best is not None:
        lines.append(
            f"当前最优候选：{best.train_no} {best.dep_time}-{best.arr_time} "
            f"{best.seat} ¥{best.price:.0f} 耗时 {best.duration_min} 分钟"
        )
        if bad:
            lines.append(f"但违反了：{'、'.join(bad)}")
        else:
            lines.append("（这个候选其实满足，不该走到放宽这一步）")
    else:
        lines.append("本轮一条候选都没有（当天可能没票或还没放票）")
    return "\n".join(lines)


_PARSE_RE = re.compile(r"\{.*\}", re.S)

# 最近一次调用的 token 用量（bench_decide 用它算成本；生产里不看这个）
LAST_USAGE: dict = {}


def _ask_model(text: str) -> str:
    """真正打一次模型。单独抽出来是为了让测试能替换掉它。"""
    client = _client()
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=TEMPERATURE,
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text},
        ],
    )
    u = getattr(resp, "usage", None)
    LAST_USAGE.clear()
    LAST_USAGE.update({"in": getattr(u, "prompt_tokens", 0) or 0,
                       "out": getattr(u, "completion_tokens", 0) or 0})
    return (resp.choices[0].message.content or "").strip()


def _situation(goal, dims, bad, best) -> str:
    """局面指纹：约束 + 已放宽项 + 违规项。

    刻意**不**把最优候选的车次号算进去：同一组约束下换个车次，该放宽的还是同一项，
    否则缓存的命中率会掉到几乎为零。
    """
    key = [
        goal.origin, goal.dest, goal.date, goal.budget, goal.max_minutes,
        goal.arrive_after, goal.arrive_before, tuple(goal.seat_prefs),
        goal.max_transfers, goal.flex_days, tuple(dims), tuple(sorted(bad or [])),
        best is None,
    ]
    return json.dumps(key, ensure_ascii=False, default=str)


def _quota_ok(goal_id: str) -> bool:
    now = _clock()
    hits = [t for t in _QUOTA.get(goal_id, []) if now - t < QUOTA_WINDOW]
    if len(hits) >= QUOTA_PER_HOUR:
        _QUOTA[goal_id] = hits
        return False
    _QUOTA[goal_id] = hits
    return True


def _quota_mark(goal_id: str) -> None:
    _QUOTA.setdefault(goal_id, []).append(_clock())


def llm_decider(goal, dims, remaining, best=None, bad=None):
    """模型版：让模型挑一项；缓存命中直接用，配额用完退回规则（返回 None）。"""
    key = _situation(goal, dims, bad, best)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        _STATS["cache_hits"] += 1
        return _CACHE[key]

    if not _quota_ok(goal.id):
        _STATS["quota_blocks"] += 1
        log.info("任务 %s 本小时模型决策配额已用完（%d 次），本轮退回规则顺序",
                 goal.id[:8], QUOTA_PER_HOUR)
        return None

    _STATS["calls"] += 1
    _quota_mark(goal.id)
    try:
        text = _ask_model(describe(goal, dims, remaining, best, bad))
    except Exception as e:  # noqa: BLE001 — 模型不可用退回规则，不中断盯票
        log.warning("模型决策失败，退回规则顺序：%r", e)
        return None

    m = _PARSE_RE.search(text)
    choice: object = None
    if not m:
        _STATS["bad_json"] += 1
    else:
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            _STATS["bad_json"] += 1
        else:
            picked = obj.get("relax")
            choice = picked if picked in remaining else None

    _CACHE[key] = choice
    if len(_CACHE) > CACHE_MAX:
        _CACHE.popitem(last=False)
    return choice


def make_decider(kind: str = ""):
    """kind: rule / llm；留空则读 `DECIDE_MODE`（默认 llm）。

    未知值一律当 rule —— 绝不让配置写错把盯票搞停。
    """
    kind = (kind or os.getenv("DECIDE_MODE", "llm")).strip().lower()
    return llm_decider if kind == "llm" else rule_decider


if __name__ == "__main__":  # 手动看一眼模型怎么选的
    g = watch.Goal(origin="北京", dest="上海", date="2026-11-25",
                   budget=300, max_minutes=600)
    s = watch.Scheme(train_no="G1", dep_station="北京南", arr_station="上海虹桥",
                     dep_time="08:00", arr_time="14:30", duration_min=390,
                     price=553, seat="二等座", date="2026-11-25")
    dims = []
    rem = list(watch.RELAX_ORDER)
    print(describe(g, dims, rem, s, watch.check(s, g)))
    print("规则版选：", rule_decider(g, dims, rem, s, watch.check(s, g)))
    print("模型版选：", llm_decider(g, dims, rem, s, watch.check(s, g)))
    print("统计：", stats())
