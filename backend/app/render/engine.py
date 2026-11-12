"""The shared render engine: timeline JSON -> MP4.

Timeline:
{
  "width": 1080, "height": 1920, "fps": 30,
  "scenes": [{
      "duration": 4.2,
      "visual": {"type": "image", "src": "...", "motion": "zoom_in", "fit": "auto", "grade": "noir"}
              | {"type": "video", "src": "...", "start": 0.0, "crop": {"mode": "track", "keys": [[t, cx], ...]}}
              | {"type": "color", "colors": ["#101018", "#2a1a3a"]},
      "transition": "cut" | "fade" | "flash",
      "overlays": [{"type": "title" | "pill" | "cta", "text": "...", "t0": 0, "t1": 3, "y": 0.3}]
  }],
  "overlays": [...same, absolute times...],
  "captions": {"style": "pop", "words": [{"w": "Hello", "t0": 0.0, "t1": 0.4}], "highlight": "#FFE14A"},
  "audio": {"tracks": [{"src": "...", "start": 0, "src_start": 0, "duration": null, "volume": 1}],
            "music": {"src": "...", "volume": 0.12}},
  "look": {"vignette": 0.35, "grain": 0.04, "progress_bar": "#FFE14A"}
}
"""
import os
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from .. import ffutil
from . import text
from .captions import CaptionTrack

OVERSAMPLE = 1.2
MOTIONS = {  # z0, z1, px0, px1, py0, py1
    "none": (1.0, 1.0, 0, 0, 0, 0),
    "zoom_in": (1.0, 1.18, 0, 0, 0, 0),
    "zoom_out": (1.18, 1.0, 0, 0, 0, 0),
    "pan_left": (1.15, 1.15, 1, -1, 0, 0),
    "pan_right": (1.15, 1.15, -1, 1, 0, 0),
    "pan_up": (1.15, 1.15, 0, 0, 1, -1),
    "pan_down": (1.15, 1.15, 0, 0, -1, 1),
    "drift": (1.06, 1.16, -0.6, 0.6, 0.4, -0.4),
    "punch": (1.0, 1.2, 0, 0, -0.3, -0.3),
}
MOTION_CYCLE = ["zoom_in", "pan_right", "zoom_out", "pan_up", "drift", "pan_left", "punch", "pan_down"]


def imread(path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"Cannot read image: {path}")
    return img


