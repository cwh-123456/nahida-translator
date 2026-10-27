"""PDF 复制出来的英文丢了空格，能不能安全补回来。

四件事：
  1. 只有"确实粘连"的文本才判定要补（正常英文 / 代码 / 驼峰标识符 / 中文都放过）
  2. 模型只加了空格 -> 采用；改了任何字母标点 -> 一律丢弃，按原文继续
  3. 超长选区不硬塞（4k 上下文），直接跳过
  4. 补完会往事件队列投 ("source", 新原文)，让面板原文跟着变

用法：python tests/test_glued_text.py
"""
from __future__ import annotations

import os
import queue
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.engine import Translator, _squeeze, looks_glued  # noqa: E402
from app.main import TranslatorApp  # noqa: E402

BLOB = ("BesttostartwithC=vector.MultiplybyBtogetthevectorBC,"
        "andthenthevectorA(BC).Vectorsneedlesscomputingtimethanmatrices!")
FIXED = ("Best to start with C = vector. Multiply by B to get the vector BC, "
         "and then the vector A(BC). Vectors need less computing time than matrices!")


class StubTranslator(Translator):
    """不连引擎的 Translator：_chat 直接返回准备好的字符串。"""

    def __init__(self, reply: str):
        self._reply = reply
        self._translate_lock = threading.Lock()
        self.calls = 0
        self.ready = True

    def ensure_server(self, on_status=None) -> None:  # noqa: D401
        pass

    def _chat(self, messages, on_token=None, max_tokens=None,
              temperature=None, cancel=None):
        self.calls += 1
        return self._reply, 0


def case_detect() -> bool:
    print("1) 粘连判定")
    cases = [
        ("整句粘连", BLOB, True),
        ("半句粘连", "Vectorsneedlesscomputingtimethanmatrices", True),
        ("正常英文", "Vectors need less computing time than matrices", False),
        ("代码行", "result = someVeryLongFunctionNameAnotherOne(items)", False),
        ("驼峰标识", "someVeryLongFunctionNameAnotherOne andAnotherVeryLongNameHere", False),
        ("中文", "最好先从C开始，将其矢量相乘得到BC", False),
        ("URL", "https://example.com/some/long/path/to/page", False),
        ("短单词", "electroencephalography", False),
    ]
    ok = True
    for name, text, want in cases:
        got = looks_glued(text)
        ok = ok and got == want
        print(f"    {name:<6} 期望 {want} 实际 {got} {'✓' if got == want else '✗'}")
    return ok


def case_accept() -> bool:
    print("2) 只加空格 -> 采用")
    tr = StubTranslator(FIXED)
    out = tr.restore_spaces(BLOB)
    ok = out == FIXED and _squeeze(out) == _squeeze(BLOB) and tr.calls == 1
    print(f"    输出 = {out[:60]}…")
    print(f"    {'✓' if ok else '✗'}")
    return ok


def case_reject() -> bool:
    print("3) 模型动了内容 -> 丢弃，按原文继续")
    ok = True
    bad_replies = {
        "漏字": FIXED.replace("matrices", "matrix"),
        "改标点": FIXED.replace("!", "."),
        "翻成中文": "最好先从C开始，将其矢量相乘得到BC。",
        "加了引号包裹的解释": '好的，修复如下：' + FIXED,
    }
    for name, reply in bad_replies.items():
        tr = StubTranslator(reply)
        out = tr.restore_spaces(BLOB)
        good = out == BLOB
        ok = ok and good
        print(f"    {name:<8} 退回原文 {good} {'✓' if good else '✗'}")
    return ok


def case_too_long() -> bool:
    print("4) 超长选区不硬塞")
    tr = StubTranslator(FIXED)
    text = (BLOB + " ") * 40          # 5000+ 字符
    out = tr.restore_spaces(text)
    ok = out == text.strip() and tr.calls == 0
    print(f"    长度 {len(text)} -> 调用模型 {tr.calls} 次，原样返回 {out == text.strip()}")
    print(f"    {'✓' if ok else '✗'}")
    return ok


def case_app_event() -> bool:
    print("5) 补完投递 source 事件给面板")
    class Fake:
        def __init__(self):
            self.translator = StubTranslator(FIXED)
            self.q = queue.Queue()
            self.pending = ("", 0, 0)

    app = Fake()
    out = TranslatorApp._restore_source_if_glued(app, BLOB, 7, lambda _s: None, None)
    got = list(app.q.queue)
    ok = (out == FIXED and len(got) == 1
          and got[0] == ("source", FIXED, 7))
    print(f"    返回是否已补空格 {out == FIXED}；事件 {got}")
    print(f"    {'✓' if ok else '✗'}")
    return ok


def main() -> int:
    results = [
        case_detect(),
        case_accept(),
        case_reject(),
        case_too_long(),
        case_app_event(),
    ]
    print("\n结果:", "PASS" if all(results) else "FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
