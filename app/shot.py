"""截图取图：Ctrl+Alt+D 全屏框选，或直接读剪贴板里的图片。

为什么不是"半透明遮罩 + 实时截图"：
  遮罩窗口一旦半透明，用户就得在暗掉的桌面上框选；而如果为了让选区保持原亮度
  去给窗口挖洞（SetWindowRgn），鼠标一进入洞内就不再属于这个窗口，拖动会当场断掉。
  所以这里先把整个屏幕抓成一张图当背景冻结显示，框选期间鼠标全程在自己窗口上，
  松手直接从这张图里裁剪——既不会丢事件，也不可能把遮罩本身截进去。
"""
from __future__ import annotations

import ctypes
import logging

from PIL import Image, ImageGrab

log = logging.getLogger("shot")

user32 = ctypes.windll.user32
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def virtual_screen() -> tuple[int, int, int, int]:
    """整个虚拟桌面（多显示器时为所有屏幕的外接矩形）的 (x, y, w, h)。

    注意 x/y 可以为负：副屏放在主屏左边时就是这样。
    """
    try:
        return (
            int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
        )
    except Exception:
        return (0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))


def grab_screen() -> tuple[Image.Image, tuple[int, int, int, int]]:
    """抓整个虚拟桌面，返回 (图像, 虚拟屏幕矩形)。"""
    vx, vy, vw, vh = virtual_screen()
    try:
        img = ImageGrab.grab(all_screens=True)
    except TypeError:      # 老版本 Pillow 没有 all_screens
        img = ImageGrab.grab()
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img, (vx, vy, vw, vh)


