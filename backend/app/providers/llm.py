"""LLM router: Gemini -> Groq -> OpenRouter -> Ollama, with model auto-discovery and 429 cooldowns.

Model names change every few months, so nothing is hard-coded: each provider's model list is
fetched and the best match for the tier ("smart" or "bulk") is picked. Overrides live in Settings.
"""
import json
import re
import time

import httpx

from .. import config, db

_models_cache: dict = {}   # provider -> (ts, [ids])
_cooldown: dict = {}       # (provider, model) -> until ts
BUDGET_CHARS = {"gemini": 300_000, "groq": 20_000, "openrouter": 40_000, "ollama": 9_000}


class LLMError(RuntimeError):
    pass


def _cached(provider: str, fetch):
    hit = _models_cache.get(provider)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    ids = fetch()
    _models_cache[provider] = (time.time(), ids)
    return ids


def _ver(name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return float(m[1]) if m else 0.0


# ---------------------------------------------------------------- Gemini
_G = "https://generativelanguage.googleapis.com/v1beta"
_G_SKIP = ("tts", "image", "live", "embedding", "audio", "vision", "learnlm", "aqa", "robotics", "computer")


def _gemini_models(key: str) -> list[str]:
    def fetch():
        r = httpx.get(f"{_G}/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=20)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            name = m["name"].split("/")[-1]
            if "generateContent" in m.get("supportedGenerationMethods", []) and not any(s in name for s in _G_SKIP):
                out.append(name)
        return out
    return _cached("gemini", fetch)


def _gemini_candidates(key: str, tier: str) -> list[str]:
    ov = config.get("GEMINI_MODEL")
    names = _gemini_models(key)
    stable = [n for n in names if "preview" not in n and "exp" not in n] or names
    lite = sorted([n for n in stable if "flash-lite" in n], key=_ver, reverse=True)
    flash = sorted([n for n in stable if "flash" in n and "lite" not in n], key=_ver, reverse=True)
    gemma = sorted([n for n in stable if n.startswith("gemma")], key=lambda n: (_ver(n), "27b" in n or "31b" in n),
                   reverse=True)
    order = (flash[:1] + lite[:1] + gemma[:1] + flash[1:2]) if tier == "smart" else (lite[:1] + gemma[:1] + flash[:1] + lite[1:2])
    return ([ov] if ov else []) + [m for m in order if m != ov]


def _gemini_call(key, model, system, prompt, json_mode, max_tokens, temperature):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if model.startswith("gemma"):
        if system:
            body["contents"][0]["parts"][0]["text"] = system + "\n\n" + prompt
    else:
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
    r = httpx.post(f"{_G}/models/{model}:generateContent", headers={"x-goog-api-key": key}, json=body, timeout=180)
    r.raise_for_status()
    cands = r.json().get("candidates") or []
    parts = (cands[0].get("content") or {}).get("parts") or [] if cands else []
    txt = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not txt.strip():
        raise LLMError("empty response")
    return txt


# ---------------------------------------------------------------- OpenAI-compatible (Groq, OpenRouter)
def _oa_models(provider, base, key):
    def fetch():
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
    return _cached(provider, fetch)


def _groq_candidates(key, tier):
    ov = config.get("GROQ_MODEL")
    ids = _oa_models("groq", "https://api.groq.com/openai/v1", key)
    bad = ("whisper", "guard", "tts", "playai", "safeguard", "compound", "orpheus")
    ids = [i for i in ids if not any(b in i.lower() for b in bad)]
    pref = ["gpt-oss-120b", "llama-3.3-70b", "qwen", "kimi", "gpt-oss-20b", "llama"] if tier == "smart" else \
           ["gpt-oss-20b", "llama-3.1-8b", "gpt-oss-120b", "llama-3.3-70b", "qwen", "llama"]
    out = []
    for p in pref:
        for i in ids:
            if p in i.lower() and i not in out:
                out.append(i)
    return ([ov] if ov else []) + [m for m in out[:4] if m != ov]


def _openrouter_candidates(key, tier):
    ov = config.get("OPENROUTER_MODEL")
    if ov:
        return [ov]
    ids = [i for i in _oa_models("openrouter", "https://openrouter.ai/api/v1", key) if i.endswith(":free")]
    pref = ["gpt-oss", "llama-3.3-70b", "gemma", "qwen", "deepseek", "mistral"]
    out = [i for p in pref for i in ids if p in i]
    return list(dict.fromkeys(out))[:3] or ids[:2]


def _oa_call(base, key, model, system, prompt, json_mode, max_tokens, temperature):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": min(max_tokens, 8000)}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=180)
    if r.status_code == 400 and json_mode:  # model without JSON mode
        body.pop("response_format")
        r = httpx.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=180)
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"].get("content") or ""
    if not txt.strip():
        raise LLMError("empty response")
    return txt


# ---------------------------------------------------------------- Ollama
def _ollama_models():
    def fetch():
        r = httpx.get(config.get("OLLAMA_URL") + "/api/tags", timeout=3)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", []) if "embed" not in m["name"]]
    try:
        return _cached("ollama", fetch)
    except Exception:
        _models_cache["ollama"] = (time.time() - 3540, [])   # remember "not running" for a minute
        return []


def _ollama_call(model, system, prompt, json_mode, max_tokens, temperature):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs, "stream": False,
            "options": {"temperature": temperature, "num_ctx": 8192, "num_predict": max_tokens}}
    if json_mode:
        body["format"] = "json"
    r = httpx.post(config.get("OLLAMA_URL") + "/api/chat", json=body, timeout=900)
    r.raise_for_status()
    return r.json()["message"]["content"]


