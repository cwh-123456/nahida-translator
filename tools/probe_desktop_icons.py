"""把桌面图标层单独抓成图片。

为什么不用 ImageGrab 截屏：桌面常被其他窗口盖住，截屏只能看到最上层。
这里直接对桌面的 `SysListView32` 调 PrintWindow，拿到的是**图标层自己的
绘制结果**，前面的窗口挡不住。

用途：判断某个快捷方式当前到底画成了什么（空白？默认文档？还是真的图标）。
排查"图标不显示"时，这是唯一能拿到直接证据的办法——
`SHGetFileInfo` 之类返回的是 shell 的解析结果，可能与桌面实际绘制不一致。

    python tools/probe_desktop_icons.py [输出.png]

抓完可以直接看图，也可以再看它打印的"非壁纸像素占比"。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import c_int, c_uint, c_void_p, Structure, byref, sizeof
from ctypes import wintypes

from PIL import Image

u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
u32.FindWindowW.restype = c_void_p
u32.FindWindowExW.argtypes = [c_void_p, c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]
u32.FindWindowExW.restype = c_void_p
u32.GetDC.argtypes = [c_void_p]
u32.GetDC.restype = c_void_p
u32.ReleaseDC.argtypes = [c_void_p, c_void_p]
u32.GetWindowRect.argtypes = [c_void_p, ctypes.POINTER(wintypes.RECT)]
u32.PrintWindow.argtypes = [c_void_p, c_void_p, c_uint]
u32.PrintWindow.restype = wintypes.BOOL
u32.EnumWindows.argtypes = [c_void_p, wintypes.LPARAM]

gdi32.CreateCompatibleDC.argtypes = [c_void_p]
gdi32.CreateCompatibleDC.restype = c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [c_void_p, c_int, c_int]
gdi32.CreateCompatibleBitmap.restype = c_void_p
gdi32.SelectObject.argtypes = [c_void_p, c_void_p]
gdi32.SelectObject.restype = c_void_p
gdi32.GetDIBits.argtypes = [c_void_p, c_void_p, c_uint, c_uint, c_void_p, c_void_p, c_uint]
gdi32.GetDIBits.restype = c_int
gdi32.DeleteObject.argtypes = [c_void_p]
gdi32.DeleteDC.argtypes = [c_void_p]


class BIH(Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def find_desktop_listview() -> int | None:
    """桌面列表控件。正常情况下在 Progman 下，某些主题/壁纸轮播会挂到 WorkerW。"""
    progman = u32.FindWindowW("Progman", None)
    if progman:
        dv = u32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
        if dv:
            return u32.FindWindowExW(dv, None, "SysListView32", None)

    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, c_void_p, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        dv = u32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
        if dv:
            found.append(dv)
        return True

    u32.EnumWindows(cb, 0)
    if found:
        return u32.FindWindowExW(found[0], None, "SysListView32", None)
    return None


def capture(out_path: str) -> Image.Image:
    hwnd = find_desktop_listview()
    if not hwnd:
        raise RuntimeError("找不到桌面列表控件（SysListView32）")

    rect = wintypes.RECT()
    u32.GetWindowRect(hwnd, byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    screen = u32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # PW_RENDERFULLCONTENT：不加这个，DWM 下经常抓到全黑
        if not u32.PrintWindow(hwnd, mem, 0x2):
            raise RuntimeError("PrintWindow 失败")
        bi = BIH()
        bi.biSize = sizeof(bi)
        bi.biWidth = w
        bi.biHeight = -h          # 负数 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, byref(bi), 0)
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).copy().convert("RGB")
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        u32.ReleaseDC(None, screen)

    img.save(out_path)
    return img


def main(argv: list[str]) -> int:
    out = argv[1] if len(argv) > 1 else "desktop_layer.png"
    img = capture(out)
    print("已保存 %s（%dx%d）" % (out, img.size[0], img.size[1]))

    # 有内容的桌面不可能是单一颜色；全黑/全同色说明这一抓是空的
    colors = img.getcolors(maxcolors=1 << 20) or []
    colors.sort(reverse=True)
    total = img.size[0] * img.size[1]
    if colors:
        top_n, top_c = colors[0]
        print("最常见颜色 %s 占 %.1f%%" % (top_c, 100.0 * top_n / total))
        if top_n / total > 0.97:
            print("警告：几乎是一整片同色，这次可能没抓到内容（换 PrintWindow 标志再试）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
