"""「单击唤醒面板、拖选不唤醒」的钩子层测试。

为什么不能"按下就唤醒"：拖选的按下也走同一条钩子。如果按一下就唤醒，
用户想选面板底下那行文字时，面板会在拖到一半时变成可交互并把整个拖拽
抢走——这是比"叉号点不动"更糟的回归。所以判据必须是"按下→松开几乎没
位移"的单击。
"""
from __future__ import annotations

import os
import queue
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pynput import mouse  # noqa: E402

from app.main import TranslatorApp  # noqa: E402
from app.selection import SelectionWatcher  # noqa: E402

LEFT = mouse.Button.left


def click_test() -> bool:
    hits: list[tuple[int, int]] = []
    w = SelectionWatcher(on_selection=lambda *a: None, on_single_click=lambda x, y: hits.append((x, y)))
    w._on_click(100, 200, LEFT, True)
    w._on_click(101, 201, LEFT, False)      # 松手位置差 1px：算单击
    print(f"[1] 单击：回调 {hits}（应为 [(100, 200)]）")
    return hits == [(100, 200)]


def drag_test() -> bool:
    hits: list[tuple[int, int]] = []
    w = SelectionWatcher(on_selection=lambda *a: None, on_single_click=lambda x, y: hits.append((x, y)))
    w._on_click(100, 200, LEFT, True)
    w._on_click(260, 240, LEFT, False)      # 拖了 160px：是拖选，不是点面板
    print(f"[2] 拖选：回调 {hits}（应为 []）")
    return hits == []


def passthrough_test() -> bool:
    """穿透面板上的单击要投递「panel_click」；非穿透/面板外的单击不要投递。"""
    def run(click_through: bool, inside: bool) -> bool:
        q: queue.Queue = queue.Queue()
        fake = types.SimpleNamespace(
            q=q,
            result=types.SimpleNamespace(
                fast_visible=lambda: True,
                click_through=lambda: click_through,
                fast_contains=lambda x, y: inside,
            ),
        )
        TranslatorApp._on_panel_click(fake, 500, 400)
        if q.empty():
            return False
        item = q.get_nowait()
        return item == ("panel_click", 500, 400)

    cases = [
        ("穿透 + 面板内", True, True, True),
        ("穿透 + 面板外", True, False, False),
        ("不穿透 + 面板内", False, True, False),
    ]
    ok = True
    for i, (tag, through, inside, want) in enumerate(cases, start=3):
        got = run(through, inside)
        print(f"[{i}] {tag}：投递点击={got}（应为 {want}）")
        ok = ok and got == want
    return ok


def body_vs_header_test() -> bool:
    """落点在正文区不唤醒（保持穿透选字），在标题带/按钮上才唤醒。"""
    def run(in_body: bool, click_through: bool = True) -> bool:
        q: queue.Queue = queue.Queue()
        woke: list[str] = []
        fake = types.SimpleNamespace(
            q=q,
            result=types.SimpleNamespace(
                fast_visible=lambda: True,
                visible=lambda: True,
                click_through=lambda: click_through,
                fast_contains=lambda x, y: True,
                click_in_body=lambda x, y: in_body,
                wake=lambda: woke.append("wake"),
            ),
        )
        TranslatorApp._apply_panel_click(fake, 500, 400)
        return bool(woke)

    cases = [
        ("正文区", True, True, False),
        ("标题带/按钮", True, False, True),
        ("不穿透（已经在交互）", False, True, False),
    ]
    ok = True
    for i, (tag, through, body, want) in enumerate(cases, start=6):
        got = run(body, through)
        print(f"[{i}] {tag}：唤醒={got}（应为 {want}）")
        ok = ok and got == want
    return ok


def main() -> int:
    ok = (click_test() and drag_test() and passthrough_test()
          and body_vs_header_test())
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