# ---------------------------------------------------------------- router
def _order() -> list[str]:
    return [p.strip() for p in config.get("LLM_PROVIDERS").split(",") if p.strip()]


def available() -> list[str]:
    out = []
    for p in _order():
        if p == "gemini" and config.get("GEMINI_API_KEY"):
            out.append(p)
        elif p == "groq" and config.get("GROQ_API_KEY"):
            out.append(p)
        elif p == "openrouter" and config.get("OPENROUTER_API_KEY"):
            out.append(p)
        elif p == "ollama" and _ollama_models():
            out.append(p)
    return out


def budget_chars() -> int:
    """How much source text one prompt may carry for the first available provider."""
    av = available()
    return BUDGET_CHARS.get(av[0], 9000) if av else 9000


def _attempts(provider: str, tier: str):
    if provider == "gemini":
        key = config.get("GEMINI_API_KEY")
        for m in _gemini_candidates(key, tier):
            yield m, lambda s, p, j, mt, t, m=m: _gemini_call(key, m, s, p, j, mt, t)
    elif provider == "groq":
        key = config.get("GROQ_API_KEY")
        for m in _groq_candidates(key, tier):
            yield m, lambda s, p, j, mt, t, m=m: _oa_call("https://api.groq.com/openai/v1", key, m, s, p, j, mt, t)
    elif provider == "openrouter":
        key = config.get("OPENROUTER_API_KEY")
        for m in _openrouter_candidates(key, tier):
            yield m, lambda s, p, j, mt, t, m=m: _oa_call("https://openrouter.ai/api/v1", key, m, s, p, j, mt, t)
    elif provider == "ollama":
        models = _ollama_models()
        ov = config.get("OLLAMA_MODEL")
        m = ov if ov else (models[0] if models else None)
        if m:
            yield m, lambda s, p, j, mt, t, m=m: _ollama_call(m, s, p, j, mt, t)


def complete(prompt: str, system: str = "", json_mode: bool = True, tier: str = "bulk",
             max_tokens: int = 8192, temperature: float = 0.7) -> str:
    errors = []
    provs = available()
    if not provs:
        raise LLMError("No LLM configured. Add a free Gemini or Groq key in Settings, or start Ollama.")
    for prov in provs:
        if len(prompt) + len(system) > BUDGET_CHARS.get(prov, 9000) * 1.6:
            errors.append(f"{prov}: prompt too large")
            continue
        try:
            attempts = list(_attempts(prov, tier))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{prov}: {str(e)[:120]}")
            continue
        for model, call in attempts:
            if _cooldown.get((prov, model), 0) > time.time():
                errors.append(f"{prov}/{model}: cooling down")
                continue
            for attempt in range(2):
                try:
                    txt = call(system, prompt, json_mode, max_tokens, temperature)
                    db.log_usage(prov, f"requests:{model}", 1)
                    return txt
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code
                    errors.append(f"{prov}/{model}: HTTP {code} {e.response.text[:140]}")
                    if code == 429:
                        if attempt == 0 and prov != "gemini":
                            time.sleep(8)
                            continue
                        _cooldown[(prov, model)] = time.time() + 300
                    elif code in (500, 502, 503, 504) and attempt == 0:
                        time.sleep(3)
                        continue
                    break
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{prov}/{model}: {type(e).__name__} {str(e)[:120]}")
                    break
    raise LLMError("All LLM providers failed -> " + " | ".join(errors[-6:]))


def parse_json(txt: str):
    t = txt.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise LLMError("Model did not return JSON")
    s = min(starts)
    close = "}" if t[s] == "{" else "]"
    e = t.rfind(close)
    try:
        return json.loads(t[s:e + 1])
    except Exception as ex:
        raise LLMError(f"Model returned invalid JSON: {ex}") from ex


def complete_json(prompt: str, system: str = "", tier: str = "bulk", max_tokens: int = 8192,
                  temperature: float = 0.7):
    last = None
    for _ in range(2):
        try:
            return parse_json(complete(prompt, system, True, tier, max_tokens, temperature))
        except LLMError as e:
            last = e
            if "All LLM providers failed" in str(e) or "No LLM" in str(e):
                raise
    raise last


def status() -> dict:
    out = {}
    for p in ("gemini", "groq", "openrouter", "ollama"):
        out[p] = p in available()
    return out
