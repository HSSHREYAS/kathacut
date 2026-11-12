"""Story Studio: PDF / text / idea -> scripts -> narrated, illustrated, captioned videos."""
import hashlib
import json
import math
import re
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from .. import config, db, worker
from ..providers import images, llm, tts
from ..render import engine
from . import pdfparse

ART_STYLES = {
    "shadow_puppet": {
        "label": "Shadow puppet (Tholu Bommalata)",
        "suffix": "in the style of Indian leather shadow puppet theatre (Tholu Bommalata), backlit translucent "
                  "painted leather figures with ornate perforated patterns, flat side-profile poses, warm amber "
                  "lamp glow on a dark cloth screen, rich reds and golds",
        "grade": "warm", "vignette": 0.5, "grain": 0.04, "highlight": "#FFC247"},
    "dark_ink": {
        "label": "Dark ink horror",
        "suffix": "dark graphic-novel ink illustration, heavy black shadows, muted teal and crimson palette, "
                  "eerie fog, dramatic rim light, unsettling atmosphere, highly detailed",
        "grade": "cold", "vignette": 0.6, "grain": 0.07, "highlight": "#FF4B4B"},
    "storybook": {
        "label": "Painted storybook",
        "suffix": "painterly storybook illustration, visible gouache brushwork, soft golden light, "
                  "rich detail, cinematic composition",
        "grade": "none", "vignette": 0.3, "grain": 0.03, "highlight": "#FFE14A"},
    "moody_anime": {
        "label": "Moody anime film",
        "suffix": "cinematic anime film still, moody volumetric lighting, detailed painted background, "
                  "muted colours, atmospheric depth",
        "grade": "none", "vignette": 0.35, "grain": 0.03, "highlight": "#7CF5FF"},
    "paper_cut": {
        "label": "Paper-cut diorama",
        "suffix": "layered paper-cut diorama, handcrafted paper textures, soft drop shadows between layers, "
                  "miniature stage lighting, depth of field",
        "grade": "none", "vignette": 0.35, "grain": 0.02, "highlight": "#FFE14A"},
    "vintage_engraving": {
        "label": "Vintage engraving",
        "suffix": "antique copperplate engraving illustration, fine cross-hatching, aged sepia paper, "
                  "19th-century book plate",
        "grade": "sepia", "vignette": 0.45, "grain": 0.06, "highlight": "#F2C98A"},
    "archive_photo": {
        "label": "Archive documentary",
        "suffix": "documentary illustration in muted desaturated tones, film grain, dramatic chiaroscuro, "
                  "period-accurate details",
        "grade": "noir", "vignette": 0.5, "grain": 0.08, "highlight": "#FFE14A"},
}
GENRES = {
    "horror": "Slow-building dread. Concrete sensory details, short sentences, one rule that must not be broken. "
              "Never explain the monster. End unresolved or with a chilling final image.",
    "mythology": "Reverent, epic and intimate at once. Tell the lesser-known angle of the story. Name the "
                 "tradition or text it comes from. End on the emotional truth, then ask viewers if they knew it.",
    "mystery": "A puzzle told in present tense. Plant clues, raise one sharp question, withhold the answer "
               "until the last line or the next part.",
    "documentary": "True history told like a thriller. Precise names, dates and places from the source. "
                   "Lead with the most surprising fact, then explain how it happened.",
    "facts": "Rapid, surprising, true facts. Each beat should make the viewer think 'wait, what?'. "
             "No filler, no generic statements.",
    "folk_tale": "A regional folk tale told by a village elder. Name the place. Simple words, strong images, "
                 "a warning or moral that lands in the last line.",
}
WPS = 2.55  # narration words per second


