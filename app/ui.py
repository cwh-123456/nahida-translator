"""界面层：划词后光标旁的小悬浮图标 + 译文卡片窗口（tkinter，无额外 GUI 依赖）。

配色取自《原神》纳西妲（草神）：深林绿底 + 草绿高亮 + 奶白文字，辅以金色微光。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

try:  # 直接以脚本方式加载时（调试用）退回绝对导入
    from . import mathrender
except ImportError:  # pragma: no cover
    from app import mathrender

log = logging.getLogger("ui")

# 纳西妲主题
BG = "#101f18"          # 深林绿（面板底色）
BG_SOFT = "#1b3327"     # 浅一档的绿（按钮底）
BORDER = "#33553f"      # 边框 / 分隔
FG = "#f1f8e8"          # 奶白正文
MUTED = "#9cb8a5"       # 次要文字
ACCENT = "#9be26f"      # 草元素绿（高亮）
ACCENT_DIM = "#5f9e4a"  # 深一档草绿
GOLD = "#e9c97d"        # 金色点缀（微光/星点）

# 两套配色。leaf 是纳西妲主题，划词翻译和图片翻译都用它，整个程序一个绿。
# light 是白底黑字，目前没有入口在用，只是留着：万一以后想加"白底阅读模式"
# （截图内容多是白纸文档时更接近 PDF 阅读器），换主题的骨架已经在这了。
LEAF = {
    "name": "leaf",
    "bg": BG, "bg_soft": BG_SOFT, "border": BORDER,
    "fg": FG, "muted": MUTED, "accent": ACCENT, "accent_dim": ACCENT_DIM,
    "gold": GOLD, "rule": "#2b4a37", "select": "#335c45",
    "header_top": (28, 62, 47), "header_bottom": (16, 31, 24),
    "header_glow": (140, 230, 150, 34), "header_vein": (150, 220, 150, 26),
    "header_rule": (95, 158, 74, 130), "spark": (233, 201, 125),
    "gif": True,
}
LIGHT = {
    "name": "light",
    "bg": "#ffffff", "bg_soft": "#f3f3f3", "border": "#d6d6d6",
    "fg": "#1a1a1a", "muted": "#6e6e6e", "accent": "#2b6cb0",
    "accent_dim": "#1f4e7e", "gold": "#9a6a12",
    "rule": "#e6e6e6", "select": "#cfe2ff",
    "header_top": (252, 252, 252), "header_bottom": (242, 242, 242),
    "header_glow": (0, 0, 0, 8), "header_vein": (0, 0, 0, 10),
    "header_rule": (214, 214, 214, 255), "spark": (190, 190, 190),
    "gif": False,
}


def _clamp_pos(x: int, y: int, w: int, h: int, root: tk.Misc) -> tuple[int, int]:
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x = max(4, min(int(x), sw - w - 4))
    y = max(4, min(int(y), sh - h - 4))
    return x, y


def pick_cjk_font() -> str | None:
    import os

    for path in (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if os.path.exists(path):
            return path
    return None


def render_glyph_icon(size: int, state: str = "normal", glyph: str = "译"):
    """画一枚圆角渐变图标（4 倍超采样后缩放，边缘平滑），返回 PIL.Image。"""
    from PIL import Image, ImageDraw, ImageFont

    scale = 4
    edge = max(size, 8) * scale
    radius = int(edge * 0.30)
    palettes = {
        "normal": ((163, 232, 122), (74, 143, 62), 60),
        "hover": ((192, 245, 156), (92, 170, 74), 150),
        "pressed": ((136, 205, 100), (58, 120, 50), 100),
    }
    top, bottom, rim_alpha = palettes.get(state, palettes["normal"])

    gradient = Image.new("RGBA", (edge, edge))
    gd = ImageDraw.Draw(gradient)
    for y in range(edge):
        t = y / max(edge - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        gd.line([(0, y), (edge, y)], fill=color)

    mask = Image.new("L", (edge, edge), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, edge - 1, edge - 1), radius=radius, fill=255)
    icon = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    icon.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle(
        (scale, scale, edge - scale - 1, edge - scale - 1),
        radius=max(radius - scale, 2),
        outline=(255, 255, 255, rim_alpha),
        width=max(1, scale // 2),
    )
    font_path = pick_cjk_font()
    glyph_px = max(10, int(edge * 0.52))
    try:
        font = ImageFont.truetype(font_path, glyph_px) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    cx, cy = edge / 2, edge / 2
    if state == "pressed":
        cy += scale * 0.5
    draw.text((cx, cy + scale), glyph, font=font, fill=(12, 24, 60, 110), anchor="mm")
    draw.text((cx, cy), glyph, font=font, fill=(255, 255, 255, 255), anchor="mm")
    return icon.resize((size, size), Image.LANCZOS)


def apply_round_region(win: tk.Toplevel, size: int, radius_ratio: float = 0.30) -> bool:
    """把方形窗口裁成圆角（Windows 专用，失败则保持方形）。"""
    try:
        import ctypes

        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
        if not hwnd:
            return False
        radius = max(2, int(size * radius_ratio))
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, size + 1, size + 1, radius * 2, radius * 2)
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        return True
    except Exception:
        return False


def taskbar_top() -> int | None:
    """任务栏顶边 y 坐标（任务栏在下方时有效），用于把按钮放到任务栏正上方。"""
    try:
        import ctypes
        from ctypes import wintypes

        class APPBARDATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT),
                ("rc", wintypes.RECT),
                ("lParam", ctypes.c_long),
            ]

        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        if ctypes.windll.shell32.SHAppBarMessage(5, ctypes.byref(abd)):  # ABM_GETTASKBARPOS
            if abd.uEdge == 3:  # ABE_BOTTOM
                return int(abd.rc.top)
    except Exception:
        pass
    # 兜底：用工作区（不含任务栏）的下边界推算
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            if rect.bottom > 0:
                return int(rect.bottom)
    except Exception:
        pass
    return None


# 公式高到几倍行高就放弃"塞一行"，改成独占一行居中。
# 3 行矩阵大约 3.6 倍行高，仍然按行内排；再高（十几行的 aligned）才会走到这里
BLOCK_LINES = 7.0


MODE_TITLES = {
    "auto": "自动识别",
    "translate": "英译中",
    "word": "单词详解",
    "zh2en": "中译英",
    "summary": "长文摘要",
    "term": "术语解释",
}


def render_header_band(width: int, height: int, th: dict | None = None):
    """标题带背景：深绿渐变 + 草绿光晕 + 细叶脉 + 金色星点（纳西妲风格）。

    light 主题用的是同一套结构，只是换成白底灰边、光晕压到几乎看不见，
    几何一次都不用改。
    """
    from PIL import Image, ImageDraw, ImageFilter

    th = th or LEAF
    w = max(int(width), 16)
    h = max(int(height), 16)
    base = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(base)
    top, bottom = th["header_top"], th["header_bottom"]
    for y in range(h):
        t = y / max(h - 1, 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        d.line([(0, y), (w, y)], fill=c + (255,))

    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-w * 0.25, -h * 1.2, w * 0.55, h * 1.5), fill=th["header_glow"])
    glow = glow.filter(ImageFilter.GaussianBlur(max(4, h // 5)))
    base = Image.alpha_composite(base, glow)

    veins = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(veins)
    vein_rgb = th["header_vein"][:3]
    for i, (x0, span, alpha) in enumerate(
        ((w * 0.04, w * 0.55, 26), (w * 0.26, w * 0.48, 18), (w * 0.52, w * 0.42, 14))
    ):
        y0 = h * (0.18 + 0.20 * i)
        vd.arc((x0, y0 - h * 0.6, x0 + span, y0 + h * 1.1), start=200, end=340,
               fill=vein_rgb + (alpha if th["name"] == "leaf" else alpha // 5),
               width=max(1, h // 24))
    base = Image.alpha_composite(base, veins)

    spark = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(spark)
    spark_rgb = th["spark"]
    for sx, sy, r, a in (
        (w * 0.70, h * 0.30, 3.2, 150),
        (w * 0.79, h * 0.62, 2.4, 105),
        (w * 0.61, h * 0.68, 1.8, 85),
        (w * 0.90, h * 0.38, 1.6, 75),
    ):
        if th["name"] != "leaf":
            a //= 6            # 中性主题不要星点抢视线，留一点肌理就够
        sd.line((sx - r, sy, sx + r, sy), fill=spark_rgb + (a,), width=1)
        sd.line((sx, sy - r, sx, sy + r), fill=spark_rgb + (a,), width=1)
    base = Image.alpha_composite(base, spark)

    d = ImageDraw.Draw(base)
    d.line([(0, h - 1), (w, h - 1)], fill=th["header_rule"], width=1)
    return base.convert("RGB")


class PillButton:
    """抗锯齿圆角按钮（PIL 渲染、三态反馈），用于面板上的模式 / 复制 / 重译 / 关闭。"""

    HEIGHT = 27

    def __init__(self, parent: tk.Misc, text: str, command, *, family: str,
                 pt_size: int, accent: bool = False, chevron: bool = False,
                 close: bool = False, bg: str = BG, th: dict | None = None):
        from PIL import ImageTk

        self.parent = parent
        self.text = text
        self.command = command
        self.family = family
        self.pt_size = pt_size
        self.accent = accent
        self.chevron = chevron
        self.close = close
        self.th = th or LEAF
        self.bg = self.th["bg"]
        self._state = "normal"
        self._images: dict[str, object] = {}

        self.widget = tk.Label(parent, bd=0, highlightthickness=0, padx=0, pady=0,
                               cursor="hand2", bg=self.bg)
        for state in ("normal", "hover", "pressed"):
            self._images[state] = ImageTk.PhotoImage(self._render(state))
        self.widget.configure(image=self._images["normal"])
        self.widget.bind("<Enter>", lambda _e: self._set("hover"))
        self.widget.bind("<Leave>", lambda _e: self._set("normal"))
        self.widget.bind("<ButtonPress-1>", lambda _e: self._set("pressed"))
        self.widget.bind("<ButtonRelease-1>", self._on_release)

    # ---------- 绘制 ----------
    def _palette(self, state: str):
        if self.th["name"] == "light":
            # 白底上的按钮：浅灰填充 + 细边框，强调色用深蓝描边，
            # 不要做成深色块——那样会把白底页面切得很碎
            if self.close:
                return {
                    "normal": ((0, 0, 0, 0), None, (120, 120, 120, 255)),
                    "hover": ((226, 226, 226, 255), None, (30, 30, 30, 255)),
                    "pressed": ((208, 208, 208, 255), None, (30, 30, 30, 255)),
                }[state]
            if self.accent:
                return {
                    "normal": ((255, 255, 255, 255), (43, 108, 176, 110), (30, 30, 30, 255)),
                    "hover": ((238, 245, 255, 255), (43, 108, 176, 210), (20, 40, 70, 255)),
                    "pressed": ((222, 236, 254, 255), (43, 108, 176, 160), (20, 40, 70, 255)),
                }[state]
            return {
                "normal": ((244, 244, 244, 255), (214, 214, 214, 255), (40, 40, 40, 255)),
                "hover": ((234, 234, 234, 255), (185, 185, 185, 255), (20, 20, 20, 255)),
                "pressed": ((222, 222, 222, 255), (170, 170, 170, 255), (20, 20, 20, 255)),
            }[state]

        if self.close:
            if state == "hover":
                return (38, 67, 47, 255), None, (241, 248, 232, 255)
            if state == "pressed":
                return (28, 51, 39, 255), None, (241, 248, 232, 255)
            return (0, 0, 0, 0), None, (156, 184, 165, 255)
        if self.accent:
            return {
                "normal": ((29, 59, 43, 255), (155, 226, 111, 120), (200, 240, 168, 255)),
                "hover": ((41, 80, 58, 255), (185, 244, 150, 220), (226, 255, 207, 255)),
                "pressed": ((24, 49, 36, 255), (155, 226, 111, 160), (210, 245, 180, 255)),
            }[state]
        return {
            "normal": ((27, 51, 39, 255), (51, 85, 63, 255), (241, 248, 232, 255)),
            "hover": ((38, 67, 47, 255), (79, 130, 89, 255), (255, 255, 255, 255)),
            "pressed": ((22, 41, 30, 255), (64, 108, 76, 255), (232, 241, 224, 255)),
        }[state]

    def _render(self, state: str):
        from PIL import Image, ImageDraw, ImageFont

        s = 4
        try:
            ppp = float(self.parent.winfo_fpixels("1p"))
        except Exception:
            ppp = 1.333
        font_px = max(10, int(round(self.pt_size * ppp)))
        font_path = pick_cjk_font()
        try:
            font = ImageFont.truetype(font_path, font_px * s) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        h = self.HEIGHT
        pad = 14 * s
        fill, border, fg = self._palette(state)
        if self.close:
            w = h
        else:
            text_w = font.getlength(self.text)
            chev = 13 * s if self.chevron else 0
            w = int(round((text_w + chev) / s)) + 28
        W, H = w * s, h * s

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        radius = H / 2
        if self.close:
            if fill[3] > 0:
                d.ellipse((1, 1, W - 2, H - 2), fill=fill)
            m = W * 0.30
            width = max(2, 2 * s)
            d.line((m, m, W - m, H - m), fill=fg, width=width)
            d.line((W - m, m, m, H - m), fill=fg, width=width)
        else:
            d.rounded_rectangle((1, 1, W - 2, H - 2), radius=radius - 1, fill=fill,
                                outline=border, width=max(1, s // 2) if border else 0)
            d.text((pad, H / 2), self.text, font=font, fill=fg, anchor="lm")
            if self.chevron:
                text_w = font.getlength(self.text)
                cx = pad + text_w + 8 * s
                cy = H / 2
                wdt = max(1, 2 * s // 2)
                d.line((cx - 4 * s, cy - 2 * s, cx, cy + 2 * s), fill=fg, width=wdt)
                d.line((cx, cy + 2 * s, cx + 4 * s, cy - 2 * s), fill=fg, width=wdt)
        return img.resize((w, h), Image.LANCZOS)

    # ---------- 交互 ----------
    def _set(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.widget.configure(image=self._images[state])

    def _on_release(self, event) -> None:
        inside = (0 <= event.x < self.widget.winfo_width()
                  and 0 <= event.y < self.widget.winfo_height())
        self._set("hover" if inside else "normal")
        if inside and self.command:
            self.command()

    def set_text(self, text: str) -> None:
        if text == self.text:
            return
        from PIL import ImageTk

        self.text = text
        for state in ("normal", "hover", "pressed"):
            self._images[state] = ImageTk.PhotoImage(self._render(state))
        self.widget.configure(image=self._images[self._state])


class ModePopup:
    """自绘的模式选择浮层：深色圆角、悬停高亮、当前项打勾。"""

    WIDTH = 176
    ROW_H = 34

    def __init__(self, root: tk.Tk, family: str, labels: list[tuple[str, str]],
                 current: str, on_select):
        self.root = root
        self.family = family
        self.labels = labels
        self.current = current
        self.on_select = on_select
        self.win: tk.Toplevel | None = None
        self._rows: dict[str, tuple[tk.Frame, tk.Canvas, tk.Label]] = {}
        self._x = self._y = 0
        self._region_size = None
        self._shown = False

    def _build(self) -> None:
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.configure(bg=BORDER)
        inner = tk.Frame(win, bg=BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(inner, bg=BG, height=6).pack(fill="x")
        for key, label in self.labels:
            row = tk.Frame(inner, bg=BG, height=self.ROW_H, cursor="hand2")
            row.pack(fill="x", padx=6, pady=1)
            row.pack_propagate(False)
            check = tk.Canvas(row, width=16, height=16, bg=BG,
                              highlightthickness=0, bd=0)
            check.pack(side="left", padx=(9, 6))
            txt = tk.Label(row, text=label, bg=BG, fg=FG, anchor="w",
                           font=(self.family, 10), cursor="hand2")
            txt.pack(side="left", fill="x", expand=True)
            for widget in (row, check, txt):
                widget.bind("<Enter>", lambda _e, k=key: self._hover(k))
                widget.bind("<Leave>", lambda _e: self._paint())
                widget.bind("<ButtonRelease-1>", lambda _e, k=key: self._choose(k))
            self._rows[key] = (row, check, txt)
        tk.Frame(inner, bg=BG, height=6).pack(fill="x")
        win.bind("<Escape>", lambda _e: self.close())
        self.win = win

    # ---------- 绘制 ----------
    def _paint(self, hover: str | None = None) -> None:
        for key, (row, check, txt) in self._rows.items():
            selected = key == self.current
            if key == hover:
                bg = "#2f5240"
            elif selected:
                bg = "#1f3a2e"
            else:
                bg = BG
            fg = ACCENT if selected else FG
            row.configure(bg=bg)
            check.configure(bg=bg)
            txt.configure(bg=bg, fg=fg)
            check.delete("all")
            if selected:
                check.create_line(3, 9, 7, 13, fill=ACCENT, width=2)
                check.create_line(7, 13, 13, 5, fill=ACCENT, width=2)

    def _hover(self, key: str) -> None:
        self._paint(hover=key)

    def _choose(self, key: str) -> None:
        self.close()
        try:
            self.on_select(key)
        except Exception:
            pass

    # ---------- 显示 ----------
    def _height(self) -> int:
        return 6 + len(self.labels) * (self.ROW_H + 2) + 6 + 2

    def _apply_round_region(self) -> None:
        if self.win is None:
            return
        try:
            import ctypes

            self.win.update_idletasks()
            w, h = self.win.winfo_width(), self.win.winfo_height()
            if w <= 4 or h <= 4 or (w, h) == self._region_size:
                return
            hwnd = ctypes.windll.user32.GetAncestor(self.win.winfo_id(), 2)
            if not hwnd:
                return
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, 24, 24)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
            self._region_size = (w, h)
        except Exception:
            pass

    def show(self, x: int, y: int, current: str | None = None) -> None:
        if self.win is None:
            self._build()
        self.current = current or self.current
        w, h = self.WIDTH, self._height()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self._x = max(6, min(int(x), sw - w - 8))
        self._y = max(6, min(int(y), sh - h - 8))
        self._paint()
        self.win.geometry(f"{w}x{h}+{self._x}+{self._y}")
        self.win.deiconify()
        self.win.lift()
        self._shown = True
        self._apply_round_region()
        try:
            self.win.focus_force()
        except tk.TclError:
            pass

    def fast_visible(self) -> bool:
        return self._shown

    def fast_contains(self, x: int, y: int) -> bool:
        if not self._shown:
            return False
        return (self._x <= x <= self._x + self.WIDTH
                and self._y <= y <= self._y + self._height())

    def close(self) -> None:
        self._shown = False
        if self.win is not None:
            try:
                self.win.withdraw()
            except tk.TclError:
                self.win = None

    def destroy(self) -> None:
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None

    def visible(self) -> bool:
        return self.win is not None and self.win.winfo_ismapped()

    def contains(self, x: int, y: int) -> bool:
        if not self.visible():
            return False
        try:
            return (self._x <= x <= self._x + self.WIDTH
                    and self._y <= y <= self._y + self._height())
        except Exception:
            return False


class MascotWindow:
    """右下角的循环动图「看板娘」：无边框圆角小窗，可拖动、点击可唤起面板。"""

    def __init__(self, root: tk.Tk, cfg: dict, gif_path: str,
                 on_click=None, on_menu=None):
        self.root = root
        self.cfg = cfg
        self.gif_path = gif_path
        self.on_click = on_click
        self.on_menu = on_menu
        self.size = int(cfg["ui"].get("mascot_size", 180))
        self.win: tk.Toplevel | None = None
        self.label: tk.Label | None = None
        self._frames: list = []
        self._durations: list[int] = []
        self._idx = 0
        self._job = None
        self._x = self._y = 0
        self._drag = None
        self._moved = False
        self._shown = False
        self._region_applied = False

    # ---------- 加载帧 ----------
    def _load(self) -> None:
        from PIL import Image, ImageSequence, ImageTk

        im = Image.open(self.gif_path)
        for frame in ImageSequence.Iterator(im):
            rgba = frame.convert("RGBA")
            if rgba.size != (self.size, self.size):
                rgba = rgba.resize((self.size, self.size), Image.LANCZOS)
            self._frames.append(ImageTk.PhotoImage(rgba))
            self._durations.append(max(20, int(frame.info.get("duration") or 40)))

    # ---------- 构建 ----------
    def _build(self) -> None:
        if not self._frames:
            self._load()
        if not self._frames:
            return
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.configure(bg=BORDER)
        label = tk.Label(win, bd=0, highlightthickness=0, bg=BG, cursor="hand2")
        label.pack(padx=1, pady=1)
        label.configure(image=self._frames[0])
        label.bind("<ButtonPress-1>", self._press)
        label.bind("<B1-Motion>", self._motion)
        label.bind("<ButtonRelease-1>", self._release)
        label.bind("<Button-3>", self._right_click)
        self.win, self.label = win, label

    def _apply_round_region(self) -> None:
        if self.win is None or self._region_applied:
            return
        if apply_round_region(self.win, self.size + 2, 0.10):
            self._region_applied = True

    # ---------- 显示/隐藏 ----------
    def show(self) -> None:
        if self.win is None:
            self._build()
        if self.win is None:
            return
        pos = self.cfg.get("ui", {}).get("mascot_pos")
        if pos:
            x, y = int(pos[0]), int(pos[1])
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            bar = taskbar_top()
            y = (bar - self.size - 70) if bar else (sh - self.size - 120)
            x = sw - self.size - 22
        self._place(x, y)
        self.win.deiconify()
        self.win.lift()
        self._shown = True
        self._apply_round_region()
        self._schedule()

    def hide(self) -> None:
        self._cancel()
        self._shown = False
        if self.win is not None:
            try:
                self.win.withdraw()
            except tk.TclError:
                self.win = None

    def destroy(self) -> None:
        self._cancel()
        self._shown = False
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None

    def fast_visible(self) -> bool:
        return self._shown

    def fast_contains(self, x: int, y: int) -> bool:
        if not self._shown:
            return False
        return (self._x <= x <= self._x + self.size + 2
                and self._y <= y <= self._y + self.size + 2)

    def visible(self) -> bool:
        return self.win is not None and self.win.winfo_ismapped()

    def contains(self, x: int, y: int) -> bool:
        if not self.visible():
            return False
        return (self._x <= x <= self._x + self.size + 2
                and self._y <= y <= self._y + self.size + 2)

    # ---------- 播放 ----------
    def _schedule(self) -> None:
        self._cancel()
        if not self._frames:
            return
        self._job = self.root.after(self._durations[self._idx], self._tick)

    def _tick(self) -> None:
        self._job = None
        if self.win is None or not self.visible():
            return
        self._idx = (self._idx + 1) % len(self._frames)
        try:
            self.label.configure(image=self._frames[self._idx])
        except tk.TclError:
            return
        self._schedule()

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    # ---------- 交互 ----------
    def _place(self, x: int, y: int) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self._x = max(0, min(int(x), sw - self.size - 2))
        self._y = max(0, min(int(y), sh - self.size - 2))
        self.win.geometry(f"{self.size + 2}x{self.size + 2}+{self._x}+{self._y}")

    def _press(self, event) -> None:
        self._drag = (event.x_root, event.y_root, self._x, self._y)
        self._moved = False

    def _motion(self, event) -> None:
        if not self._drag:
            return
        dx = event.x_root - self._drag[0]
        dy = event.y_root - self._drag[1]
        if abs(dx) + abs(dy) > 4:
            self._moved = True
        self._place(self._drag[2] + dx, self._drag[3] + dy)

    def _release(self, _event=None) -> None:
        was_moved = self._moved
        self._drag = None
        self._moved = False
        if was_moved:
            self.cfg.setdefault("ui", {})["mascot_pos"] = [self._x, self._y]
            return
        if self.on_click:
            try:
                self.on_click()
            except Exception:
                pass

    def _right_click(self, _event=None):
        if self.on_menu:
            try:
                self.on_menu()
            except Exception:
                pass
        return "break"


class FloatingIcon:
    """选区旁的悬浮按钮：PIL 抗锯齿绘制的圆角图标，支持悬停/按下反馈。"""

    def __init__(self, root: tk.Tk, cfg: dict, on_click):
        self.root = root
        self.cfg = cfg
        self.on_click = on_click
        self.win: tk.Toplevel | None = None
        self.label: tk.Label | None = None
        self.size = int(cfg["ui"].get("icon_size", 38))
        self.timeout = float(cfg["ui"].get("icon_timeout", 5.0))
        self._hide_job = None
        self._x = self._y = 0
        self._shown = False
        self._photos: dict[str, object] = {}
        self._state = "normal"
        self._region_applied = False

    # ---------------- 图标绘制（复用统一的圆角图标渲染） ----------------
    def _render_icon(self, state: str):
        return render_glyph_icon(self.size, state)

    # ---------------- 构建 ----------------
    def _build(self) -> None:
        from PIL import ImageTk

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.configure(bg="#4a8f3c")
        label = tk.Label(win, bd=0, highlightthickness=0, padx=0, pady=0, cursor="hand2")
        label.pack()
        for state in ("normal", "hover", "pressed"):
            self._photos[state] = ImageTk.PhotoImage(self._render_icon(state))
        label.configure(image=self._photos["normal"])
        label.bind("<Enter>", self._on_enter)
        label.bind("<Leave>", self._on_leave)
        label.bind("<ButtonPress-1>", self._on_press)
        label.bind("<ButtonRelease-1>", self._on_release)
        self.win, self.label = win, label

    def _apply_round_region(self) -> None:
        """用 Win32 窗口区域把方窗口裁成圆角（避免四角底色外露）。"""
        if self._region_applied or self.win is None:
            return
        if apply_round_region(self.win, self.size):
            self._region_applied = True

    # ---------------- 状态 ----------------
    def _set_state(self, state: str) -> None:
        if self.label is None or state == self._state:
            return
        photo = self._photos.get(state)
        if photo is not None:
            self._state = state
            self.label.configure(image=photo)

    def _on_enter(self, _event=None) -> None:
        self._cancel_hide()
        self._set_state("hover")

    def _on_leave(self, _event=None) -> None:
        if self._state != "normal":
            self._set_state("normal")

    def _on_press(self, _event=None) -> None:
        self._cancel_hide()
        self._set_state("pressed")

    def _on_release(self, event=None) -> None:
        inside = True
        if event is not None:
            inside = 0 <= event.x <= self.size and 0 <= event.y <= self.size
        self._set_state("hover" if inside else "normal")
        if inside:
            self._clicked()

    def _clicked(self) -> None:
        self._cancel_hide()
        self.hide()
        self.on_click()

    # ---------------- 显示/隐藏 ----------------
    def show(self, x: int, y: int) -> None:
        if self.win is None:
            self._build()
        self._cancel_hide()
        dx, dy = int(self.size * 0.42), int(self.size * 0.5)
        px, py = _clamp_pos(x + dx, y + dy, self.size, self.size, self.root)
        self._x, self._y = px, py
        self._shown = True
        self._state = "normal"
        if self.label is not None:
            self.label.configure(image=self._photos["normal"])
        self.win.geometry(f"{self.size}x{self.size}+{px}+{py}")
        self.win.deiconify()
        self.win.lift()
        self._apply_round_region()
        self._hide_job = self.root.after(int(self.timeout * 1000), self.hide)

    def hide(self) -> None:
        self._cancel_hide()
        self._shown = False
        if self.win is not None:
            try:
                self.win.withdraw()
                self._state = "normal"
            except tk.TclError:
                self.win = None

    # 以下两个方法不调用 Tk，可在系统钩子线程里安全使用
    def fast_visible(self) -> bool:
        return self._shown

    def fast_contains(self, x: int, y: int) -> bool:
        if not self._shown:
            return False
        return (self._x <= x <= self._x + self.size
                and self._y <= y <= self._y + self.size)

    def _cancel_hide(self) -> None:
        if self._hide_job is not None:
            try:
                self.root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None

    @property
    def position(self) -> tuple[int, int]:
        return self._x, self._y

    def visible(self) -> bool:
        return self.win is not None and self.win.winfo_ismapped()

    def contains(self, x: int, y: int) -> bool:
        if not self.visible():
            return False
        return self._x <= x <= self._x + self.size and self._y <= y <= self._y + self.size


class DockButton:
    """常驻在任务栏正上方的「译」按钮：点一下把翻译面板拉到最前面。"""

    def __init__(self, root: tk.Tk, cfg: dict, on_click, on_menu=None):
        self.root = root
        self.cfg = cfg
        self.on_click = on_click
        self.on_menu = on_menu
        self.size = int(cfg["ui"].get("dock_size", 46))
        self.win: tk.Toplevel | None = None
        self.label: tk.Label | None = None
        self._photos: dict[str, object] = {}
        self._state = "normal"
        self._region_applied = False
        self._x = self._y = 0
        self._shown = False
        self._x = self._y = 0

    def _build(self) -> None:
        from PIL import ImageTk

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.configure(bg="#4a8f3c")
        label = tk.Label(win, bd=0, highlightthickness=0, padx=0, pady=0, cursor="hand2")
        label.pack()
        for state in ("normal", "hover", "pressed"):
            self._photos[state] = ImageTk.PhotoImage(render_glyph_icon(self.size, state))
        label.configure(image=self._photos["normal"])
        label.bind("<Enter>", lambda _e: self._set_state("hover"))
        label.bind("<Leave>", lambda _e: self._set_state("normal"))
        label.bind("<ButtonPress-1>", lambda _e: self._set_state("pressed"))
        label.bind("<ButtonRelease-1>", self._on_release)
        label.bind("<Button-3>", self._on_right_click)
        self.win, self.label = win, label

    def _on_right_click(self, _event=None):
        if self.on_menu:
            try:
                self.on_menu()
            except Exception:
                pass
        return "break"

    def _set_state(self, state: str) -> None:
        if self.label is None or self._state == state:
            return
        photo = self._photos.get(state)
        if photo is not None:
            self._state = state
            self.label.configure(image=photo)

    def _on_release(self, event=None) -> None:
        self._set_state("hover")
        if event is not None and not (0 <= event.x <= self.size and 0 <= event.y <= self.size):
            return
        self.on_click()

    def show(self) -> None:
        if self.win is None:
            self._build()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        bar_top = taskbar_top()
        y = (bar_top - self.size - 12) if bar_top else (sh - self.size - 70)
        x = sw - self.size - 18
        self._x = max(4, min(x, sw - self.size - 4))
        self._y = max(4, min(y, sh - self.size - 4))
        self.win.geometry(f"{self.size}x{self.size}+{self._x}+{self._y}")
        self.win.deiconify()
        self.win.lift()
        self._shown = True
        if not self._region_applied and apply_round_region(self.win, self.size):
            self._region_applied = True

    def hide(self) -> None:
        self._shown = False
        if self.win is not None:
            try:
                self.win.withdraw()
            except tk.TclError:
                self.win = None

    def destroy(self) -> None:
        self._shown = False
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None

    def fast_visible(self) -> bool:
        return self._shown

    def fast_contains(self, x: int, y: int) -> bool:
        if not self._shown:
            return False
        return self._x <= x <= self._x + self.size and self._y <= y <= self._y + self.size

    def contains(self, x: int, y: int) -> bool:
        if self.win is None or not self.win.winfo_ismapped():
            return False
        return self._x <= x <= self._x + self.size and self._y <= y <= self._y + self.size


WS_EX_TRANSPARENT = 0x20
GWL_EXSTYLE = -20

# 面板在"结果已经出现"之后保持可交互的秒数。
# 之前是 6 秒，且只从 open()（任务开始）算——图片翻译要 12 秒，结果到手时
# 早就过期了，用户点叉必然要点两下。现在由 touch() 在结果/每个 token 时重置。
INTERACTIVE_SECONDS = 30.0


def set_click_through(win: tk.Toplevel, enable: bool) -> bool:
    """让窗口鼠标穿透（点击穿透到下面的程序），用于面板挡住所选文字时。"""
    try:
        import ctypes

        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enable:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        return True
    except Exception:
        return False


def mouse_left_down() -> bool:
    """左键此刻是不是真的按着（不依赖 Tk 有没有收到事件）。"""
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
    except Exception:
        return True


def move_together(items) -> bool:
    """把多个窗口在**同一批**里摆到位（DeferWindowPos，原子移动）。

    为什么不直接用 Tk 的 `geometry()`：它只是记账，真正的 SetWindowPos 排在
    Tk 的 idle 队列里。拖动的每个鼠标事件都会记一次，而快速拖动时事件把队列
    刷满、idle 迟迟轮不上，两个窗口的 SetWindowPos 之间就可能夹进系统合成
    的一帧——用户看到的就是"背景动图落后于面板，手一停就追上来"。

    这里绕开 Tk 直接批量下发：Begin/EndDeferWindowPos 之间系统不会重画，
    所以不存在"只挪了其中一个"的中间态。

    items: [(toplevel, x, y, w, h), ...]
    """
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        prepared = []
        for win, x, y, w, h in items:
            if win is None:
                return False
            hwnd = u32.GetAncestor(win.winfo_id(), 2)
            if not hwnd:
                return False
            prepared.append((hwnd, int(x), int(y), max(int(w), 1), max(int(h), 1)))
        if not prepared:
            return False

        u32.BeginDeferWindowPos.restype = ctypes.c_void_p
        u32.BeginDeferWindowPos.argtypes = [ctypes.c_int]
        u32.DeferWindowPos.restype = ctypes.c_void_p
        u32.DeferWindowPos.argtypes = [
            ctypes.c_void_p, wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        u32.EndDeferWindowPos.restype = wintypes.BOOL
        u32.EndDeferWindowPos.argtypes = [ctypes.c_void_p]

        SWP_NOZORDER = 0x0004   # 别动层级：背景必须留在面板下面
        SWP_NOACTIVATE = 0x0010  # 别抢焦点
        hdwp = u32.BeginDeferWindowPos(len(prepared))
        if not hdwp:
            return False
        for hwnd, x, y, w, h in prepared:
            hdwp = u32.DeferWindowPos(
                hdwp, hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE
            )
            if not hdwp:
                return False
        return bool(u32.EndDeferWindowPos(hdwp))
    except Exception:
        return False


def sys_rect(win: tk.Toplevel) -> tuple[int, int, int, int] | None:
    """窗口在系统里的真实矩形 (x, y, w, h)。

    只读（GetWindowRect），不发任何窗口消息，不会重入 Tk 的事件回调，
    所以在 Tk 主线程里随时可以调。Tk 自己的 winfo_x 反映的是"Tk 认为"
    的位置，两者不一致时以系统为准——用户看到的就是系统这一份。
    """
    if win is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
        if not hwnd:
            return None
        r = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        return None


def apply_round_rect_region(win: tk.Toplevel, w: int, h: int, radius: int = 16) -> bool:
    """把矩形窗口裁成圆角（任意宽高）。"""
    try:
        import ctypes

        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
        if not hwnd or w <= 4 or h <= 4:
            return False
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        return True
    except Exception:
        return False


class PanelBackground:
    """面板背后的循环动图：按面板尺寸 cover 适配（填满高度、左右裁剪），面板半透明透出。"""

    SRC = 300  # 预缩放尺寸，播放时再按面板尺寸裁切放大

    def __init__(self, root: tk.Tk, gif_path: str):
        self.root = root
        self.gif_path = gif_path
        self.win: tk.Toplevel | None = None
        self.label: tk.Label | None = None
        self._frames: list = []
        self._durations: list[int] = []
        self._idx = 0
        self._job = None
        self._photo = None
        self._target = (700, 850)
        self._region = None
        self._geom = None
        self.darken = 0.16

    # ---------- 加载 ----------
    def _load(self) -> None:
        from PIL import Image, ImageSequence

        im = Image.open(self.gif_path)
        for frame in ImageSequence.Iterator(im):
            rgb = frame.convert("RGB")
            if rgb.size != (self.SRC, self.SRC):
                rgb = rgb.resize((self.SRC, self.SRC), Image.BILINEAR)
            # 预先压暗（在 300px 小图上做，播放时不再逐帧混合，省 CPU）
            rgb = Image.blend(rgb, Image.new("RGB", (self.SRC, self.SRC), (10, 20, 15)), 0.16)
            self._frames.append(rgb)
            self._durations.append(max(20, int(frame.info.get("duration") or 33)))

    # ---------- 窗口 ----------
    def _build(self) -> None:
        if not self._frames:
            self._load()
        if not self._frames:
            return
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg="#0b1712")
        # anchor="nw"：万一窗口比帧图大一两个像素（几何刚写完还没被系统应用），
        # 让图贴左上角，而不是居中留一圈暗边
        label = tk.Label(win, bd=0, highlightthickness=0, bg="#0b1712", anchor="nw")
        label.pack(fill="both", expand=True)
        self.win, self.label = win, label

    def show(self, x: int, y: int, w: int, h: int) -> None:
        if self.win is None:
            self._build()
        if self.win is None:
            return
        self._target = (max(int(w), 10), max(int(h), 10))
        self._geom = (int(x), int(y), self._target[0], self._target[1])
        self.win.geometry(f"{self._target[0]}x{self._target[1]}+{int(x)}+{int(y)}")
        if not self.win.winfo_ismapped():
            self.win.deiconify()
        self._apply_region()
        # 立刻画第一帧：否则要等第一次 tick（≥70ms），这段时间背景是空白的
        self._render_now()
        self._schedule()

    def sync(self, x: int, y: int, w: int, h: int) -> None:
        if self.win is None:
            return
        w, h = max(int(w), 10), max(int(h), 10)
        geom = (int(x), int(y), w, h)
        if geom == self._geom and self.win.winfo_ismapped():
            return
        self._geom = geom
        resized = (w, h) != self._target
        if resized:
            self._target = (w, h)
            self._region = None
        try:
            self.win.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        except tk.TclError:
            return
        self._apply_region()
        # 尺寸一变就必须当场重画帧图：动画 tick 是 ≥70ms 一次，拖动期间还被
        # pause() 停掉了，等它的话标签里一直是旧尺寸的图——居中放进新窗口，
        # 就是"背景动图和面板不同步"（流式翻译时面板每 80ms 就可能长高一点）。
        if resized:
            self._render_now()

    def force_geometry(self, x: int, y: int, w: int, h: int) -> None:
        """无条件把窗口挪到 (x, y) 并改成 (w, h)。

        和 sync() 的区别：sync() 认为"几何没变"就不动手，可一旦系统的真实
        位置被别的什么东西挪走过，Tk 记的还是老坐标，两条记录对不上时
        `geometry()` 会被 Tk 当成"没变化"直接跳过，窗口就永远卡错位。
        这里先挪开一个像素再挪回来，逼 Tk 真发一次 SetWindowPos。
        """
        if self.win is None:
            return
        x, y = int(x), int(y)
        w, h = max(int(w), 10), max(int(h), 10)
        self._target = (w, h)
        self._geom = (x, y, w, h)
        if self._region != (w, h):
            self._region = None
        for gx, gy in ((x + 1, y + 1), (x, y)):
            try:
                self.win.geometry(f"{w}x{h}+{gx}+{gy}")
                self.win.update_idletasks()
            except tk.TclError:
                return
        self._apply_region()
        self._render_now()

    def hide(self) -> None:
        self._cancel()
        if self.win is None:
            return
        try:
            self.win.withdraw()
        except tk.TclError:
            pass
        # 半透明 + 圆角 region 的 overrideredirect 窗口，偶尔 Tk 侧已经算作隐藏、
        # 但 HWND 还留在屏幕上（表现就是"面板关了，只剩一块背景动图"）。
        # 这里额外用系统调用确认一次，不依赖 Tk 的内部状态。
        try:
            import ctypes

            u32 = ctypes.windll.user32
            hwnd = u32.GetAncestor(self.win.winfo_id(), 2)
            if hwnd and u32.IsWindowVisible(hwnd):
                u32.ShowWindow(hwnd, 0)  # SW_HIDE
                log.warning("背景层窗口未随面板隐藏，已用系统调用强制隐藏")
        except Exception:
            pass

    def destroy(self) -> None:
        self._cancel()
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None

    def _apply_region(self) -> None:
        if self.win is None:
            return
        w, h = self._target
        if self._region == (w, h):
            return
        if apply_round_rect_region(self.win, w, h, 16):
            self._region = (w, h)

    # ---------- 播放 ----------
    def pause(self) -> None:
        """暂停播放：拖动/缩放面板时调用，避免逐帧 PIL 运算拖慢主线程。"""
        self._cancel()

    def resume(self) -> None:
        if self.win is not None and self.win.winfo_ismapped():
            # 拖动期间尺寸可能变过（流式翻译还在长高），先把帧图对齐再恢复播放
            self._render_now()
            self._schedule()

    def _render_now(self) -> None:
        """按当前目标尺寸立刻重画一帧，不等动画 tick。"""
        if self.win is None or self.label is None or not self._frames:
            return
        try:
            self.label.configure(image=self._render())
        except tk.TclError:
            pass

    def _schedule(self) -> None:
        self._cancel()
        if not self._frames:
            return
        # 面板背景不需要高帧率，压到 ~14fps，给主线程和系统钩子留余量
        self._job = self.root.after(max(70, self._durations[self._idx]), self._tick)

    def _tick(self) -> None:
        self._job = None
        if self.win is None or not self.win.winfo_ismapped():
            return
        try:
            self._idx = (self._idx + 1) % len(self._frames)
            self.label.configure(image=self._render())
        except tk.TclError:
            return
        self._schedule()

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _compose(self, w: int, h: int):
        """把当前帧按 cover 裁到 (w, h)，返回 PIL 图。

        纵向**必须锚在顶部**（top=0）：居中裁切的话，面板每长高一点，整张动图
        就跟着往上滑半个高度差——流式翻译里每 80ms 长一次，看到的就是"背景
        和面板不同步、动图在自己往上爬"。锚顶之后变高只是从下面多露出一截。
        """
        from PIL import Image

        frame = self._frames[self._idx]
        sw, sh = frame.size
        scale = max(w / sw, h / sh)  # cover：填满高度，左右裁掉多余
        nw, nh = max(1, int(round(sw * scale))), max(1, int(round(sh * scale)))
        img = frame.resize((nw, nh), Image.BILINEAR)
        left = max(0, (nw - w) // 2)
        return img.crop((left, 0, left + w, h))

    def _render(self):
        from PIL import ImageTk

        w, h = self._target
        self._photo = ImageTk.PhotoImage(self._compose(w, h))
        return self._photo


class ResultWindow:
    """译文卡片：标题、原文摘要、可选中复制的译文、耗时状态与操作按钮。"""

    def __init__(self, root: tk.Tk, cfg: dict, on_retry=None, on_copy=None,
                 on_close=None, on_mode_click=None, bg_gif: str | None = None,
                 style: str = "leaf", on_dismiss=None):
        self.root = root
        self.cfg = cfg
        self.on_retry = on_retry
        self.on_copy = on_copy
        self.on_close = on_close
        self.on_dismiss = on_dismiss
        self.on_mode_click = on_mode_click
        self._style = style if style in ("leaf", "light") else "leaf"
        self.th = LIGHT if self._style == "light" else LEAF
        self.win: tk.Toplevel | None = None
        self.text: tk.Text | None = None
        self.status: tk.Label | None = None
        self.source_input: tk.Text | None = None   # 原文：可编辑，用户能改/直接输入
        self._src_ph = "在此输入要翻译的内容…（Ctrl+回车开始）"
        self._src_ph_on = False      # 输入框里当前显示的是不是占位提示
        self._src_focused = False
        self.mode_label: tk.Label | None = None
        self._mode_text = "自动识别"
        self._region_size: tuple[int, int] | None = None
        self.width = int(cfg["ui"].get("popup_width", 500))
        self.min_h = int(cfg["ui"].get("popup_height", 300))
        self._x = self._y = 0
        self._fit_job = None
        self._source = ""
        self._shown = False
        self._geom_cache = (0, 0, 1, 1)
        self._interactive_until = 0.0
        self._click_through = False
        # 正文区的屏幕矩形（x, y, w, h）。钩子线程只读这个缓存做算数判断，
        # 绝不碰 Tk；主线程每 250ms 刷新一次。
        self._body_rect: tuple[int, int, int, int] | None = None
        self._resize_origin = None
        self._resize_dir = None
        self._drag_active = False
        # 拖动/缩放期间被推迟的重排（松手后补一次），见 _fit()
        self._fit_deferred = False
        self._resize_strips: dict[str, tk.Frame] = {}
        self.scroll: tk.Scrollbar | None = None
        self._scroll_shown = False

        # 背景动画层：面板半透明，让底下的动图透出来。
        # 只有 leaf 主题（纳西妲绿）要这一层；light 主题是实心白底，不需要动图。
        self._bg_gif_path = bg_gif
        self.bg = PanelBackground(root, bg_gif) if (bg_gif and self.th["gif"]) else None
        self._panel_alpha = self._alpha_from_cfg()
        self._syncing_bg = False
        self._bg_geom: tuple[int, int, int, int] | None = None
        self._order_warned = 0.0
        self._align_warned = 0.0
        self._topmost_job = None
        self._verify_jobs: list = []
        # 公式混排：图片对象必须留引用，否则被 GC 后控件里会变成空白
        self._math_photos: list = []
        self._plain = ""          # 未经渲染的原文（含 LaTeX），复制时用它
        self._region_applied = False

    def _alpha_from_cfg(self) -> float:
        if self.bg is None:
            return 1.0
        try:
            return float(self.cfg["ui"].get("panel_alpha", 0.72))
        except Exception:
            return 0.72

    def set_style(self, style: str) -> None:
        """切换配色：leaf（纳西妲绿）/ light（白底黑字）。

        当前产品决定是「图片翻译和划词翻译都用纳西妲绿」，两条路径都传 leaf，
        所以 light 平时不会被用到，留着是因为换主题的骨架已经在这了：以后想加
        「白底阅读模式」只要给按钮接一句 set_style("light")，布局代码不用动。

        面板是长生命周期的单例，已经建好的控件不会认新颜色，所以换主题时
        直接销毁窗口，下次 show 时按新主题重新 _build 一次。
        """
        style = style if style in ("leaf", "light") else "leaf"
        if style == self._style:
            return
        self._style = style
        self.th = LIGHT if style == "light" else LEAF
        if self.bg is not None:
            try:
                self.bg.hide()
                if self.bg.win is not None:
                    self.bg.win.destroy()
            except Exception:
                pass
            self.bg = None
        if self.th["gif"] and self._bg_gif_path:
            self.bg = PanelBackground(self.root, self._bg_gif_path)
        self._panel_alpha = self._alpha_from_cfg()
        self._destroy_window()

    def _destroy_window(self) -> None:
        """销毁窗口和控件引用，让下一次 show 按当前主题重新构建。"""
        for attr in ("_fit_job", "_header_job", "_topmost_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._cancel_verify()
        if self.bg is not None:
            try:
                self.bg.hide()
            except Exception:
                pass
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
        self.win = None
        self.text = None
        self.status = None
        self.source_input = None
        self.mode_label = None
        self.header = None
        self.scroll = None
        self._src_ph_on = False
        self._src_focused = False
        self._scroll_shown = False
        self._math_photos = []
        self._resize_strips = {}
        self._shown = False
        self._region_applied = False

    # ---------- 构建 ----------
    def _build(self) -> None:
        ui = self.cfg["ui"]
        family = ui.get("font_family", "Microsoft YaHei UI")
        size = int(ui.get("font_size", 11))

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        # 新窗口天生不带"穿透"样式，标志位必须跟着归零。否则重建窗口后
        # _click_through 还是 True，apply_click_through(True) 会被当成重复设置直接返回
        self._click_through = False
        # 翻译面板不做置顶：跟随系统窗口层级，被其他窗口盖住是正常行为
        if self.bg is not None:
            try:
                win.attributes("-alpha", self._panel_alpha)
            except tk.TclError:
                pass
        win.bind("<Configure>", self._on_win_configure, add="+")
        # 背景层是独立窗口，只能靠事件保证它和面板同生共死：
        # 面板一旦 Unmap（任何路径隐藏，不只是 close()），立刻把背景收掉。
        win.bind("<Unmap>", self._on_panel_unmap, add="+")
        win.bind("<Map>", self._on_panel_map, add="+")
        outer = tk.Frame(win, bg=self.th["border"])
        outer.pack(fill="both", expand=True)
        body = tk.Frame(outer, bg=self.th["bg"])
        body.pack(fill="both", expand=True, padx=1, pady=1)

        # 标题带：自绘的深绿渐变 + 草绿光晕 + 叶脉 + 金色星点
        self.header_h = 46
        self._header_job = None
        self._header_photo = None
        self._header_item = None
        header = tk.Canvas(body, height=self.header_h, bg=self.th["bg"],
                           highlightthickness=0, bd=0)
        header.pack(fill="x")
        header.bind("<Configure>", self._on_header_configure)
        header.bind("<ButtonPress-1>", self._drag_start)
        header.bind("<B1-Motion>", self._drag_move)
        header.bind("<ButtonRelease-1>", self._drag_end)
        self.header = header

        # 模式按钮：抗锯齿圆角按钮，点它弹出模式菜单
        self.mode_pill = PillButton(
            header, self._mode_text, self._on_mode_click, family=family,
            pt_size=max(9, size - 1), accent=True, chevron=True, th=self.th,
        )
        self.mode_label = self.mode_pill.widget
        self._mode_item = header.create_window(
            14, self.header_h // 2, anchor="w", window=self.mode_pill.widget
        )
        # 关闭按钮：独立圆形按钮（<Button-1> 与 <ButtonPress-1> 是同一个事件，
        # 拖拽绑定不能绑到按钮上，否则会把点击回调覆盖掉）
        self.close_pill = PillButton(
            header, "", self.close, family=family,
            pt_size=max(9, size - 1), close=True, th=self.th,
        )
        self.close_btn = self.close_pill.widget
        self._close_item = header.create_window(
            14, self.header_h // 2, anchor="e", window=self.close_pill.widget
        )

        if ui.get("show_source", True):
            # 原文区：可编辑的多行文本框。划词得到的句子会自动填进来，
            # 用户可以直接改、或删掉后输入自己想翻的内容，点「重译」生效。
            self.source_input = tk.Text(
                body, bg=self.th["bg"], fg=self.th["fg"], bd=0, highlightthickness=0,
                wrap="word", font=(family, int(ui.get("source_font_size", 9))),
                height=1, padx=4, pady=2, cursor="xterm",
                insertbackground=self.th["accent"], selectbackground=self.th["select"],
                spacing1=1, spacing2=1,
            )
            self.source_input.pack(fill="x", padx=8, pady=(8, 6))
            # 空面板时给一句灰字提示，让用户知道这里可以打字；一聚焦就清掉。
            self.source_input.tag_configure("ph", foreground=self.th["muted"])
            # 只有 Ctrl+回车才翻译：普通回车必须留给换行（原文是多行的）
            self.source_input.bind("<Control-Return>", self._on_source_return)
            self.source_input.bind("<FocusIn>", self._on_source_focus_in)
            self.source_input.bind("<FocusOut>", self._on_source_focus_out)
            # 兜底：万一没收到 FocusIn，敲第一个键时也把灰字清掉
            self.source_input.bind("<KeyPress>", self._on_source_key)

        tk.Frame(body, bg=self.th["rule"], height=1).pack(fill="x", padx=12)

        wrap = tk.Frame(body, bg=self.th["bg"])
        wrap.pack(fill="both", expand=True, padx=6, pady=(2, 4))
        self.text = tk.Text(
            wrap, bg=self.th["bg"], fg=self.th["fg"], bd=0, highlightthickness=0, wrap="word",
            font=(family, size), padx=6, pady=6, height=3, cursor="arrow",
            insertbackground=self.th["accent"], selectbackground=self.th["select"],
            spacing1=2, spacing2=2,
        )
        scroll = tk.Scrollbar(wrap, command=self.text.yview, width=8, bd=0,
                              troughcolor=self.th["bg"], bg=self.th["border"], activebackground=self.th["muted"],
                              relief="flat")
        self.scroll = scroll
        self._scroll_shown = True
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.configure(state="disabled")
        # mathtext 渲染不了（矩阵、分段函数等）时，用这个样式把源码标出来
        self.text.tag_configure("raw_math", foreground=self.th["gold"],
                                font=(family, max(9, size - 2)))
        self.text.bind("<Control-c>", self._copy_event)
        self.text.bind("<Button-1>", self._on_text_click)

        footer = tk.Frame(body, bg=self.th["bg"], height=40)
        footer.pack(fill="x", padx=12, pady=(2, 8))
        footer.pack_propagate(False)
        self.copy_pill = PillButton(
            footer, "复制", lambda: self._copy_all(), family=family,
            pt_size=max(9, size - 3), th=self.th,
        )
        self.copy_pill.widget.pack(side="right", padx=(8, 0))
        self.retry_pill = PillButton(
            footer, "重译", lambda: self.on_retry and self.on_retry(), family=family,
            pt_size=max(9, size - 3), th=self.th,
        )
        self.retry_pill.widget.pack(side="right", padx=(8, 0))
        self.status = tk.Label(
            footer, text="", bg=self.th["bg"], fg=self.th["muted"],
            font=(family, max(8, size - 3)), anchor="w",
        )
        self.status.pack(side="left", fill="x", expand=True)

        # 后台把公式渲染器热起来：首次 import matplotlib 要 0.6 秒左右，
        # 不预热的话第一次出现公式时面板会卡一下
        threading.Thread(target=mathrender.warmup, daemon=True).start()

        # 四边 + 四角的透明缩放条（贴在内容边缘，视觉上和背景一致）
        self._build_resize_strips(body)

        win.bind("<Escape>", lambda _e: self.close())
        # 点面板任意位置：把它抬到最前（跟随系统的层级，但点一下要能回来）
        win.bind("<ButtonPress-1>", lambda _e: self._raise_self(), add="+")
        # 拖动只绑在标题栏空白区：标题文字是「模式」按钮、右侧是关闭按钮，
        # 它们各自有自己的点击处理（<Button-1> 与 <ButtonPress-1> 是同一个事件，不能重复绑）
        for widget in (header,):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self.win = win

    # ---------- 背景动画 ----------
    def _on_win_configure(self, _event=None) -> None:
        try:
            if self.win is not None:
                self._geom_cache = (
                    self.win.winfo_x(), self.win.winfo_y(),
                    self.win.winfo_width(), self.win.winfo_height(),
                )
        except tk.TclError:
            pass
        self._sync_bg()

    def _sync_bg(self) -> None:
        """把背景层对齐到面板几何；带防重入 + 去重，避免 Configure 死循环。"""
        # 面板已关闭时不要再去动背景层，否则等于在隐藏之后又去改它的几何和 region
        if not self._shown:
            return
        if self.bg is None or self.win is None or self._syncing_bg:
            return
        self._syncing_bg = True
        try:
            w = self.win.winfo_width()
            h = self.win.winfo_height()
            if w <= 4 or h <= 4:
                return
            geom = (self.win.winfo_x(), self.win.winfo_y(), w, h)
            if geom == self._bg_geom:
                return
            self._bg_geom = geom
            self.bg.sync(*geom)
            # 这里不要再调用 bg.lower(win)：每次移动都改一次 z-order 会触发背景层整窗重绘，
            # 拖动时肉眼就能看到动图比面板慢半拍。z-order 只在显示/抬升时调整一次。
        except tk.TclError:
            pass
        finally:
            self._syncing_bg = False

    def _sync_bg_at(self, x: int, y: int, w: int | None = None, h: int | None = None) -> None:
        """按「将要变成」的几何同步背景层。

        拖动/缩放时面板的 wm geometry 还排在队列里，这时 winfo_x/y 可能还是旧值，
        用它去对齐背景就会稳定落后一帧。这里直接用调用方算好的目标坐标。
        """
        if not self._shown or self.bg is None or self.win is None:
            return
        try:
            if w is None or h is None:
                w, h = self.win.winfo_width(), self.win.winfo_height()
        except tk.TclError:
            return
        if w <= 4 or h <= 4:
            return
        geom = (int(x), int(y), int(w), int(h))
        if geom == self._bg_geom:
            return
        self._bg_geom = geom
        self.bg.sync(*geom)

    def _move_both(self, x: int, y: int, w: int, h: int) -> None:
        """把面板和背景层原子地摆到同一位置、同一尺寸。

        Tk 的 geometry() 只记账，SetWindowPos 要等 idle。拖动时每个鼠标事件都
        记一次，快速拖动会把 idle 挤到后面，于是"面板已经动了、背景还没动"，
        截图和肉眼都能看到错位，手一停又自己恢复。这里直接批量下发，
        两个窗口在同一批里移动，中间没有只动一个的时刻。
        """
        if self.bg is None or self.bg.win is None:
            return
        move_together([
            (self.win, x, y, w, h),
            (self.bg.win, x, y, w, h),
        ])

    # ---------- 面板与背景层的可见性绑定 ----------
    def _is_self_event(self, event) -> bool:
        """只认面板窗口自身的事件，忽略子控件冒泡上来的 Map/Unmap。"""
        if event is None:
            return True
        widget = getattr(event, "widget", None)
        return widget is None or widget is self.win

    def _on_panel_unmap(self, event=None) -> None:
        """面板被隐藏：背景层必须同时隐藏。

        任何隐藏路径（close、异常、后续新增的代码）都会触发 Unmap，
        所以这里兜住的是"面板没了但背景还留在屏幕上"这一类问题。

        注意：bind 在 Toplevel 上时，子控件的 Map/Unmap 也会冒泡到这里
        （滚动条显隐、文本换行都会触发），必须按 event.widget 过滤，
        否则会把"某个子控件被隐藏"误判成"面板被隐藏"。
        """
        if not self._is_self_event(event):
            return
        unexpected = self._shown
        if unexpected:
            self._shown = False
            for attr in ("_fit_job", "_header_job", "_topmost_job"):
                job = getattr(self, attr, None)
                if job is not None:
                    try:
                        self.root.after_cancel(job)
                    except Exception:
                        pass
                    setattr(self, attr, None)
        if self.bg is not None:
            self.bg.hide()
        if unexpected:
            log.warning("面板被非预期隐藏（未经过 close），已同步隐藏背景层")

    def _on_panel_map(self, event=None) -> None:
        """面板重新显示：确保背景层也在，并且贴着当前几何。"""
        if not self._is_self_event(event):
            return
        if not self._shown or self.bg is None or self.win is None:
            return
        try:
            w, h = self.win.winfo_width(), self.win.winfo_height()
            if w <= 4 or h <= 4:
                return
            if self.bg.win is None or not self.bg.win.winfo_ismapped():
                self.bg.show(self._x, self._y, w, h)
            else:
                self.bg.sync(self._x, self._y, w, h)
            self._bg_geom = (self._x, self._y, w, h)
        except tk.TclError:
            pass

    def _cancel_verify(self) -> None:
        """取消关闭时排队的复查任务（面板重新显示时必须清掉）。"""
        for job in self._verify_jobs:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._verify_jobs.clear()

    def _verify_hidden(self, tag: str = "") -> None:
        """关闭后复查：面板与背景层在系统层面都不能还可见。

        这里只用只读的系统调用（GetAncestor / IsWindowVisible）判断，
        真需要再隐藏时走 Tk 的 withdraw()。
        不能在 Tk 窗口上调用 SetWindowPos / ShowWindow 这类会发窗口消息的 API：
        Windows 会同步派发 WM_WINDOWPOSCHANGING，重入 Tk 的 <Configure> 回调，
        而此刻 ctypes 正持有释放状态的 GIL，会造成解释器致命错误。
        """
        # 面板又打开了就别管：这是上一次关闭留下的延迟复查，
        # 不能让它把刚恢复显示的面板再藏起来
        if self._shown:
            return
        try:
            import ctypes

            u32 = ctypes.windll.user32
            targets = [("面板", self.win)]
            if self.bg is not None:
                targets.insert(0, ("背景层", self.bg.win))
            for name, win in targets:
                if win is None:
                    continue
                try:
                    hwnd = u32.GetAncestor(win.winfo_id(), 2)
                    still = bool(hwnd) and bool(u32.IsWindowVisible(hwnd))
                except tk.TclError:
                    continue
                if still:
                    try:
                        win.withdraw()
                    except tk.TclError:
                        pass
                    log.warning("关闭后%s仍然可见%s，已再次隐藏", name, tag)
        except Exception:
            pass

    def _raise_with_bg(self) -> None:
        """把面板抬到最前，并保证背景层紧贴在它下面。"""
        try:
            self.win.lift()
            if self.bg is not None and self.bg.win is not None:
                self.bg.win.lower(self.win)
        except tk.TclError:
            pass

    # ---------- 两层窗口的层级不变量 ----------
    # 面板和背景动图是两个各自独立的顶层窗口，它们在系统 z-order 里的先后
    # 不是我们能长期控制的：任何一次点击/激活之后，系统都可能把这两层重新
    # 排一遍。背景层是实心不透明的，一旦翻到面板上面，面板就被完整盖住——
    # 用户看到的就是"面板没了，只剩一块动图"，哪怕面板本身还在、还是 mapped。
    #
    # 穿透状态下这个翻面是必现的：面板不接点击，点击落到下面的程序，系统把
    # 那个程序插到最前面时顺手重排了我们这两层。所以不能只在"打开时排一次"，
    # 得有个便宜的不变量检查，被破坏就压回去。
    GW_HWNDPREV = 3      # 系统里的"更靠上的那个窗口"
    _MAX_WALK = 600      # 走链的保护上限，防止异常数据下死循环

    def bg_order_broken(self) -> bool:
        """背景层是不是跑到面板上面去了。

        只读：GetWindow 走 z-order 链，不发任何窗口消息，不重入 Tk。
        不能用 GetWindow(面板, GW_HWNDPREV) == 背景层 直接判断——两层之间
        常常夹着别的窗口（输入法、托盘、其它 Tk 辅助窗口），直接比会漏判。
        """
        if self.bg is None or self.bg.win is None or self.win is None:
            return False
        try:
            import ctypes

            if not (self.win.winfo_ismapped() and self.bg.win.winfo_ismapped()):
                return False
            u32 = ctypes.windll.user32
            panel = u32.GetAncestor(self.win.winfo_id(), 2)
            cur = u32.GetAncestor(self.bg.win.winfo_id(), 2)
            if not (panel and cur):
                return False
            # 从背景层往"更靠上"的方向走：先遇到面板说明背景层在面板下面（正常）；
            # 一直走到顶都没遇到，说明背景层压在面板上面。
            for _ in range(self._MAX_WALK):
                cur = u32.GetWindow(cur, self.GW_HWNDPREV)
                if not cur:
                    return True
                if cur == panel:
                    return False
            return False
        except Exception:
            return False

    def keep_bg_below(self) -> bool:
        """检查并修复：背景层必须在面板下面。返回是否真的修过。"""
        if not self.bg_order_broken():
            return False
        self._raise_with_bg()
        now = time.time()
        if now - self._order_warned > 5.0:      # 每 5 秒最多记一条，避免刷屏
            self._order_warned = now
            log.warning("背景层翻到了面板上面（面板会被完全盖住），已重新压回下面")
        return True

    def keep_bg_aligned(self) -> bool:
        """检查并修复：背景层的**真实**矩形必须和面板一致。返回是否修过。

        为什么要用系统矩形而不是 Tk 的记录：正常路径（open/sync/拖动）都会
        同时写两边的 geometry，Tk 认为它们一直是对齐的。可一旦某一次
        SetWindowPos 没落到背景窗口上（穿透、窗口被别的程序挪动、Tk 认为
        "几何没变"而跳过），Tk 的记录就和屏幕上的位置分了叉，之后所有
        `sync()` 都被"几何没变"挡掉，错位会一直留到面板尺寸再变一次为止。
        用户看到的就是"面板跑了、动图留在原地"。

        这里只读两次 GetWindowRect（不发窗口消息、不重入 Tk），发现不一致
        就用 force_geometry 逼 Tk 真挪一次。
        """
        if not self._shown or self.bg is None or self.bg.win is None or self.win is None:
            return False
        try:
            if not (self.win.winfo_ismapped() and self.bg.win.winfo_ismapped()):
                return False
        except tk.TclError:
            return False
        rp = sys_rect(self.win)
        rb = sys_rect(self.bg.win)
        if rp is None or rb is None:
            return False
        if rp == rb:
            return False
        now = time.time()
        if now - self._align_warned > 5.0:
            self._align_warned = now
            log.warning(
                "背景层与面板错位：面板 %s，背景 %s（Δ=%s），已重新对齐",
                rp, rb, (rp[0] - rb[0], rp[1] - rb[1], rp[2] - rb[2], rp[3] - rb[3]),
            )
        try:
            w, h = self.win.winfo_width(), self.win.winfo_height()
            x, y = self.win.winfo_x(), self.win.winfo_y()
        except tk.TclError:
            return False
        if w <= 4 or h <= 4:
            return False
        # 面板自己的 Tk 坐标就是屏幕上真实那份（用户看到的面板就在那儿），
        # 所以直接把背景写成面板的 Tk 几何；两个窗口在同一套坐标空间里，
        # 不需要在这里处理 DPI 缩放。
        self.bg.force_geometry(x, y, w, h)
        self._bg_geom = (x, y, w, h)
        return True

    def wake(self) -> None:
        """用户点了面板（此时面板正处在穿透状态）：恢复可交互并把层级摆正。

        穿透状态下那一下点击已经落到下面的程序了，救不回来；这里保证的是
        "下一次点击一定打在面板上"，同时把面板从被盖住的状态里抬回来。
        """
        self._raise_self()
        self.keep_bg_below()

    def click_in_body(self, x: int, y: int) -> bool:
        """这个屏幕坐标是不是落在正文区（而不是标题带 / 底栏 / 按钮）。

        用来区分两种意图：点正文区的人多半是想选面板底下的文字（穿透的初衷），
        点标题带和按钮的人是想操作面板（关掉、切模式、复制）。
        """
        if self.text is None:
            return False
        try:
            x0, y0 = self.text.winfo_rootx(), self.text.winfo_rooty()
            w, h = self.text.winfo_width(), self.text.winfo_height()
        except tk.TclError:
            return False
        return x0 <= x <= x0 + w and y0 <= y <= y0 + h

    def _raise_self(self) -> None:
        """把面板拉到最前：lift 有时压不过前台窗口，短暂置顶再恢复。"""
        try:
            if self.win is None:
                return
            self.touch()
            self._sync_bg()
            self._raise_with_bg()
            try:
                self.win.attributes("-topmost", True)
                if self._topmost_job is not None:
                    try:
                        self.root.after_cancel(self._topmost_job)
                    except Exception:
                        pass
                self._topmost_job = self.root.after(80, self._clear_topmost)
            except tk.TclError:
                pass
        except tk.TclError:
            pass

    def _clear_topmost(self) -> None:
        self._topmost_job = None
        # 面板已经关了就别再动它：在隐藏窗口上改 topmost / z-order 属于多余动作
        if not self._shown:
            return
        try:
            if self.win is not None:
                self.win.attributes("-topmost", False)
            if self.bg is not None and self.bg.win is not None and self.bg.win.winfo_ismapped():
                self.bg.win.lower(self.win)
        except tk.TclError:
            pass

    # ---------- 标题带 ----------
    def _on_header_configure(self, event) -> None:
        width = int(event.width)
        if width < 20:
            return
        try:
            self.header.coords(self._close_item, width - 14, self.header_h // 2)
        except Exception:
            pass
        if self._header_job is not None:
            try:
                self.root.after_cancel(self._header_job)
            except Exception:
                pass
        self._header_job = self.root.after(60, lambda: self._refresh_header(width))

    def _refresh_header(self, width: int) -> None:
        self._header_job = None
        if self.win is None:
            return
        try:
            from PIL import ImageTk

            photo = ImageTk.PhotoImage(render_header_band(width, self.header_h, self.th))
            self._header_photo = photo
            if self._header_item is None:
                self._header_item = self.header.create_image(
                    0, 0, anchor="nw", image=photo
                )
                self.header.tag_lower(self._header_item)
            else:
                self.header.itemconfigure(self._header_item, image=photo)
        except Exception:
            pass

    def _build_resize_strips(self, body: tk.Frame) -> None:
        """在面板四边/四角铺 6~12px 的缩放热区，鼠标移上去变缩放光标，拖动即可改尺寸。"""
        edge, corner = 6, 10
        specs = {
            "n": dict(x=0, y=0, relwidth=1.0, height=edge, anchor="nw",
                      cursor="sb_v_double_arrow"),
            "s": dict(x=0, relx=0.0, rely=1.0, relwidth=1.0, height=edge, anchor="sw",
                      cursor="sb_v_double_arrow"),
            "w": dict(x=0, y=0, width=edge, relheight=1.0, anchor="nw",
                      cursor="sb_h_double_arrow"),
            "e": dict(relx=1.0, y=0, width=edge, relheight=1.0, anchor="ne",
                      cursor="sb_h_double_arrow"),
        }
        corners = {
            "nw": dict(x=0, y=0, width=corner, height=corner, anchor="nw",
                       cursor="size_nw_se"),
            "ne": dict(relx=1.0, y=0, width=corner, height=corner, anchor="ne",
                       cursor="size_ne_sw"),
            "sw": dict(x=0, rely=1.0, width=corner, height=corner, anchor="sw",
                       cursor="size_ne_sw"),
            "se": dict(relx=1.0, rely=1.0, width=corner, height=corner, anchor="se",
                       cursor="size_nw_se"),
        }
        self._resize_strips = {}
        for direction, cfg in {**specs, **corners}.items():
            strip = tk.Frame(body, bg=self.th["bg"], bd=0, highlightthickness=0,
                             cursor=cfg.pop("cursor"))
            strip.place(**cfg)
            strip.bind("<ButtonPress-1>", lambda _e, d=direction: self._edge_press(d, _e))
            strip.bind("<B1-Motion>", self._edge_drag)
            strip.bind("<ButtonRelease-1>", self._edge_release)
            strip.bind("<Enter>", lambda _e: None)
            self._resize_strips[direction] = strip

    def _on_mode_click(self, _event=None):
        if self.on_mode_click:
            try:
                self.on_mode_click()
            except Exception:
                pass
        return "break"

    def _on_close_click(self, _event=None) -> None:
        self.close()
        return "break"

    def _apply_round_region(self) -> None:
        """把面板窗口裁成圆角（Win32 SetWindowRgn），尺寸变化时重新应用。"""
        if self.win is None:
            return
        try:
            import ctypes

            self.win.update_idletasks()
            w, h = self.win.winfo_width(), self.win.winfo_height()
            if w <= 8 or h <= 8 or (w, h) == self._region_size:
                return
            hwnd = ctypes.windll.user32.GetAncestor(self.win.winfo_id(), 2)
            if not hwnd:
                return
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, 32, 32)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
            self._region_size = (w, h)
        except Exception:
            pass

    # ---------- 外部接口 ----------
    def show_empty(self, pos: tuple[int, int] | None = None) -> None:
        """显示一个空的翻译面板（启动时 / 托盘点开时）。"""
        if self.win is None:
            self._build()
        self._cancel_verify()
        self._source = ""
        if self.source_input is not None:
            self._set_source_text("")
        self._set_text("")
        self.set_status("就绪 · 选中后点「译」或按 Ctrl+Alt+T")
        w = self.width
        h = self.min_h
        if pos is None:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            # 默认贴右侧，避免挡住屏幕中间正在阅读/选词的区域
            pos = (max(8, sw - w - 28), max(80, (sh - h) // 2))
        px, py = _clamp_pos(pos[0], pos[1], w, h, self.root)
        self._x, self._y = px, py
        self.win.geometry(f"{w}x{h}+{px}+{py}")
        self.win.deiconify()
        self.win.lift()
        self._shown = True
        self._geom_cache = (px, py, w, h)
        if self.text is not None:
            self.text.configure(height=max(3, (h - 120) // 20))
        self._apply_round_region()
        if self.bg is not None:
            self.bg.show(px, py, w, h)
            self._bg_geom = (px, py, w, h)
            self._raise_with_bg()
        if bool(self.cfg["ui"].get("panel_click_through", False)):
            self.apply_click_through(True)  # 手动指定“始终穿透”
        else:
            self.touch()
        self.refresh_zones()

    @property
    def position(self) -> tuple[int, int]:
        return self._x, self._y
    def set_mode(self, mode: str) -> None:
        """模式按钮文字：显示当前选择的模式。"""
        self._mode_text = MODE_TITLES.get(mode, "自动识别")
        if getattr(self, "mode_pill", None) is not None:
            self.mode_pill.set_text(self._mode_text)

    def open(self, x: int, y: int, source: str, status: str = "正在翻译…") -> None:
        if self.win is None:
            self._build()
        self._cancel_verify()
        was_visible = self.visible()
        self._source = source
        if self.source_input is not None:
            self._set_source_text(source)
        self._set_text("")
        self.set_status(status)
        w = self.width
        h = max(self.win.winfo_height() if was_visible else 0, self.min_h)
        if was_visible:
            # 面板已经开着（用户可能自己挪过位置）：原地更新，不跳位置
            self.win.geometry(f"{w}x{h}+{self._x}+{self._y}")
        else:
            # 首次出现：不要贴在鼠标/选区旁挡住文字。
            # 有记住的位置就用上次的；没有就默认放在屏幕右侧。
            saved = self.cfg.get("ui", {}).get("window_pos")
            if saved:
                px, py = _clamp_pos(int(saved[0]), int(saved[1]), w, h, self.root)
            else:
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                px = max(8, sw - w - 28)
                py = max(80, (sh - h) // 2)
            self._x, self._y = px, py
            self.win.geometry(f"{w}x{h}+{px}+{py}")
        self.win.deiconify()
        self.win.lift()
        self._shown = True
        self._geom_cache = (self._x, self._y, w, h)
        # 不用 focus_force：面板跟随系统层级，不抢占当前程序的键盘焦点
        self._apply_round_region()
        if self.bg is not None:
            self.bg.show(self._x, self._y, w, h)
            self._bg_geom = (self._x, self._y, w, h)
            self._raise_with_bg()
        if bool(self.cfg["ui"].get("panel_click_through", False)):
            self.apply_click_through(True)  # 手动指定“始终穿透”
        else:
            self.touch()
        self.refresh_zones()

    def set_status(self, text: str) -> None:
        if self.status is not None:
            self.status.configure(text=text)

    def set_source(self, source: str) -> None:
        self._source = source
        if self.source_input is not None:
            self._set_source_text(source)

    def _set_source_text(self, text: str) -> None:
        """把原文写进输入框。用户随后可以改，也可以整段换成别的内容。"""
        w = self.source_input
        if w is None:
            return
        self._src_ph_on = False
        w.delete("1.0", "end")
        if text:
            w.insert("1.0", text)
        # 光标放到末尾：划词得到的句子通常是"接着改"，不是从头改
        w.mark_set("insert", "end-1c")
        self._fit_source_height(text)
        if not text and not self._src_focused:
            self._show_source_placeholder()

    def _source_display_lines(self, text: str) -> int:
        """估算这段文字在输入框里会占几行（按真实字体宽度折算换行）。"""
        try:
            font = tkfont.Font(font=self.source_input.cget("font"))
            avail = max(80, self.width - 36)   # 左右 padding + 面板留白
            n = 0
            for para in (text or "").split("\n"):
                w = font.measure(para if para else " ")
                n += max(1, -(-w // avail))    # 向上取整
            return max(1, n)
        except Exception:
            return max(1, (text or "").count("\n") + 1)

    def _fit_source_height(self, text: str) -> None:
        """按内容行数撑高原文框（上限 5 行，再多就在框内滚动）。"""
        if self.source_input is None:
            return
        try:
            n = self._source_display_lines(text)
            self.source_input.configure(height=max(1, min(n, 5)))
        except tk.TclError:
            pass

    def _show_source_placeholder(self) -> None:
        w = self.source_input
        if w is None or not self._src_ph or self._src_ph_on:
            return
        if w.get("1.0", "end-1c"):
            return
        w.insert("1.0", self._src_ph)
        w.tag_add("ph", "1.0", "end")
        self._src_ph_on = True

    def _clear_source_placeholder(self) -> None:
        w = self.source_input
        if w is None or not self._src_ph_on:
            return
        w.delete("1.0", "end")
        self._src_ph_on = False

    def _on_source_focus_in(self, _event=None) -> None:
        self._src_focused = True
        # 一聚焦就把灰字清掉，否则用户得先手动删一遍才能输入
        self._clear_source_placeholder()

    def _on_source_key(self, _event=None) -> None:
        """任意按键上前再清一次灰字（返回 None，让这一键正常落到文本框）。"""
        if self._src_ph_on:
            self._clear_source_placeholder()
        return None

    def _on_source_focus_out(self, _event=None) -> None:
        self._src_focused = False
        w = self.source_input
        if w is None:
            return
        if not w.get("1.0", "end-1c").strip():
            self._clear_source_placeholder()
            self._show_source_placeholder()

    def get_source(self) -> str:
        """读取原文框里当前的内容（用户可能改过，也可能整段换掉）。"""
        w = self.source_input
        if w is None:
            return self._source
        if self._src_ph_on:
            # 光看标志位不够：输入法直接送字、程序化填字都可能绕过焦点事件。
            # 只有框里确实还是那句灰字才当成空，否则按真实内容读。
            try:
                if w.get("1.0", "end-1c") == self._src_ph:
                    return ""
            except tk.TclError:
                return ""
            self._src_ph_on = False
        try:
            return w.get("1.0", "end-1c").strip()
        except tk.TclError:
            return self._source

    def focus_source(self, force: bool = False) -> None:
        """把光标送进原文框（用户点「译」/托盘叫出空面板时用）。

        force=True 时连窗口焦点一起抢过来：面板平时故意不抢焦点，
        但用户是特意点开它来打字的，这一下抢焦点才符合预期。
        """
        if self.source_input is None:
            return
        try:
            if force and self.win is not None:
                self.win.focus_force()
            self._clear_source_placeholder()
            self.source_input.focus_set()
            self.source_input.mark_set("insert", "end-1c")
        except tk.TclError:
            pass

    def _on_source_return(self, _event=None):
        """原文框里按 Ctrl+回车：用当前内容重译一次。"""
        if self.on_retry:
            try:
                self.on_retry()
            except Exception:
                pass
        return "break"

    def append(self, piece: str) -> None:
        if self.text is None:
            return
        # 翻译进行中也要保持可点：用户随时可能想点叉取消（上一轮刚做的取消机制）
        self.touch()
        if self.text.get("1.0", "end-1c") == "" and self.status is not None \
                and self.status.cget("text").startswith("正在"):
            self.set_status("")
        self.text.configure(state="normal")
        self.text.insert("end", piece)
        self.text.see("end")
        self.text.configure(state="disabled")
        self._schedule_fit()

    def clear(self) -> None:
        self._plain = ""
        self._set_text("")

    def set_final(self, text: str) -> None:
        """翻译结束：用清洗后的最终文本替换流式过程中显示的内容。

        带公式时改走混排渲染（公式渲染成图片插进 Text），纯文本仍然走老路径。
        """
        self._set_rich(text)

    def finish(self, seconds: float, tokens: int) -> None:
        self.set_status(f"本地模型 · {seconds:.1f}s")
        # 结果出现的这一刻才算"用户开始看结果"：重置可交互窗口，
        # 否则图片翻译（十几秒）会一出现就处于穿透状态，点叉要点两下。
        self.touch()
        self._schedule_fit(force=True)

    def show_error(self, message: str) -> None:
        self._plain = ""
        self._set_text(message)
        self.set_status("出错了")
        self.touch()

    # ---------- 文字 + 公式混排 ----------
    def _set_rich(self, value: str) -> None:
        value = value or ""
        if not mathrender.has_math(value):
            # 没有公式标记时这一整段都按普通文字画，模型漏出来的 \quad、
            # \text{} 会原样变成源码。这里是最后一道关，顺手清掉。
            value = mathrender.tidy_text(value)
        self._plain = value
        if self.text is None:
            return
        if not mathrender.has_math(value):
            self._set_text(value)
            return
        ui = self.cfg["ui"]
        family = ui.get("font_family", "Microsoft YaHei UI")
        size = int(ui.get("font_size", 11))
        try:
            linespace = tkfont.Font(family=family, size=size).metrics("linespace")
        except tk.TclError:
            linespace = size + 8
        # 公式字面高度跟正文行高对齐，避免同一行里一大一小
        px = max(12, int(round(linespace * 0.76)))
        try:
            dpi = max(72.0, float(self.root.winfo_fpixels("1i")))
        except tk.TclError:
            dpi = 96.0
        max_w = max(140, self.width - 46)

        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self._math_photos.clear()
        segs = mathrender.split_segments(value)
        prev_block = False
        for idx, seg in enumerate(segs):
            if seg[0] == "text":
                s = seg[1]
                nxt = segs[idx + 1] if idx + 1 < len(segs) else None
                # 独立公式自己会换行：紧挨着它的空行交给它处理，
                # 否则「原文换行 + 公式前后各一个换行」会叠出大片空白
                if prev_block:
                    s = s.lstrip("\n")
                if nxt is not None and nxt[0] == "math" and nxt[2]:
                    s = s.rstrip("\n")
                s = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", s)
                if s:
                    self.text.insert("end", s)
                prev_block = False
            else:
                # 先渲染、并定下"要不要独占一行"，再决定吞不吞后面的标点：
                # 这两件事必须用同一个判断，否则标点会被摘走却没地方落脚
                img, block, align = self._prepare_formula(
                    seg[1], seg[2], px, dpi, linespace)
                tail = ""
                nxt = segs[idx + 1] if idx + 1 < len(segs) else None
                # 独立公式后面紧跟的句号/逗号：摘出来跟着公式行，否则它会
                # 独自占一行，整段看着像被切碎了
                if block and img is not None and nxt is not None and nxt[0] == "text":
                    # 中文标点后面通常直接接汉字，不能要求后面是空白；
                    # 英文标点则要，免得把 "1.5" 拆开
                    m = re.match(
                        r"[ \t]*([。，、；：！？]+|[,.;:!?]+(?=\s|$))", nxt[1])
                    if m:
                        tail = m.group(1)
                        segs[idx + 1] = ("text", nxt[1][m.end():])
                prev_block = self._insert_formula(
                    seg[1], img, block, align, px, max_w, tail)
        self.text.configure(state="disabled")
        self._schedule_fit(force=True)

    def _prepare_formula(self, tex: str, display: bool, px: int, dpi: float,
                         linespace: int):
        """渲染公式，并决定怎么摆。返回 (图 | None, block, align)。

        判据只有三条：
        - `$$...$$`（模型自己标的独立公式）一律独占一行，这是语义
        - 高得离谱的（超过 7 行高，比如十几行的 aligned）独占一行
        - 其余一律行内，能在同一行塞下就塞下

        前两版是"含矩阵就独占一行"，结果「20 [矩阵] 的解为 [矩阵]。」被拆成
        四行，看着像被刀切过。矩阵只要宽度塞得下，行内排版反而是对的。

        align 分两种：矮公式用 baseline（排版准确，跟中文基线对齐）；
        高于一行半的用 center。Tk 的图片只能向上溢出，用 baseline 让一个
        三行矩阵坐在基线上时，同一行的中文会被压到整行的最底下，
        看起来像掉下去了；center 则是文字在公式侧面居中，读起来正常。
        """
        if self.text is None:
            return None, False, "baseline"
        color = self.th["fg"]
        probe = mathrender.render_image(
            tex, px=px, dpi=dpi, color=color, baseline=False)
        if probe is None:
            return None, False, "baseline"
        if display or probe.height > linespace * BLOCK_LINES:
            return probe, True, "center"
        if probe.height > linespace * 1.6:
            return probe, False, "center"
        # 行内小公式：补上基线以下的空白，让 align="baseline" 能精确对齐
        img = mathrender.render_image(tex, px=px, dpi=dpi, color=color)
        return (img or probe), False, "baseline"

    def _insert_formula(self, tex: str, img, block: bool, align: str, px: int,
                        max_w: int, tail: str = "") -> bool:
        """把公式图插进去，返回它是否占用了独立一行。

        `img` 为 None 表示渲染不出来：原样给出 LaTeX 源码并标色，绝不静默吞掉。
        """
        if self.text is None:
            return False
        if img is None:
            self.text.insert("end", " " + tex.strip() + " ", "raw_math")
            return False

        from PIL import Image, ImageTk

        if block and img.height > 240:
            # 太高的整块公式才缩，普通三行矩阵保持原字号（矩阵要看得清）
            ratio = 240.0 / float(img.height)
            img = img.resize((max(1, int(img.width * ratio)),
                              max(1, int(img.height * ratio))), Image.LANCZOS)

        # 尾部标点要在同一行落脚，缩进得给它让出位置
        reserve = int(px * 1.6) if (block and tail) else 0
        limit = max(60, max_w - reserve)
        if img.width > limit:
            ratio = limit / float(img.width)
            img = img.resize((limit, max(1, int(img.height * ratio))), Image.LANCZOS)
        if block:
            # 独立公式居中：Tk 的段落缩进/justify 对"只有图片的行"不生效，
            # 干脆把透明边距补进图片本身——图片占满整行，内容自然落在正中。
            # 有尾部标点时只补左侧：让「公式 + 标点」作为一个整体居中，
            # 标点才不会飘到行尾去
            left = max(0, (max_w - img.width - reserve) // 2)
            right = 0 if tail else left
            if left or right:
                wide = Image.new(
                    "RGBA", (img.width + left + right, img.height), (0, 0, 0, 0))
                wide.paste(img, (left, 0))
                img = wide
        photo = ImageTk.PhotoImage(img, master=self.text)
        self._math_photos.append(photo)

        if block:
            at_start = self.text.index("end-1c") == "1.0"
            if not at_start and self.text.get("end-2c", "end-1c") != "\n":
                self.text.insert("end", "\n")
            self.text.image_create("end", image=photo, align="center", padx=2, pady=3)
            if tail:
                self.text.insert("end", tail)
            self.text.insert("end", "\n")
            return True
        self.text.image_create("end", image=photo, align=align, padx=1)
        return False

    def _set_text(self, value: str) -> None:
        if self.text is None:
            return
        self._plain = value or ""
        self._math_photos.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if value:
            self.text.insert("1.0", value)
        self.text.configure(state="disabled")
        self._schedule_fit(force=True)

    # ---------- 尺寸自适应 ----------
    def _schedule_fit(self, force: bool = False) -> None:
        if force and self._fit_job is not None:
            try:
                self.root.after_cancel(self._fit_job)
            except Exception:
                pass
            self._fit_job = None
        if self._fit_job is None:
            self._fit_job = self.root.after(80, self._fit)

    def _fit(self) -> None:
        self._fit_job = None
        # 拖 / 缩放面板的时候不做重排：重排要改高度、还要重新渲染背景帧图
        # （6ms 量级），夹在两个鼠标事件之间就会把"两个窗口一起动"这个原子性
        # 打断——用户看到的就是背景动图起步慢半拍。松手时补一次就够了。
        if getattr(self, "_drag_active", False) or getattr(self, "_resize_dir", None):
            self._fit_deferred = True
            return
        # 面板已经关了就别再算高度：关闭时 _fit 可能还排在队列里，
        # 它会重新写几何 + 重设圆角，正是"关了又冒出半块东西"的来源
        if not self._shown:
            return
        if self.win is None or self.text is None:
            return
        try:
            # 1) 先固定宽度，让文本按最终宽度换行
            cur_h = max(self.win.winfo_height(), 80)
            self.win.geometry(f"{self.width}x{cur_h}+{self._x}+{self._y}")
            self.win.update_idletasks()
            # 2) 按显示高度设置文本控件高度（留 1 行余量，避免裁切）
            #    有公式图片时 displaylines 会低估高度（一张 60px 的公式只算 1 行），
            #    所以优先按像素折算行数
            n = 0
            try:
                # count 在窗口刚 deiconify、还没真正映射时可能返回 None，
                # 直接取 [0] 会抛 TypeError 把整个 _fit 打断（面板高度就停在上一次）
                ypx = self.text.count("1.0", "end-1c", "ypixels")
                if isinstance(ypx, (tuple, list)):
                    ypx = ypx[0] if ypx else 0
                ypx = int(ypx or 0)
                ls = tkfont.Font(font=self.text.cget("font")).metrics("linespace")
                n = int(ypx / max(1, ls)) + 2 if ypx > 0 else 0
            except (tk.TclError, IndexError, ValueError, TypeError):
                n = 0
            if n <= 0:
                lines = self.text.count("1.0", "end-1c", "displaylines")
                if isinstance(lines, (tuple, list)):
                    lines = lines[0] if lines else 0
                n = (int(lines or 1)) + 1
            n = max(3, min(n, 20))
            if int(self.text.cget("height")) != n:
                self.text.configure(height=n)
                self.win.update_idletasks()
            # 3) 按内容实际高度设置窗口高度（不低于用户设定的默认高度），并保证不出屏
            need = self.win.winfo_reqheight() + 6
            h = max(need, self.min_h)
            sh = self.root.winfo_screenheight()
            h = min(h, sh - 40)
            y = min(self._y, max(4, sh - h - 8))
            self._y = y
            self.win.geometry(f"{self.width}x{h}+{self._x}+{y}")
            # 命中判断（悬停唤醒 / 单击唤醒）用的是这个缓存，不更新的话
            # 面板长高之后下半截会被当成"面板外"
            self._geom_cache = (self._x, y, self.width, h)
            # 顺序很重要：下面 _toggle_scroll / _apply_round_region 里都有
            # update_idletasks()，会把面板的新尺寸**立刻**画出来；那之前必须
            # 先把背景层跟到同一尺寸，否则多出来的那一条就是露底的空白。
            self._sync_bg_at(self._x, y, self.width, h)
            self._move_both(self._x, y, self.width, h)
            self._toggle_scroll()
            self._apply_round_region()
        except tk.TclError:
            pass

    def _toggle_scroll(self) -> None:
        """内容放得下时隐藏右侧滚动条，不挡视线。"""
        if self.scroll is None or self.text is None:
            return
        try:
            self.win.update_idletasks()
            _first, last = self.text.yview()
            need = last < 0.998
            if need and not self._scroll_shown:
                self.scroll.pack(side="right", fill="y")
                self._scroll_shown = True
            elif not need and self._scroll_shown:
                self.scroll.pack_forget()
                self._scroll_shown = False
        except tk.TclError:
            pass

    # ---------- 四边/四角拖拽改尺寸 ----------
    def _edge_press(self, direction: str, event):
        self._resize_dir = direction
        self._resize_origin = (
            event.x_root, event.y_root,
            self._x, self._y,
            self.win.winfo_width(), self.win.winfo_height(),
        )
        if self.bg is not None:
            self.bg.pause()
        return "break"

    def _edge_drag(self, event):
        if getattr(self, "_resize_dir", None) is None or self._resize_origin is None:
            return "break"
        d = self._resize_dir
        x0m, y0m, x0, y0, w0, h0 = self._resize_origin
        dx = event.x_root - x0m
        dy = event.y_root - y0m

        x, y, w, h = x0, y0, w0, h0
        if "e" in d:
            w = w0 + dx
        if "w" in d:
            w = w0 - dx
            x = x0 + dx
        if "s" in d:
            h = h0 + dy
        if "n" in d:
            h = h0 - dy
            y = y0 + dy

        min_w, min_h = 340, 170
        if w < min_w:
            if "w" in d:
                x -= min_w - w
            w = min_w
        if h < min_h:
            if "n" in d:
                y -= min_h - h
            h = min_h

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = max(0, min(x, sw - 80))
        y = max(0, min(y, sh - 80))
        w = min(w, sw - x)
        h = min(h, sh - y)

        self.width = int(w)
        self.min_h = int(h)
        self._x, self._y = int(x), int(y)
        self.win.geometry(f"{self.width}x{self.min_h}+{self._x}+{self._y}")
        self._geom_cache = (self._x, self._y, self.width, self.min_h)
        # 同 _fit：_apply_round_region 里的 update_idletasks 会先把面板画出来，
        # 所以背景必须在那之前跟上，并且两个窗口要一批挪完。
        self._sync_bg_at(self._x, self._y, self.width, self.min_h)
        self._move_both(self._x, self._y, self.width, self.min_h)
        self._apply_round_region()
        return "break"

    def _edge_release(self, _event=None):
        self._resize_dir = None
        self._resize_origin = None
        if self.bg is not None:
            self.bg.resume()
        if getattr(self, "_fit_deferred", False):
            self._fit_deferred = False
            self._schedule_fit(force=True)
        self.keep_bg_aligned()
        if self.on_close:
            try:
                self.on_close()  # 保存尺寸/位置
            except Exception:
                pass
        return "break"

    # ---------- 交互 ----------
    def _on_text_click(self, event):
        if self.text is not None:
            self.text.focus_set()

    def _copy_event(self, _event=None):
        if self.text is not None:
            try:
                sel = self.text.get("sel.first", "sel.last")
            except tk.TclError:
                # 没有选区时复制全文：用原始文本，公式保持 LaTeX 源码而不是丢失
                sel = self._plain or self.text.get("1.0", "end-1c")
            if sel:
                self.root.clipboard_clear()
                self.root.clipboard_append(sel)
                self.set_status("已复制")
        return "break"

    def _copy_all(self) -> None:
        if self.text is not None:
            content = (self._plain or self.text.get("1.0", "end-1c")).strip()
            if content:
                self.root.clipboard_clear()
                self.root.clipboard_append(content)
                self.set_status("已复制译文")
                if self.on_copy:
                    self.on_copy(content)

    def _drag_start(self, event):
        self._drag_dx = event.x_root - self.win.winfo_x()
        self._drag_dy = event.y_root - self.win.winfo_y()
        self._drag_active = True
        # 拖动时先停掉动图刷新：每帧都要做一次 PIL 缩放裁切，会拖慢主线程，
        # 表现出来就是"背景比面板慢半拍"。
        if self.bg is not None:
            self.bg.pause()

    def _drag_move(self, event):
        # 没有对应的"按下"就不动。面板穿透时按下会被下面的程序吃掉，等
        # 悬停唤醒让面板重新可交互时，手上这一串移动事件才落到标题带上——
        # 此时 _drag_dx/_drag_dy 还是上一次拖动的残值，照着算会把面板
        # 一把甩到离光标很远的地方（用户描述为"面板自己跳走了"）。
        if not getattr(self, "_drag_active", False):
            return
        if self.win is None:
            return
        x = event.x_root - getattr(self, "_drag_dx", 0)
        y = event.y_root - getattr(self, "_drag_dy", 0)
        self.win.geometry(f"+{x}+{y}")
        self._x, self._y = x, y
        self._geom_cache = (x, y, self._geom_cache[2], self._geom_cache[3])
        # 用缓存里的尺寸而不是 winfo：拖动时流式翻译还在长高，刚写的 geometry
        # 可能还没生效，读 winfo 会拿到上一帧的宽高
        w, h = self._geom_cache[2], self._geom_cache[3]
        self._sync_bg_at(x, y, w, h)
        # 上面两行都只是让 Tk 记账，真正落到屏幕上要等 idle。拖动时事件一个接
        # 一个，idle 可能排不上，于是面板先动、背景后动。这里再原子挪一次。
        self._move_both(x, y, w, h)

    def release_stale_drag(self) -> None:
        """左键已经松开、但松开事件没落到面板上（在窗口外松手）时清掉拖动状态。

        不清的话 _drag_active 会一直挂着，下一次单纯的移动就会变成"幽灵拖动"，
        把面板甩到光标那里去。
        """
        if getattr(self, "_drag_active", False) and not mouse_left_down():
            self._drag_active = False
            if getattr(self, "_fit_deferred", False):
                self._fit_deferred = False
                self._schedule_fit(force=True)

    def _drag_end(self, _event=None):
        """拖动结束：记住位置，下次点「译」直接回到这里。"""
        self._drag_dx = self._drag_dy = 0
        self._drag_active = False
        if self.bg is not None:
            self.bg.resume()
        # 拖动期间攒下的重排在这里一次性补上（拖动中不做，见 _fit）
        if getattr(self, "_fit_deferred", False):
            self._fit_deferred = False
            self._schedule_fit(force=True)
        # 松手时也核一次：拖动过程中若有一次背景没跟上，这里立刻补回来
        self.keep_bg_aligned()
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass
        return "break"

    # ---------- 状态 ----------
    def hide(self) -> None:
        self.close()

    def close(self) -> None:
        """关闭面板。用户点叉 / 按 Esc 都会走到这里。

        `on_close` 只负责保存位置，拖动结束时也会调它，所以**不能**拿它判断
        "用户要结束这次任务"。真正表示"用户不要这个结果了"的是 on_dismiss，
        这里只调它一次：上层据此取消还在跑的识别/翻译。
        """
        was_shown = self._shown
        self._shown = False
        # 取消挂起的重排/重绘：面板关了就不该再有任何回调去动窗口，
        # 否则 _fit() 会在关闭之后再写一次几何和圆角区域，把图层搅乱
        for attr in ("_fit_job", "_header_job", "_topmost_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        # 先收背景层：它只是面板的装饰，任何时刻都不该比面板活得久
        if self.bg is not None:
            self.bg.hide()
        if self.win is not None:
            try:
                self.win.withdraw()
            except tk.TclError:
                pass
        self._verify_hidden("（关闭时）")
        # 再复查两次：防止排队中的回调、或系统合成器的最后一帧让某一层留在屏幕上
        self._cancel_verify()
        for delay in (250, 900):
            try:
                job = self.root.after(delay, self._verify_hidden, f"（关闭后 {delay}ms）")
                self._verify_jobs.append(job)
            except Exception:
                pass
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass
        # 只在"本来是开着的"时候通知：close() 可能被重复调用（Esc + 巡检），
        # 重复通知会让上层把下一轮刚开的任务当成旧的取消掉
        if was_shown and self.on_dismiss:
            try:
                self.on_dismiss()
            except Exception:
                pass

    # 不调用 Tk 的快速命中判断：给系统钩子线程用
    def fast_visible(self) -> bool:
        return self._shown

    def click_through(self) -> bool:
        """是否处于鼠标穿透状态（穿透时点击落不到面板上）。"""
        return self._click_through

    def touch(self, seconds: float | None = None) -> None:
        """用户正在使用面板：保持可交互（并取消穿透）。

        必须在"结果出现/新 token 到达"时也调用，否则可交互窗口是从任务开始
        算起的——图片翻译要十几秒，结果一到就已经在穿透状态了。
        """
        import time as _t

        self._interactive_until = _t.time() + (
            INTERACTIVE_SECONDS if seconds is None else float(seconds)
        )
        # 用户手动勾了"面板始终穿透"就别自作主张恢复可点
        if self._click_through and not bool(
                self.cfg["ui"].get("panel_click_through", False)):
            self.apply_click_through(False)

    # ---------- 悬停唤醒（给系统钩子线程用的纯算数判断） ----------
    def refresh_zones(self) -> None:
        """主线程：刷新正文区矩形缓存。钩子线程靠它区分"正文"和"按钮区"。"""
        if self.text is None or self.win is None:
            self._body_rect = None
            return
        try:
            self._body_rect = (
                self.text.winfo_rootx(), self.text.winfo_rooty(),
                self.text.winfo_width(), self.text.winfo_height(),
            )
        except tk.TclError:
            self._body_rect = None

    def action_zone_contains(self, x: int, y: int) -> bool:
        """这个屏幕坐标是不是落在"可操作区"（标题带 / 按钮 / 边角 / 滚动条）。

        排除正文区：半透明面板下能看见底下的字，点正文的人是想要选那些字。
        纯算数，钩子线程可以调。
        """
        if not self.fast_contains(x, y):
            return False
        rect = self._body_rect
        if not rect:
            return True
        bx, by, bw, bh = rect
        return not (bx <= x <= bx + bw and by <= y <= by + bh)

    def hover_wake(self) -> None:
        """鼠标悬停在可操作区：提前把穿透关掉，让"这一下点击"就打在面板上。

        与 wake() 的区别：不抬窗、不抢层级，只改穿透位。层级错乱由 250ms
        巡检的 keep_bg_below() 负责，钩子线程触发时不该做重活。
        """
        self.apply_click_through(False)
        self.touch()

    def apply_click_through(self, enable: bool) -> None:
        """面板 + 背景层一起鼠标穿透（开启后点击直接落到下面的程序）。"""
        enable = bool(enable)
        if enable == self._click_through:
            return
        self._click_through = enable
        for win in (self.win, self.bg.win if self.bg is not None else None):
            if win is not None:
                set_click_through(win, enable)

    def fast_contains(self, x: int, y: int) -> bool:
        if not self._shown:
            return False
        px, py, w, h = self._geom_cache
        return px <= x <= px + w and py <= y <= py + h

    def visible(self) -> bool:
        return self.win is not None and self.win.winfo_ismapped()

    def contains(self, x: int, y: int) -> bool:
        if not self.visible():
            return False
        try:
            x0, y0 = self.win.winfo_rootx(), self.win.winfo_rooty()
            w, h = self.win.winfo_width(), self.win.winfo_height()
        except tk.TclError:
            return False
        return x0 <= x <= x0 + w and y0 <= y <= y0 + h
