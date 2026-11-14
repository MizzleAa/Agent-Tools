"""이미지 바이트 다루기 — 형식·크기 알아보기, 작은 PNG(썸네일·보기용) 만들기. 외부 라이브러리 없이 동작한다.

사내 API 의 원본은 BMP 처럼 큰 형식이 많다. 화면에 원본을 수십 장 그리면 느려지므로 작은 PNG 를 만들어 보여 준다.
Pillow 가 깔려 있으면 그걸 쓰고(모든 형식), 없으면 BMP 만 직접 줄인다 (PNG·JPEG·GIF·WEBP 는 브라우저가 원본을 바로 그린다).
"""

from __future__ import annotations

import struct
import zlib

RASTER = {"bmp": "image/bmp", "png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
          "tiff": "image/tiff"}
BROWSER = {"bmp", "png", "jpeg", "gif", "webp"}   # 브라우저가 바로 그리는 형식 (SVG 는 스크립트가 들 수 있어 받지 않는다)


def sniff(data: bytes) -> str:
    """바이트 머리로 래스터 이미지 형식을 알아낸다 (모르면 빈 글자)."""
    if data[:2] == b"BM" and len(data) > 26:
        return "bmp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return ""


def size_of(data: bytes, fmt: str) -> tuple[int, int] | None:
    """(가로, 세로). 머리에서 읽을 수 있을 때만."""
    try:
        if fmt == "bmp":
            w, h = struct.unpack_from("<ii", data, 18)
            return abs(w), abs(h)
        if fmt == "png":
            return struct.unpack_from(">II", data, 16)
        if fmt == "gif":
            return struct.unpack_from("<HH", data, 6)
        if fmt == "jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack_from(">HH", data, i + 5)
                    return w, h
                i += 2 + struct.unpack_from(">H", data, i + 2)[0]
    except (struct.error, IndexError):
        return None
    return None


# ───────────────────────── 작은 PNG 만들기

def shrink_to_png(data: bytes, fmt: str, max_side: int) -> bytes | None:
    """긴 변이 max_side 이하인 PNG. 만들 수 없으면 None (그때는 원본을 그대로 보여 준다)."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        Image = None
    if Image is not None:
        import io
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.thumbnail((max_side, max_side))
                if im.mode not in ("L", "RGB", "RGBA"):
                    im = im.convert("RGB")
                out = io.BytesIO()
                im.save(out, "PNG", optimize=True)
                return out.getvalue()
        except Exception:   # noqa: BLE001 — 줄이지 못하면 원본을 쓴다
            return None
    if fmt == "bmp":
        try:
            return _bmp_to_png(data, max_side)
        except (struct.error, IndexError, ValueError, ZeroDivisionError):
            return None
    return None


def _png(width: int, height: int, rows: list[bytes], gray: bool) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rows)
    head = struct.pack(">IIBBBBB", width, height, 8, 0 if gray else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _bmp_to_png(data: bytes, max_side: int) -> bytes | None:
    """비압축 BMP(1·4·8·24·32 비트)를 가까운 픽셀로 줄여 PNG 로. 다른 BMP 는 None."""
    offset = struct.unpack_from("<I", data, 10)[0]
    header = struct.unpack_from("<I", data, 14)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    bpp, compression = struct.unpack_from("<HI", data, 28)
    if width <= 0 or height == 0 or compression not in (0, 3) or bpp not in (1, 4, 8, 24, 32):
        return None
    top_down = height < 0
    height = abs(height)
    stride = ((width * bpp + 31) // 32) * 4
    if offset + stride * height > len(data):
        return None
    palette: list[tuple[int, int, int]] = []
    if bpp <= 8:
        count = struct.unpack_from("<I", data, 46)[0] or (1 << bpp)
        start = 14 + header
        palette = [(data[start + 4 * i + 2], data[start + 4 * i + 1], data[start + 4 * i]) for i in range(count)]
    scale = max(width, height) / max_side if max(width, height) > max_side else 1.0
    tw, th = max(1, int(width / scale)), max(1, int(height / scale))
    xs = [min(width - 1, int(x * scale)) for x in range(tw)]
    gray = bpp <= 8 and all(r == g == b for r, g, b in palette)
    rows = []
    for y in range(th):
        sy = min(height - 1, int(y * scale))
        row_at = offset + stride * (sy if top_down else height - 1 - sy)
        row = data[row_at: row_at + stride]
        if bpp == 8:
            idx = [row[x] for x in xs]
        elif bpp == 4:
            idx = [(row[x >> 1] >> (0 if x & 1 else 4)) & 0x0F for x in xs]
        elif bpp == 1:
            idx = [(row[x >> 3] >> (7 - (x & 7))) & 1 for x in xs]
        else:
            step = bpp // 8
            rows.append(bytes(v for x in xs for v in (row[x * step + 2], row[x * step + 1], row[x * step])))
            continue
        if gray:
            rows.append(bytes(palette[i][0] if i < len(palette) else 0 for i in idx))
        else:
            rows.append(bytes(v for i in idx for v in (palette[i] if i < len(palette) else (0, 0, 0))))
    return _png(tw, th, rows, gray)
