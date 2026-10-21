"""「鼠标一移过去就能点」的测试：悬停唤醒 + 可交互窗口的起点。

背景（为什么会有这个文件）：
- 面板闲置后是整窗 WS_EX_TRANSPARENT 穿透。穿透状态下 Windows 已经把点击
  派发给了下面的程序，钩子只能"事后知道"，救不回来。所以只靠"单击后唤醒"
  必然要点两下。
- 唯一可靠的办法是**在鼠标移过去的那一刻**就把穿透关掉（悬停唤醒）。
- 另外 touch() 以前只在 open()（任务开始）时调，图片翻译要 12 秒，
  结果到手时可交互窗口早就过期了 —— 起点也必须是 finish()。

这些都只测钩子层的纯算数逻辑，不需要开 Tk 窗口。
"""
from __future__ import annotations

import os
import queue
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.main import TranslatorApp  # noqa: E402
from app.ui import INTERACTIVE_SECONDS, ResultWindow  # noqa: E402

PANEL = (100, 100, 400, 300)        # 面板屏幕矩形
BODY = (120, 160, 360, 220)         # 正文区：面板内部


def make_fake(click_through: bool = True, visible: bool = True,
              in_action: bool = True):
    q: queue.Queue = queue.Queue()
    fake = types.SimpleNamespace(
        q=q,
        _shotting=False,
        _hover_armed=True,
        result=types.SimpleNamespace(
            fast_visible=lambda: visible,
            click_through=lambda: click_through,
            action_zone_contains=lambda x, y: in_action,
        ),
    )
    return fake, q


def hover_dedup_test() -> bool:
    """进入可操作区投递一次；同一区域继续移动不再投递（否则每秒几百个事件）。"""
    fake, q = make_fake()
    for _ in range(50):                      # 模拟高频移动事件
        TranslatorApp._on_mouse_move(fake, 300, 120)
    n = q.qsize()
    print(f"[1] 悬停 50 次：投递 {n} 次（应为 1）")
    return n == 1


def hover_body_test() -> bool:
    """正文区不唤醒：半透明面板下能看见底下的字，点正文是想选那些字。"""
    fake, q = make_fake(in_action=False)
    TranslatorApp._on_mouse_move(fake, 300, 260)
    print(f"[2] 悬停进正文区：投递 {q.qsize()} 次（应为 0）")
    return q.qsize() == 0


def hover_when_interactive_test() -> bool:
    """面板本来就点得动时不必唤醒。"""
    fake, q = make_fake(click_through=False)
    TranslatorApp._on_mouse_move(fake, 300, 120)
    print(f"[3] 面板已可交互：投递 {q.qsize()} 次（应为 0）")
    return q.qsize() == 0


def hover_hidden_test() -> bool:
    """面板没显示时不能投递（否则会在用户读别的东西时突然改穿透位）。"""
    fake, q = make_fake(visible=False)
    TranslatorApp._on_mouse_move(fake, 300, 120)
    print(f"[4] 面板不可见：投递 {q.qsize()} 次（应为 0）")
    return q.qsize() == 0


def hover_rearm_test() -> bool:
    """唤醒之后又变回穿透（比如过了 30 秒）——再次移入必须能再唤醒一次。"""
    fake, q = make_fake()
    TranslatorApp._on_mouse_move(fake, 300, 120)      # 第一次：投递
    q.queue.clear()
    # 面板恢复可交互（hover_wake 之后），钩子重新 arm
    fake.result.click_through = lambda: False
    TranslatorApp._on_mouse_move(fake, 300, 120)
    fake.result.click_through = lambda: True          # 又变穿透
    TranslatorApp._on_mouse_move(fake, 300, 120)
    print(f"[5] 再次移入：投递 {q.qsize()} 次（应为 1）")
    return q.qsize() == 1


def apply_hover_test() -> bool:
    """主线程侧：面板可见才真的改穿透位。"""
    woken: list[str] = []
    fake = types.SimpleNamespace(
        result=types.SimpleNamespace(
            visible=lambda: True,
            hover_wake=lambda: woken.append("wake"),
        )
    )
    TranslatorApp._apply_panel_hover(fake)
    hidden = types.SimpleNamespace(
        result=types.SimpleNamespace(
            visible=lambda: False,
            hover_wake=lambda: woken.append("wake"),
        )
    )
    TranslatorApp._apply_panel_hover(hidden)
    print(f"[6] hover_wake 调用 {len(woken)} 次（应为 1，只对可见面板）")
    return len(woken) == 1


def action_zone_test() -> bool:
    """可操作区 = 面板内减去正文区。用缓存矩形做纯算数判断。"""
    ns = types.SimpleNamespace(
        _body_rect=BODY,
        fast_contains=lambda x, y: (
            PANEL[0] <= x <= PANEL[0] + PANEL[2] and PANEL[1] <= y <= PANEL[1] + PANEL[3]
        ),
    )
    cases = [
        ("标题带（面板内、正文外）", (300, 120), True),
        ("正文区中间", (300, 260), False),
        ("面板外", (900, 900), False),
    ]
    ok = True
    for i, (tag, pos, want) in enumerate(cases, start=7):
        got = ResultWindow.action_zone_contains(ns, *pos)
        print(f"[{i}] {tag}：可操作={got}（应为 {want}）")
        ok = ok and got == want
    # 没有缓存（面板刚建、还没量过）时保守当成"可操作"，宁可多给一次机会
    ns._body_rect = None
    got = ResultWindow.action_zone_contains(ns, 300, 260)
    print(f"[10] 无正文缓存：可操作={got}（应为 True）")
    return ok and got is True