# ----------------------------------------------------------------------------- source handling
def _load_source(p: dict, pdir: Path, ctx) -> dict:
    params = p["params"]
    if params.get("source") == "pdf":
        return pdfparse.parse(pdir / "source.pdf", pdir, ctx.sub(0.02, 0.12))
    txt = (params.get("text") or "").strip()
    chunks = [txt[i:i + 3000] for i in range(0, len(txt), 3000)] or [""]
    return {"pages": [{"n": i + 1, "text": c} for i, c in enumerate(chunks)], "figures": []}


def _marked(pages: list[dict]) -> str:
    return "\n".join(f"[p.{pg['n']}] {pg['text']}" for pg in pages if pg["text"].strip())


def _digest(pages: list[dict], budget: int, ctx) -> str:
    """Map step for documents larger than one prompt: pull page-cited facts from each chunk."""
    chunk_budget = int(budget * 0.8)
    chunks, cur, size = [], [], 0
    for pg in pages:
        t = f"[p.{pg['n']}] {pg['text']}\n"
        if cur and size + len(t) > chunk_budget:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(t[:chunk_budget])
        size += len(t)
    if cur:
        chunks.append("".join(cur))
    if len(chunks) > 40:
        step = len(chunks) / 40
        chunks = [chunks[int(i * step)] for i in range(40)]
    facts = []
    for i, ch in enumerate(chunks):
        ctx.progress(0.12 + 0.28 * i / len(chunks), f"Digesting source {i + 1}/{len(chunks)}")
        try:
            r = llm.complete_json(
                "Extract the most interesting, concrete, story-worthy facts from this text. Keep names, dates, "
                "numbers and places exact. Each page starts with a [p.N] marker.\n"
                'Return JSON: {"facts":[{"page":N,"fact":"one or two sentences","drama":1-5}]} with at most 14 facts.\n\n'
                "TEXT:\n" + ch, tier="bulk", max_tokens=3000, temperature=0.2)
            for f in r.get("facts", []):
                if f.get("fact"):
                    facts.append({"page": int(f.get("page") or 0), "fact": str(f["fact"]), "drama": int(f.get("drama") or 3)})
        except llm.LLMError:
            raise
        except Exception:
            continue
    facts.sort(key=lambda f: -f["drama"])
    out, size = [], 0
    for f in facts:
        line = f"[p.{f['page']}] {f['fact']}"
        if size + len(line) > budget:
            break
        out.append((f["page"], line))
        size += len(line) + 1
    out.sort()
    return "\n".join(line for _, line in out)


# ----------------------------------------------------------------------------- prompts
def _plan_prompt(params: dict, source_text: str, has_doc: bool) -> str:
    n = int(params.get("n_shorts", 3))
    want_long = bool(params.get("long"))
    series = bool(params.get("series"))
    genre = params.get("genre", "documentary")
    parts = []
    if has_doc:
        parts.append("You are planning videos from the SOURCE below. Use only what the source says. "
                     "Each page begins with a [p.N] marker.")
    else:
        parts.append("You are planning original story videos from the IDEA below.")
    if series:
        parts.append(f"Plan ONE continuous story split into {n} short parts (a series). Each part must end on a "
                     "cliffhanger; the last part resolves it.")
    else:
        parts.append(f"Pick the {n} strongest, clearly different stories or moments for short vertical videos.")
    if want_long:
        parts.append("Also plan ONE long-form video that tells the bigger story.")
    parts.append(f"Genre and tone: {genre}. {GENRES.get(genre, '')}")
    if params.get("notes"):
        parts.append("Creator's notes: " + str(params["notes"])[:600])
    parts.append(
        'Return JSON:\n{"bible":"3-5 sentences describing the recurring people and places and exactly how they '
        'look (age, clothing, colours, era) so an illustrator can draw them consistently",\n'
        '"videos":[{"format":"short" or "long","title":"max 8 words, curiosity-driven","angle":"2 sentences: what '
        'this video covers and why a viewer cannot scroll past","pages":[page numbers used, empty if none]}]}\n'
        f"Exactly {n} short videos" + (" followed by exactly 1 long video." if want_long else "."))
    parts.append(("SOURCE:\n" if has_doc else "IDEA:\n") + source_text)
    return "\n\n".join(parts)


