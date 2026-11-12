"""Speech-to-text with word timestamps: Groq Whisper (free tier) -> local faster-whisper."""
import re
import threading
from pathlib import Path

import httpx

from .. import config, db, ffutil

_local = {}
_lock = threading.Lock()
CHUNK = 600.0  # seconds per Groq upload (64 kbps mono mp3 ~ 4.8 MB, limit is 25 MB)


def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE).lower()


def _punctuate(words: list[dict], segments: list[dict]) -> None:
    """Groq's word list has no punctuation; borrow it from the segment text."""
    toks = [t for s in segments for t in s["text"].split()]
    j = 0
    for w in words:
        n = _norm(w["w"])
        for k in range(j, min(j + 4, len(toks))):
            if _norm(toks[k]) == n:
                w["w"] = toks[k]
                j = k + 1
                break


def _groq(audio_src, workdir: Path, duration: float, language, progress) -> dict:
    key = config.get("GROQ_API_KEY")
    words, segments, lang = [], [], language
    n = max(1, int(-(-duration // CHUNK)))
    for i in range(n):
        off = i * CHUNK
        mp3 = workdir / f"stt_{i}.mp3"
        ffutil.extract_audio(audio_src, mp3, 16000, start=off, dur=min(CHUNK, duration - off))
        data = {"model": "whisper-large-v3-turbo", "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"], "temperature": "0"}
        if language:
            data["language"] = language
        with open(mp3, "rb") as f:
            r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                           headers={"Authorization": f"Bearer {key}"}, data=data,
                           files={"file": (mp3.name, f, "audio/mpeg")}, timeout=300)
        r.raise_for_status()
        j = r.json()
        lang = lang or j.get("language")
        segs = [{"t0": s["start"] + off, "t1": s["end"] + off, "text": s["text"].strip()}
                for s in j.get("segments") or []]
        ws = [{"w": w["word"].strip(), "t0": w["start"] + off, "t1": w["end"] + off}
              for w in j.get("words") or [] if w.get("word", "").strip()]
        _punctuate(ws, segs)
        words += ws
        segments += segs
        mp3.unlink(missing_ok=True)
        db.log_usage("groq-whisper", "audio_seconds", min(CHUNK, duration - off))
        if progress:
            progress((i + 1) / n, f"Transcribing with Groq {i + 1}/{n}")
    return {"language": lang, "words": words, "segments": segments, "provider": "groq"}


def _faster_whisper(audio_src, workdir: Path, duration: float, language, progress) -> dict:
    size = config.get("WHISPER_LOCAL_MODEL") or "small"
    with _lock:
        if size not in _local:
            from faster_whisper import WhisperModel
            if progress:
                progress(0.0, f"Loading local Whisper '{size}' (first run downloads it)")
            _local[size] = WhisperModel(size, device="cpu", compute_type="int8",
                                        download_root=str(config.MODELS / "whisper"))
        model = _local[size]
    wav = workdir / "stt.wav"
    ffutil.extract_audio(audio_src, wav, 16000)
    import soundfile as sf
    samples, _sr = sf.read(str(wav), dtype="float32")   # decoded by ffmpeg above; avoids the PyAV dependency
    segs_iter, info = model.transcribe(samples, word_timestamps=True, vad_filter=True, language=language)
    words, segments = [], []
    for s in segs_iter:
        segments.append({"t0": s.start, "t1": s.end, "text": s.text.strip()})
        for w in s.words or []:
            if w.word.strip():
                words.append({"w": w.word.strip(), "t0": float(w.start), "t1": float(w.end)})
        if progress and duration:
            progress(min(1.0, s.end / duration), f"Transcribing locally {int(100 * min(1.0, s.end / duration))}%")
    wav.unlink(missing_ok=True)
    db.log_usage("faster-whisper", "audio_seconds", duration)
    return {"language": info.language, "words": words, "segments": segments, "provider": "faster-whisper"}


def transcribe(media_path, workdir, language: str | None = None, progress=None) -> dict:
    workdir = Path(workdir)
    duration = ffutil.probe(media_path)["duration"]
    language = language or None
    errors = []
    if config.get("GROQ_API_KEY"):
        try:
            return _groq(media_path, workdir, duration, language, progress)
        except Exception as e:  # noqa: BLE001
            errors.append(f"groq: {str(e)[:200]}")
    try:
        return _faster_whisper(media_path, workdir, duration, language, progress)
    except Exception as e:  # noqa: BLE001
        errors.append(f"local: {str(e)[:200]}")
    raise RuntimeError("Transcription failed -> " + " | ".join(errors))
