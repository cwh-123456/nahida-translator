"""面板和背景动图必须"一起动"——用系统矩形验证，不信 Tk 的记账。

用户三次反馈同一件事：图片翻译时拖面板，背景动图会落在后面，手一停就
自己追上来。前两轮我量的都是"事件处理完之后"的矩形，读到的永远是对齐的，
所以怎么修都看不到效果。

这轮的结论：Tk 的 geometry() 只记账，真正的 SetWindowPos 排在 idle 队列里。
拖动时每个鼠标事件记一次，快速拖动把队列刷满、idle 轮不上，两个窗口的
SetWindowPos 之间就夹进系统合成的一帧 —— 面板先动、背景后动。

所以这里断言的不是"跑完 idle 之后对齐"，而是**不跑 idle 就已经对齐**：
任一次移动调用返回时，两个窗口在系统里的矩形必须已经等于目标值，且彼此相等。
"""
import ctypes
import os
import sys
import tkinter as tk
import types
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ui

GIF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "assets", "nahida_swing.gif")

CFG = {
    "ui": {
        "style": "leaf",
        "width": 520,
        "min_height": 200,
        "gif": "assets/nahida_swing.gif",
        "panel_auto_passthrough": False,
        "panel_click_through": False,
        "window_pos": [1180, 420],
    }
}

u32 = ctypes.windll.user32


def rect(win):
    hwnd = u32.GetAncestor(win.winfo_id(), 2)
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def make_window(root):
    win = ui.ResultWindow(root, CFG, style="leaf", bg_gif=GIF)
    win.open(1180, 420, "截图 900×600", "图片识别（CPU）·正在读图…")
    root.update()
    win.append("第一段译文，用来让面板有内容。")
    root.update()
    win.finish(3.2, 128)
    return win


def press(win, x=150, y=20):
    return types.SimpleNamespace(x_root=win.win.winfo_x() + x,
                                 y_root=win.win.winfo_y() + y)


def test_drag_is_immediate_and_matched(root) -> bool:
    """一次拖动之后**不跑 idle**：面板和背景都已经在新位置且彼此相等。"""
    win = make_window(root)
    p0 = rect(win.win)
    r0 = rect(win.bg.win)
    win._drag_start(press(win))
    win._drag_move(types.SimpleNamespace(x_root=p0[0] + 260, y_root=p0[1] + 130))
    p1 = rect(win.win)
    r1 = rect(win.bg.win)
    ok = True
    print("[1] 拖动一步（不跑 idle）")
    print("    面板 %s -> %s" % (p0, p1))
    print("    背景 %s -> %s" % (r0, r1))
    if p1 == p0:
        print("    FAIL 面板没有立刻移动：说明还在等 Tk 的 idle")
        ok = False
    if p1 != r1:
        print("    FAIL 面板与背景不同步：Δ=%s" % ([a - b for a, b in zip(p1, r1)],))
        ok = False
    if ok:
        print("    OK 两边同时到位")
    win._drag_end()
    return ok


def test_fast_drag_burst(root) -> bool:
    """连续 200 次拖动、中间一次 idle 都不跑：不允许任何一次落后。"""
    win = make_window(root)
    win._drag_start(press(win))
    base = rect(win.win)
    worst = 0
    bad = 0
    for i in range(200):
        win._drag_move(types.SimpleNamespace(
            x_root=base[0] + i * 3, y_root=base[1] + i * 2))
        p, r = rect(win.win), rect(win.bg.win)
        d = max(abs(a - b) for a, b in zip(p, r))
        worst = max(worst, d)
        if d:
            bad += 1
    last = rect(win.win)
    # 拖动是"光标 - 按下时相对窗口的偏移"，press() 用的是 (150, 20)
    want = (base[0] + 199 * 3 - 150, base[1] + 199 * 2 - 20)
    print("[2] 连续 200 步拖动（不跑 idle）：不一致 %d 次，最大差 %dpx，末位置 %s（应为 %s）"
          % (bad, worst, last[:2], want))
    ok = bad == 0 and last[:2] == want
    print("    %s" % ("OK" if ok else "FAIL 有一步落后"))
    win._drag_end()
    return ok


def test_fit_deferred_while_dragging(root) -> bool:
    """拖动中不做重排（会打断原子移动），松手后补上。"""
    win = make_window(root)
    h0 = rect(win.win)[3]
    win._drag_start(press(win))
    win._drag_move(types.SimpleNamespace(x_root=rect(win.win)[0] + 20,
                                        y_root=rect(win.win)[1] + 10))
    for _ in range(6):
        win.append("再加一段文字，让自动重排真的想去改高度。")
    win._fit()
    h_drag = rect(win.win)[3]
    deferred = bool(getattr(win, "_fit_deferred", False))
    print("[3] 拖动中 _fit：高度 %d -> %d，推迟标记=%s" % (h0, h_drag, deferred))
    win._drag_end()
    root.update()          # 松手后 80ms 的补排程
    for _ in range(6):
        win.append("松手之后继续追加，让高度追上来。")
    win._fit()
    h_end = rect(win.win)[3]
    print("    松手后高度 = %d，两窗口一致 = %s" % (
        h_end, rect(win.win) == rect(win.bg.win)))
    ok = deferred and h_drag == h0 and h_end > h0 and rect(win.win) == rect(win.bg.win)
    print("    %s" % ("OK" if ok else "FAIL"))
    return ok


def test_resize_keeps_pair(root) -> bool:
    """真实长高之后（不跑 idle），两个窗口的尺寸也一致。"""
    win = make_window(root)
    before = rect(win.win)
    for _ in range(8):
        win.append("这一段是为了把面板撑高：一行两行三行四行五行六行七行的文字量。")
    win._fit()
    p, r = rect(win.win), rect(win.bg.win)
    print("[4] 长高：面板 %s -> %s，背景 %s" % (before, p, r))
    ok = p[3] > before[3] and p == r
    print("    %s" % ("OK" if ok else "FAIL 尺寸没同时到位"))
    return ok


def test_zorder_kept(root) -> bool:
    """原子移动不能顺手改层级：背景必须还在面板下面。"""
    win = make_window(root)
    win._drag_start(press(win))
    for i in range(8):
        win._drag_move(types.SimpleNamespace(x_root=rect(win.win)[0] + 4,
                                            y_root=rect(win.win)[1] + 3))
    broken = win.bg_order_broken()
    win._drag_end()
    print("[5] 拖动 8 步后背景层级被翻上去 = %s" % broken)
    ok = not broken
    print("    %s" % ("OK" if ok else "FAIL 背景盖到面板上面了"))
    return ok


def main() -> int:
    if not os.path.exists(GIF):
        print("缺少背景动图，跳过")
        return 0
    root = tk.Tk()
    root.withdraw()
    results = []
    try:
        for fn in (test_drag_is_immediate_and_matched, test_fast_drag_burst,
                   test_fit_deferred_while_dragging, test_resize_keeps_pair,
                   test_zorder_kept):
            try:
                results.append(fn(root))
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print("    异常：%s" % exc)
                results.append(False)
            print()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    ok = all(results)
    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
