"""面板冒烟测试：中性配色 + 矩阵公式混排。

跑法：
    D:/python/python.exe tests/test_panel_style.py
产物：logs/panelcheck/light.png、leaf.png
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tkinter as tk  # noqa: E402

from app import ui  # noqa: E402

OUT = os.path.join(ROOT, "logs", "panelcheck")
os.makedirs(OUT, exist_ok=True)

TEXT = (
    r"20 $Sy=\begin{bmatrix}1&0&0\\1&1&0\\1&1&1\end{bmatrix}"
    r"\begin{bmatrix}y_1\\y_2\\y_3\end{bmatrix}="
    r"\begin{bmatrix}c_1\\c_2\\c_3\end{bmatrix}$ 的解为 "
    r"$y=\begin{bmatrix}c_1\\c_2-c_1\\c_3-c_2\end{bmatrix}$。"
    "\n\n"
    r"$S^{-1}c=\begin{bmatrix}1&0&0\\-1&1&0\\1&-1&1\end{bmatrix}"
    r"\begin{bmatrix}c_1\\c_2\\c_3\end{bmatrix}$。"
    "因为 $S$ 的列线性无关，所以 $S$ 可逆，且 $SS^{-1}=S^{-1}S=I$。"
)

CFG = {
    "ui": {
        "font_family": "Microsoft YaHei UI",
        "font_size": 11,
        "popup_width": 560,
        "popup_height": 300,
    }
}


def pump(root, n: int = 40) -> None:
    for _ in range(n):
        root.update()
        time.sleep(0.02)


def shot(style: str) -> tuple[int, tuple[int, int]]:
    root = tk.Tk()
    root.withdraw()
    win = ui.ResultWindow(root, CFG, style=style)
    win.open(80, 80, "截图 757×267", "图片识别（CPU）·24.0s")
    win.set_final(TEXT)
    pump(root, 60)
    win.finish(24.0, 0)
    pump(root, 40)

    names = win.text.image_names()
    n_img = len(names) if names else 0
    geo = (win.win.winfo_width(), win.win.winfo_height())
    print(f"[{style}] 公式图片 {n_img} 张，面板 {geo[0]}x{geo[1]}，"
          f"alpha={win.win.attributes('-alpha')}")

    from PIL import ImageGrab

    # Tk 报的是逻辑像素（150% 缩放下屏幕只有 1707 宽），ImageGrab 抓的是物理像素
    # （2560×1600）。比例要用两者实际宽度算，不能用 fpixels("1i")，那个值本身也被
    # 系统按逻辑尺寸缩放过。
    # 测试跑在编辑器上面，面板可能被盖住：截图前先顶到最前
    win.win.lift()
    win.win.attributes("-topmost", True)
    pump(root, 12)

    from PIL import ImageGrab

    full = ImageGrab.grab(all_screens=True)
    k = full.width / float(root.winfo_screenwidth())
    x, y = win.win.winfo_rootx(), win.win.winfo_rooty()
    box = (int(x * k), int(y * k), int((x + geo[0]) * k), int((y + geo[1]) * k))
    full.crop(box).save(os.path.join(OUT, f"{style}.png"))
    win.close()
    pump(root, 5)
    root.destroy()
    return n_img, geo


def style_switch() -> bool:
    """同一块面板在 leaf / light 之间来回切，不能崩、配色要跟着变。"""
    root = tk.Tk()
    root.withdraw()
    win = ui.ResultWindow(root, CFG, style="leaf")
    win.open(60, 60, "划词", "翻译中")
    win.set_final("普通文本 $x^2$ 测试。")
    pump(root, 20)
    leaf_bg = win.text.cget("bg")

    win.set_style("light")          # 换主题应销毁旧窗口
    assert win.win is None, "换主题后旧窗口还活着"
    win.open(0, 0, "截图 757×267", "识别中")
    win.set_final(r"$y=\begin{bmatrix}c_1\\c_2\end{bmatrix}$ 测试")
    pump(root, 20)
    light_bg = win.text.cget("bg")
    alpha = float(win.win.attributes("-alpha"))

    win.set_style("leaf")
    win.open(70, 70, "划词", "翻译中")
    pump(root, 20)
    back_bg = win.text.cget("bg")
    win.close()
    pump(root, 5)
    root.destroy()

    ok = leaf_bg == ui.LEAF["bg"] and light_bg == ui.LIGHT["bg"] and back_bg == leaf_bg
    ok = ok and abs(alpha - 1.0) < 1e-6
    print(f"[切换] leaf={leaf_bg} neutral={light_bg} 回到={back_bg} alpha={alpha}")
    return ok


def line_kinds(win) -> dict:
    """按行统计每行里出现了什么：{'text', 'image'}。"""
    chunks = win.text.dump("1.0", "end-1c", text=True, image=True)
    by_line: dict[int, set] = {}
    # dump 返回 (类型, 内容, 索引)，索引在第三个位置
    for kind, _val, idx in chunks:
        line = int(str(idx).split(".")[0])
        by_line.setdefault(line, set()).add(kind)
    return by_line


def inline_packing() -> bool:
    """短文字 + 大矩阵要能排在同一行里，而不是一句一断成四行。

    之前"含矩阵就独占一行"，一句「20 [矩阵] 的解为 [矩阵]。」被拆成四行。
    现在只有 `$$` 和高得离谱的才独占一行，其余行内排。
    """
    root = tk.Tk()
    root.withdraw()
    win = ui.ResultWindow(root, CFG, style="leaf")
    win.open(50, 50, "划词", "翻译中")
    win.set_final(TEXT)
    pump(root, 30)

    kinds = line_kinds(win)
    mixed = [ln for ln, k in kinds.items() if "text" in k and "image" in k]
    n_lines = len([1 for ln, k in kinds.items() if k])
    ok = bool(mixed) and n_lines <= 5
    print(f"[排布] 共 {n_lines} 行，图文混排的行 {mixed}"
          f"{'' if ok else '  ← 大矩阵还是被强行独占了一行'}")
    win.close()
    pump(root, 5)
    root.destroy()
    return ok


def both_paths_green() -> bool:
    """产品决定：图片翻译和划词翻译都用纳西妲绿，谁都不许偷偷切白底。

    直接读 main.py 的两个入口，比重启整个 App 去点按钮快得多。
    """
    import re

    src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
    bad = re.findall(r'set_style\("light"\)', src)
    n_leaf = len(re.findall(r'set_style\("leaf"\)', src))
    ok = not bad and n_leaf >= 2
    print(f"[入口] set_style(leaf) ×{n_leaf}，set_style(light) ×{len(bad)}")
    return ok


def main() -> int:
    n_light, _ = shot("light")
    n_leaf, _ = shot("leaf")
    ok = (n_light >= 3 and n_leaf >= 3 and style_switch()
          and both_paths_green() and inline_packing())
    print("结果:", "PASS" if ok else "FAIL", f"(浅色 {n_light} / 绿色 {n_leaf})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
