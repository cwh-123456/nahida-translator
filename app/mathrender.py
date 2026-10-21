"""把译文里的 LaTeX 公式渲染成图片，供 tkinter Text 做「文字 + 公式」混排。

为什么不用 WebView + KaTeX：那要多一个组件、多一份本地资源，还要处理进程间通信。
本项目已有的 matplotlib 自带 mathtext，能把绝大部分论文/PPT 里的公式排出来，
完全离线、零新增依赖，所以先用它。mathtext 不支持的写法（矩阵 bmatrix、
分段函数 cases 等）一律渲染失败 → 调用方降级显示 LaTeX 源码，不静默丢弃。

对外只暴露三样东西：
    has_math(text)          —— 译文里有没有公式，没有就走原来的纯文本路径
    split_segments(text)    —— 切成 [(kind, payload), ...]，kind 是 "text"/"math"
    render_image(tex, ...)  —— 渲染成 PIL.Image，失败返回 None（带缓存）
"""
from __future__ import annotations

import io
import logging
import re
import threading

log = logging.getLogger("mathrender")

# 行内：$...$ 与 \(...\)；独立：$$...$$ 与 \[...\]
# 行内那条用 (?<!\\) 避免把转义的 \$ 当公式起点，用 (?:\\.|[^$\\])+ 跳过 \$
_PATTERN = re.compile(
    r"(\$\$(?P<d1>.+?)\$\$"
    r"|\\\[(?P<d2>.+?)\\\]"
    r"|\\\((?P<i1>.+?)\\\)"
    r"|\$(?P<i2>(?:\\.|[^$\\])+?)\$)",
    re.S,
)

# mathtext 不认识的排版指令：公式编号、标签、换行序号
_TAG_CMD = re.compile(r"\\(?:tag|label)\*?\{[^{}]*\}")
_NONUMBER = re.compile(r"\\(?:nonumber|notag)\b")
_WS = re.compile(r"\s+")

# $5 和 $10 这种「价格」也会被当成公式，所以候选片段必须长得像公式：
# 至少带一个 LaTeX 命令、上标、下标、花括号或等号
_MATHISH = re.compile(r"[\\^_{}=]")

_CACHE: dict[tuple[str, int, str, int], object] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_LIMIT = 240

_RC_READY = False


def _ensure_rc() -> None:
    """mathtext 的字体风格只需要设一次；Computer Modern 最接近论文观感。"""
    global _RC_READY
    if _RC_READY:
        return
    import matplotlib

    matplotlib.use("Agg", force=False)
    matplotlib.rcParams["mathtext.fontset"] = "cm"
    matplotlib.rcParams["axes.unicode_minus"] = False
    _RC_READY = True


def has_math(text: str) -> bool:
    return bool(text) and _PATTERN.search(text) is not None


def split_segments(text: str) -> list[tuple]:
    """切成 [("text", 字符串) | ("math", 公式, 是否独立公式)]。

    切分只做一次，渲染和插入都基于它，保证两边看到的是同一份结构。
    """
    out: list[tuple] = []
    pos = 0
    while True:
        m = _PATTERN.search(text, pos)
        if m is None:
            break
        if m.start() > pos:
            out.append(("text", text[pos:m.start()]))
        if m.group("d1") is not None:
            body, display = m.group("d1"), True
        elif m.group("d2") is not None:
            body, display = m.group("d2"), True
        elif m.group("i1") is not None:
            body, display = m.group("i1"), False
        else:
            body, display = m.group("i2"), False
        if _MATHISH.search(body or ""):
            out.append(("math", body, display))
            pos = m.end()
        else:
            # 不像公式（例如 "$5 和 $10"）：只把开头的分隔符当普通文字吐出来，
            # 从它后面重新扫，别把后面真正的公式一起吞掉
            out.append(("text", text[m.start()]))
            pos = m.start() + 1
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def _normalize(tex: str) -> str:
    """把模型常写、但 mathtext 不认的东西抹掉，否则整条公式会降级成源码。

    真实例子里 Qwen3-VL 每次都会输出 \\tag{1}，mathtext 不认它，而 matplotlib
    的宽松模式会「静默地把整段当普通文字画出来」——结果就是面板里出现一张
    看起来像源码的图片。所以这里先清理，再用严格解析器渲染。
    """
    s = _TAG_CMD.sub("", tex or "")
    s = _NONUMBER.sub("", s)
    s = s.replace("&", " ")
    s = s.replace("\\\\", " ")          # 公式内部的换行
    s = _WS.sub(" ", s)
    s = s.strip()
    # 模型偶尔会自己再包一层 $，去掉，避免 $$x$$ 变成 $x$
    return s.strip("$").strip()


def render_image(tex: str, px: int = 16, color: str = "#f1f8e8",
                 dpi: float = 96.0):
    """把一段 LaTeX 渲染成透明底 PIL.Image（字面高度约等于 px 像素）。

    失败返回 None —— 调用方据此降级，绝不抛异常导致整个面板画不出来。
    返回图的底部已经补上了基线以下的空白，配合 Text 的 align="baseline"
    能跟中文正文对齐。
    """
    clean = _normalize(tex)
    if not clean:
        return None
    key = (clean, int(px), color, int(dpi))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    try:
        _ensure_rc()
        from matplotlib.mathtext import math_to_image
        from matplotlib.font_manager import FontProperties
        from PIL import Image, ImageOps
    except Exception as exc:  # 没有 matplotlib/PIL 也不该让程序挂掉
        log.warning("公式渲染不可用: %s", exc)
        return None

    try:
        # 用严格解析器（math_to_image）：解析失败会抛异常，不会静默画成源码。
        # 一律用黑色画在白底上，再把亮度反相当透明度——math_to_image 存出来的
        # PNG 是不透明的白底（没有 transparent 参数），直接贴进深色面板会变成
        # 一块块白方块。用「掩膜」既拿到抗锯齿边缘，又完全不受底色影响。
        buf = io.BytesIO()
        depth = math_to_image(
            f"${clean}$", buf,
            prop=FontProperties(size=px * 72.0 / dpi),
            dpi=dpi, format="png", color="black",
        )
        buf.seek(0)
        mask = Image.open(buf).convert("L")
        mask.load()
        alpha = ImageOps.invert(mask)
        try:
            from PIL import ImageColor
            rgb = ImageColor.getrgb(color)[:3]
        except Exception:
            rgb = (241, 248, 232)
        img = Image.new("RGBA", mask.size, rgb + (0,))
        img.putalpha(alpha)
    except Exception as exc:
        # mathtext 解析失败（矩阵、cases、中文等）走这里，属于预期内的降级
        log.info("公式无法渲染，降级为源码: %r (%s)", clean[:40], type(exc).__name__)
        return None

    # 补底部空白：align="baseline" 把图片底边贴在基线上，
    # 不补的话带下标/分式的公式会被整体抬高一截
    try:
        pad = int(round(float(depth)))
        if pad > 0:
            out = Image.new("RGBA", (img.width, img.height + pad), (0, 0, 0, 0))
            out.paste(img, (0, 0))
            img = out
    except Exception:
        pass

    with _CACHE_LOCK:
        if len(_CACHE) < _CACHE_LIMIT:
            _CACHE[key] = img
    return img


def supported(tex: str, px: int = 16, dpi: float = 96.0) -> bool:
    return render_image(tex, px=px, dpi=dpi) is not None


def warmup() -> None:
    """后台预热：matplotlib 首次 import + 字体缓存约 0.6 秒。

    放在后台线程里做，第一次出现公式时面板就不会卡那一下。
    """
    try:
        render_image("x^2", px=14)
    except Exception:
        pass
