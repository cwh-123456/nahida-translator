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

    # 分段判定：价格不算公式，单变量要算
    seg = mathrender.split_segments("价格是 $5 和 $10，而 $S$ 是矩阵")
    kinds = [(s[0], s[1][:12]) for s in seg]
    print("切分:", kinds)
    assert any(k == "math" for k, _ in kinds), "单变量 $S$ 被漏掉了"
    assert sum(1 for k, _ in kinds if k == "math") == 1, "价格被误判成公式了"

    print(f"\n{ok}/{len(CASES)} 条公式渲染成功，图在 {OUT}")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
