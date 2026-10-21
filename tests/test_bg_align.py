"""背景层必须始终贴着面板：错位能被巡检发现并修回来。

用户报的现象：图片翻译过程中拖面板，面板走了、动图留在原地，而且**再也
不回来**。这一组用例钉住两条性质：

1. 只要系统里两个窗口的矩形不一致，`keep_bg_aligned()` 必须发现并修好；
2. 没有"按下"的移动事件绝不能拖动面板（否则穿透漏掉按下时面板会自己跳走）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
import tkinter as tk
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as cfgmod  # noqa: E402
from app import ui  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIF = os.path.join(ROOT, "assets", "nahida_swing.gif")
U32 = ctypes.windll.user32

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOSENDCHANGING = 0x0400   # 不派发 WM_WINDOWPOSCHANGING，避免重入 Tk 的回调


def hwnd_of(win):
    return U32.GetAncestor(win.winfo_id(), 2)


def sys_rect(win):
    return ui.sys_rect(win)


def pump(root, ms: int) -> None:
    end = time.time() + ms / 1000.0
    while time.time() < end:
        root.update()
        time.sleep(0.006)


def move_hwnd_only(win, dx: int, dy: int) -> None:
    """只挪系统里的那个窗口，绕过 Tk 的 geometry 记录（模拟"某次没跟上"）。"""
    r = sys_rect(win)
    U32.SetWindowPos(hwnd_of(win), 0, r[0] + dx, r[1] + dy, 0, 0,
                     SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSENDCHANGING)


# ---------------------------------------------------------------- 用例
def repair_after_desync(win, bg, root) -> bool:
    """背景被挪走 192/198 像素：巡检必须发现并贴回去。"""
    rp, rb = sys_rect(win.win), sys_rect(bg.win)
    print(f"  对齐时：面板 {rp} 背景 {rb}")
    if rp != rb:
        print("  起点就不对齐，用例无意义")
        return False

    move_hwnd_only(bg.win, 192, -198)
    pump(root, 80)
    rp, rb = sys_rect(win.win), sys_rect(bg.win)
    print(f"  挪走后：面板 {rp} 背景 {rb}  Tk 记录的背景 x={bg.win.winfo_x()}")
    if rp == rb:
        print("  挪不动（可能被系统拒绝），用例无意义")
        return False
    if not win.keep_bg_aligned():
        print("  巡检没发现错位 → 失败")
        return False
    pump(root, 60)
    rp, rb = sys_rect(win.win), sys_rect(bg.win)
    print(f"  修完之后：面板 {rp} 背景 {rb}")
    return rp == rb


def repair_size_mismatch(win, bg, root) -> bool:
    """背景不只挪了位置，尺寸也不对（例如卡在旧的动画帧尺寸）。"""
    rp = sys_rect(win.win)
    U32.SetWindowPos(hwnd_of(bg.win), 0, rp[0] - 40, rp[1] + 90, rp[2] + 30,
                     rp[3] - 20,
                     SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSENDCHANGING)
    pump(root, 80)
    before = sys_rect(bg.win)
    if before == rp:
        print("  挪不动，用例无意义")
        return False
    ok = win.keep_bg_aligned()
    pump(root, 60)
    after = sys_rect(bg.win)
    print(f"  尺寸错位：修前 {before} → 修后 {after}（面板 {rp}）")
    return ok and after == rp


def no_churn_when_aligned(win, bg, root) -> bool:
    """已经对齐时巡检不能乱动窗口（每 250ms 抖一次会看得见）。"""
    if sys_rect(win.win) != sys_rect(bg.win):
        win.keep_bg_aligned()
        pump(root, 40)
    first = win.keep_bg_aligned()
    second = win.keep_bg_aligned()
    moved = sys_rect(win.win) != sys_rect(bg.win)
    print(f"  对齐状态下连调两次：{first}, {second}（应都是 False），错位={moved}")
    return (not first) and (not second) and (not moved)


def drag_needs_press(win, bg, root) -> bool:
    """没有按下的 B1-Motion 不许拖动面板（残留的 _drag_dx 会把面板甩走）。"""
    win._drag_dx, win._drag_dy = 300, 200      # 上一次拖动留下的残值
    win._drag_active = False
    before = sys_rect(win.win)
    for i in range(5):
        win._drag_move(types.SimpleNamespace(x_root=before[0] + 500 + i * 30,
                                            y_root=before[1] + 400 + i * 30))
    pump(root, 60)
    after = sys_rect(win.win)
    print(f"  无按下连拖 5 次：{before} → {after}")
    return before == after


def drag_after_press(win, bg, root) -> bool:
    """正常按下+移动仍然要能动，并且背景跟着走。"""
    before = sys_rect(win.win)
    press = types.SimpleNamespace(x_root=before[0] + 200, y_root=before[1] + 22)
    win._drag_start(press)
    for i in range(6):
        win._drag_move(types.SimpleNamespace(x_root=press.x_root - i * 15,
                                            y_root=press.y_root - i * 15))
        pump(root, 20)
    after, bg_rect = sys_rect(win.win), sys_rect(bg.win)
    win._drag_end()
    pump(root, 60)
    print(f"  按下后拖动：{before} → {after}，背景 {bg_rect}")
    return after[0] < before[0] and bg_rect == after


def main() -> int:
    if not os.path.exists(GIF):
        print(f"缺少背景动图 {GIF}，跳过")
        return 0
    cfg = cfgmod.load_config()
    root = tk.Tk()
    root.withdraw()
    win = ui.ResultWindow(root, cfg, style="leaf", bg_gif=GIF)
    win.open(200, 160, "截图 768×269", "正在识别图片…")
    pump(root, 300)
    bg = win.bg
    if bg is None or bg.win is None:
        print("背景层没建起来（配了 panel_bg=false？）")
        return 1
    print("窗口：面板 %s 背景 %s\n" % (sys_rect(win.win), sys_rect(bg.win)))

    checks = [
        ("对齐时巡检不乱动", no_churn_when_aligned(win, bg, root)),
        ("位置错位能修回来", repair_after_desync(win, bg, root)),
        ("尺寸错位能修回来", repair_size_mismatch(win, bg, root)),
        ("无按下不许拖动", drag_needs_press(win, bg, root)),
        ("按下后能正常拖", drag_after_press(win, bg, root)),
    ]
    print()
    ok = True
    for name, got in checks:
        print(f"[{'PASS' if got else 'FAIL'}] {name}")
        ok = ok and got
    print("\n结果:", "PASS" if ok else "FAIL")
    try:
        win.close()
        root.destroy()
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
