"""Text-to-speech: Kokoro (local, Apache-2.0) and Edge-TTS (Indian languages), with word timings."""
import asyncio
import re
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from .. import config, db, ffutil

_KO = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
VOICES = [
    {"id": "kokoro:am_michael", "label": "Michael — deep US narrator (Kokoro)", "lang": "en"},
    {"id": "kokoro:am_fenrir", "label": "Fenrir — dark, dramatic (Kokoro)", "lang": "en"},
    {"id": "kokoro:am_onyx", "label": "Onyx — low and calm (Kokoro)", "lang": "en"},
    {"id": "kokoro:bm_george", "label": "George — British documentary (Kokoro)", "lang": "en"},
    {"id": "kokoro:bm_fable", "label": "Fable — British storyteller (Kokoro)", "lang": "en"},
    {"id": "kokoro:af_heart", "label": "Heart — warm US female (Kokoro)", "lang": "en"},
    {"id": "kokoro:af_bella", "label": "Bella — expressive US female (Kokoro)", "lang": "en"},
    {"id": "kokoro:bf_emma", "label": "Emma — British female (Kokoro)", "lang": "en"},
    {"id": "kokoro:hf_alpha", "label": "Alpha — Hindi female (Kokoro)", "lang": "hi"},
    {"id": "kokoro:hm_omega", "label": "Omega — Hindi male (Kokoro)", "lang": "hi"},
    {"id": "edge:en-IN-PrabhatNeural", "label": "Prabhat — Indian English male (Edge)", "lang": "en"},
    {"id": "edge:en-IN-NeerjaNeural", "label": "Neerja — Indian English female (Edge)", "lang": "en"},
    {"id": "edge:en-US-ChristopherNeural", "label": "Christopher — US narrator (Edge)", "lang": "en"},
    {"id": "edge:en-GB-RyanNeural", "label": "Ryan — British male (Edge)", "lang": "en"},
    {"id": "edge:hi-IN-MadhurNeural", "label": "Madhur — Hindi male (Edge)", "lang": "hi"},
    {"id": "edge:hi-IN-SwaraNeural", "label": "Swara — Hindi female (Edge)", "lang": "hi"},
    {"id": "edge:kn-IN-GaganNeural", "label": "Gagan — Kannada male (Edge)", "lang": "kn"},
    {"id": "edge:kn-IN-SapnaNeural", "label": "Sapna — Kannada female (Edge)", "lang": "kn"},
    {"id": "edge:te-IN-MohanNeural", "label": "Mohan — Telugu male (Edge)", "lang": "te"},
    {"id": "edge:ta-IN-ValluvarNeural", "label": "Valluvar — Tamil male (Edge)", "lang": "ta"},
]
_KLANG = {"a": "en-us", "b": "en-gb", "h": "hi", "e": "es", "f": "fr-fr", "i": "it", "j": "ja", "p": "pt-br", "z": "cmn"}
_kokoro = None
_klock = threading.Lock()


def _get_kokoro():
    global _kokoro
    with _klock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            model = ffutil.download(_KO + "kokoro-v1.0.onnx", config.MODELS / "kokoro-v1.0.onnx", timeout=600)
            voices = ffutil.download(_KO + "voices-v1.0.bin", config.MODELS / "voices-v1.0.bin", timeout=600)
            _kokoro = Kokoro(str(model), str(voices))
        return _kokoro


def estimate_words(text: str, samples: np.ndarray, sr: int) -> list[dict]:
    """Spread words across the voiced span, weighted by length, with pauses at punctuation."""
    toks = text.split()
    if not toks:
        return []
    dur = len(samples) / sr
    env = np.abs(samples)
    voiced = np.where(env > max(1e-4, env.max() * 0.03))[0]
    t0 = voiced[0] / sr if len(voiced) else 0.0
    t1 = voiced[-1] / sr if len(voiced) else dur
    spans = []
    for w in toks:
        core = len(re.sub(r"[^\w]", "", w, flags=re.UNICODE)) or 1
        pause = 2.2 if w[-1:] in ",;:—-" else (4.0 if w[-1:] in ".?!…" else 0.0)
        spans.append((core + 1.3, pause))
    total = sum(a + b for a, b in spans) - spans[-1][1]
    unit = (t1 - t0) / max(total, 1e-6)
    out, t = [], t0
    for w, (a, b) in zip(toks, spans):
        out.append({"w": w, "t0": round(float(t), 3), "t1": round(float(t + a * unit), 3)})
        t += (a + b) * unit
    return out


def _kokoro_synth(text: str, voice: str, speed: float, out_wav: Path) -> dict:
    k = _get_kokoro()
    lang = _KLANG.get(voice[:1], "en-us")
    with _klock:
        samples, sr = k.create(text, voice=voice, speed=speed, lang=lang)
    samples = np.asarray(samples, dtype=np.float32)
    sf.write(str(out_wav), samples, sr)
    db.log_usage("kokoro", "chars", len(text))
    return {"duration": len(samples) / sr, "words": estimate_words(text, samples, sr)}


async def _edge_stream(text, voice, rate, mp3):
    import edge_tts
    try:
        com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    except TypeError:
        com = edge_tts.Communicate(text, voice, rate=rate)
    words = []
    with open(mp3, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append({"w": ch["text"], "t0": ch["offset"] / 1e7,
                              "t1": (ch["offset"] + ch["duration"]) / 1e7})
    return words


def _edge_synth(text: str, voice: str, speed: float, out_wav: Path) -> dict:
    mp3 = out_wav.with_suffix(".mp3")
    rate = f"{int(round((speed - 1) * 100)):+d}%"
    words = asyncio.run(_edge_stream(text, voice, rate, mp3))
    ffutil.run(["-i", mp3, "-ac", "1", "-ar", "24000", out_wav])
    mp3.unlink(missing_ok=True)
    samples, sr = sf.read(str(out_wav), dtype="float32")
    toks = text.split()
    if words and len(words) == len(toks):   # restore punctuation for caption paging
        for w, tk in zip(words, toks):
            w["w"] = tk
    elif not words:
        words = estimate_words(text, samples, sr)
    db.log_usage("edge-tts", "chars", len(text))
    return {"duration": len(samples) / sr, "words": words}


def synth(text: str, voice: str, out_wav, speed: float = 1.0) -> dict:
    """Synthesise one passage. Returns {"duration", "words":[{w,t0,t1}]}."""
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    engine, _, name = voice.partition(":")
    primary = (_kokoro_synth, name) if engine == "kokoro" else (_edge_synth, name)
    backup = (_edge_synth, "en-US-ChristopherNeural") if engine == "kokoro" else (_kokoro_synth, "am_michael")
    try:
        return primary[0](text, primary[1], speed, out_wav)
    except Exception as e:  # noqa: BLE001
        first = e
    try:
        return backup[0](text, backup[1], speed, out_wav)
    except Exception as e2:  # noqa: BLE001
        raise RuntimeError(f"TTS failed ({voice}): {first} / fallback: {e2}") from e2
