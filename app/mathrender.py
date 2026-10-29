"""把译文里的 LaTeX 公式渲染成图片，供 tkinter Text 做「文字 + 公式」混排。

为什么不用 WebView + KaTeX：那要多一个组件、多一份本地资源，还要处理进程间
通信。matplotlib 自带 mathtext 能把绝大部分论文/PPT 里的公式排出来，完全离线、
零新增依赖。

mathtext 唯独不认识矩阵类环境（bmatrix / pmatrix / cases / aligned …），一遇到
就整条公式渲染失败、退化成一串 LaTeX 源码。所以这里在 mathtext 之上搭了一层
「结构化排版」：把 \\begin{...}...\\end{...} 拆成行列网格，每个单元格仍交给
mathtext 渲染，再按列宽和基线拼回去、自己画上定界括号。矩阵、分段函数、多行
对齐公式因此都能出图。

对外只暴露三样东西：
    has_math(text)          —— 译文里有没有公式，没有就走纯文本路径
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

# mathtext 不认识的排版指令：公式编号、标签、样式开关
_TAG_CMD = re.compile(r"\\(?:tag|label)\*?\{[^{}]*\}")
_NONUMBER = re.compile(r"\\(?:nonumber|notag)\b")
_DROP_CMDS = re.compile(
    r"\\(?:displaystyle|textstyle|scriptstyle|scriptscriptstyle|limits|nolimits|mathstrut)\b"
)
_WS = re.compile(r"\s+")

# $5 和 $10 这种「价格」也会被当成公式，所以候选片段必须长得像公式：
# 带一个 LaTeX 命令 / 上标 / 下标 / 花括号 / 等号，或者干脆就是个短符号（$S$、$x$）
_MATHISH = re.compile(r"[\\^_{}=]")
_CJK = re.compile(r"[\u2e80-\u9fff\uff00-\uffef\u3000-\u303f]")
# 光秃秃的短内容也算公式：单字母变量 $S$、纯数字 $1$、单个符号 $=$。
# 价格 "$5 和 $10" 里夹着中文，先被上面的 _CJK 挡掉，不会走到这里。
_BARE_SYMBOL = re.compile(r"[A-Za-z][A-Za-z0-9]?|\d+(?:\.\d+)?|[^\w\s]{1,2}")

# mathtext 缺的几个常见别名：\ge、\le 这类简写它不认，\implies / \iff / \land
# 更是直接报错。模型输出里这些出现频率很高，一路映射到 mathtext 认识的名字。
_CMD_ALIAS = {
    "ge": "geq", "le": "leq",
    "implies": "Rightarrow", "impliedby": "Leftarrow", "iff": "Leftrightarrow",
    "land": "wedge", "lor": "vee", "lnot": "neg", "gets": "leftarrow",
}
_CMD_FIX = re.compile(
    r"\\(" + "|".join(_CMD_ALIAS) + r")(?![A-Za-z])"
)
# \text{...} 里的空格是「真的空格」，不能替换成数学细间距
_TEXTLIKE = re.compile(r"\\[A-Za-z]*text[A-Za-z]*\s*\{[^{}]*\}")
# 前面不是反斜杠的空格才替换：`\ ` 本身就是一个间距命令，改成 `\\,` 会被当成换行
_SPACE = re.compile(r"(?<!\\) ")

_BEGIN = re.compile(r"\\begin\s*\{\s*(?P<env>[A-Za-z]+\*?)\s*\}")
_END = re.compile(r"\\end\s*\{\s*(?P<env>[A-Za-z]+\*?)\s*\}")
_LEFT_TALE = re.compile(r"\\left\s*([\(\)\[\]\{\}\|\.])\s*$")
_RIGHT_HEAD = re.compile(r"\\right\s*([\(\)\[\]\{\}\|\.])")

_ROW = "row"
_COL = "col"

# 矩阵类环境：单元格水平居中，没有「按等号对齐」的语义
_GRID_MODE = {
    "matrix": "center", "smallmatrix": "center", "array": "center",
    "pmatrix": "center", "bmatrix": "center", "Bmatrix": "center",
    "vmatrix": "center", "Vmatrix": "center",
    "cases": "left", "rcases": "left",
}
_CENTER_ENVS = frozenset({"gather", "gather*", "gathered", "multline", "multline*"})

# 环境自带的定界符：kind 决定画什么形状
_ENV_DELIM = {
    "pmatrix": ("paren", "paren"),
    "bmatrix": ("bracket", "bracket"),
    "Bmatrix": ("brace", "brace"),
    "vmatrix": ("bar", "bar"),
    "Vmatrix": ("dbar", "dbar"),
    "cases": ("brace", None),
    "rcases": (None, "brace"),
}
# \left( \right] 这类手写定界符，映射到同样的形状
_CHAR_DELIM = {
    "(": "paren", ")": "paren",
    "[": "bracket", "]": "bracket",
    "{": "brace", "}": "brace",
    "|": "bar",
}

_CACHE: dict[tuple, object] = {}
_CACHE_LOCK = threading.Lock()
# matplotlib 不是线程安全的：预热线程和界面线程同时进来时，偶尔会让
# math_to_image 抛奇怪的解析错误（表现为公式忽然降级成源码）。串行掉。
_RENDER_LOCK = threading.RLock()
_CACHE_LIMIT = 240

# 超采样倍数：按 3 倍分辨率渲染再降采样，边缘比 1 倍清楚得多，
# 这也是「看起来像正规 PDF」的关键一步（1 倍时根号、Σ 的斜边会发毛）
SUPERSAMPLE = 3

# 字体风格：stix = Times 风格（学术 PDF 最常见），cm = LaTeX 默认
FONT_SET = "stix"

_RC_READY = False


def set_font_set(name: str) -> None:
    """切换公式字体风格：stix / cm / dejavuserif / stixsans。"""
    global FONT_SET, _RC_READY
    name = (name or "stix").strip().lower()
    if name == FONT_SET:
        return
    FONT_SET = name
    _RC_READY = False
    _ensure_rc()


def _ensure_rc() -> None:
    """mathtext 的字体风格设一次就够：换风格时先置 _RC_READY=False 再进来。"""
    global _RC_READY
    if _RC_READY:
        return
    with _RENDER_LOCK:
        if _RC_READY:
            return
        import matplotlib

        matplotlib.use("Agg", force=False)
        matplotlib.rcParams["mathtext.fontset"] = FONT_SET
        matplotlib.rcParams["axes.unicode_minus"] = False
        _RC_READY = True


# ---------------------------------------------------------------- 切分
def _looks_math(body: str) -> bool:
    """这段 $...$ 到底是不是公式。

    「$5 和 $10」这种价格里会夹中文，一律不算；光秃秃的 $S$ / $x$ 要算，
    否则正文里满篇单字母变量都会被当普通文字。
    """
    b = (body or "").strip()
    if not b or _CJK.search(b):
        return False
    if _MATHISH.search(b):
        return True
    return _BARE_SYMBOL.fullmatch(b) is not None


def has_math(text: str) -> bool:
    return bool(text) and _PATTERN.search(text) is not None


# 落在公式**外面**的 LaTeX 命令，文本层是按普通文字画的，源码会原样显示出来。
# 视觉模型特别喜欢把公式切碎（`(A^{-1}` 单独成图），间距命令 `\quad` 就掉在
# 文本段里，用户看到的是字面的反斜杠命令。这些正则负责把残渣捡干净。
_TIDY_TEXT_ARG = re.compile(
    r"\\(?:text|textrm|textnormal|textbf|textit|mathrm|mathbf|mathit|mathsf"
    r"|mathtt|mbox|operatorname)\s*\{([^{}]*)\}"
)
_TIDY_SPACE = re.compile(
    r"\\(?:q?quad|qquad|hspace\*?\s*\{[^{}]*\}|hfill|hss|thinspace|medspace"
    r"|thickspace|enspace|negthinspace|,|;|:|!| )"
)
_TIDY_CMD = re.compile(r"\\[A-Za-z]+\*?")
_TIDY_RUN = re.compile(r"[ \t]{2,}")

# 漏到文本段里的命令不能一删了之：\neq 删掉就把「i ≠ j」变成「i j」，
# 语义直接反了。常见的数学符号在这里映射成 Unicode，实在不认识的才丢。
_TIDY_UNICODE = {
    "neq": "≠", "ne": "≠", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝",
    "cdot": "·", "times": "×", "div": "÷", "ast": "∗",
    "pm": "±", "mp": "∓", "infty": "∞",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←",
    "Rightarrow": "⇒", "implies": "⇒", "Leftarrow": "⇐",
    "Leftrightarrow": "⇔", "iff": "⇔", "mapsto": "↦",
    "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂", "nabla": "∇",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "cup": "∪", "cap": "∩", "emptyset": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "land": "∧", "lor": "∨",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "degree": "°", "circ": "∘",
    "prime": "′", "cdots": "⋯", "ldots": "…", "dots": "…",
    "vdots": "⋮", "ddots": "⋱",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ",
    "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_TIDY_SYMBOL = re.compile(
    r"\\(" + "|".join(sorted(_TIDY_UNICODE, key=len, reverse=True)) + r")(?![A-Za-z])"
)


def tidy_text(s: str) -> str:
    """清理掉进纯文本段的 LaTeX 残渣：间距命令、\\text{...}、孤立的 $$。

    只动"有反斜杠命令或 $$"的文本，正常中文段落原样返回。
    `\\text{row }` 这种取花括号里的内容（`row `），数学符号转成 Unicode
    （`\\neq` → `≠`），其余识别不了的命令才删——文本段里出现命令本身就是
    模型切分失误，但能救回符号就别丢。
    """
    if not s or ("\\" not in s and "$$" not in s):
        return s
    s = _TIDY_TEXT_ARG.sub(lambda m: m.group(1), s)
    s = _TIDY_SPACE.sub(" ", s)
    s = _TIDY_SYMBOL.sub(lambda m: _TIDY_UNICODE[m.group(1)], s)
    s = s.replace("\\\\", " ")
    s = s.replace("\\{", "{").replace("\\}", "}").replace("\\%", "%")
    s = s.replace("\\$", "$").replace("\\_", "_").replace("\\&", "&")
    s = _TIDY_CMD.sub("", s)
    s = s.replace("$$", "")
    s = _TIDY_RUN.sub(" ", s)
    return s


def split_segments(text: str) -> list[tuple]:
    """切成 [("text", 字符串) | ("math", 公式, 是否独立公式)]。

    切分只做一次，渲染和插入都基于它，保证两边看到的是同一份结构。
    文本段在这里顺手过一遍 tidy_text：模型把公式切碎时，`\\quad` 之类的
    命令会漏到公式外面，让它在源码层就被清掉，比在每个调用方各修一遍可靠。
    """
    out: list[tuple] = []

    def add_text(s: str) -> None:
        s = tidy_text(s)
        if s:
            out.append(("text", s))

    pos = 0
    while True:
        m = _PATTERN.search(text, pos)
        if m is None:
            break
        if m.start() > pos:
            add_text(text[pos:m.start()])
        if m.group("d1") is not None:
            body, display = m.group("d1"), True
        elif m.group("d2") is not None:
            body, display = m.group("d2"), True
        elif m.group("i1") is not None:
            body, display = m.group("i1"), False
        else:
            body, display = m.group("i2"), False
        if _looks_math(body):
            out.append(("math", body, display))
            pos = m.end()
        elif m.group("d1") is not None or m.group("d2") is not None:
            # $$...$$ / \[...\] 是模型明确标的独立公式，但里面塞了中文：
            # mathtext 排不了 CJK，整段渲染必然失败。这时把标记连同内容
            # 一起当文本收下（tidy 会剥掉 \text 外壳），而不是逐字符吐出
            # 一堆孤立的 $——那样用户看到的是两排美元符号。
            add_text(body)
            pos = m.end()
        else:
            # 不像公式（例如 "$5 和 $10"）：只把开头的分隔符当普通文字吐出来，
            # 从它后面重新扫，别把后面真正的公式一起吞掉
            out.append(("text", text[m.start()]))
            pos = m.start() + 1
    if pos < len(text):
        add_text(text[pos:])
    return out


# ---------------------------------------------------------------- 清洗
def _preclean(tex: str) -> str:
    """整段公式的预处理：只删注释性指令，保留 \\\\ 和 & 交给结构化排版。"""
    s = _TAG_CMD.sub("", tex or "")
    s = _NONUMBER.sub("", s)
    s = _WS.sub(" ", s).strip()
    return s.strip("$").strip()


def _space_to_thin(s: str) -> str:
    """把数学区里的空格换成细间距 \\,，但 \\text{...} 和转义空格 \\ 里的不动。"""
    out = []
    pos = 0
    for m in _TEXTLIKE.finditer(s):
        out.append(_SPACE.sub(lambda _m: "\\,", s[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_SPACE.sub(lambda _m: "\\,", s[pos:]))
    return "".join(out)


def _normalize(tex: str) -> str:
    """把「普通公式片段」整理成 mathtext 吃得下的形式。

    mathtext 会吃掉空格（`20 Sy =` 会排成 `20Sy=`），所以把空格换成细间距 \\,，
    保持「20 Sy = …」这种原文的空隙。
    """
    s = _TAG_CMD.sub("", tex or "")
    s = _NONUMBER.sub("", s)
    s = _DROP_CMDS.sub("", s)
    s = _CMD_FIX.sub(lambda m: "\\" + _CMD_ALIAS[m.group(1)], s)
    s = s.replace("&", " ")          # 非矩阵环境里的对齐符，没有意义
    s = s.replace("\\\\", " ")       # 公式内换行，交给上层切段
    s = _WS.sub(" ", s).strip().strip("$").strip()
    return _space_to_thin(s)


# ---------------------------------------------------------------- 环境解析
def _skip_brace(s: str, i: int) -> int:
    """跳过 array 的列格式参数 `{ccc}`。"""
    while i < len(s) and s[i].isspace():
        i += 1
    if i >= len(s) or s[i] != "{":
        return i
    depth = 0
    while i < len(s):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _extract_env(tex: str, pos: int):
    """从 pos 处的 \\begin{...} 取出 (环境名, 内容, 结束后位置)。

    用栈配平嵌套（bmatrix 里嵌 pmatrix 很常见），拿不到配对就返回 None。
    """
    m = _BEGIN.match(tex, pos)
    if m is None:
        return None
    stack = [m.group("env")]
    i = m.end()
    if stack[0] == "array":
        i = _skip_brace(tex, i)
    body_start = i
    while i < len(tex):
        mb = _BEGIN.search(tex, i)
        me = _END.search(tex, i)
        if me is None:
            return None
        if mb is not None and mb.start() < me.start():
            stack.append(mb.group("env"))
            i = mb.end()
            if stack[-1] == "array":
                i = _skip_brace(tex, i)
            continue
        stack.pop()
        i = me.end()
        if not stack:
            return m.group("env"), tex[body_start:me.start()], i
    return None


def _absorb_delims(prefix: str, suffix: str, env: str):
    """把紧贴环境的 \\left[ / \\right] 吸收成环境自己的括号。

    模型很爱写 `\\left[\\begin{matrix}…\\end{matrix}\\right]`，如果直接把
    \\left[ 留在普通片段里，那一小段单独渲染必然失败，整条公式就废了。
    """
    left, right = _ENV_DELIM.get(env, (None, None))
    m = _LEFT_TALE.search(prefix)
    if m:
        kind = _CHAR_DELIM.get(m.group(1))
        if kind:
            left = kind
            prefix = prefix[:m.start()]
    m = _RIGHT_HEAD.match(suffix)
    if m:
        kind = _CHAR_DELIM.get(m.group(1))
        if kind:
            right = kind
            suffix = suffix[m.end():]
    return prefix, suffix, left, right


def _split_envs(tex: str) -> list[tuple]:
    """拆成 [("tex", 片段) | ("env", 环境名, 内容, 左括号, 右括号)]。"""
    parts: list[tuple] = []
    pos = 0
    n = len(tex)
    while pos < n:
        m = _BEGIN.search(tex, pos)
        if m is None:
            break
        got = _extract_env(tex, m.start())
        if got is None:
            pos = m.end()
            continue
        env, body, end = got
        prefix = tex[pos:m.start()]
        rest = tex[end:]
        prefix, rest, left, right = _absorb_delims(prefix, rest, env)
        if prefix.strip():
            parts.append(("tex", prefix))
        parts.append(("env", env, body, left, right))
        pos = end + (len(tex) - end - len(rest))
    if pos < n and tex[pos:].strip():
        parts.append(("tex", tex[pos:]))
    return parts


def _split_top(body: str, want: str) -> list[str]:
    """按顶层分隔符切分：want="row" 认 \\\\，want="col" 认 &。

    花括号里的、以及嵌套环境里的分隔符都不算数。
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            mb = _BEGIN.match(body, i)
            if mb is not None:
                got = _extract_env(body, i)
                if got is not None:
                    buf.append(body[i:got[2]])
                    i = got[2]
                    continue
            if i + 1 < n:
                if body[i + 1] == "\\" and depth == 0 and want == _ROW:
                    out.append("".join(buf))
                    buf = []
                    i += 2
                    continue
                buf.append(body[i:i + 2])
                i += 2
                continue
            buf.append(c)
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == "&" and depth == 0 and want == _COL:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


# ---------------------------------------------------------------- 渲染
def _rgb(color: str) -> tuple:
    try:
        from PIL import ImageColor

        return ImageColor.getrgb(color)[:3]
    except Exception:
        return (241, 248, 232)


def _render_core(tex: str, px: int, dpi: float, color: str):
    """mathtext 渲染一段普通公式 → (超采样 RGBA 图, 基线以下高度/像素)。"""
    _ensure_rc()
    from matplotlib.font_manager import FontProperties
    from matplotlib.mathtext import math_to_image
    from PIL import Image, ImageOps

    render_dpi = float(dpi) * SUPERSAMPLE
    buf = io.BytesIO()
    # 严格解析器：解析失败会抛异常，不会静默把源码当文字画出来
    depth_pt = math_to_image(
        f"${tex}$", buf,
        prop=FontProperties(size=px * 72.0 / dpi),
        dpi=render_dpi, format="png", color="black",
    )
    buf.seek(0)
    mask = Image.open(buf).convert("L")
    mask.load()
    # 一律黑字白底渲染，再把亮度反相当透明度：既保留抗锯齿边缘，
    # 又完全不受面板底色影响（math_to_image 存出来的是不透明白底）
    alpha = ImageOps.invert(mask)
    img = Image.new("RGBA", mask.size, _rgb(color) + (0,))
    img.putalpha(alpha)
    return img, max(0.0, float(depth_pt) * render_dpi / 72.0)


def _render_simple(tex: str, px: int, dpi: float, color: str):
    s = _normalize(tex)
    if not s:
        return None
    try:
        return _render_core(s, px, dpi, color)
    except Exception:
        pass
    # 二次尝试：\left/\right 是 mathtext 最容易不认的东西，拆掉再试一次
    alt = re.sub(r"\\(?:left|right)\s*\.", "", s)
    alt = re.sub(r"\\(?:left|right)\b", "", alt)
    if alt != s:
        try:
            return _render_core(alt, px, dpi, color)
        except Exception:
            pass
    return None


def _bezier(p0, p1, p2, steps: int = 16):
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1.0 - t
        out.append((
            u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
        ))
    return out


def _make_delim(kind: str, height: int, px: int, color: str, side: str):
    """画一个撑到 height 高的定界符（左/右由 side 决定朝向）。"""
    from PIL import Image, ImageDraw

    rgb = _rgb(color)
    lw = max(1, int(round(px * 0.055 * SUPERSAMPLE)))
    arm = max(lw + 1, int(round(px * 0.30 * SUPERSAMPLE)))
    h = max(2, int(round(height)))

    if kind in ("bar", "dbar"):
        w = lw * 3 if kind == "dbar" else lw
        img = Image.new("RGBA", (max(1, w), h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, max(0, lw - 1), h - 1], fill=rgb)
        if kind == "dbar":
            d.rectangle([max(0, w - lw), 0, w - 1, h - 1], fill=rgb)
    elif kind == "bracket":
        # 用三个矩形拼，不用 d.line：折线在拐角处的描边宽度会被 PIL 摊薄，
        # 上臂和下臂一个粗一个细（用户看到的"下面那条横线没了"就是这么来的）
        img = Image.new("RGBA", (arm + lw, h + lw), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x = lw // 2
        top = lw // 2
        bot = h + lw // 2
        d.rectangle([x, top, x + lw - 1, bot], fill=rgb)              # 竖
        d.rectangle([x, top, x + arm, top + lw - 1], fill=rgb)        # 上横臂
        d.rectangle([x, bot - lw + 1, x + arm, bot], fill=rgb)        # 下横臂
    elif kind == "brace":
        img = Image.new("RGBA", (arm + lw, h + lw), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        y0, y1 = lw / 2.0, h + lw / 2.0
        mid = (y0 + y1) / 2.0
        spine = arm * 0.36
        pts = []
        pts += _bezier((arm, y0), (spine, y0), (spine, mid * 0.62))
        pts += _bezier((spine, mid * 0.62), (spine, mid * 0.90), (0.0, mid))
        pts += _bezier((0.0, mid), (spine, mid + (mid - y0) * 0.10),
                       (spine, y1 - (mid - y0) * 0.38))
        pts += _bezier((spine, y1 - (mid - y0) * 0.38), (spine, y1), (arm, y1))
        d.line(pts, fill=rgb, width=lw, joint="curve")
    else:      # paren
        bw = max(4, int(round(px * 0.30 * SUPERSAMPLE)))
        img = Image.new("RGBA", (bw * 2, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # 90°→270° 逆时针：从上方中点绕过左侧到下方中点，正好是 "("
        d.arc([0, 0, bw * 2 - 1, h - 1], 90, 270, fill=rgb, width=lw)

    # 四周留一圈透明边：横臂正好压在画布最外一行时，Tk 的行框和降采样都会啃掉
    # 那一行，看上去就变成"括号只有一竖 + 上面一横"。留边要够一个显示像素
    # （SUPERSAMPLE 个采样像素），少了等于没留。
    edge = max(lw, SUPERSAMPLE)
    padded = Image.new("RGBA", (img.width + edge * 2, img.height + edge * 2), (0, 0, 0, 0))
    padded.paste(img, (edge, edge))
    img = padded

    if side == "R":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    return img


def _hstack(pieces: list, px: int):
    """把若干 (图, 深度) 按公共基线横向拼起来。"""
    from PIL import Image

    gap = max(1, int(round(px * 0.34 * SUPERSAMPLE)))
    base = max(d for _, d in pieces)
    top = max(img.height - d for img, d in pieces)
    h = max(1, int(round(top + base)))
    w = max(1, sum(img.width for img, _ in pieces) + gap * (len(pieces) - 1))
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    x = 0
    for img, d in pieces:
        canvas.alpha_composite(img, (x, max(0, int(round(base - d)))))
        x += img.width + gap
    return canvas, base


def _render_env(env: str, body: str, px: int, dpi: float, color: str,
                left, right):
    """把 \\begin{env} 的内容排成网格，返回 (图, 深度)。"""
    from PIL import Image

    mode = _GRID_MODE.get(env)
    if mode is None:
        mode = "center" if env in _CENTER_ENVS else "right"

    rows = _split_top(body, _ROW)
    # 行尾的间距参数 `\\[6pt]` 会粘在下一行开头，剥掉
    rows = [re.sub(r"^\s*\[[^\]]*\]", "", r) for r in rows]
    while rows and not rows[-1].strip():
        rows.pop()
    if not rows:
        return None

    grid = [_split_top(r, _COL) for r in rows]
    ncol = max(len(r) for r in grid)
    for r in grid:
        r.extend([""] * (ncol - len(r)))

    cells: list[list] = []
    for row in grid:
        line = []
        for cell in row:
            c = cell.strip()
            if not c:
                line.append(None)
                continue
            got = _render_part(c, px, dpi, color)
            if got is None:
                return None      # 有一格排不出来就整条降级，别缺一块
            line.append(got)
        cells.append(line)

    if not any(any(line) for line in cells):
        return None

    colgap = max(1, int(round(px * 0.85 * SUPERSAMPLE)))
    rowgap = max(1, int(round(px * 0.34 * SUPERSAMPLE)))

    colw = [0] * ncol
    for line in cells:
        for j, got in enumerate(line):
            if got:
                colw[j] = max(colw[j], got[0].width)

    rows_h: list[int] = []
    rows_d: list[float] = []
    for line in cells:
        got = [c for c in line if c]
        d = max([c[1] for c in got] or [0.0])
        a = max([c[0].height - c[1] for c in got] or [0.0])
        rows_d.append(d)
        rows_h.append(int(round(a + d)))

    content_w = sum(colw) + colgap * (ncol - 1)
    content_h = sum(rows_h) + rowgap * (len(cells) - 1)

    bheight = content_h * 1.04
    pad = max(1, int(round(px * 0.22 * SUPERSAMPLE)))
    limg = _make_delim(left, bheight, px, color, "L") if left else None
    rimg = _make_delim(right, bheight, px, color, "R") if right else None

    total_w = content_w
    if limg is not None:
        total_w += limg.width + pad
    if rimg is not None:
        total_w += rimg.width + pad
    # 画布高度必须容得下定界符：括号图本身比 bheight 高一圈（line 有宽度，
    # 上下各溢出一半），只按 bheight 开画布会把下边那条横臂裁掉，
    # 看上去就是"括号只剩一竖 + 上面一横"
    total_h = max(content_h, int(round(bheight)))
    for dimg in (limg, rimg):
        if dimg is not None:
            total_h = max(total_h, dimg.height)

    canvas = Image.new("RGBA", (max(1, int(total_w)), max(1, total_h)), (0, 0, 0, 0))
    x0 = 0
    if limg is not None:
        canvas.alpha_composite(limg, (0, max(0, (total_h - limg.height) // 2)))
        x0 = limg.width + pad

    y = (total_h - content_h) // 2
    for i, line in enumerate(cells):
        base = y + rows_d[i]
        for j, got in enumerate(line):
            if got is None:
                continue
            img, d = got
            colx = x0 + sum(colw[:j]) + colgap * j
            if mode == "center":
                cx = colx + (colw[j] - img.width) // 2
            elif mode == "right" and j == 0:
                cx = colx + (colw[j] - img.width)
            else:
                cx = colx
            canvas.alpha_composite(img, (int(cx), max(0, int(round(base - d)))))
        y += rows_h[i] + rowgap

    # 右定界符：宽度早就预留好了，但之前一直没画上来——表现为矩阵只剩左边的 "["
    if rimg is not None:
        canvas.alpha_composite(
            rimg, (max(0, int(total_w) - rimg.width), max(0, (total_h - rimg.height) // 2)))

    # 矩阵以数学轴为中心，数学轴大约在基线上方 0.25em
    depth = total_h / 2.0 - px * 0.25 * SUPERSAMPLE
    return canvas, max(0.0, depth)


def _render_part(tex: str, px: int, dpi: float, color: str):
    """一段公式（可能还含嵌套环境）→ (图, 深度)。"""
    parts = _split_envs(tex)
    if not parts:
        return None
    if len(parts) == 1 and parts[0][0] == "tex":
        return _render_simple(parts[0][1], px, dpi, color)

    pieces = []
    for p in parts:
        if p[0] == "tex":
            if not _normalize(p[1]).strip():
                continue
            got = _render_simple(p[1], px, dpi, color)
            if got is None:
                return None
        else:
            got = _render_env(p[1], p[2], px, dpi, color, p[3], p[4])
            if got is None:
                return None
        pieces.append(got)
    if not pieces:
        return None
    return _hstack(pieces, px)


def render_image(tex: str, px: int = 16, color: str = "#f1f8e8",
                 dpi: float = 96.0, baseline: bool = True):
    """把一段 LaTeX 渲染成透明底 PIL.Image（字面高度约等于 px 像素）。

    失败返回 None —— 调用方据此降级，绝不抛异常导致整个面板画不出来。
    baseline=True 时底部补上基线以下的空白，配合 Text 的 align="baseline"
    能跟中文正文对齐；独立成行的公式用 baseline=False，按内容高度居中。
    """
    clean = _preclean(tex)
    if not clean:
        return None
    # mathtext 的中文字形是空的：不报错，只在控制台警告，然后画出一排豆腐块。
    # 与其给用户一堆"口"，不如老实降级成源码。
    if _CJK.search(clean):
        log.info("公式含中日韩字符，无法出图，降级源码: %r", clean[:40])
        return None
    key = (clean, int(px), color, int(dpi), FONT_SET, bool(baseline))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    try:
        from PIL import Image
    except Exception as exc:            # 没有 PIL 也不该让程序挂掉
        log.warning("公式渲染不可用: %s", exc)
        return None

    try:
        with _RENDER_LOCK:
            got = _render_part(clean, px, dpi, color)
    except Exception as exc:
        log.info("公式渲染异常: %r (%s)", clean[:48], type(exc).__name__)
        got = None
    if got is None:
        # mathtext 解析失败（未知命令、中文等）走这里，属于预期内的降级
        log.info("公式无法渲染，降级为源码: %r", clean[:48])
        return None

    img, depth = got
    # 补底部空白：align="baseline" 把图片底边贴在基线上，
    # 不补的话带下标/分式的公式会被整体抬高一截
    pad = int(round(depth)) if baseline else 0
    if pad > 0:
        out = Image.new("RGBA", (img.width, img.height + pad), (0, 0, 0, 0))
        out.paste(img, (0, 0))
        img = out

    # 3 倍渲染后降采样回目标像素：边缘更干净，Σ、根号、分式线不发毛
    if SUPERSAMPLE > 1:
        img = img.resize(
            (max(1, round(img.width / SUPERSAMPLE)),
             max(1, round(img.height / SUPERSAMPLE))),
            Image.LANCZOS,
        )

    with _CACHE_LOCK:
        if len(_CACHE) < _CACHE_LIMIT:
            _CACHE[key] = img
    return img


def supported(tex: str, px: int = 16, dpi: float = 96.0) -> bool:
    return render_image(tex, px=px, dpi=dpi) is not None


def warmup() -> None:
    """后台预热：matplotlib 首次 import + 字体缓存约 0.6 秒。"""
    try:
        render_image("x^2", px=14)
    except Exception:
        pass
