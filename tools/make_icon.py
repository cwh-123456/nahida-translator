"""把一张图做成 Windows 快捷方式用的多尺寸 .ico。

为什么需要这个脚本（而不是一句 magick 命令）：

1. **必须裁切**。全身像缩到 16px 只剩一堆色块，图标要的是脸部特写。
2. **必须多档**。桌面图标四档取 16/48/96/256，Explorer 又在 100%~250% 缩放下跑，
   实际要 64/128/256。缺档时系统拿最接近的硬放大（糊）甚至画不出来（空白）。
3. **建议加圆角**。插画四边常是浅色，方块图标在浅色壁纸上会糊进背景、看不出边界。
4. **PIL 的 save(sizes=...) 对 ICO 不会写多帧** —— 它只把 base 缩成最小尺寸，
   结果是单帧小图。所以这里自己拼 ICONDIR + ICONDIRENTRY + 位图数据。

用法：

    python tools/make_icon.py <原图路径> [x0,y0,x1,y1] [-o 输出.ico]

不传裁切框时，自动在画面**中部偏上**取正方形（人脸大多在上半部分），
并打印建议值供微调。裁切框必须是正方形。

例子（本项目当前用的参数）：

    python tools/make_icon.py assets/nahida_src.jpg 135,49,625,539 -o assets/nahida.ico
"""

from __future__ import annotations

import io
import os
import struct
import sys

from PIL import Image, ImageDraw, ImageFilter

SIZES = (16, 24, 32, 48, 64, 96, 128, 256)
RADIUS_RATIO = 0.14  # 圆角半径占边长的比例，接近 Win11 图标观感
MASTER = 1024        # 先缩到这个尺寸再往小缩，避免反复重采样


def parse_box(text: str) -> tuple[int, int, int, int]:
    parts = [int(v.strip()) for v in text.replace("，", ",").split(",")]
    if len(parts) != 4:
        raise ValueError("裁切框要四个整数：x0,y0,x1,y1")
    x0, y0, x1, y1 = parts
    if x1 <= x0 or y1 <= y0:
        raise ValueError("裁切框的 x1/y1 必须大于 x0/y0")
    if (x1 - x0) != (y1 - y0):
        raise ValueError("裁切框必须是正方形（现在 %dx%d）" % (x1 - x0, y1 - y0))
    return x0, y0, x1, y1


def auto_box(size: tuple[int, int]) -> tuple[int, int, int, int]:
    """默认裁切：正方形，水平居中，垂直略偏上（脸通常在画面上半部）。"""
    w, h = size
    side = min(w, h)
    x0 = (w - side) // 2
    y0 = max(0, int((h - side) * 0.35))   # 0.35 而不是 0.5，整体往上提
    return x0, y0, x0 + side, y0 + side


def rounded(img: Image.Image, ratio: float = RADIUS_RATIO, ss: int = 4) -> Image.Image:
    """加圆角。ss 倍超采样后降采样，边缘才平滑（直接画会锯齿）。"""
    n = img.size[0] * ss
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, n - 1, n - 1), radius=int(n * ratio), fill=255
    )
    out = img.convert("RGBA")
    out.putalpha(mask.resize(img.size, Image.LANCZOS))
    return out


def shrink(img: Image.Image, n: int) -> Image.Image:
    """降到目标尺寸。小尺寸补一次轻锐化，否则细节糊成一片。"""
    out = img.resize((n, n), Image.LANCZOS)
    if n <= 48:
        out = out.filter(ImageFilter.UnsharpMask(radius=0.6, percent=55, threshold=2))
    return out


def to_dib(img: Image.Image) -> bytes:
    """ICO 里的 DIB：40 字节头 + 自下而上 BGRA + 4 字节对齐的 AND 掩码。"""
    w, h = img.size
    px = img.load()
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            xor += bytes((b, g, r, a))

    stride = ((w + 31) // 32) * 4
    mask = bytearray(stride * h)
    # 全透明处把掩码位置 1：不认 alpha 的老程序也不会出黑框
    for y in range(h):
        row = (h - 1 - y) * stride
        for x in range(w):
            if px[x, y][3] < 8:
                mask[row + (x >> 3)] |= 0x80 >> (x & 7)

    header = struct.pack(
        "<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0
    )
    return header + bytes(xor) + bytes(mask)


def build_ico(frames: dict[int, Image.Image]) -> bytes:
    blobs = []
    for n in SIZES:
        if n >= 256:
            buf = io.BytesIO()
            frames[n].save(buf, format="PNG", optimize=True)  # Vista+ 认 PNG 帧，体积小很多
            blobs.append(buf.getvalue())
        else:
            blobs.append(to_dib(frames[n]))

    entries = b""
    offset = 6 + 16 * len(SIZES)
    for n, blob in zip(SIZES, blobs):
        entries += struct.pack(
            "<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)
    return struct.pack("<HHH", 0, 1, len(SIZES)) + entries + b"".join(blobs)


def verify(path: str) -> bool:
    """结构 + 逐帧 + Windows 能否按每个尺寸加载，全过才算成。"""
    data = open(path, "rb").read()
    _, _, count = struct.unpack("<HHH", data[:6])
    print("\n校验 %s（%d 字节，%d 帧）" % (path, len(data), count))
    ok = count == len(SIZES)

    for i in range(count):
        off = 6 + i * 16
        w, h, _c, _r, _p, bpp, size, o = struct.unpack("<BBBBHHII", data[off:off + 16])
        w, h = w or 256, h or 256
        kind = "PNG" if data[o:o + 8] == b"\x89PNG\r\n\x1a\n" else "DIB"
        over = o + size > len(data)
        print("  %3dx%-3d %2dbpp %7d 字节 %-3s 越界=%s" % (w, h, bpp, size, kind, over))
        ok = ok and not over

    try:
        import ctypes

        u32 = ctypes.windll.user32
        u32.LoadImageW.restype = ctypes.c_void_p
        u32.DestroyIcon.argtypes = [ctypes.c_void_p]
        full = os.path.abspath(path)
        for n in SIZES:
            handle = u32.LoadImageW(None, full, 1, n, n, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            print("  LoadImage %3dx%-3d -> %s" % (n, n, "OK" if handle else "失败"))
            if handle:
                u32.DestroyIcon(handle)
            else:
                ok = False
    except Exception as exc:  # 非 Windows 或没有 user32
        print("  跳过 LoadImage 检查：%s" % exc)

    return ok


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    src_path = argv[1]
    box = None
    out_path = "assets/nahida.ico"

    rest = argv[2:]
    i = 0
    while i < len(rest):
        if rest[i] in ("-o", "--out"):
            if i + 1 >= len(rest):
                print("-o 后面要跟输出路径")
                return 1
            out_path = rest[i + 1]
            i += 2
        else:
            box = parse_box(rest[i])
            i += 1

    src = Image.open(src_path).convert("RGB")
    if box is None:
        box = auto_box(src.size)
        print("未指定裁切框，自动取 %s —— 想调构图就把这四个数改一下再跑"
              % ",".join(str(v) for v in box))
    else:
        w, h = src.size
        if box[2] > w or box[3] > h:
            print("裁切框超出图片范围（图是 %dx%d）" % (w, h))
            return 1

    master = src.crop(box).resize((MASTER, MASTER), Image.LANCZOS)
    base = rounded(master)
    frames = {n: shrink(base, n) for n in SIZES}

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(build_ico(frames))

    ok = verify(out_path)
    print("\n结果:", "PASS" if ok else "FAIL")
    if ok:
        print("图标缓存可能需要刷新：桌面按 F5，或把快捷方式重命名一次。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
