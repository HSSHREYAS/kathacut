"""Clip Studio: long video -> ranked moments -> face-tracked vertical shorts + a best-of cut."""
import json
import math
import re
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from .. import config, db, ffutil, worker
from ..providers import llm, stt
from ..render import engine
from . import reframe, youtube

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
HOOK_WORDS = ("secret", "never", "nobody", "truth", "mistake", "why", "how", "crazy", "insane", "biggest",
              "worst", "best", "first time", "the reason", "what happened", "turns out", "actually")


def find_source(pdir: Path):
    files = [f for f in pdir.glob("source.*") if f.suffix.lower() in VIDEO_EXT]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def _sentences(words: list[dict]) -> list[dict]:
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        end = w["w"][-1:] in ".?!…।" or nxt is None or nxt["t0"] - w["t1"] > 0.8 or len(cur) >= 32
        if end:
            out.append({"i": len(out), "t0": cur[0]["t0"], "t1": cur[-1]["t1"],
                        "text": " ".join(x["w"] for x in cur)})
            cur = []
    return out


def _energy(src: Path, pdir: Path, sentences: list[dict]) -> list[float]:
    """Loudness z-score per sentence (laughter, raised voices and emphasis read as peaks)."""
    wav = pdir / "energy.wav"
    try:
        ffutil.extract_audio(src, wav, 8000)
        x, sr = sf.read(str(wav), dtype="float32")
        wav.unlink(missing_ok=True)
        hop = sr // 2
        n = len(x) // hop
        rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-9)
        db_ = 20 * np.log10(rms + 1e-6)
        z = (db_ - db_.mean()) / (db_.std() + 1e-6)
        out = []
        for s in sentences:
            a, b = int(s["t0"] * 2), max(int(s["t0"] * 2) + 1, int(math.ceil(s["t1"] * 2)))
            out.append(float(z[a:b].max()) if a < len(z) else 0.0)
        return out
    except Exception:
        return [0.0] * len(sentences)


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _llm_pick(sentences, lo, hi, want, budget, notes, ctx) -> list[dict]:
    blocks, cur, size = [], [], 0
    for s in sentences:
        line = f"[{s['i']}] ({_mmss(s['t0'])}) {s['text']}\n"
        if cur and (size + len(line) > budget - 3500 or s["t1"] - cur[0]["t1"] > 1500):
            blocks.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(line)
    if cur:
        blocks.append(cur)
    total = sentences[-1]["t1"] - sentences[0]["t0"] or 1.0
    picks = []
    for bi, blk in enumerate(blocks):
        ctx.progress(0.62 + 0.3 * bi / len(blocks), f"Finding the best moments {bi + 1}/{len(blocks)}")
        k = max(2, math.ceil(want * 1.6 * (blk[-1]["t1"] - blk[0]["t0"]) / total) + 1)
        body = "".join(f"[{s['i']}] ({_mmss(s['t0'])}) {s['text']}\n" for s in blk)
        prompt = (
            "You are a short-form video editor. Below is a transcript; each line is one sentence with its index "
            "and timestamp.\n"
            f"Pick the {k} best stand-alone moments for vertical shorts. Each clip must:\n"
            f"- last between {lo} and {hi} seconds (use the timestamps),\n"
            "- start on a sentence that hooks a viewer with zero context (a bold claim, question, or the start of a story),\n"
            "- contain one complete idea, story or joke and end right after its payoff,\n"
            "- not overlap another clip.\n"
            + (f"Editor's notes: {notes[:400]}\n" if notes else "") +
            'Return JSON: {"clips":[{"start":first sentence index,"end":last sentence index,"title":"max 7 word '
            'on-screen hook title","hook":"why the first 3 seconds grab attention","reason":"why it works as a clip",'
            '"score":0-100 virality estimate}]}\n\nTRANSCRIPT:\n' + body)
        try:
            r = llm.complete_json(prompt, tier="smart" if len(blocks) <= 6 else "bulk", max_tokens=3000,
                                  temperature=0.4)
            for c in r.get("clips", []) if isinstance(r, dict) else []:
                try:
                    picks.append({"start": int(c["start"]), "end": int(c["end"]),
                                  "title": str(c.get("title") or "").strip()[:70],
                                  "hook": str(c.get("hook") or "").strip()[:240],
                                  "reason": str(c.get("reason") or "").strip()[:240],
                                  "score": float(c.get("score") or 50)})
                except (KeyError, TypeError, ValueError):
                    continue
        except llm.LLMError:
            if bi == 0:
                return []
    return picks