_SCRIPT_SYSTEM = (
    "You are the head writer of a storytelling channel with millions of followers. You write narration that is "
    "spoken aloud: short sentences, plain words, strong verbs, no cliches, no filler, no emojis, no stage "
    "directions. You always answer with valid JSON only.")


def _script_prompt(params: dict, v: dict, bible: str, source_text: str, figures: list[dict], has_doc: bool,
                   part: int | None, n_parts: int) -> str:
    genre = params.get("genre", "documentary")
    lang = params.get("language", "English")
    if v["format"] == "short":
        secs = int(params.get("short_seconds", 45))
        words = int(secs * WPS)
        shape = (f"A vertical short of about {secs} seconds: {words} words of narration in total (stay within "
                 f"10%), split into {max(5, secs // 6)}-{max(7, secs // 4)} scenes of 1-2 sentences each.")
    else:
        mins = float(params.get("long_minutes", 4))
        words = int(mins * 60 * WPS)
        shape = (f"A long-form video of about {mins:g} minutes: {words} words of narration in total, split into "
                 f"{int(mins * 7)}-{int(mins * 10)} scenes of 1-3 sentences each. Give it a clear three-act shape.")
    rules = [
        f"Write the script for this video.\nTITLE: {v['title']}\nANGLE: {v.get('angle', '')}",
        shape,
        f"Genre and tone: {genre}. {GENRES.get(genre, '')}",
        "Rules:\n"
        "- Scene 1 is the HOOK: one sentence of at most 12 words that opens a question, a contradiction or a threat. "
        "Never start with 'Did you know', 'In this video' or a greeting.\n"
        "- Every scene must move the story forward; vary sentence length; put the most important word last.\n"
        "- Build to a payoff in the second-to-last scene.\n"
        f"- Write the narration in {lang}. Spell out anything that would be read awkwardly aloud.",
    ]
    if part and n_parts > 1 and part < n_parts:
        rules.append(f"- This is part {part} of {n_parts}. The final scene must be a cliffhanger that ends by asking "
                     "viewers to choose between two short options in the comments (for example 'Comment OPEN or RUN').")
    elif part and n_parts > 1:
        rules.append(f"- This is the final part ({part} of {n_parts}). Resolve the story with a strong last image.")
    else:
        rules.append("- The final scene lands the ending and then asks one specific question viewers will want to "
                     "answer in the comments.")
    if has_doc:
        rules.append("- Use ONLY facts found in the SOURCE. For each scene set \"page\" to the page number that "
                     "supports it. Do not invent names, dates, numbers or quotes.")
    vis = ("For each scene describe ONE image for an illustrator in \"prompt\": subject, action, setting, lighting, "
           "camera framing. 25-45 words, in English. Each prompt must stand alone, so repeat how recurring "
           "characters look every time. Never ask for text, letters, captions or logos in the image.")
    if figures:
        vis += (" If one of the AVAILABLE FIGURES clearly shows what the scene talks about, set \"figure\" to its "
                "id instead (still write a prompt as backup).")
    rules.append(vis)
    rules.append(
        'Return JSON:\n{"title":"final title, max 8 words","hook":"the scene 1 sentence",'
        '"description":"2-3 sentence caption for the post, ending with a question","hashtags":["5-8 tags without #"],'
        '"cta":"max 5 words shown on screen at the end, e.g. Comment STAY or GO",'
        '"scenes":[{"narration":"...","prompt":"...","figure":null,"on_screen":"optional place/date label, max 4 words, or empty",'
        '"page":null}]}')
    if bible:
        rules.append("CHARACTER AND WORLD BIBLE (keep visuals consistent with it):\n" + bible)
    if figures:
        rules.append("AVAILABLE FIGURES:\n" + "\n".join(f"{f['id']} (page {f['page']}, {f['w']}x{f['h']})" for f in figures[:40]))
    if source_text:
        rules.append(("SOURCE:\n" if has_doc else "IDEA:\n") + source_text)
    return "\n\n".join(rules)


