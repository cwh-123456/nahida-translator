"""面板与背景层的层级不变量：背景动图不许跑到面板上面。

用户报的现象是「点叉后只剩下了背景动图」。面板其实没关、也没隐藏，
只是背景层（实心不透明）翻到了它上面——`winfo_ismapped()` 依然是 True，
所以只看 Tk 自己的状态永远发现不了，必须问系统 z-order，或者量像素。

测试步骤（每一步都验"用户能不能看见面板"，而不只是标志位）：
  1. 正常状态不误报
  2. 人为把背景层抬到面板上面（等价于系统点击后重排的结果）：检测要报出来，
     并且那一块画面确实不再是面板
  3. 修复之后画面要真的变回面板
  4. 正常时不许反复修（否则每 250ms 抬一次，动图会抖）
  5. 背景层隐藏时不许误报
  6. wake()：穿透状态下点面板 → 恢复可交互
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import ImageGrab  # noqa: E402

from app import config  # noqa: E402
from app.ui import ResultWindow  # noqa: E402

OUT = os.path.join(ROOT, "logs", "panelcheck")


def pump(root, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def dark_ratio(x: int, y: int, w: int, h: int) -> float:
    """截屏后量「暗像素」占比。

    标题带是深林绿渐变（亮度 30~60），背景动图是亮色粉彩（亮度 180+），
    普通程序窗口是浅灰/白。面板在上面时这一条几乎全是暗的；
    被任何东西盖住时都不是。比读标志位直接——用户看到的就是这个。
    """
    full = ImageGrab.grab(all_screens=True)
    k = full.width / float(ctypes.windll.user32.GetSystemMetrics(0))
    gray = full.crop((int(x * k), int(y * k),
                      int((x + w) * k), int((y + h) * k))).convert("L")
    hist = gray.histogram()
    return sum(hist[:90]) / float(gray.width * gray.height)


def shot(name: str, x: int, y: int, w: int, h: int) -> None:
    full = ImageGrab.grab(all_screens=True)
    k = full.width / float(ctypes.windll.user32.GetSystemMetrics(0))
    full.crop((int(x * k), int(y * k),
               int((x + w) * k), int((y + h) * k))).save(os.path.join(OUT, name))


def main() -> int:
    cfg = config.load_config()
    root = tk.Tk()
    root.withdraw()
    gif = config.abspath(cfg["ui"].get("mascot_gif", r"assets\nahida_swing.gif"))
    if not os.path.exists(gif):
        print("缺少背景动图，跳过")
        return 0
    panel = ResultWindow(root, cfg, bg_gif=gif)
    panel.open(0, 0, "截图 766×268", "正在识别图片…")
    pump(root, 0.8)
    panel.set_final("矩阵 $A$ 满足 $\\lambda^2-5\\lambda+6=0$，所以 $A$ 可对角化。")
    panel.finish(1.0, 10)
    pump(root, 0.8)
    x, y, w, h = panel._geom_cache
    ok = True

    # 先把面板抬到最前，量一个"面板确实可见"的基准值
    panel._raise_self()
    pump(root, 0.5)
    base = dark_ratio(x, y + 4, w - 8, 30)
    print(f"[0] 面板置前时标题带暗像素占比={base:.2f}（应 >0.6，说明量的是面板）")
    ok &= base > 0.6
    ok &= not panel.bg_order_broken()

    # 人为制造故障：等价于"系统把背景层排到了面板前面"
    panel.bg.win.lift(panel.win)
    pump(root, 0.4)
    broken = panel.bg_order_broken()
    covered = dark_ratio(x, y + 4, w - 8, 30)
    shot("order_broken.png", x, y, w, 200)
    print(f"[1] 把背景层抬上去：检测到={broken}（应为 True）  "
          f"标题带暗像素占比={covered:.2f}（应 <0.3，面板被盖住了）")
    ok &= broken and covered < 0.3

    fixed = panel.keep_bg_below()
    pump(root, 0.4)
    back = dark_ratio(x, y + 4, w - 8, 30)
    shot("order_fixed.png", x, y, w, 200)
    print(f"[2] 调用 keep_bg_below：修过了={fixed}  现在仍被盖住={panel.bg_order_broken()}")
    print(f"    标题带暗像素占比={back:.2f}（应回到 >0.6，面板真的露出来了）")
    ok &= fixed and not panel.bg_order_broken() and back > 0.6

    again = panel.keep_bg_below()
    print(f"[3] 再调一次：修过了={again}（应为 False，正常时不该动）")
    ok &= not again

    panel.bg.hide()
    pump(root, 0.3)
    hid = panel.bg_order_broken()
    print(f"[4] 背景层隐藏时：检测到={hid}（应为 False，不许误报）")
    ok &= not hid

    panel.bg.show(x, y, w, h)
    pump(root, 0.3)
    panel.apply_click_through(True)
    panel.wake()
    pump(root, 0.4)
    print(f"[5] wake() 后：穿透={panel.click_through()}（应为 False） "
          f"被盖住={panel.bg_order_broken()}")
    ok &= not panel.click_through() and not panel.bg_order_broken()

    # 落点判定：标题带上的点击算"操作面板"，正文上的算"要选下面的字"
    head = panel.click_in_body(x + w // 2, y + 18)
    body = panel.click_in_body(x + w // 2, y + 160)
    print(f"[6] 落点判定：标题带算正文={head}（应 False）  正文算正文={body}（应 True）")
    ok &= (not head) and body

    print("\n结果:", "PASS" if ok else "FAIL")
    root.destroy()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