def touch_start_point_test() -> bool:
    """touch() 必须把可交互窗口推到 30 秒后，而不是旧的 6 秒。"""
    import time

    applied: list[bool] = []
    ns = types.SimpleNamespace(
        cfg={"ui": {}},
        _click_through=True,
        _interactive_until=0.0,
        apply_click_through=lambda e: applied.append(e),
    )
    before = time.time()
    ResultWindow.touch(ns)
    span = ns._interactive_until - before
    print(f"[11] touch() 可交互窗口 = {span:.1f}s（应为 {INTERACTIVE_SECONDS:.0f}s）")
    if span < INTERACTIVE_SECONDS - 1:
        return False
    if applied != [False]:
        print(f"     穿透应被关掉一次，实际 {applied}")
        return False
    return True


def touch_respects_manual_test() -> bool:
    """用户勾了「始终穿透」就别自作主张恢复可点。"""
    applied: list[bool] = []
    ns = types.SimpleNamespace(
        cfg={"ui": {"panel_click_through": True}},
        _click_through=True,
        _interactive_until=0.0,
        apply_click_through=lambda e: applied.append(e),
    )
    ResultWindow.touch(ns)
    print(f"[12] 手动始终穿透：apply_click_through 调用 {applied}（应为 []）")
    return applied == []


def finish_touches_test() -> bool:
    """结果出现的这一刻要重置可交互窗口。

    12.3s 是图片翻译真机实测耗时（tests/test_vision_e2e.py）；6 秒窗口从
    open() 算起的话，结果一到就已经是穿透状态了。
    """
    calls: list[int] = []
    ns = types.SimpleNamespace(
        set_status=lambda t: None,
        touch=lambda: calls.append(1),
        _schedule_fit=lambda force=False: None,
    )
    ResultWindow.finish(ns, 12.3, 100)
    print(f"[13] finish() 调 touch {len(calls)} 次（应为 1）")
    return len(calls) == 1


def real_zone_test() -> bool:
    """真实窗口：正文区矩形必须落在面板内，标题带必须判为可操作。

    这一步防的是"坐标空间搞错"——钩子给的屏幕坐标和 Tk 的 winfo_rootx
    必须站在同一套坐标系里，否则悬停唤醒会永远不触发（或乱触发）。
    """
    import tkinter as tk
    import time

    from app import ui

    cfg = {"ui": {"font_family": "Microsoft YaHei UI", "font_size": 11,
                  "popup_width": 520, "popup_height": 280}}
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"[14] 无显示环境，跳过真实窗口检查：{exc}")
        return True
    try:
        root.withdraw()
        win = ui.ResultWindow(root, cfg, style="leaf", bg_gif=None)
        win.open(120, 120, "测试选区", "正在翻译…")
        win.set_final("这是一段用于测量的正文，够长到能撑出几行高度。" * 6)
        for _ in range(40):
            root.update()
            time.sleep(0.02)
        win.refresh_zones()
        rect = win._body_rect
        if not rect:
            print("[14] 真实窗口：正文矩形没有刷新出来（应非空）")
            return False
        bx, by, bw, bh = rect
        px, py, pw, ph = win._geom_cache
        inside = (px <= bx and bx + bw <= px + pw and py <= by and by + bh <= py + ph)
        print(f"[14] 正文矩形 {rect} 在面板 {win._geom_cache} 内：{inside}（应为 True）")
        if not inside or bw < 40 or bh < 20:
            return False
        # 正文中心点：不可操作
        cx, cy = bx + bw // 2, by + bh // 2
        body_ok = win.action_zone_contains(cx, cy) is False
        # 标题带的点（面板顶边往下一点）：可操作
        title_ok = win.action_zone_contains(px + pw // 2, py + 8) is True
        print(f"[15] 正文中心可操作={not body_ok and '?'} → {win.action_zone_contains(cx, cy)}"
              f"（应 False）  标题带 → {win.action_zone_contains(px + pw // 2, py + 8)}（应 True）")
        win.touch()
        touched = win._interactive_until - time.time()
        print(f"[16] touch 后可交互剩余 {touched:.1f}s（应≈{INTERACTIVE_SECONDS:.0f}s）")
        win.close()
        return body_ok and title_ok and touched > INTERACTIVE_SECONDS - 2
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def queue_wiring_test() -> bool:
    """钩子投递的事件名必须真的被主线程的分发器认出来。

    这条看着简单，但"事件名拼错/忘了加分支"的后果是悬停完全静默失效，
    而且没有任何报错——只能在真机上表现为"还是要点两下"。
    """
    called: list[str] = []
    ns = types.SimpleNamespace(_epoch=0, _apply_panel_hover=lambda: called.append("hover"))
    TranslatorApp._handle(ns, ("panel_hover",))
    print(f"[17] 分发 panel_hover：处理 {len(called)} 次（应为 1）")
    return called == ["hover"]


def main() -> int:
    ok = (hover_dedup_test() and hover_body_test() and hover_when_interactive_test()
          and hover_hidden_test() and hover_rearm_test() and apply_hover_test()
          and action_zone_test() and touch_start_point_test()
          and touch_respects_manual_test() and finish_touches_test()
          and queue_wiring_test() and real_zone_test())
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