def _heuristic_pick(sentences, energy, lo, hi, want) -> list[dict]:
    """No-LLM fallback: windows scored by loudness peaks, questions and hook words."""
    cands = []
    target = (lo + hi) / 2
    for a in range(len(sentences)):
        b = a
        while b + 1 < len(sentences) and sentences[b]["t1"] - sentences[a]["t0"] < target:
            b += 1
        d = sentences[b]["t1"] - sentences[a]["t0"]
        if d < lo or d > hi:
            continue
        first = sentences[a]["text"].lower()
        sc = 45 + 6 * float(np.mean(energy[a:b + 1])) + 8 * ("?" in first) + \
            6 * sum(1 for h in HOOK_WORDS if h in first)
        cands.append({"start": a, "end": b, "title": " ".join(sentences[a]["text"].split()[:6]).rstrip(",.") ,
                      "hook": "", "reason": "Picked by loudness and hook-word heuristics (no LLM was available).",
                      "score": float(max(5, min(95, sc)))})
    return cands


def _fit(c: dict, sentences, lo, hi):
    """Clamp a pick to valid sentence indexes and the allowed duration."""
    n = len(sentences)
    a, b = max(0, min(n - 1, c["start"])), max(0, min(n - 1, c["end"]))
    if b < a:
        a, b = b, a
    while sentences[b]["t1"] - sentences[a]["t0"] > hi and b > a:
        b -= 1
    while sentences[b]["t1"] - sentences[a]["t0"] < lo and b + 1 < n:
        b += 1
    d = sentences[b]["t1"] - sentences[a]["t0"]
    if d < lo * 0.6 or d > hi * 1.25:
        return None
    return a, b


@worker.handler("clips_analyze")
def analyze(job, ctx):
    p = db.get_project(job["project_id"])
    pid, params = p["id"], p["params"]
    pdir = config.PROJECTS / pid
    db.update_project(pid, status="analyzing")
    src = find_source(pdir)
    title = p["title"]
    if not src:
        if params.get("source") != "youtube" or not params.get("url"):
            raise RuntimeError("No video file found for this project")
        ctx.progress(0.01, "Downloading video")
        src, yt_title = youtube.download(params["url"], pdir, ctx.sub(0.01, 0.2))
        if yt_title and (not title or title.startswith("Clips ")):
            title = yt_title[:90]
            db.update_project(pid, title=title)
    info = ffutil.probe(src)
    if not info["has_audio"]:
        raise RuntimeError("This video has no audio track, so there is nothing to transcribe.")
    tfile = pdir / "transcript.json"
    if tfile.exists():
        tr = json.loads(tfile.read_text("utf-8"))
    else:
        ctx.progress(0.2, "Transcribing")
        tr = stt.transcribe(src, pdir, params.get("language") or None, ctx.sub(0.2, 0.58))
        tfile.write_text(json.dumps(tr), "utf-8")
    words = tr["words"]
    if len(words) < 20:
        raise RuntimeError("Almost no speech was found in this video.")
    sentences = _sentences(words)
    ctx.progress(0.6, "Measuring audio energy")
    energy = _energy(src, pdir, sentences)
    lo, hi = int(params.get("min_seconds", 20)), int(params.get("max_seconds", 60))
    want = int(params.get("n_clips", 5))
    picks = []
    if llm.available():
        picks = _llm_pick(sentences, lo, hi, want, llm.budget_chars(), params.get("notes") or "", ctx)
    used_llm = bool(picks)
    if not picks:
        picks = _heuristic_pick(sentences, energy, lo, hi, want)
    clips = []
    for c in picks:
        fit = _fit(c, sentences, lo, hi)
        if not fit:
            continue
        a, b = fit
        e = float(np.mean(energy[a:b + 1]))
        boost = 15 * (1 / (1 + math.exp(-e)) - 0.5) if used_llm else 0
        clips.append({"id": db.new_id()[:8], "start": round(max(0.0, sentences[a]["t0"] - 0.15), 2),
                      "end": round(min(info["duration"], sentences[b]["t1"] + 0.3), 2),
                      "title": c["title"], "hook": c["hook"], "reason": c["reason"],
                      "score": round(max(1, min(99, c["score"] + boost))),
                      "text": " ".join(s["text"] for s in sentences[a:b + 1]), "outputs": []})
    clips.sort(key=lambda c: -c["score"])
    kept = []
    for c in clips:
        if all(min(c["end"], k["end"]) - max(c["start"], k["start"]) < 0.4 * (c["end"] - c["start"]) for k in kept):
            kept.append(c)
        if len(kept) >= max(want * 2, want + 3):
            break
    for i, c in enumerate(kept):
        c["selected"] = i < want
    if not kept:
        raise RuntimeError("No usable moments were found. Try a wider clip-length range.")
    db.patch_data(pid, clips=kept, picked_by="llm" if used_llm else "heuristic",
                  source={"file": src.name, "title": title, **info}, stt=tr.get("provider"),
                  language=tr.get("language"))
    db.update_project(pid, status="ready")


