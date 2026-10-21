"""框选界面自检：压暗背景 + 选区保持原色，顺便量一下拖动耗时。

跑法：D:/python/python.exe tests/test_shot_dark.py
产物：logs/shotcheck/selector.png
"""
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tkinter as tk  # noqa: E402

from app.shot import RegionSelector  # noqa: E402

OUT = os.path.join(ROOT, "logs", "shotcheck")
os.makedirs(OUT, exist_ok=True)

STATE = {"shots": 0, "problems": []}


def brightness(img) -> float:
    """平均亮度，0-255。只看贴出来的那层，不受窗口叠加影响。"""
    from PIL import ImageStat

    return ImageStat.Stat(img.convert("L")).mean[0]


def check_layers(sel) -> None:
    """量三件事：底图真的压暗了、选区那块仍是原亮度、选框和辅助线在最上层。"""
    order = list(sel.canvas.find_all())
    idx = {item: i for i, item in enumerate(order)}

    ratio = brightness(sel._dimmed) / max(1e-6, brightness(sel._screen))
    print(f"底图亮度比 {ratio:.2f}（期望 ≈{1 - sel.DIM:.2f}）")
    if not (0.35 <= ratio <= 0.55):
        STATE["problems"].append(f"底图没压暗到位: {ratio:.2f}")

    if sel._sel_box is None or sel._sel_photo is None:
        STATE["problems"].append("选区原图没有贴出来")
    else:
        # PhotoImage 不能直接算亮度，回到原屏上裁同一块比
        lx, ty, rx, ry = sel._sel_box
        raw = brightness(sel._screen.crop(sel._sel_box))
        dim = brightness(sel._dimmed.crop(sel._sel_box))
        print(f"选区内 原图 {raw:.0f} / 压暗层 {dim:.0f}")
        if raw < 1 or dim / raw > 0.6:
            STATE["problems"].append("选区内也被压暗了")

    for name, item in (("选框", sel._rect), ("提示底", sel._hint_bg),
                       ("提示文字", sel._hint)):
        if idx.get(item, -1) < idx.get(sel._sel_item, 0):
            STATE["problems"].append(f"{name}被选区原图盖住了")
    for i, line in enumerate(sel._cross):
        if idx.get(line, -1) < idx.get(sel._sel_item, 0):
            STATE["problems"].append(f"辅助线{i}被选区原图盖住了")


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    sel = RegionSelector(root)

    def ev(x, y, widget):
        return types.SimpleNamespace(x=x, y=y, widget=widget)

    def drive():
        # 模拟一次从 (140,140) 拖到 (900,560) 的框选
        sel._on_press(ev(140, 140, sel.canvas))
        for step in range(1, 13):
            sel._on_move(ev(140 + step * 63, 140 + step * 35, sel.canvas))
            root.update()
        check_layers(sel)
        root.after(180, grab)

    def grab():
        from PIL import ImageGrab

        full = ImageGrab.grab(all_screens=True)
        k = full.width / float(root.winfo_screenwidth())
        x0, y0 = sel.win.winfo_rootx(), sel.win.winfo_rooty()
        box = (int(x0 * k), int(y0 * k),
               int((x0 + sel._w) * k), int((y0 + sel._h) * k))
        shot = full.crop(box)
        # 整屏太大，截选区附近一块就够看
        shot.save(os.path.join(OUT, "_full_selector.png"))
        sel._cancel()

    root.after(500, drive)
    result = sel.select()          # 阻塞到 _cancel

    # 第二轮：只量性能
    sel2 = RegionSelector(root)
    def start2():
        sel2._on_press(ev(150, 150, sel2.canvas))
        root.after(120, lambda: perf2(sel2))

    def perf2(s):
        t0 = time.perf_counter()
        for i in range(30):
            s._on_move(ev(200 + i * 20, 200 + i * 12, s.canvas))
            root.update()
        dt = (time.perf_counter() - t0) / 30 * 1000
        print(f"拖动一帧 {dt:.1f} ms（含 PhotoImage 重建 + 重绘）")
        s._cancel()

    root.after(500, start2)
    sel2.select()

    print("第一轮返回:", result)
    ok = os.path.exists(os.path.join(OUT, "_full_selector.png")) and not STATE["problems"]
    for msg in STATE["problems"]:
        print("  ✗", msg)
    print("结果:", "PASS" if ok else "FAIL")
    root.destroy()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
