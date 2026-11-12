"""Text sprites (BGRA numpy arrays) drawn with Pillow, plus fast alpha blitting."""
import functools
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .. import config, ffutil

_GF = "https://github.com/google/fonts/raw/main/ofl/"
FONT_URLS = {
    "poppins-xb": _GF + "poppins/Poppins-ExtraBold.ttf",
    "poppins-sb": _GF + "poppins/Poppins-SemiBold.ttf",
    "anton": _GF + "anton/Anton-Regular.ttf",
    "bebas": _GF + "bebasneue/BebasNeue-Regular.ttf",
    "serif": _GF + "dmserifdisplay/DMSerifDisplay-Regular.ttf",
}
_SYSTEM_FALLBACKS = [
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


@functools.lru_cache(maxsize=8)
def _font_path(name: str) -> str | None:
    url = FONT_URLS.get(name)
    if url:
        dst = config.FONTS / Path(url).name
        try:
            return str(ffutil.download(url, dst, timeout=30))
        except Exception:
            pass
    for p in _SYSTEM_FALLBACKS:
        if Path(p).exists():
            return p
    return None


@functools.lru_cache(maxsize=64)
def font(name: str, size: int):
    p = _font_path(name)
    if p:
        return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def hex_rgb(c: str) -> tuple:
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _to_bgra(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGBA2BGRA)


def wrap(words: list[str], fnt, max_width: int) -> list[list[int]]:
    """Greedy wrap; returns lines as lists of word indexes."""
    space = fnt.getlength(" ")
    lines, cur, cur_w = [], [], 0.0
    for i, w in enumerate(words):
        ww = fnt.getlength(w)
        if cur and cur_w + space + ww > max_width:
            lines.append(cur)
            cur, cur_w = [i], ww
        else:
            cur_w = ww if not cur else cur_w + space + ww
            cur.append(i)
    if cur:
        lines.append(cur)
    return lines


def words_sprite(words: list[str], colors: list[str], font_name: str, size: int, max_width: int,
                 stroke: int = 0, stroke_color: str = "#000000", shadow: bool = True,
                 box_idx: int | None = None, box_color: str = "#FFE14A",
                 line_gap: float = 1.12, align: str = "center") -> np.ndarray:
    """Draw words (each with its own colour) wrapped into centred lines. Returns BGRA."""
    fnt = font(font_name, size)
    space = fnt.getlength(" ")
    lines = wrap(words, fnt, max_width)
    asc, desc = fnt.getmetrics()
    line_h = int((asc + desc) * line_gap)
    widths = [sum(fnt.getlength(words[i]) for i in ln) + space * (len(ln) - 1) for ln in lines]
    pad = stroke + int(size * 0.28) + 6
    W = int(max(widths)) + pad * 2
    H = line_h * len(lines) + pad * 2
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pos = []
    for li, ln in enumerate(lines):
        x = pad + ((W - 2 * pad - widths[li]) / 2 if align == "center" else 0)
        y = pad + li * line_h
        for i in ln:
            pos.append((i, x, y))
            x += fnt.getlength(words[i]) + space
    if box_idx is not None:
        for i, x, y in pos:
            if i == box_idx:
                bx = int(size * 0.16)
                d.rounded_rectangle([x - bx, y - int(size * 0.04), x + fnt.getlength(words[i]) + bx,
                                     y + asc + desc + int(size * 0.04)],
                                    radius=int(size * 0.2), fill=hex_rgb(box_color) + (255,))
    for i, x, y in pos:
        boxed = box_idx is not None and i == box_idx
        d.text((x, y), words[i], font=fnt, fill=hex_rgb(colors[i]) + (255,),
               stroke_width=0 if boxed else stroke, stroke_fill=hex_rgb(stroke_color) + (255,))
    if shadow:
        a = img.getchannel("A")
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sh.putalpha(a.filter(ImageFilter.GaussianBlur(max(2, size * 0.09))).point(lambda v: int(v * 0.75)))
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        base.alpha_composite(sh, (0, int(size * 0.06)))
        base.alpha_composite(img)
        img = base
    return _to_bgra(img)


def text_sprite(text: str, font_name: str, size: int, max_width: int, color: str = "#FFFFFF",
                stroke: int = 0, shadow: bool = True) -> np.ndarray:
    ws = text.split()
    if not ws:
        ws = [" "]
    return words_sprite(ws, [color] * len(ws), font_name, size, max_width, stroke=stroke, shadow=shadow)


def pill_sprite(text: str, font_name: str, size: int, max_width: int, color: str = "#FFFFFF",
                bg: str = "#000000", bg_alpha: int = 170) -> np.ndarray:
    fnt = font(font_name, size)
    ws = text.split() or [" "]
    lines = wrap(ws, fnt, max_width)
    asc, desc = fnt.getmetrics()
    line_h = int((asc + desc) * 1.1)
    strs = [" ".join(ws[i] for i in ln) for ln in lines]
    tw = int(max(fnt.getlength(s) for s in strs))
    px, py = int(size * 0.7), int(size * 0.4)
    W, H = tw + 2 * px, line_h * len(strs) + 2 * py
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=min(H // 2, int(size * 0.9)),
                        fill=hex_rgb(bg) + (bg_alpha,))
    for i, s in enumerate(strs):
        d.text(((W - fnt.getlength(s)) / 2, py + i * line_h), s, font=fnt, fill=hex_rgb(color) + (255,))
    return _to_bgra(img)


def blit(frame: np.ndarray, bgra: np.ndarray, x: int, y: int, opacity: float = 1.0) -> None:
    """Alpha-composite a BGRA sprite onto a BGR frame in place (clipped)."""
    h, w = bgra.shape[:2]
    H, W = frame.shape[:2]
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
    if x0 >= x1 or y0 >= y1 or opacity <= 0:
        return
    src = bgra[y0 - y:y1 - y, x0 - x:x1 - x]
    a = src[..., 3:4].astype(np.uint16)
    a += a >> 7  # 0..256
    if opacity < 1.0:
        a = (a * int(opacity * 256)) >> 8
    roi = frame[y0:y1, x0:x1].astype(np.uint16)
    out = (roi * (256 - a) + src[..., :3].astype(np.uint16) * a) >> 8
    frame[y0:y1, x0:x1] = out.astype(np.uint8)


def blit_center(frame, bgra, cx: int, cy: int, opacity: float = 1.0, scale: float = 1.0) -> None:
    if abs(scale - 1.0) > 0.01:
        h, w = bgra.shape[:2]
        bgra = cv2.resize(bgra, (max(1, int(w * scale)), max(1, int(h * scale))),
                          interpolation=cv2.INTER_LINEAR)
    h, w = bgra.shape[:2]
    blit(frame, bgra, int(cx - w / 2), int(cy - h / 2), opacity)