def imwrite(path, img, quality: int = 92) -> None:
    ext = Path(path).suffix or ".jpg"
    ok, buf = cv2.imencode(ext, img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if ok:
        buf.tofile(str(path))


def cover(img, w: int, h: int) -> np.ndarray:
    ih, iw = img.shape[:2]
    s = max(w / iw, h / ih)
    nw, nh = max(w, int(round(iw * s))), max(h, int(round(ih * s)))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    x, y = (nw - w) // 2, (nh - h) // 2
    return r[y:y + h, x:x + w]


def fit_blur(img, w: int, h: int, boost: float = 1.0) -> np.ndarray:
    """Whole image over a blurred, darkened copy of itself. boost>1 crops the edges a little."""
    bg = cover(img, max(8, w // 8), max(8, h // 8))
    bg = cv2.GaussianBlur(bg, (0, 0), 5)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    bg = (bg * 0.42).astype(np.uint8)
    ih, iw = img.shape[:2]
    s = min(w / iw, h / ih) * boost
    nw, nh = int(round(iw * s)), int(round(ih * s))
    fg = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    if nw > w:
        fg = fg[:, (nw - w) // 2:(nw - w) // 2 + w]
    if nh > h:
        fg = fg[(nh - h) // 2:(nh - h) // 2 + h]
    fh, fw = fg.shape[:2]
    x, y = (w - fw) // 2, (h - fh) // 2
    bg[y:y + fh, x:x + fw] = fg
    return bg


def grade(img, name: str | None) -> np.ndarray:
    if not name or name == "none":
        return img
    f = img.astype(np.float32) / 255.0
    gray = (f[..., 0] * 0.114 + f[..., 1] * 0.587 + f[..., 2] * 0.299)[..., None]
    if name == "noir":
        f = gray * 0.85 + f * 0.15
        f = (f - 0.5) * 1.22 + 0.5
        f = f * np.array([1.04, 1.0, 0.97], np.float32)
    elif name == "sepia":
        f = (gray * 0.8 + f * 0.2) * np.array([0.74, 0.9, 1.04], np.float32)
    elif name == "cold":
        f = (gray * 0.25 + f * 0.75) * np.array([1.1, 1.0, 0.88], np.float32)
        f = (f - 0.5) * 1.1 + 0.48
    elif name == "warm":
        f = f * np.array([0.9, 1.0, 1.08], np.float32)
    elif name == "punchy":
        f = (f - gray) * 1.18 + gray
        f = (f - 0.5) * 1.1 + 0.5
    return (np.clip(f, 0, 1) * 255).astype(np.uint8)


class ImageScene:
    def __init__(self, v: dict, W: int, H: int, duration: float, index: int):
        self.W, self.H, self.dur = W, H, max(0.1, duration)
        img = imread(v["src"])
        cw, ch = int(W * OVERSAMPLE), int(H * OVERSAMPLE)
        fit = v.get("fit", "auto")
        ratio = (img.shape[1] / img.shape[0]) / (W / H)
        if fit == "cover" or (fit == "auto" and 0.8 < ratio < 1.25):
            canvas = cover(img, cw, ch)
        else:
            canvas = fit_blur(img, cw, ch, boost=1.0 if fit == "contain" else 1.35)
        self.canvas = grade(canvas, v.get("grade"))
        motion = v.get("motion") or MOTION_CYCLE[index % len(MOTION_CYCLE)]
        self.m = MOTIONS.get(motion, MOTIONS["zoom_in"])

    def render(self, t: float) -> np.ndarray:
        p = min(1.25, max(0.0, t / self.dur))
        z0, z1, px0, px1, py0, py1 = self.m
        z = z0 + (z1 - z0) * p
        ch, cw = self.canvas.shape[:2]
        vw, vh = cw / z, ch / z
        cx = cw / 2 + (px0 + (px1 - px0) * p) * (cw - vw) / 2
        cy = ch / 2 + (py0 + (py1 - py0) * p) * (ch - vh) / 2
        M = np.array([[vw / self.W, 0, cx - vw / 2], [0, vh / self.H, cy - vh / 2]], np.float32)
        return cv2.warpAffine(self.canvas, M, (self.W, self.H),
                              flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                              borderMode=cv2.BORDER_REPLICATE)

    def close(self):
        self.canvas = None


class ColorScene:
    def __init__(self, v: dict, W: int, H: int, duration: float, index: int):
        cols = v.get("colors") or [v.get("color", "#0d0d14"), "#1c1830"]
        c0 = np.array(text.hex_rgb(cols[0])[::-1], np.float32)
        c1 = np.array(text.hex_rgb(cols[-1])[::-1], np.float32)
        g = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
        self.img = np.broadcast_to(c0 * (1 - g) + c1 * g, (H, W, 3)).astype(np.uint8)

    def render(self, t: float) -> np.ndarray:
        return self.img.copy()

    def close(self):
        self.img = None


class VideoScene:
    """Sequentially decoded source video with a (possibly moving) crop window."""

    def __init__(self, v: dict, W: int, H: int, duration: float, index: int):
        self.W, self.H = W, H
        self.cap = cv2.VideoCapture(str(v["src"]))
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open video: {v['src']}")
        self.sfps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.start = float(v.get("start", 0.0))
        if self.start > 0:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(self.start * self.sfps)))
        self.read_count = 0
        self.frame = None
        self.out = None
        self.out_idx = -1
        crop = v.get("crop") or {"mode": "center"}
        self.segments = crop.get("segments") or [dict(crop, t0=0.0, t1=1e9)]

    def _seg(self, t: float) -> dict:
        for s in self.segments:
            if s.get("t0", 0) <= t < s.get("t1", 1e9):
                return s
        return self.segments[-1]

    def render(self, t: float) -> np.ndarray:
        target = int(max(0.0, t) * self.sfps + 1e-6)
        while self.read_count <= target:
            ok, fr = self.cap.read()
            if not ok:
                break
            self.frame = fr
            self.read_count += 1
        if self.frame is None:
            return np.zeros((self.H, self.W, 3), np.uint8)
        if self.out_idx == self.read_count and self.out is not None:
            return self.out.copy()
        fr = self.frame
        sh, sw = fr.shape[:2]
        seg = self._seg(t)
        mode = seg.get("mode", "center")
        a = self.W / self.H
        if mode == "blur":
            out = fit_blur(fr, self.W, self.H, boost=float(seg.get("boost", 1.0)))
        else:
            cx = cy = 0.5
            keys = seg.get("keys")
            if mode == "track" and keys:
                ts = [k[0] for k in keys]
                cx = float(np.interp(t, ts, [k[1] for k in keys]))
                if len(keys[0]) > 2:
                    cy = float(np.interp(t, ts, [k[2] for k in keys]))
            if sw / sh > a:
                cw, chh = int(round(sh * a)), sh
            else:
                cw, chh = sw, int(round(sw / a))
            x = int(min(max(cx * sw - cw / 2, 0), sw - cw))
            y = int(min(max(cy * sh - chh / 2, 0), sh - chh))
            win = fr[y:y + chh, x:x + cw]
            interp = cv2.INTER_AREA if cw > self.W else cv2.INTER_CUBIC
            out = cv2.resize(win, (self.W, self.H), interpolation=interp)
        self.out, self.out_idx = out, self.read_count
        return out.copy()

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.frame = self.out = None


SCENE_TYPES = {"image": ImageScene, "video": VideoScene, "color": ColorScene}


def _overlay_item(o: dict, t0: float, t1: float, W: int, H: int) -> list[dict]:
    m = min(W, H)
    kind = o.get("type", "pill")
    items = []
    txt = str(o.get("text", "")).strip()
    if not txt:
        return items
    if kind == "title":
        size = int(m * float(o.get("size", 0.105)))
        sp = text.text_sprite(txt.upper() if o.get("upper", True) else txt, o.get("font", "anton"), size,
                              int(W * 0.86), o.get("color", "#FFFFFF"), stroke=max(2, int(size * 0.07)))
        cy = int(H * float(o.get("y", 0.3)))
        items.append({"sprite": sp, "cx": W // 2, "cy": cy})
        if o.get("sub"):
            s2 = text.text_sprite(str(o["sub"]), "poppins-sb", int(m * 0.042), int(W * 0.8),
                                  o.get("sub_color", "#FFE14A"), stroke=max(2, int(m * 0.004)))
            items.append({"sprite": s2, "cx": W // 2, "cy": cy + sp.shape[0] // 2 + s2.shape[0] // 2 - int(size * 0.2)})
    elif kind == "cta":
        sp = text.pill_sprite(txt, "poppins-xb", int(m * 0.05), int(W * 0.8), color="#111111",
                              bg=o.get("bg", "#FFE14A"), bg_alpha=255)
        items.append({"sprite": sp, "cx": W // 2, "cy": int(H * float(o.get("y", 0.82)))})
    else:
        sp = text.pill_sprite(txt, "poppins-sb", int(m * float(o.get("size", 0.032))), int(W * 0.8),
                              color=o.get("color", "#FFFFFF"), bg=o.get("bg", "#000000"),
                              bg_alpha=int(o.get("bg_alpha", 165)))
        cx = W // 2
        if o.get("x") == "left":
            cx = int(W * 0.05) + sp.shape[1] // 2
        elif o.get("x") == "right":
            cx = W - int(W * 0.05) - sp.shape[1] // 2
        items.append({"sprite": sp, "cx": cx, "cy": int(H * float(o.get("y", 0.075)))})
    for it in items:
        it.update(t0=t0, t1=t1, fade=float(o.get("fade", 0.25)))
    return items


def _build_overlays(tl: dict, starts: list, total: float, W: int, H: int) -> list[dict]:
    items = []
    for i, s in enumerate(tl["scenes"]):
        for o in s.get("overlays") or []:
            t0 = starts[i] + float(o.get("t0", 0))
            t1 = starts[i] + float(o["t1"] if o.get("t1") is not None else s["duration"])
            items += _overlay_item(o, t0, t1, W, H)
    for o in tl.get("overlays") or []:
        items += _overlay_item(o, float(o.get("t0", 0)), float(o["t1"] if o.get("t1") is not None else total), W, H)
    return items


def _vignette(W: int, H: int, strength: float):
    if strength <= 0:
        return None
    y, x = np.ogrid[-1:1:H * 1j, -1:1:W * 1j]
    d = np.sqrt(x * x + y * y) / 1.414
    mask = 1.0 - strength * np.clip((d - 0.35) / 0.65, 0, 1) ** 1.6
    return cv2.merge([(mask * 255).astype(np.uint8)] * 3)


def mix_audio(tl: dict, total: float, out_wav: str) -> str:
    au = tl.get("audio") or {}
    tracks = [t for t in au.get("tracks") or [] if t.get("src") and Path(t["src"]).exists()]
    music = au.get("music") or {}
    if not (music.get("src") and Path(music["src"]).exists()):
        music = None
    args, filt, labels = [], [], []
    for i, t in enumerate(tracks):
        args += ["-i", t["src"]]
        f = []
        ss, d = float(t.get("src_start") or 0), t.get("duration")
        if ss > 0 or d:
            f.append(f"atrim=start={ss:.3f}" + (f":duration={float(d):.3f}" if d else ""))
            f.append("asetpts=PTS-STARTPTS")
        f += ["aresample=48000", "aformat=channel_layouts=stereo", f"volume={float(t.get('volume', 1.0)):.3f}"]
        delay = int(float(t.get("start") or 0) * 1000)
        if delay > 0:
            f.append(f"adelay={delay}:all=1")
        filt.append(f"[{i}:a]{','.join(f)}[a{i}]")
        labels.append(f"[a{i}]")
    n = len(tracks)
    n_in = n
    if n == 0:
        args += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        voice, n_in = "[0:a]", 1
    elif n == 1:
        voice = "[a0]"
    else:
        filt.append("".join(labels) + f"amix=inputs={n}:normalize=0:dropout_transition=0[voice]")
        voice = "[voice]"
    if music:
        args += ["-stream_loop", "-1", "-i", music["src"]]
        mv = float(music.get("volume", 0.12))
        filt.append(f"[{n_in}:a]aresample=48000,aformat=channel_layouts=stereo,volume={mv:.3f},"
                    f"atrim=duration={total:.3f}[m]")
        filt.append(f"{voice}asplit=2[v1][v2]")
        filt.append("[m][v2]sidechaincompress=threshold=0.02:ratio=6:attack=30:release=500[md]")
        filt.append("[v1][md]amix=inputs=2:normalize=0:dropout_transition=0[mix]")
        voice = "[mix]"
    fo = max(0.0, total - 0.35)
    filt.append(f"{voice}apad,atrim=duration={total:.3f},afade=t=out:st={fo:.3f}:d=0.35[out]")
    ffutil.run([*args, "-filter_complex", ";".join(filt), "-map", "[out]", "-ar", "48000",
                "-c:a", "pcm_s16le", out_wav])
    return out_wav


def render(tl: dict, out_path, progress=None) -> dict:
    W, H, fps = int(tl.get("width", 1080)), int(tl.get("height", 1920)), int(tl.get("fps", 30))
    W -= W % 2
    H -= H % 2
    scenes = tl["scenes"]
    if not scenes:
        raise ValueError("Timeline has no scenes")
    starts, t = [], 0.0
    for s in scenes:
        starts.append(t)
        t += float(s["duration"])
    total = t
    n_frames = max(1, int(round(total * fps)))
    look = tl.get("look") or {}

    objs: dict = {}

    def get(i):
        if i not in objs:
            v = scenes[i].get("visual") or {"type": "color"}
            cls = SCENE_TYPES.get(v.get("type"), ColorScene)
            try:
                objs[i] = cls(v, W, H, float(scenes[i]["duration"]), i)
            except Exception:
                if cls is ColorScene:
                    raise
                objs[i] = ColorScene({}, W, H, 1.0, i)
        return objs[i]

    cap_spec = tl.get("captions") or {}
    captions = CaptionTrack(cap_spec, W, H) if cap_spec.get("words") else None
    overlays = _build_overlays(tl, starts, total, W, H)
    vign = _vignette(W, H, float(look.get("vignette", 0)))
    grain = float(look.get("grain", 0))
    noise = None
    if grain > 0:
        rng = np.random.default_rng(7)
        noise = [cv2.resize(rng.integers(0, 256, (H // 2, W // 2, 1), dtype=np.uint8), (W, H),
                            interpolation=cv2.INTER_LINEAR) for _ in range(6)]
        noise = [cv2.merge([nz] * 3) for nz in noise]
    bar = look.get("progress_bar")
    bar_col = text.hex_rgb(bar)[::-1] if bar else None

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="kc_")
    wav = os.path.join(tmpdir, "mix.wav")
    if progress:
        progress(0.0, "Mixing audio")
    mix_audio(tl, total, wav)

    enc = ffutil.video_encoder()
    errlog = open(os.path.join(tmpdir, "ffmpeg.log"), "wb")
    cmd = [ffutil.ffmpeg(), "-hide_banner", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-i", wav, "-map", "0:v", "-map", "1:a",
           "-c:v", *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", "-shortest", out_path]
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errlog, **kw)
    si = 0
    try:
        for f in range(n_frames):
            t = f / fps
            while si + 1 < len(scenes) and t >= starts[si + 1]:
                si += 1
                old = objs.pop(si - 2, None)
                if old:
                    old.close()
            lt = t - starts[si]
            frame = get(si).render(lt)
            tr = scenes[si].get("transition", "cut")
            if si > 0 and tr == "fade" and lt < 0.35:
                prev = get(si - 1).render(float(scenes[si - 1]["duration"]) + lt)
                a = lt / 0.35
                frame = cv2.addWeighted(prev, 1 - a, frame, a, 0)
            elif si > 0 and tr == "flash" and lt < 0.2:
                frame = cv2.convertScaleAbs(frame, alpha=1.0, beta=(1 - lt / 0.2) * 110)
            if vign is not None:
                frame = cv2.multiply(frame, vign, scale=1 / 255.0)
            if noise is not None:
                frame = cv2.addWeighted(frame, 1.0, noise[f % len(noise)], grain, -grain * 128)
            for o in overlays:
                if o["t0"] <= t < o["t1"]:
                    fd = max(0.01, o["fade"])
                    a_in = min(1.0, (t - o["t0"]) / fd)
                    a = min(a_in, (o["t1"] - t) / fd, 1.0)
                    text.blit_center(frame, o["sprite"], o["cx"], o["cy"] + int((1 - a_in) * 18), a)
            if captions:
                captions.draw(frame, t)
            if bar_col:
                bh = max(4, H // 240)
                cv2.rectangle(frame, (0, 0), (int(W * (f + 1) / n_frames), bh), bar_col, -1)
            proc.stdin.write(frame.data)
            if progress and f % 15 == 0:
                progress(f / n_frames, f"Rendering {int(100 * f / n_frames)}%")
        proc.stdin.close()
        proc.wait()
    except (BrokenPipeError, OSError):
        proc.wait()
    finally:
        for o in objs.values():
            o.close()
        errlog.close()
    if proc.returncode != 0:
        tail = Path(errlog.name).read_text("utf-8", "replace")[-1500:]
        raise RuntimeError(f"Video encode failed: {tail}")
    for p in (wav, errlog.name):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass
    if progress:
        progress(1.0, "Render finished")
    return {"duration": total, "frames": n_frames, "width": W, "height": H}


def thumbnail(video_path, out_jpg, t: float = 1.0) -> None:
    ffutil.run(["-ss", f"{t:.2f}", "-i", video_path, "-frames:v", "1", "-q:v", "3", out_jpg], check=False)
