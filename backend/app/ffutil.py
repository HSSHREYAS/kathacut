"""FFmpeg helpers. Uses a system ffmpeg if present, else the one bundled by imageio-ffmpeg."""
import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


@functools.lru_cache(maxsize=1)
def ffmpeg() -> str:
    p = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(args: list, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [ffmpeg(), "-hide_banner", "-y", *[str(a) for a in args]]
    r = subprocess.run(cmd, capture_output=True, **_NOWIN)
    if check and r.returncode != 0:
        tail = r.stderr.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"ffmpeg failed: {tail}")
    return r


def probe(path) -> dict:
    """duration / size / fps / has_audio, parsed from `ffmpeg -i`."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, **_NOWIN)
    txt = r.stderr.decode("utf-8", "replace")
    out = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0, "has_audio": False, "has_video": False}
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if m:
        out["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    for line in txt.splitlines():
        if "Stream #" in line and "Video:" in line and not out["has_video"]:
            m = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if m:
                out["width"], out["height"], out["has_video"] = int(m[1]), int(m[2]), True
            m = re.search(r"([\d.]+) fps", line)
            if m:
                out["fps"] = float(m[1])
        if "Stream #" in line and "Audio:" in line:
            out["has_audio"] = True
    return out


@functools.lru_cache(maxsize=1)
def video_encoder() -> tuple:
    """NVENC when the GPU supports it, else x264."""
    if os.getenv("KATHACUT_ENCODER", "").lower() != "cpu":
        r = run(["-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc",
                 "-f", "null", "-"], check=False)
        if r.returncode == 0:
            return ("h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0",
                    "-maxrate", "7M", "-bufsize", "14M")
    return ("libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "7M", "-bufsize", "14M")


def extract_audio(src, dst, sr: int = 16000, start: float | None = None, dur: float | None = None):
    args = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    args += ["-i", src, "-vn", "-ac", "1", "-ar", sr]
    if str(dst).endswith(".mp3"):
        args += ["-b:a", "64k"]
    run([*args, dst])
    return dst


def download(url: str, dst: Path, timeout: float = 120.0) -> Path:
    """Fetch a model/font/asset once."""
    dst = Path(dst)
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
    tmp.replace(dst)
    return dst
