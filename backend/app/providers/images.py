"""Scene images: Cloudflare FLUX -> Pollinations -> Pixabay stock -> generated title card."""
import base64
import hashlib
import re
import shutil
import time
import urllib.parse
from pathlib import Path

import cv2
import httpx
import numpy as np

from .. import config, db
from ..render import text

SIZES = {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024)}


def _valid(path: Path) -> bool:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR) is not None
    except Exception:
        return False


def _cloudflare(prompt: str, out: Path, aspect: str, seed: int) -> None:
    acc, tok = config.get("CLOUDFLARE_ACCOUNT_ID"), config.get("CLOUDFLARE_API_TOKEN")
    if not (acc and tok):
        raise RuntimeError("not configured")
    r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                   headers={"Authorization": f"Bearer {tok}"},
                   json={"prompt": prompt[:2000], "steps": 4, "seed": seed}, timeout=120)
    r.raise_for_status()
    img = (r.json().get("result") or {}).get("image")
    if not img:
        raise RuntimeError("no image in response")
    out.write_bytes(base64.b64decode(img))
    db.log_usage("cloudflare-flux", "images", 1)


def _pollinations(prompt: str, out: Path, aspect: str, seed: int) -> None:
    w, h = SIZES.get(aspect, SIZES["9:16"])
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:1200], safe="")
    params = {"width": w, "height": h, "seed": seed, "nologo": "true", "model": "flux"}
    for attempt in range(3):
        r = httpx.get(url, params=params, timeout=120, follow_redirects=True)
        if r.status_code in (402, 429) and attempt < 2:
            # keyless tier: one image every ~45 s per IP; wait for the slot instead of failing
            m = re.search(r"Retry after (\d+)s", r.text)
            time.sleep(min(60, int(m[1]) + 1 if m else 46))
            continue
        break
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image") or len(r.content) < 4000:
        raise RuntimeError("no image returned")
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("invalid image data")
    img = img[: int(img.shape[0] * 0.945)]          # the keyless tier stamps a logo along the bottom edge
    cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 94])[1].tofile(str(out))
    db.log_usage("pollinations", "images", 1)


def _pixabay(query: str, out: Path, aspect: str, seed: int) -> None:
    key = config.get("PIXABAY_API_KEY")
    if not key:
        raise RuntimeError("not configured")
    r = httpx.get("https://pixabay.com/api/", params={
        "key": key, "q": query[:100], "image_type": "photo", "safesearch": "true", "per_page": 8,
        "orientation": "vertical" if aspect == "9:16" else "horizontal"}, timeout=30)
    r.raise_for_status()
    hits = r.json().get("hits") or []
    if not hits:
        raise RuntimeError("no results")
    hit = hits[seed % len(hits)]
    img = httpx.get(hit["largeImageURL"], timeout=60, follow_redirects=True)
    img.raise_for_status()
    out.write_bytes(img.content)
    out.with_suffix(".credit.txt").write_text(f"Pixabay / {hit.get('user', '')} — {hit.get('pageURL', '')}", "utf-8")
    db.log_usage("pixabay", "requests", 1)


_AI = {"cloudflare": _cloudflare, "pollinations": _pollinations}


def available() -> dict:
    return {"cloudflare": bool(config.get("CLOUDFLARE_ACCOUNT_ID") and config.get("CLOUDFLARE_API_TOKEN")),
            "pollinations": "pollinations" in config.get("IMAGE_PROVIDERS"),
            "pixabay": bool(config.get("PIXABAY_API_KEY"))}


def generate(prompt: str, out_path, aspect: str = "9:16", seed: int = 1, stock_query: str | None = None) -> str:
    """Fill out_path with an image for the prompt. Returns the provider used; raises if all fail."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{prompt}|{aspect}|{seed}".encode()).hexdigest()
    cached = config.CACHE / "img" / f"{key}.jpg"
    if cached.exists() and _valid(cached):
        shutil.copyfile(cached, out)
        return "cache"
    errors = []
    for name in [p.strip() for p in config.get("IMAGE_PROVIDERS").split(",") if p.strip()]:
        try:
            if name in _AI:
                _AI[name](prompt, out, aspect, seed)
            elif name == "pixabay":
                _pixabay(stock_query or prompt, out, aspect, seed)
            else:
                continue
            if not _valid(out):
                raise RuntimeError("invalid image data")
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out, cached)
            return name
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {str(e)[:100]}")
    raise RuntimeError("; ".join(errors) or "no image provider configured")


def stock(query: str, out_path, aspect: str = "9:16", seed: int = 0) -> str:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    _pixabay(query, out, aspect, seed)
    if not _valid(out):
        raise RuntimeError("invalid image data")
    return "pixabay"


def placeholder(label: str, out_path, aspect: str = "9:16", palette=("#141021", "#3b1d4a")) -> str:
    """Always-works fallback: a moody gradient card with the scene's key phrase."""
    w, h = SIZES.get(aspect, SIZES["9:16"])
    c0 = np.array(text.hex_rgb(palette[0])[::-1], np.float32)
    c1 = np.array(text.hex_rgb(palette[1])[::-1], np.float32)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    g = np.clip((yy / h) * 0.7 + (xx / w) * 0.3, 0, 1)[..., None]
    img = (c0 * (1 - g) + c1 * g)
    d = np.sqrt((xx - w * 0.5) ** 2 + (yy - h * 0.38) ** 2) / (0.6 * max(w, h))
    img += (np.clip(1 - d, 0, 1) ** 2)[..., None] * 38
    img = np.clip(img, 0, 255).astype(np.uint8)
    if label.strip():
        sp = text.text_sprite(label.strip()[:70], "serif", int(min(w, h) * 0.085), int(w * 0.8), "#F4EBD8")
        text.blit_center(img, sp, w // 2, int(h * 0.38), 0.95)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    buf.tofile(str(out_path))
    return "placeholder"
