"""量：真实鼠标拖动 + 流式长高时，背景层窗口「落后」面板多少像素、落后多久。

之前几轮探针都是在事件处理完之后读矩形，读到的是两个窗口都已经落到位的状态，
所以一直是 0px。用户的反馈说明这是**瞬时**错位：拖动中能看见，松手就恢复。
那就必须用一个独立线程高频采样，才能抓到中间态。

做法：
  - 主线程跑真实 Tk 事件循环（就是线上那条路径）；
  - 一个线程用 SetCursorPos + mouse_event 真的按住标题带拖；
  - 另一个线程每 2ms 读两个窗口的 GetWindowRect，记录所有不一致的样本。
"""
import ctypes
import os
import sys
import threading
import time
import tkinter as tk
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
u32.SetProcessDpiAwareness = getattr(u32, "SetProcessDpiAwareness", None)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

CHUNK = ("纳西妲是须弥的草神，掌管智慧与梦境。她喜欢在净善宫外的草地上晒太阳，"
         "也喜欢听旅人讲外面的故事。遇到解不开的难题时，她会歪着头想很久。")
LINES = [
    "这是第一段译文，用来让面板一点点长高。",
    "第二段：矩阵 $\\begin{bmatrix} 1 & 0 \\\\ 0 & 1 \\end{bmatrix}$ 混排。",
    "第三段：再补一些文字，让自动重排真的去改高度。",
    "第四段：拖动过程中面板仍在流式变高，这是最容易错位的时刻。",
    "第五段：继续追加内容。",
]


def rect(hwnd):
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def hwnd_of(win):
    return u32.GetAncestor(win.winfo_id(), 2)


def main() -> int:
    if not os.path.exists(GIF):
        print("缺少背景动图，跳过")
        return 0

    root = tk.Tk()
    root.withdraw()
    win = ui.ResultWindow(root, CFG, style="leaf", bg_gif=GIF)
    win.open(1180, 420, "截图 900×600", "图片识别（CPU）·正在读图…")
    root.update()
    win.append(LINES[0])
    root.update()
    win.finish(3.2, 128)

    bg = win.bg
    hp, hb = hwnd_of(win.win), hwnd_of(bg.win)
    print("面板 hwnd=%s  背景 hwnd=%s" % (hp, hb))
    print("起始矩形  面板=%s  背景=%s" % (rect(hp), rect(hb)))

    samples = []
    stop = threading.Event()

    def sampler():
        # 高频采样：只读 GetWindowRect，不碰 Tk
        while not stop.is_set():
            try:
                samples.append((time.perf_counter(), rect(hp), rect(hb)))
            except Exception:
                pass
            time.sleep(0.002)

    th = threading.Thread(target=sampler, daemon=True)

    # ---- 模拟流式翻译：每 80ms 追加一段并重排（线上就是这条路径）----
    state = {"i": 1}

    def stream():
        if state["i"] >= len(LINES):
            return
        win.append(LINES[state["i"]])
        win._fit()
        state["i"] += 1
        root.after(80, stream)

    # ---- 真实鼠标拖动：按住标题带，分 40 步走 300px ----
    def drag():
        x0, y0, w, _h = rect(hp)
        sx, sy = x0 + 250, y0 + 20
        try:
            u32.SetForegroundWindow(hp)
        except Exception:
            pass
        u32.SetCursorPos(sx, sy)
        time.sleep(0.05)
        u32.mouse_event(0x0002, 0, 0, 0, 0)  # 左键按下
        time.sleep(0.05)
        for i in range(40):
            u32.SetCursorPos(sx + int(300 * (i + 1) / 40), sy + int(150 * (i + 1) / 40))
            time.sleep(0.012)
        time.sleep(0.05)
        u32.mouse_event(0x0004, 0, 0, 0, 0)  # 松开

    th.start()
    root.after(100, stream)
    root.after(200, lambda: threading.Thread(target=drag, daemon=True).start())
    root.after(1800, root.quit)
    root.mainloop()
    time.sleep(0.15)  # 松手后再采一小段，看是否自己恢复
    stop.set()
    th.join(timeout=1.0)
    print("拖动结束后 _drag_active=%s  _x=%s" % (
        getattr(win, "_drag_active", None), getattr(win, "_x", None)))

    # ---- 统计 ----
    bad = [(t, p, b) for (t, p, b) in samples if p != b]
    dur = [(t, p, b) for (t, p, b) in samples if (p[2], p[3]) != (b[2], b[3])]
    print("\n样本 %d 个，其中不一致 %d 个（%.1f%%）" % (
        len(samples), len(bad), 100.0 * len(bad) / max(1, len(samples))))
    if samples:
        t_end = samples[-1][0]
        t0 = samples[0][0]
        print("采样窗口 %.0f ms" % ((t_end - t0) * 1000))
    if bad:
        dx = max(abs(p[0] - b[0]) for _, p, b in bad)
        dy = max(abs(p[1] - b[1]) for _, p, b in bad)
        print("最大位置差：dx=%dpx dy=%dpx" % (dx, dy))
        print("尺寸不一致样本 %d 个，最大 Δw=%d Δh=%d" % (
            len(dur),
            max([abs(p[2] - b[2]) for _, p, b in dur] or [0]),
            max([abs(p[3] - b[3]) for _, p, b in dur] or [0]),
        ))
        # 连续不一致的最长持续时长
        runs, cur = [], None
        for t, p, b in samples:
            if p != b:
                cur = [t, t] if cur is None else [cur[0], t]
            elif cur is not None:
                runs.append(cur)
                cur = None
        if cur is not None:
            runs.append(cur)
        longest = max((r[1] - r[0]) for r in runs) if runs else 0
        print("最长连续不一致 %.0f ms（约 %d 帧@16ms）" % (longest * 1000, longest * 60))
        print("\n前 12 个不一致样本（t_rel_ms  面板  背景  Δx Δy）：")
        t0 = samples[0][0]
        for t, p, b in bad[:12]:
            print("  %7.1f  %-24s %-24s  %+d %+d" % (
                (t - t0) * 1000, p, b, p[0] - b[0], p[1] - b[1]))
    else:
        print("全程无错位样本 —— 这条路径下两个窗口是原子的")

    try:
        win.close()
        root.destroy()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
