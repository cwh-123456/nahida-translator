"""背景动图必须和面板同步（尤其是"翻译进行中拖动"这个场景）。

症状：图片翻译进行中拖动面板，能明显看到动图比面板慢一拍、边缘露出错位；
翻译完成后就不会。原因是背景层的帧图只在动画 tick（≥70ms）里渲染，
而 `_fit()` 每次内容变高都会立刻改窗口尺寸，改完到下一次 tick 之间，
标签里还是**旧尺寸的图**——图被居中放在新尺寸的窗口里，看起来就是偏移。

判定方法：任何时候（包括刚改完几何、动画还没 tick）背景层里那张图的像素尺寸
都必须等于它的目标尺寸。
"""
from __future__ import annotations

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


def pump(root, n: int = 20) -> None:
    for _ in range(n):
        root.update()
        time.sleep(0.01)


def img_size(bg) -> tuple[int, int] | None:
    photo = getattr(bg, "_photo", None)
    if photo is None:
        return None
    return (photo.width(), photo.height())


def content_stability_test(root) -> bool:
    """面板长高时，背景里"原本看得见的那块"必须原地不动。

    这是用户真正看到的性质。之前裁切是**居中**取的，面板每长高 Δh，整张动图就
    往上滑 Δh/2——流式翻译每 80ms 长一次，看到的就是"背景自己在往上爬"。
    实测：300→402 时滑了 51px，正好是高度差的一半。
    """
    from PIL import ImageChops, ImageStat

    bg = ui.PanelBackground(root, GIF)
    bg._load()
    bg._build()
    bg.show(100, 100, 520, 300)
    root.update()
    ok = True
    for h1, h2 in ((300, 402), (402, 470), (300, 520)):
        a = bg._compose(520, h1)
        b = bg._compose(520, h2)
        gap = ImageStat.Stat(
            ImageChops.difference(a, b.crop((0, 0, 520, h1)))
        ).mean[0]
        tag = "OK" if gap < 1.0 else "内容位移"
        print(f"[E] {h1}→{h2}：顶部 {h1} 行平均像素差 {gap:.2f}/255  {tag}")
        ok = ok and gap < 1.0
    # 面板高过宽度之后 cover 会开始放大（scale 跟着高度走），这里只用来说明，
    # 不算失败——正常译文很少长到这个程度
    tall = ImageStat.Stat(ImageChops.difference(
        bg._compose(520, 520), bg._compose(520, 640).crop((0, 0, 520, 520))
    )).mean[0]
    print(f"[E] 520→640（超高，cover 开始缩放）：{tall:.1f}/255（已知残留）")
    bg.destroy()
    return ok


def main() -> int:
    if not os.path.exists(GIF):
        print(f"缺少背景动图 {GIF}，跳过")
        return 0
    root = tk.Tk()
    root.withdraw()
    win = ui.ResultWindow(root, CFG, style="leaf", bg_gif=GIF)
    win.open(120, 120, "截图 757×267", "图片识别（CPU）·正在读图…")
    pump(root, 30)

    bg = win.bg
    if bg is None or bg.win is None:
        print("背景层没有建起来（配置里 gif 关掉了？）")
        return 1

    ok = True
    worst = 0
    # ---- 场景 A：翻译进行中，内容一段段变高（不拖动）----
    for i in range(6):
        before = bg._target
        win.append(CHUNK)
        win._fit()                     # 等价于 after(80) 里那次重排
        got, want = img_size(bg), bg._target
        dy = 0 if got is None else abs(got[1] - want[1])
        worst = max(worst, dy)
        tag = "OK" if got == want else "错位"
        print(f"[A{i + 1}] 目标 {want}  帧图 {got}  Δh={dy}  {tag}")
        ok = ok and got == want
        if want == before:
            print("      （这一步高度没变，跳过也没关系）")

    # ---- 场景 B：翻译进行中拖动面板（动画被 pause 了）----
    press = types.SimpleNamespace(x_root=200, y_root=160)
    win._drag_start(press)
    for i in range(5):
        move = types.SimpleNamespace(x_root=200 + i * 37, y_root=160 + i * 23)
        win._drag_move(move)           # 面板移动 + 背景跟着移动
        pos_got = (bg._geom[0], bg._geom[1])
        pos_want = (win._geom_cache[0], win._geom_cache[1])
        if pos_got != pos_want:
            print(f"[B{i + 1}] 位置不同步：背景 {pos_got} / 面板 {pos_want}")
            ok = False
        win.append(CHUNK)
        win._fit()                     # 拖动过程中内容还在流式变高
        got, want = img_size(bg), bg._target
        dy = 0 if got is None else abs(got[1] - want[1])
        worst = max(worst, dy)
        tag = "OK" if got == want else "错位"
        print(f"[B{i + 1}] 位置 {pos_got}  目标 {want}  帧图 {got}  Δh={dy}  {tag}")
        ok = ok and got == want

    win._drag_end()
    pump(root, 20)
    got, want = img_size(bg), bg._target
    print(f"[C] 拖动结束后 tick 恢复：目标 {want} 帧图 {got}")
    ok = ok and got == want

    win.close()
    ok = content_stability_test(root) and ok

    print(f"\n最大高度错位 {worst}px")
    print("结果:", "PASS" if ok else "FAIL")
    try:
        root.destroy()
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