# ----------------------------------------------------------------------------- rendering
ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}


def _cut(src: Path, start: float, dur: float, dst: Path) -> Path:
    """Frame-accurate, constant-frame-rate intermediate so audio and video stay in sync."""
    enc = ffutil.video_encoder()
    q = ["-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0"] if enc[0] == "h264_nvenc" else \
        ["-preset", "veryfast", "-crf", "17"]
    ffutil.run(["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
                "-vf", "fps=30,scale=-2:'min(1080,ih)',format=yuv420p", "-c:v", enc[0], *q,
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", dst])
    return dst


def _clip_words(words, start, end):
    return [{"w": w["w"], "t0": round(max(0.0, w["t0"] - start), 3), "t1": round(w["t1"] - start, 3)}
            for w in words if w["t0"] >= start - 0.05 and w["t1"] <= end + 0.05]


@worker.handler("clips_render")
def render(job, ctx):
    p = db.get_project(job["project_id"])
    pid, data = p["id"], p["data"]
    pdir = config.PROJECTS / pid
    jp = job["params"]
    src = find_source(pdir)
    if not src:
        raise RuntimeError("Source video is missing")
    words = json.loads((pdir / "transcript.json").read_text("utf-8"))["words"]
    ids = jp.get("clip_ids") or [c["id"] for c in data["clips"] if c.get("selected")]
    todo = [c for c in data["clips"] if c["id"] in ids]
    if not todo:
        raise RuntimeError("No clips selected")
    quality = jp.get("quality", "final")
    full = ASPECTS.get(jp.get("aspect", "9:16"), ASPECTS["9:16"])
    W, H = full if quality == "final" else (full[0] // 2, full[1] // 2)
    highlight = jp.get("highlight_color") or "#FFE14A"
    outdir, tmpdir = pdir / "out", pdir / "tmp"
    outdir.mkdir(exist_ok=True)
    tmpdir.mkdir(exist_ok=True)
    stamp = int(time.time())
    want_best_of = bool(jp.get("best_of"))
    span = 0.85 if want_best_of else 1.0
    cuts = {}
    for i, c in enumerate(todo):
        lo, hi = span * i / len(todo), span * (i + 1) / len(todo)
        ctx.progress(lo, f"Cutting clip {i + 1}/{len(todo)}")
        dur = c["end"] - c["start"]
        tmp = _cut(src, c["start"], dur, tmpdir / f"{c['id']}.mp4")
        cuts[c["id"]] = tmp
        if jp.get("skip_shorts"):
            continue
        crop = reframe.analyze(tmp, W / H, progress=ctx.sub(lo, lo + (hi - lo) * 0.25))
        overlays = []
        if c.get("title") and jp.get("show_title", True):
            overlays.append({"type": "title", "text": c["title"], "y": 0.15, "size": 0.074, "t1": min(3.2, dur),
                             "font": "poppins-xb"})
        tl = {"width": W, "height": H, "fps": 30,
              "scenes": [{"duration": dur, "visual": {"type": "video", "src": str(tmp), "start": 0.0, "crop": crop},
                          "overlays": overlays}],
              "audio": {"tracks": [{"src": str(tmp), "start": 0, "volume": 1.0}]},
              "look": {"progress_bar": highlight}}
        if jp.get("captions", True):
            tl["captions"] = {"style": jp.get("caption_style") or "pop", "words": _clip_words(words, c["start"], c["end"]),
                              "highlight": highlight}
        name = f"clip_{c['id']}_{quality}_{stamp}"
        info = engine.render(tl, outdir / f"{name}.mp4", ctx.sub(lo + (hi - lo) * 0.25, hi))
        engine.thumbnail(outdir / f"{name}.mp4", outdir / f"{name}.jpg", min(1.0, dur / 2))
        caption = "\n".join([c.get("title", ""), "", c.get("hook", ""), "",
                             "— Posting checklist —",
                             "• Only post clips of your own videos or ones you have permission to use.",
                             "• Add your own take in the caption; raw re-uploads are not monetisable."])
        fresh = db.get_project(pid)["data"]
        for x in fresh["clips"]:
            if x["id"] == c["id"]:
                x.setdefault("outputs", []).insert(0, {
                    "file": f"out/{name}.mp4", "thumb": f"out/{name}.jpg", "quality": quality, "created": stamp,
                    "duration": round(info["duration"], 1), "width": info["width"], "height": info["height"],
                    "caption": caption})
        db.patch_data(pid, clips=fresh["clips"])
    if want_best_of:
        ctx.progress(span, "Building the best-of cut")
        bw, bh = (1920, 1080) if quality == "final" else (960, 540)
        scenes, tracks, cap_words, t = [], [], [], 0.0
        ordered = sorted(todo, key=lambda c: c["start"])
        for n, c in enumerate(ordered, 1):
            scenes.append({"duration": 1.8, "visual": {"type": "color", "colors": ["#0c0c12", "#221a33"]},
                           "transition": "fade" if n > 1 else "cut",
                           "overlays": [{"type": "title", "text": c.get("title") or f"Moment {n}", "y": 0.46,
                                         "sub": f"Moment {n} of {len(ordered)}", "size": 0.09}]})
            t += 1.8
            dur = c["end"] - c["start"]
            scenes.append({"duration": dur, "transition": "fade",
                           "visual": {"type": "video", "src": str(cuts[c["id"]]), "start": 0.0, "crop": {"mode": "center"}}})
            tracks.append({"src": str(cuts[c["id"]]), "start": t, "volume": 1.0})
            for w in _clip_words(words, c["start"], c["end"]):
                cap_words.append({"w": w["w"], "t0": w["t0"] + t, "t1": w["t1"] + t})
            t += dur
        tl = {"width": bw, "height": bh, "fps": 30, "scenes": scenes, "audio": {"tracks": tracks}, "look": {}}
        if jp.get("captions", True):
            tl["captions"] = {"style": "karaoke", "words": cap_words, "highlight": highlight}
        name = f"bestof_{quality}_{stamp}"
        info = engine.render(tl, outdir / f"{name}.mp4", ctx.sub(span, 0.99))
        engine.thumbnail(outdir / f"{name}.mp4", outdir / f"{name}.jpg", 0.9)
        fresh = db.get_project(pid)["data"]
        fresh.setdefault("best_of", []).insert(0, {
            "file": f"out/{name}.mp4", "thumb": f"out/{name}.jpg", "quality": quality, "created": stamp,
            "duration": round(info["duration"], 1), "width": info["width"], "height": info["height"],
            "caption": "Best moments: " + " · ".join(c.get("title", "") for c in ordered)})
        db.patch_data(pid, best_of=fresh["best_of"])
    for f in tmpdir.glob("*.mp4"):
        try:
            f.unlink()
        except OSError:
            pass
