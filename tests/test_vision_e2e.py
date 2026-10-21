"""真机端到端：拿一张"英文讲义 + 公式"的图，看视觉模型是否真的译成中文。

不用 mock，直接打本机 8378 上的 llama-server（没用 GPU，纯 CPU，约 30~60 秒）。
跑法：D:/python/python.exe tests/test_vision_e2e.py [图片路径]
产物：logs/visioncheck/slide.png（造的测试图）+ 输出文本
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app import config, mathrender  # noqa: E402
from app.vision import VisionTranslator, han_ratio  # noqa: E402

OUT = os.path.join(ROOT, "logs", "visioncheck")
os.makedirs(OUT, exist_ok=True)

# 故意挑用户截图里出现过的那类内容：一句英文定义 + 一个矩阵等式
LINES = [
    "Eigenvalues and Eigenvectors",
    "A scalar λ is an eigenvalue of A if there is a nonzero vector x",
    "such that Ax = λx. The vector x is then called an eigenvector.",
    "Example: consider the matrix",
    "The determinant of A minus λI gives the characteristic polynomial.",
]


def make_slide() -> Image.Image:
    img = Image.new("RGB", (1100, 620), (255, 255, 255))
    d = ImageDraw.Draw(img)
    font_path = r"C:\Windows\Fonts\msyh.ttc"
    big = ImageFont.truetype(font_path, 34)
    mid = ImageFont.truetype(font_path, 24)
    d.text((50, 34), LINES[0], font=big, fill=(20, 20, 20))
    d.line((50, 86, 1050, 86), fill=(180, 180, 180), width=2)
    y = 110
    for line in LINES[1:4]:
        d.text((50, y), line, font=mid, fill=(35, 35, 35))
        y += 44
    # 一个 2x3 矩阵：让模型必须吐 LaTeX 而不是照抄
    d.text((120, y + 12), "A =", font=mid, fill=(35, 35, 35))
    d.text((175, y + 2), "[ 2  1  0 ]", font=mid, fill=(35, 35, 35))
    d.text((175, y + 34), "[ 1  3  1 ]", font=mid, fill=(35, 35, 35))
    d.text((120, y + 90), "λI = diag(λ, λ, λ)", font=mid, fill=(35, 35, 35))
    d.text((50, y + 140), LINES[4], font=mid, fill=(35, 35, 35))
    img.save(os.path.join(OUT, "slide.png"))
    return img


def english_residue(text: str) -> list[str]:
    """剔掉公式后，还在正文里连着出现的英文串——就是"没翻译的那几句"。"""
    segs = mathrender.split_segments(text)
    prose = " ".join(s[1] for s in segs if s[0] == "text")
    return [m.group(0).strip() for m in re.finditer(r"(?:[A-Za-z][\w'-]*[ ,]+){3,}[A-Za-z][\w'-]*", prose)]


def main() -> int:
    cfg = config.load_config()
    path = sys.argv[1] if len(sys.argv) > 1 else None
    img = Image.open(path) if path else make_slide()
    img.load()

    v = VisionTranslator(cfg)
    print("模型文件:", "齐" if v.available() else f"缺 {v.missing_files()}")
    if not v.available():
        print("结果: SKIP（模型没下全）")
        return 0

    try:
        wait = v.ensure_server(on_status=lambda s: print(" …", s))
        print(wait or "服务已就绪")
    except Exception as exc:
        print("起服务失败:", exc)
        return 1

    res = v.translate_image(img, mode="translate", on_status=lambda s: print(" …", s))
    text = res.text
    with open(os.path.join(OUT, "result.txt"), "w", encoding="utf-8") as f:
        f.write(text)

    print("\n----- 译文 -----")
    print(text)
    print("----------------")
    r = han_ratio(text)
    residue = english_residue(text)
    print(f"汉字占比 {r:.2f}  耗时 {res.seconds:.1f}s  tokens {res.tokens}")
    print("正文英文残留:", residue or "无")
    ok = r >= 0.35 and not residue
    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