# ----------------------------------------------------------------------------- script post-processing
def _key_tokens(s: str) -> list[str]:
    toks = set(re.findall(r"\b\d{2,}\b", s))
    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-zA-Z]{3,}\b", s):
        toks.add(m.group(0).lower())
    return list(toks)


def _grounded(narration: str, page, pages_by_n: dict):
    if not page:
        return None
    toks = _key_tokens(narration)
    if not toks:
        return None
    hay = " ".join(pages_by_n.get(n, "") for n in (page - 1, page, page + 1)).lower()
    hits = sum(1 for t in toks if t in hay)
    return hits / len(toks) >= 0.6


def _normalise(raw: dict, v: dict, figures_by_id: dict, pages_by_n: dict) -> dict:
    scenes = []
    for s in raw.get("scenes") or []:
        nar = " ".join(str(s.get("narration") or "").split())
        if not nar:
            continue
        fig = s.get("figure") if s.get("figure") in figures_by_id else None
        page = s.get("page")
        try:
            page = int(page) if page else None
        except (TypeError, ValueError):
            page = None
        scenes.append({"id": db.new_id()[:8], "narration": nar, "prompt": str(s.get("prompt") or "").strip(),
                       "figure": fig, "on_screen": str(s.get("on_screen") or "").strip()[:40], "page": page,
                       "grounded": _grounded(nar, page, pages_by_n)})
    if not scenes:
        raise llm.LLMError("The model returned a script with no scenes")
    tags = [re.sub(r"[^\w]", "", str(t)) for t in (raw.get("hashtags") or [])]
    return {**v, "title": str(raw.get("title") or v["title"]).strip()[:90],
            "hook": str(raw.get("hook") or scenes[0]["narration"]).strip(),
            "description": str(raw.get("description") or "").strip(),
            "hashtags": [t for t in tags if t][:10], "cta": str(raw.get("cta") or "").strip()[:40],
            "scenes": scenes, "outputs": v.get("outputs", [])}


def _write_script(params, v, bible, src, pages, has_doc, budget, part, n_parts) -> dict:
    figures = src["figures"]
    pages_by_n = {pg["n"]: pg["text"] for pg in pages}
    if has_doc:
        want = sorted({int(n) for n in (v.get("pages") or []) if str(n).isdigit() or isinstance(n, int)})
        near = sorted({m for n in want for m in (n - 1, n, n + 1) if m in pages_by_n}) or list(pages_by_n)
        text = _marked([{"n": n, "text": pages_by_n[n]} for n in near])[:max(2000, budget - 5000)]
        figs = [f for f in figures if f["page"] in near] or figures[:20]
    else:
        text = _marked(pages)[:max(2000, budget - 5000)] if pages and pages[0]["text"] else ""
        text = re.sub(r"\[p\.\d+\] ", "", text)
        figs = []
    raw = llm.complete_json(_script_prompt(params, v, bible, text, figs, has_doc, part, n_parts),
                            system=_SCRIPT_SYSTEM, tier="smart",
                            max_tokens=12000 if v["format"] == "long" else 5000, temperature=0.85)
    return _normalise(raw, v, {f["id"]: f for f in figures}, pages_by_n if has_doc else {})


