"""YouTube acquisition via yt-dlp. Works from a home connection; datacenter IPs are blocked by YouTube.

Intended for your own uploads or videos you have permission to use.
"""
from pathlib import Path

from .. import config, ffutil


def download(url: str, outdir, progress=None) -> tuple[Path, str]:
    import yt_dlp

    outdir = Path(outdir)

    def hook(d):
        if progress and d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                progress(min(0.99, d.get("downloaded_bytes", 0) / tot), "Downloading video")

    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(outdir / "source.%(ext)s"),
        "ffmpeg_location": ffutil.ffmpeg(),
        "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
        "progress_hooks": [hook], "retries": 3,
        "match_filter": yt_dlp.utils.match_filter_func("duration < 14400"),
    }
    cookies = config.DATA / "cookies.txt"
    if cookies.exists():
        opts["cookiefile"] = str(cookies)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "Could not download that link. YouTube blocks cloud servers and changes often: run KathaCut on your "
            "own computer, update yt-dlp (pip install -U yt-dlp), or download the video yourself and upload the "
            f"file instead. Details: {str(e)[:300]}") from e
    files = sorted(outdir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not files:
        raise RuntimeError("Download finished but no video file was produced.")
    return files[0], str((info or {}).get("title") or "")
