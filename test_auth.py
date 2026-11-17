"""口令网关门禁测试（Streamlit AppTest，容器内也跑）。

覆盖四态：
  1. 配置了口令、未登录  → 只看到密码框，看不到任何业务界面
  2. 输错口令           → 报错且仍未登录
  3. 输对口令           → 进入主界面，导航/按钮出现
  4. 未配置口令         → 直接可用（本地开发不受影响）

run: python test_auth.py
"""
from __future__ import annotations

import os
import sys
import traceback

PASS, FAIL = 0, 0


def ok(name: str, cond: bool, extra="") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def _labels(widgets) -> list[str]:
    return [getattr(w, "label", "") or "" for w in widgets]


def _run_app(password_env: str | None):
    """在独立进程语义下跑一次 AppTest：改环境变量后重新加载模块。"""
    import importlib

    if password_env is None:
        os.environ.pop("APP_PASSWORD", None)
    else:
        os.environ["APP_PASSWORD"] = password_env

    import streamlit.testing.v1 as stt

    return stt.AppTest.from_file("a.py", default_timeout=240)


def test_gate_blocks_when_locked():
    print("\n[1] 配置了口令且未登录 → 只看到密码框")
    at = _run_app("test-pw-123")
    at.run()
    ok("没有异常", len(at.exception) == 0, [str(e.value)[:120] for e in at.exception])
    inputs = _labels(at.text_input)
    ok("出现口令输入框", any("口令" in x for x in inputs), inputs)
    ok("没有渲染业务按钮（🔍 查询）",
       not any("查询" in x for x in _labels(at.button)), _labels(at.button))
    ok("authed 未置真", not at.session_state.get("authed"), at.session_state.get("authed"))


def test_wrong_password():
    print("\n[2] 输错口令 → 报错且仍未登录")
    at = _run_app("test-pw-123")
    at.run()
    at.text_input[0].set_value("wrong-pw")
    at.button[0].click().run()
    msgs = [e.value for e in at.error]
    ok("提示口令不正确", any("不正确" in str(m) for m in msgs), msgs)
    ok("仍然没有 authed", not at.session_state.get("authed"), at.session_state.get("authed"))


def test_right_password():
    print("\n[3] 输对口令 → 进入主界面")
    at = _run_app("test-pw-123")
    at.run()
    at.text_input[0].set_value("test-pw-123")
    at.button[0].click().run()
    ok("没有异常", len(at.exception) == 0, [str(e.value)[:120] for e in at.exception])
    ok("authed 为真", bool(at.session_state.get("authed")), at.session_state.get("authed"))
    buttons = _labels(at.button)
    ok("出现业务按钮（查询/规划/盯票）",
       any("查询" in x for x in buttons), buttons)
    ok("出现多约束规划入口", any("规划" in x for x in buttons), buttons)


def test_no_password_means_open():
    print("\n[4] 未配置口令 → 直接可用（本地开发）")
    at = _run_app(None)
    at.run()
    ok("没有异常", len(at.exception) == 0, [str(e.value)[:120] for e in at.exception])
    ok("没有口令框", not any("口令" in x for x in _labels(at.text_input)), _labels(at.text_input))
    ok("直接出现业务按钮", any("查询" in x for x in _labels(at.button)), _labels(at.button))


def main() -> int:
    for t in (test_gate_blocks_when_locked, test_wrong_password,
              test_right_password, test_no_password_means_open):
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
