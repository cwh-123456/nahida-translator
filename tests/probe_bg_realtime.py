"""按真实节奏模拟"流式翻译 + 拖动面板"，逐帧量两个真实窗口的矩形。

不只看 Python 里的数字：用 GetWindowRect 读 Windows 实际几何，
每帧比对面板和背景层。这样才能区分两种情况：
  a) 两个窗口真的错位/错尺寸（几何问题）
  b) 几何一致，但帧图内容在跳（cover 裁切随高度变化）
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tkinter as tk  # noqa: E402

from app import ui  # noqa: E402

GIF = os.path.join(ROOT, "assets", "nahida_swing.gif")
CFG = {"ui": {"font_family": "Microsoft YaHei UI", "font_size": 11,
              "popup_width": 520, "popup_height": 300}}
CHUNK = "这是一段流式译文，每来一段面板就会长高一点。" * 2

u32 = ctypes.windll.user32


def rect(win) -> tuple[int, int, int, int] | None:
    try:
        hwnd = u32.GetAncestor(win.winfo_id(), 2)
        box = ctypes.wintypes.RECT() if hasattr(ctypes, "wintypes") else None
    except Exception:
        return None
    import ctypes.wintypes as wt

    box = wt.RECT()
    if not hwnd or not u32.GetWindowRect(hwnd, ctypes.byref(box)):
        return None
    return (box.left, box.top, box.right - box.left, box.bottom - box.top)


def main() -> int:
    if not os.path.exists(GIF):
        print("没有动图，跳过")
        return 0
    root = tk.Tk()
    root.withdraw()
    win = ui.ResultWindow(root, CFG, style="leaf", bg_gif=GIF)
    win.open(120, 120, "截图 757×267", "图片识别（CPU）·正在读图…")
    for _ in range(20):
        root.update()
        time.sleep(0.01)
    bg = win.bg

    worst = {"size": 0, "pos": 0}
    rows = []
    # 拖动头部：模拟真实拖动（按下 → 连续 move）
    win._drag_start(types.SimpleNamespace(x_root=200, y_root=160))
    for i in range(40):
        # 每 6 帧来一段新译文（真实流式：80ms 一次重排）
        if i % 6 == 0:
            win.append(CHUNK)
        # 拖动事件：真实是随鼠标来的，这里自己算
        move = types.SimpleNamespace(x_root=200 + i * 9, y_root=160 + i * 5)
        win._drag_move(move)
        root.update()                     # 让 Tk 本帧把两个窗口都挪好
        time.sleep(0.012)
        pr, br = rect(win.win), rect(bg.win)
        if not pr or not br:
            continue
        dp = (abs(br[0] - pr[0]), abs(br[1] - pr[1]))
        ds = (abs(br[2] - pr[2]), abs(br[3] - pr[3]))
        worst["pos"] = max(worst["pos"], *dp)
        worst["size"] = max(worst["size"], *ds)
        rows.append((i, pr, br, dp, ds))

    print("帧  面板(x,y,w,h)                背景(x,y,w,h)                Δ位置  Δ尺寸")
    for i, pr, br, dp, ds in rows[::4]:
        print(f"{i:<3} {str(pr):<28} {str(br):<28} {dp}  {ds}")
    print(f"\n最大位置差 {worst['pos']}px  最大尺寸差 {worst['size']}px")

    win._drag_end()
    for _ in range(10):
        root.update()
        time.sleep(0.012)
    pr, br = rect(win.win), rect(bg.win)
    print(f"拖动结束后：面板 {pr} 背景 {br}")
    ok = worst["pos"] <= 1 and worst["size"] <= 1
    print("结果:", "PASS" if ok else "FAIL")
    win.close()
    root.destroy()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