@worker.handler("story_plan")
def plan(job, ctx):
    p = db.get_project(job["project_id"])
    pid, params = p["id"], p["params"]
    pdir = config.PROJECTS / pid
    db.update_project(pid, status="planning")
    ctx.progress(0.02, "Reading source")
    src = _load_source(p, pdir, ctx)
    pages = src["pages"]
    has_doc = params.get("source") in ("pdf", "text")
    budget = llm.budget_chars()
    full = _marked(pages)
    if not has_doc:
        source_text = (params.get("text") or "").strip()
    elif len(full) <= budget - 4000:
        source_text = full
    else:
        source_text = _digest(pages, budget - 4000, ctx)
    ctx.progress(0.42, "Planning videos")
    raw = llm.complete_json(_plan_prompt(params, source_text, has_doc), system=_SCRIPT_SYSTEM, tier="smart",
                            max_tokens=4000, temperature=0.8)
    bible = str(raw.get("bible") or "").strip()
    plans = [v for v in raw.get("videos") or [] if v.get("title")]
    if not plans:
        raise llm.LLMError("The model returned no video plan")
    n_shorts = int(params.get("n_shorts", 3))
    shorts = [v for v in plans if v.get("format") != "long"][:n_shorts]
    longs = [v for v in plans if v.get("format") == "long"][:1] if params.get("long") else []
    series = bool(params.get("series")) and len(shorts) > 1
    videos, todo = [], shorts + longs
    for i, pv in enumerate(todo):
        ctx.progress(0.45 + 0.5 * i / len(todo), f"Writing script {i + 1}/{len(todo)}: {pv['title'][:40]}")
        fmt = "long" if pv in longs else "short"
        v = {"id": db.new_id()[:8], "format": fmt, "title": str(pv["title"]), "angle": str(pv.get("angle") or ""),
             "pages": pv.get("pages") or [], "part": (i + 1) if (series and fmt == "short") else None,
             "parts": len(shorts) if series else None}
        try:
            videos.append(_write_script(params, v, bible, src, pages, has_doc, budget,
                                        v["part"], len(shorts) if series else 1))
        except llm.LLMError as e:
            if not videos and i == len(todo) - 1:
                raise
            videos.append({**v, "error": str(e)[:300], "scenes": [], "outputs": []})
        db.patch_data(pid, bible=bible, videos=videos, figures=src["figures"], source_pages=len(pages))
    db.update_project(pid, status="ready")


# ----------------------------------------------------------------------------- rendering
def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in re.split(r"(?<=[.!?…।])\s+", text) if s.strip()]
    return parts or [text]


def _narrate(v: dict, voice: str, speed: float, pdir: Path, progress) -> tuple:
    """TTS every sentence (cached), stitch one narration track, return per-scene durations + word timings."""
    sr = 24000
    ttsdir = pdir / "tts"
    ttsdir.mkdir(exist_ok=True)
    audio = [np.zeros(int(0.15 * sr), np.float32)]
    t = 0.15
    durations, words = [], []
    n = len(v["scenes"])
    for si, sc in enumerate(v["scenes"]):
        start = t
        for sent in _sentences(sc["narration"]):
            key = hashlib.sha1(f"{sent}|{voice}|{speed}".encode()).hexdigest()[:16]
            wav, meta = ttsdir / f"{key}.wav", ttsdir / f"{key}.json"
            if wav.exists() and meta.exists():
                info = json.loads(meta.read_text("utf-8"))
            else:
                info = tts.synth(sent, voice, wav, speed)
                meta.write_text(json.dumps(info), "utf-8")
            samples, fsr = sf.read(str(wav), dtype="float32")
            if samples.ndim > 1:
                samples = samples.mean(axis=1)
            if fsr != sr:
                idx = np.linspace(0, len(samples) - 1, int(len(samples) * sr / fsr))
                samples = np.interp(idx, np.arange(len(samples)), samples).astype(np.float32)
            for w in info["words"]:
                words.append({"w": w["w"], "t0": round(t + w["t0"], 3), "t1": round(t + w["t1"], 3)})
            gap = np.zeros(int(0.16 * sr), np.float32)
            audio += [samples, gap]
            t += (len(samples) + len(gap)) / sr
        pause = np.zeros(int(0.22 * sr), np.float32)
        audio.append(pause)
        t += len(pause) / sr
        durations.append(t - start)
        progress((si + 1) / n, f"Narrating scene {si + 1}/{n}")
    durations[0] += 0.15
    tail = np.zeros(int(0.9 * sr), np.float32)
    audio.append(tail)
    durations[-1] += 0.9
    out = pdir / f"narration_{v['id']}.wav"
    sf.write(str(out), np.concatenate(audio), sr)
    return out, durations, words


