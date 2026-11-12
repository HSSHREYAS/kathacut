"""Settings: .env first, then data/settings.json (edited from the UI) on top."""
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA = Path(os.getenv("KATHACUT_DATA", str(ROOT / "data"))).resolve()
PROJECTS = DATA / "projects"
MODELS = DATA / "models"
FONTS = DATA / "fonts"
CACHE = DATA / "cache"
MUSIC = DATA / "music"
for _d in (PROJECTS, MODELS, FONTS, CACHE, MUSIC):
    _d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA / "settings.json"
_lock = threading.Lock()

# name -> (label, where to get it, secret?)
FIELDS = {
    "GEMINI_API_KEY": ("Google Gemini API key", "https://aistudio.google.com/apikey", True),
    "GROQ_API_KEY": ("Groq API key (Whisper + fast LLM)", "https://console.groq.com/keys", True),
    "CLOUDFLARE_ACCOUNT_ID": ("Cloudflare account ID (FLUX images)", "https://dash.cloudflare.com/", False),
    "CLOUDFLARE_API_TOKEN": ("Cloudflare Workers AI token", "https://dash.cloudflare.com/profile/api-tokens", True),
    "PIXABAY_API_KEY": ("Pixabay API key (stock photos)", "https://pixabay.com/api/docs/", True),
    "OPENROUTER_API_KEY": ("OpenRouter key (optional fallback)", "https://openrouter.ai/keys", True),
    "GEMINI_MODEL": ("Gemini model override (blank = auto)", "", False),
    "GROQ_MODEL": ("Groq model override (blank = auto)", "", False),
    "OPENROUTER_MODEL": ("OpenRouter model override", "", False),
    "OLLAMA_URL": ("Ollama URL", "", False),
    "OLLAMA_MODEL": ("Ollama model (blank = first installed)", "", False),
    "IMAGE_PROVIDERS": ("Image provider order", "", False),
    "LLM_PROVIDERS": ("LLM provider order", "", False),
    "WHISPER_LOCAL_MODEL": ("Local Whisper size (tiny/base/small/medium)", "", False),
}
DEFAULTS = {
    "OLLAMA_URL": "http://localhost:11434",
    "IMAGE_PROVIDERS": "cloudflare,pollinations,pixabay",
    "LLM_PROVIDERS": "gemini,groq,openrouter,ollama",
    "WHISPER_LOCAL_MODEL": "small",
}


def _file() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def get(name: str, default: str = "") -> str:
    v = _file().get(name)
    if v:
        return str(v).strip()
    v = os.getenv(name)
    if v:
        return v.strip()
    return DEFAULTS.get(name, default)


def set_many(values: dict) -> None:
    with _lock:
        cur = _file()
        for k, v in values.items():
            if k not in FIELDS:
                continue
            if v is None or str(v).strip() == "":
                cur.pop(k, None)
            else:
                cur[k] = str(v).strip()
        SETTINGS_FILE.write_text(json.dumps(cur, indent=2), "utf-8")


def public_view() -> list[dict]:
    out = []
    for name, (label, url, secret) in FIELDS.items():
        v = get(name)
        shown = ("•" * 6 + v[-4:]) if (secret and v) else v
        out.append({"name": name, "label": label, "url": url, "secret": secret,
                    "set": bool(v), "value": shown})
    return out


APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
