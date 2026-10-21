"""公式渲染自检：矩阵、分段函数、多行对齐、单变量、价格误判。

跑法（用装了 matplotlib 的解释器）：
    D:/python/python.exe tests/test_mathrender.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import mathrender  # noqa: E402

OUT = os.path.join(ROOT, "logs", "mathcheck")
os.makedirs(OUT, exist_ok=True)

CASES = [
    ("矩阵连乘（用户实测原文）",
     r"20\quad Sy = \begin{bmatrix} 1 & 0 & 0 \\ 1 & 1 & 0 \\ 1 & 1 & 1 \end{bmatrix}"
     r"\begin{bmatrix} y_1 \\ y_2 \\ y_3 \end{bmatrix} = "
     r"\begin{bmatrix} c_1 \\ c_2 \\ c_3 \end{bmatrix}"),
    ("带文字的解（混合片段）",
     r"20\quad Sy = \begin{bmatrix} 1 & 0 & 0 \\ 1 & 1 & 0 \\ 1 & 1 & 1 \end{bmatrix}"
     r"\begin{bmatrix} y_1 \\ y_2 \\ y_3 \end{bmatrix} = "
     r"\begin{bmatrix} c_1 \\ c_2 \\ c_3 \end{bmatrix}\ \text{is solved by}\ y = "
     r"\begin{bmatrix} c_1 \\ c_2 - c_1 \\ c_3 - c_2 \end{bmatrix}"),
    ("圆括号 / 行列式",
     r"S^{-1}c = \begin{pmatrix} -1 & 1 & 0 \\ 1 & -1 & 1 \\ 0 & 1 & -1 \end{bmatrix}"
     r"\quad \det\begin{vmatrix} a & b \\ c & d \end{vmatrix} = ad-bc"),
    ("分段函数 cases",
     r"f(x)=\begin{cases} x^2 & x \ge 0 \\ -x & x < 0 \end{cases}"),
    ("多行对齐 aligned",
     r"\begin{aligned} a &= b + c \\ d &= e - f \end{aligned}"),
    ("\left\right 包裹 matrix",
     r"A = \left[\begin{matrix} 1 & 2 \\ 3 & 4 \end{matrix}\right]"),
    ("普通分式回归",
     r"\dfrac{\eta}{1-\beta} + \sqrt{x^2+y^2}"),
    ("单变量", r"S"),
]


def edge_ink(img, cols: int = 2) -> tuple[int, int]:
    """最左 / 最右两列里各有多少墨点。

    定界符是贴着画布边缘画的（左边贴在 x=0，右边贴在 w-宽度），所以"边缘有没有墨"
    就等于"这个括号画没画"。只判断图不是 None 是不够的：曾经右边括号宽度预留了、
    却忘了合成上去，图照样生成，只是矩阵只剩左半边括号。

    不用"整图左右翻转求差"来判：内容居中时会有 1 像素的取整偏移，
    数字看着对称，逐像素比出来的差却不小，会误报。
    """
    alpha = img.split()[-1]
    w, h = alpha.size
    left = alpha.crop((0, 0, min(cols, w), h))
    right = alpha.crop((max(0, w - cols), 0, w, h))
    return (sum(1 for p in left.getdata() if p > 40),
            sum(1 for p in right.getdata() if p > 40))


DELIM_CASES = [
    # 内容用 "0" 而不是 "1"：数字在数学模式下是正体、左右对称，
    # 翻转后应该几乎一模一样，镜像差就只反映括号本身对不对称
    ("bmatrix", r"\begin{bmatrix} 0 \end{bmatrix}", "both"),
    ("pmatrix", r"\begin{pmatrix} 0 \end{pmatrix}", "both"),
    ("vmatrix", r"\begin{vmatrix} 0 \end{vmatrix}", "both"),
    ("Bmatrix", r"\begin{Bmatrix} 0 \end{Bmatrix}", "both"),
    ("cases", r"\begin{cases} x & x > 0 \\ -x & x \le 0 \end{cases}", "left"),
]


def delim_span(img, side: str, cols: int = 12) -> tuple[int, int]:
    """定界符那几列里，墨迹从第几行到第几行。

    方括号是"竖线 + 上下各一条横臂"。之前画布高度只按内容算，括号比内容高一圈，
    下边那条横臂被裁掉了，用户看到的就是"只剩一横一竖"。所以这里要求
    墨迹从顶部一直铺到底部，差 3 行以上就说明某条横臂丢了。
    """
    alpha = img.split()[-1]
    w, h = alpha.size
    if side == "L":
        xs = range(0, min(cols, w))
    else:
        xs = range(max(0, w - cols), w)
    rows = [y for y in range(h)
            if any(alpha.getpixel((x, y)) > 40 for x in xs)]
    return (min(rows), max(rows)) if rows else (-1, -1)


def check_delim_arms() -> bool:
    ok = True
    for name, tex in (("bmatrix", r"\begin{bmatrix} 1 \\ 0 \end{bmatrix}"),
                      ("pmatrix", r"\begin{pmatrix} 1 \\ 0 \end{pmatrix}"),
                      ("vmatrix", r"\begin{vmatrix} 1 \\ 0 \end{vmatrix}")):
        # baseline=False：不要底部补的基线空白，否则"墨迹没到底"是补白的锅
        img = mathrender.render_image(tex, px=16, color="#f1f8e8", baseline=False)
        if img is None:
            print(f"  ✗ {name} 渲染失败")
            ok = False
            continue
        h = img.height
        top_l, bot_l = delim_span(img, "L")
        top_r, bot_r = delim_span(img, "R")
        good = (top_l <= 2 and bot_l >= h - 3 and top_r <= 2 and bot_r >= h - 3)
        print(f"  {'✓' if good else '✗'} {name:8s} 画布高 {h:3d}  "
              f"左 {top_l}-{bot_l}  右 {top_r}-{bot_r}"
              f"{'' if good else '  ← 括号上下横臂被裁掉了'}")
        ok = ok and good
    return ok


def check_delims() -> bool:
    ok = True
    for name, tex, want in DELIM_CASES:
        img = mathrender.render_image(tex, px=16, color="#f1f8e8")
        if img is None:
            print(f"  ✗ {name} 渲染失败")
            ok = False
            continue
        l, r = edge_ink(img)
        img.save(os.path.join(OUT, f"delim_{name}.png"))
        if want == "both":
            # 左右括号是同一个形状翻转来的，边缘墨点数应该基本相等
            good = bool(l and r) and 0.7 <= r / float(l) <= 1.4
            print(f"  {'✓' if good else '✗'} {name:8s} 边缘墨点 左 {l:3d} 右 {r:3d}"
                  f"{'' if good else '  ← 右边括号缺失或大小不对'}")
            ok = ok and good
        else:
            # cases 右边本来就没定界符，右边缘的墨点是内容里的字形，不作判据
            good = bool(l)
            print(f"  {'✓' if good else '✗'} {name:8s} 左边缘墨点 {l:3d}（只有左花括号）")
            ok = ok and good
    return ok


def main() -> int:
    ok = 0
    for i, (name, tex) in enumerate(CASES):
        img = mathrender.render_image(tex, px=16, color="#f1f8e8")
        if img is None:
            print(f"[FAIL] {name}")
            continue
        ok += 1
        img.save(os.path.join(OUT, f"{i:02d}.png"))
        print(f"[ OK ] {name}  {img.width}x{img.height}")

    print("\n定界符左右配对：")
    delims = check_delims()
    print("\n定界符上下横臂：")
    arms = check_delim_arms()

    # 分段判定：价格不算公式，单变量要算
    seg = mathrender.split_segments("价格是 $5 和 $10，而 $S$ 是矩阵")
    kinds = [(s[0], s[1][:12]) for s in seg]
    print("切分:", kinds)
    assert any(k == "math" for k, _ in kinds), "单变量 $S$ 被漏掉了"
    assert sum(1 for k, _ in kinds if k == "math") == 1, "价格被误判成公式了"

    print(f"\n{ok}/{len(CASES)} 条公式渲染成功，图在 {OUT}")
    ok = ok == len(CASES) and delims and arms
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