def _scene_image(sc: dict, style: dict, aspect: str, pdir: Path, figures_by_id: dict, seed: int) -> tuple:
    """Returns (path, provider). Figure -> user image -> AI -> stock -> placeholder."""
    imgdir = pdir / "img"
    imgdir.mkdir(exist_ok=True)
    if sc.get("image"):                      # user-supplied replacement
        up = pdir / sc["image"]
        if up.exists():
            return up, "upload"
    if sc.get("figure") and sc["figure"] in figures_by_id:
        fp = pdir / figures_by_id[sc["figure"]]["file"]
        if fp.exists():
            return fp, "figure"
    prompt = (sc.get("prompt") or sc["narration"]).strip().rstrip(".")
    full = f"{prompt}, {style['suffix']}, no text, no watermark"
    key = hashlib.sha1(f"{full}|{aspect}|{seed}".encode()).hexdigest()[:16]
    dst = imgdir / f"{key}.jpg"
    if dst.exists():
        return dst, "cache"
    try:
        return dst, images.generate(full, dst, aspect, seed, stock_query=" ".join(prompt.split()[:6]))
    except Exception:
        pass
    label = sc.get("on_screen") or " ".join(sc["narration"].split()[:5])
    ph = imgdir / f"{key}_placeholder.jpg"     # separate name, so the real image is retried next render
    images.placeholder(label, ph, aspect)
    return ph, "placeholder"


