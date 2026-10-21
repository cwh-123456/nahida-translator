"""图片输出的后处理：清洗围栏 / 判断"只转写没翻译"。

跑法：D:/python/python.exe tests/test_vision_text.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.vision import clean_image_output, han_ratio, looks_untranslated  # noqa: E402

MATRIX = r"\begin{bmatrix}1&0&0\\1&1&0\\1&1&1\end{bmatrix}"
RAW_EN = (
    "20 $Sy=" + MATRIX + r"\begin{bmatrix}y_1\\y_2\\y_3\end{bmatrix}$ is solved by "
    r"$y=\begin{bmatrix}c_1\\c_2-c_1\end{bmatrix}$. This is "
    "$S$ is square with independent columns. So $S$ has an inverse with $SS^{-1}=I$."
)
ZH = (
    "20 $Sy=" + MATRIX + r"\begin{bmatrix}y_1\\y_2\\y_3\end{bmatrix}$ 的解为 "
    r"$y=\begin{bmatrix}c_1\\c_2-c_1\end{bmatrix}$。"
    "因为 $S$ 的列线性无关，所以 $S$ 可逆，且 $SS^{-1}=S^{-1}S=I$。"
)
MOSTLY_MATH = r"$x^2+y^2=1$"


def main() -> int:
    ok = True
    print(f"英文原文  han={han_ratio(RAW_EN):.2f}  判定未翻译={looks_untranslated(RAW_EN)}")
    print(f"中文译文  han={han_ratio(ZH):.2f}  判定未翻译={looks_untranslated(ZH)}")
    print(f"纯公式    han={han_ratio(MOSTLY_MATH):.2f}  判定未翻译={looks_untranslated(MOSTLY_MATH)}")
    ok &= looks_untranslated(RAW_EN) is True
    ok &= looks_untranslated(ZH) is False
    ok &= looks_untranslated(MOSTLY_MATH) is False

    # 清洗：```latex 围栏 + 「以下是翻译」前缀都要去掉
    dirty = "```latex\n以下是图片内容的翻译：\n" + ZH + "\n```"
    cleaned = clean_image_output(dirty)
    print("清洗结果:", cleaned[:40].replace("\n", "⏎"), "...")
    ok &= not cleaned.startswith("`") and "以下是" not in cleaned and cleaned.startswith("20")

    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