def grab_clipboard_image():
    """剪贴板里的图片；没有就返回 None。支持复制图片文件（拿到路径）的情况。"""
    try:
        data = ImageGrab.grabclipboard()
    except Exception as exc:
        log.info("读取剪贴板图片失败: %s", exc)
        return None
    if isinstance(data, Image.Image):
        log.info("剪贴板里有图片 %dx%d", data.width, data.height)
        return data
    if isinstance(data, list) and data:
        for path in data:
            if isinstance(path, str) and path.lower().endswith(
                (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff")
            ):
                try:
                    img = Image.open(path)
                    img.load()
                    log.info("剪贴板里是图片文件：%s", path)
                    return img
                except Exception as exc:
                    log.info("打开剪贴板图片文件失败: %s", exc)
    return None


class RegionSelector:
    """全屏框选。调 select() 阻塞等待，返回 PIL 图；用户取消返回 None。

    必须在 Tk 主线程调用（内部自己跑一个局部的事件循环）。
    """

    MIN_SIZE = 6      # 小于这个尺寸当作误触
    DIMMING = "gray50"  # 选区外的网点遮罩，替代 Canvas 做不到的真半透明
    # 配色学 QQ 截图：中性蓝的选框 + 白/浅灰的辅助线，不掺主题绿，
    # 免得在杂志、幻灯片的彩色内容上分不清"这是界面的框"还是页面本身的框
    EDGE = "#3d9bff"
    GUIDE = "#d8d8d8"
    HINT_BG = "#1b1b1b"

    def __init__(self, root, on_done=None, on_status=None):
        import tkinter as tk

        self.tk = tk
        self.root = root
        self.on_done = on_done
        self.on_status = on_status
        self.result = None
        self.win = None
        self.canvas = None
        self._photo = None
        self._screen = None
        self._vscreen = (0, 0, 0, 0)
        self._w = self._h = 0
        self._start = None
        self._rect = None
        self._shades: list = []
        self._hint = None
        self._hint_bg = None
        self._cross: list = []
        self._active = False

    # ---------- 对外 ----------
    def select(self):
        """打开框选界面并阻塞到用户完成/取消。"""
        if self._active:
            return None
        self._active = True
        try:
            self._screen, self._vscreen = grab_screen()
            if self.on_status:
                self.on_status("正在框选要翻译的区域…")
            self._build()
            self._grab_input()
            self.root.wait_window(self.win)
            return self.result
        finally:
            self._active = False
            self._screen = None
            self._photo = None
            self._teardown()

    # ---------- 构建 ----------
    def _build(self) -> None:
        tk = self.tk
        vx, vy, vw, vh = self._vscreen
        # 窗口/画布尺寸一律用底图的实际像素数，而不是 GetSystemMetrics 的值：
        # 进程没做 DPI 感知时两者会差一个缩放倍数（实测 2560 vs 1707），
        # 用它来定位就会整张底图和窗口错位。
        vw, vh = self._screen.size
        self._w, self._h = vw, vh
        self._vscreen = (vx, vy, vw, vh)

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        win.configure(bg="black")
        win.geometry(f"{vw}x{vh}+{vx}+{vy}")

        canvas = tk.Canvas(win, width=vw, height=vh, highlightthickness=0, bd=0,
                           bg="black", cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        from PIL import ImageTk

        self._photo = ImageTk.PhotoImage(self._screen, master=win)
        canvas.create_image(0, 0, image=self._photo, anchor="nw")

        # 选区外的四块网点遮罩（初始铺满全屏）
        for _ in range(4):
            self._shades.append(
                canvas.create_rectangle(0, 0, vw, vh, fill="black",
                                        outline="", stipple=self.DIMMING, tags="shade")
            )
        self._rect = canvas.create_rectangle(0, 0, 0, 0, outline=self.EDGE, width=2,
                                             state="hidden")
        # 文字后面垫一块深色底：尺寸/提示可能落在浅色内容上，纯文字会看不见
        self._hint_bg = canvas.create_rectangle(0, 0, 0, 0, fill=self.HINT_BG,
                                                outline="", state="hidden")
        self._hint = canvas.create_text(0, 0, text="", anchor="nw", fill="#ffffff",
                                        font=("Microsoft YaHei UI", 10), state="hidden")
        self._cross = [
            canvas.create_line(0, 0, 0, 0, fill=self.GUIDE, dash=(4, 4), state="hidden"),
            canvas.create_line(0, 0, 0, 0, fill=self.GUIDE, dash=(4, 4), state="hidden"),
        ]

        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_move)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<Motion>", self._on_hover)
        canvas.bind("<ButtonPress-3>", lambda _e: self._cancel())
        win.bind("<Escape>", lambda _e: self._cancel())

        self.win = win
        self.canvas = canvas
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        try:
            win.focus_force()
        except tk.TclError:
            pass

    def _grab_input(self) -> None:
        """抢一次 Tk 的输入抓取：框选期间外的点击不会跑到别的窗口上。"""
        try:
            self.canvas.grab_set()
        except Exception:
            pass

    # ---------- 交互 ----------
    def _clamp(self, x: float, y: float) -> tuple[int, int]:
        return max(0, min(int(x), self._w)), max(0, min(int(y), self._h))

    def _place_hint(self, x: int, y: int, text: str) -> None:
        """标签跟着内容走，并在后面垫一块深色底，保证在浅色截图上也读得清。"""
        self.canvas.itemconfigure(self._hint, state="normal", text=text)
        self.canvas.coords(self._hint, x, y)
        bbox = self.canvas.bbox(self._hint)
        if bbox:
            self.canvas.coords(self._hint_bg, bbox[0] - 5, bbox[1] - 3, bbox[2] + 5, bbox[3] + 3)
            self.canvas.itemconfigure(self._hint_bg, state="normal")
            self.canvas.tag_raise(self._hint)

    def _on_hover(self, event) -> None:
        if self._start is not None:
            return
        x, y = self._clamp(event.x, event.y)
        vw, vh = self._w, self._h
        self.canvas.coords(self._cross[0], 0, y, vw, y)
        self.canvas.coords(self._cross[1], x, 0, x, vh)
        for line in self._cross:
            self.canvas.itemconfigure(line, state="normal")
        self._place_hint(
            14, 14, f"拖拽框选要翻译的区域 · Esc 取消 · 屏幕 {vw}×{vh}"
        )

    def _on_press(self, event) -> None:
        self._start = self._clamp(event.x, event.y)
        self.canvas.itemconfigure(self._rect, state="normal")
        self._update_rect(*self._start, *self._start)

    def _on_move(self, event) -> None:
        if self._start is None:
            return
        self._update_rect(*self._start, *self._clamp(event.x, event.y))

    def _update_rect(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """选区矩形 + 外部四块遮罩 + 尺寸提示，全部一次算完。"""
        vw, vh = self._w, self._h
        lx, rx = (x0, x1) if x0 <= x1 else (x1, x0)
        ty, by = (y0, y1) if y0 <= y1 else (y1, y0)
        lx, rx = max(0, lx), min(vw, rx)
        ty, by = max(0, ty), min(vh, by)

        self.canvas.coords(self._rect, lx, ty, rx, by)
        # 上、下、左、右四块，围出中间那个"洞"（Canvas 没有真半透明，用网点代替）
        boxes = [
            (0, 0, vw, ty),
            (0, by, vw, vh),
            (0, ty, lx, by),
            (rx, ty, vw, by),
        ]
        for item, box in zip(self._shades, boxes):
            self.canvas.coords(item, *box)
            # 退化成一个点时 coords 会变成 0 宽，Tk 不画，正好
            self.canvas.itemconfigure(item, state="normal" if box[2] > box[0] and box[3] > box[1] else "hidden")

        w, h = rx - lx, by - ty
        label = f"{w} × {h}"
        tx, ty2 = lx, ty - 22
        if ty2 < 4:
            ty2 = by + 6
        self._place_hint(tx + 2, ty2, label)
        for line in self._cross:
            self.canvas.itemconfigure(line, state="hidden")

    def _pointer_in_canvas(self, fx: int, fy: int) -> tuple[int, int]:
        """松手那一刻光标的真实位置（Canvas 坐标）。

        release 事件里的坐标可能滞后一两帧：机器实测快速拖拽时能少 20~45px，
        框出来的区域就比看到的框小一圈。这里直接问系统光标在哪，更准。
        """
        try:
            px, py = self.win.winfo_pointerxy()
            wx, wy = self.win.winfo_rootx(), self.win.winfo_rooty()
            return self._clamp(px - wx, py - wy)
        except Exception:
            return self._clamp(fx, fy)

    def _on_release(self, event) -> None:
        if self._start is None:
            return
        x0, y0 = self._start
        x1, y1 = self._pointer_in_canvas(event.x, event.y)
        self._start = None
        lx, rx = (x0, x1) if x0 <= x1 else (x1, x0)
        ty, by = (y0, y1) if y0 <= y1 else (y1, y0)
        if rx - lx < self.MIN_SIZE or by - ty < self.MIN_SIZE:
            log.info("框选区域过小（%dx%d），当作误触取消", rx - lx, by - ty)
            self._cancel()
            return
        self.result = self._crop(lx, ty, rx, by)
        self._close()

    def _crop(self, lx: int, ty: int, rx: int, ry: int):
        """从冻结的屏幕图里裁剪，坐标就是图像像素。"""
        sw, sh = self._screen.size
        box = (
            max(0, min(int(lx), sw)), max(0, min(int(ty), sh)),
            max(0, min(int(rx), sw)), max(0, min(int(ry), sh)),
        )
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            return None
        try:
            return self._screen.crop(box).copy()
        except Exception as exc:
            log.error("裁剪截图失败: %s", exc)
            return None

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        try:
            if self.canvas is not None:
                self.canvas.grab_release()
        except Exception:
            pass
        try:
            if self.win is not None:
                self.win.withdraw()
                self.win.update_idletasks()
        except Exception:
            pass
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass
        self.win = None
        self.canvas = None

    def _teardown(self) -> None:
        self._close()
        self._shades.clear()
        self._cross.clear()