def build_timeline(v: dict, params: dict, pdir: Path, quality: str, figures: list[dict], progress) -> dict:
    style = ART_STYLES.get(params.get("style"), ART_STYLES["storybook"])
    vertical = v["format"] == "short"
    aspect = "9:16" if vertical else "16:9"
    full = (1080, 1920) if vertical else (1920, 1080)
    W, H = full if quality == "final" else (full[0] // 2, full[1] // 2)
    voice = params.get("voice") or "kokoro:am_michael"
    speed = float(params.get("speed") or 1.0)
    narration, durations, words = _narrate(v, voice, speed, pdir, lambda p, m=None: progress(0.35 * p, m))
    figs = {f["id"]: f for f in figures}
    scenes, providers = [], {}
    n = len(v["scenes"])
    shot = 0
    for i, sc in enumerate(v["scenes"]):
        slow = not images.available()["cloudflare"]
        progress(0.35 + 0.65 * i / n, f"Creating image {i + 1}/{n}" +
                 (" (keyless service: 1 image per 45 s — add Cloudflare keys to speed up)" if slow else ""))
        path, prov = _scene_image(sc, style, aspect, pdir, figs, seed=int(params.get("seed") or 7))
        providers[prov] = providers.get(prov, 0) + 1
        is_fig = prov == "figure"
        dur = durations[i]
        k = max(1, math.ceil(dur / 4.6))
        for j in range(k):
            sub = {"duration": dur / k,
                   "visual": {"type": "image", "src": str(path), "motion": engine.MOTION_CYCLE[shot % len(engine.MOTION_CYCLE)],
                              "fit": "auto", "grade": style["grade"] if not is_fig else None},
                   "transition": "cut" if (j > 0 or i == 0) else "fade", "overlays": []}
            shot += 1
            if j == 0:
                if sc.get("on_screen"):
                    sub["overlays"].append({"type": "pill", "text": sc["on_screen"], "y": 0.12 if vertical else 0.1,
                                            "t1": min(dur / k, 3.0), "size": 0.036})
                if params.get("show_sources") and sc.get("page"):
                    sub["overlays"].append({"type": "pill", "text": f"Source: p.{sc['page']}", "y": 0.955,
                                            "x": "left", "size": 0.024, "bg_alpha": 130})
            scenes.append(sub)
    if v.get("part") and v.get("parts"):
        scenes[0]["overlays"].append({"type": "pill", "text": f"Part {v['part']} of {v['parts']}", "y": 0.07,
                                      "t1": 3.5, "bg": "#000000", "color": style["highlight"]})
    if v.get("cta"):
        last = scenes[-1]
        last["overlays"].append({"type": "cta", "text": v["cta"], "bg": style["highlight"],
                                 "t0": max(0.0, last["duration"] - 2.6), "y": 0.84 if vertical else 0.9})
    tl = {"width": W, "height": H, "fps": 30, "scenes": scenes,
          "audio": {"tracks": [{"src": str(narration), "start": 0, "volume": 1.0}]},
          "look": {"vignette": style["vignette"], "grain": style["grain"],
                   "progress_bar": style["highlight"] if vertical else None},
          "meta": {"image_providers": providers}}
    if params.get("captions", True):
        tl["captions"] = {"style": params.get("caption_style") or ("pop" if vertical else "karaoke"),
                          "words": words, "highlight": style["highlight"]}
    music = params.get("music")
    if music and (config.MUSIC / music).exists():
        tl["audio"]["music"] = {"src": str(config.MUSIC / music), "volume": float(params.get("music_volume") or 0.12)}
    return tl


def post_text(v: dict, params: dict, providers: dict) -> str:
    lines = [v["title"], "", v.get("description", "")]
    if v.get("part") and v.get("parts"):
        lines += ["", f"Part {v['part']} of {v['parts']}"]
    pages = sorted({s["page"] for s in v["scenes"] if s.get("page")})
    if pages:
        lines += ["", "Source pages: " + ", ".join(f"p.{n}" for n in pages)]
    if v.get("hashtags"):
        lines += ["", " ".join("#" + t for t in v["hashtags"])]
    ai = [k for k in providers if k in ("cloudflare", "pollinations", "cache")]
    lines += ["", "— Posting checklist —",
              "• AI narration" + (" and AI-generated illustrations" if ai else "") +
              ": switch on the platform's AI / altered-content label when you upload.",
              "• Watch it once before posting and fix anything the script got wrong."]
    return "\n".join(lines)


@worker.handler("story_render")
def render(job, ctx):
    p = db.get_project(job["project_id"])
    pid, params, data = p["id"], p["params"], p["data"]
    pdir = config.PROJECTS / pid
    jp = job["params"]
    quality = jp.get("quality", "draft")
    v = next((x for x in data.get("videos", []) if x["id"] == jp["video_id"]), None)
    if not v or not v.get("scenes"):
        raise RuntimeError("That video has no script to render")
    tl = build_timeline(v, params, pdir, quality, data.get("figures", []), ctx.sub(0.0, 0.45))
    outdir = pdir / "out"
    outdir.mkdir(exist_ok=True)
    stamp = int(time.time())
    name = f"{v['id']}_{quality}_{stamp}"
    (outdir / f"{name}.timeline.json").write_text(json.dumps(tl, indent=1), "utf-8")
    info = engine.render(tl, outdir / f"{name}.mp4", ctx.sub(0.45, 0.99))
    engine.thumbnail(outdir / f"{name}.mp4", outdir / f"{name}.jpg", min(1.5, info["duration"] / 2))
    caption = post_text(v, params, tl["meta"]["image_providers"])
    (outdir / f"{name}.txt").write_text(caption, "utf-8")
    fresh = db.get_project(pid)["data"]
    for x in fresh.get("videos", []):
        if x["id"] == v["id"]:
            x.setdefault("outputs", []).insert(0, {
                "file": f"out/{name}.mp4", "thumb": f"out/{name}.jpg", "quality": quality, "created": stamp,
                "duration": round(info["duration"], 1), "width": info["width"], "height": info["height"],
                "caption": caption, "images": tl["meta"]["image_providers"]})
    db.patch_data(pid, videos=fresh["videos"])
