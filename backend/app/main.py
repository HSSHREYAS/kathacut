"""KathaCut API + static UI. Run:  uvicorn app.main:app --app-dir backend --port 8000"""
import hmac
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import config, db, ffutil, worker
from .pipelines import clips as clips_pipe
from .pipelines import story as story_pipe
from .providers import images, llm, tts
from .render import captions

DIST = config.ROOT / "frontend" / "dist"
JOB_TYPES = {"story": ("story_plan", "story_render"), "clips": ("clips_analyze", "clips_render")}


@asynccontextmanager
async def lifespan(_app):
    worker.start()
    yield


app = FastAPI(title="KathaCut", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origin_regex=".*", allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


def _authed(request: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    given = request.cookies.get("kc") or request.headers.get("x-kc-key") or request.query_params.get("k") or ""
    return hmac.compare_digest(given, config.APP_PASSWORD)


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    guarded = path.startswith("/api/") or path.startswith("/files/")
    if guarded and request.method != "OPTIONS" and path not in ("/api/login", "/api/health") and not _authed(request):
        return JSONResponse({"detail": "Password required"}, status_code=401)
    return await call_next(request)


@app.post("/api/login")
def login(body: dict = Body(...)):
    if not hmac.compare_digest(str(body.get("password", "")), config.APP_PASSWORD or ""):
        raise HTTPException(401, "Wrong password")
    r = JSONResponse({"ok": True})
    r.set_cookie("kc", config.APP_PASSWORD, httponly=True, samesite="none", secure=True, max_age=30 * 86400)
    return r


@app.get("/api/health")
def health(request: Request):
    if not _authed(request):
        return {"ok": True, "auth_required": True}
    from . import config as c
    return {"ok": True, "auth_required": False, "worker": worker.status(), "llm": llm.status(),
            "images": images.available(),
            "stt": {"groq": bool(c.get("GROQ_API_KEY")), "local": True},
            "encoder": ffutil.video_encoder()[0], "active_jobs": len(db.active_jobs())}


@app.get("/api/meta")
def meta():
    return {
        "styles": [{"id": k, "label": v["label"], "highlight": v["highlight"]} for k, v in story_pipe.ART_STYLES.items()],
        "genres": [{"id": k, "label": k.replace("_", " ").title()} for k in story_pipe.GENRES],
        "voices": tts.VOICES,
        "caption_styles": [{"id": k, "label": v["label"]} for k, v in captions.STYLES.items()],
        "music": sorted(f.name for f in config.MUSIC.iterdir() if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")),
        "aspects": list(clips_pipe.ASPECTS),
    }


@app.get("/api/settings")
def get_settings():
    return {"fields": config.public_view()}


@app.post("/api/settings")
def set_settings(body: dict = Body(...)):
    config.set_many(body.get("values") or {})
    llm._models_cache.clear()
    llm._cooldown.clear()
    return {"fields": config.public_view()}


@app.get("/api/usage")
def usage():
    return {"today": db.usage_today()}


# ------------------------------------------------------------------ projects
def _pdir(pid: str) -> Path:
    if not pid.isalnum():
        raise HTTPException(400, "Bad project id")
    return config.PROJECTS / pid


@app.get("/api/projects")
def list_projects():
    return {"projects": db.list_projects(), "active": db.active_jobs()}


@app.post("/api/projects")
def create_project(kind: str = Form(...), title: str = Form(""), params: str = Form("{}"),
                   file: UploadFile | None = File(None)):
    if kind not in JOB_TYPES:
        raise HTTPException(400, "kind must be story or clips")
    try:
        prm = json.loads(params)
    except json.JSONDecodeError as e:
        raise HTTPException(400, "params must be JSON") from e
    if kind == "story":
        if prm.get("source") == "pdf" and not file:
            raise HTTPException(400, "Attach a PDF")
        if prm.get("source") != "pdf" and not (prm.get("text") or "").strip():
            raise HTTPException(400, "Write an idea or paste some text")
    else:
        if prm.get("source") == "youtube" and not (prm.get("url") or "").startswith("http"):
            raise HTTPException(400, "Paste a video link")
        if prm.get("source") != "youtube" and not file:
            raise HTTPException(400, "Attach a video file")
    fallback = "Story" if kind == "story" else "Clips"
    name = (title or (Path(file.filename).stem if file and file.filename else "") or
            " ".join((prm.get("text") or "").split()[:6]) or f"{fallback} project").strip()[:90]
    p = db.create_project(kind, name, prm)
    pdir = _pdir(p["id"])
    if file:
        ext = Path(file.filename or "").suffix.lower()
        if kind == "story":
            dst = pdir / "source.pdf"
        else:
            dst = pdir / f"source{ext if ext in clips_pipe.VIDEO_EXT else '.mp4'}"
        with open(dst, "wb") as f:
            shutil.copyfileobj(file.file, f, 1 << 20)
    db.update_project(p["id"], status="planning" if kind == "story" else "analyzing")
    db.enqueue(p["id"], JOB_TYPES[kind][0])
    return get_project(p["id"])


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "Project not found")
    p["jobs"] = db.jobs_for(pid)
    return p


@app.put("/api/projects/{pid}")
def update_project(pid: str, body: dict = Body(...)):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "Project not found")
    if isinstance(body.get("title"), str) and body["title"].strip():
        db.update_project(pid, title=body["title"].strip()[:90])
    if isinstance(body.get("params"), dict):
        db.update_project(pid, params={**p["params"], **body["params"]})
    if isinstance(body.get("data"), dict):
        allowed = {k: v for k, v in body["data"].items() if k in ("videos", "clips", "bible")}
        if allowed:
            db.patch_data(pid, **allowed)
    return get_project(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    pdir = _pdir(pid)
    db.delete_project(pid)
    shutil.rmtree(pdir, ignore_errors=True)
    return {"ok": True}


@app.post("/api/projects/{pid}/jobs")
def add_job(pid: str, body: dict = Body(...)):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "Project not found")
    jtype = body.get("type")
    if jtype not in JOB_TYPES[p["kind"]]:
        raise HTTPException(400, "Unknown job type")
    if jtype in ("story_plan", "clips_analyze"):
        db.update_project(pid, status="planning" if p["kind"] == "story" else "analyzing")
    return db.enqueue(pid, jtype, body.get("params") or {})


@app.post("/api/projects/{pid}/scene-image")
def scene_image(pid: str, video_id: str = Form(...), scene_id: str = Form(...), file: UploadFile = File(...)):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "Project not found")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "Use a JPG, PNG or WebP image")
    up = _pdir(pid) / "uploads"
    up.mkdir(exist_ok=True)
    rel = f"uploads/{db.new_id()}{ext}"
    with open(_pdir(pid) / rel, "wb") as f:
        shutil.copyfileobj(file.file, f)
    videos = p["data"].get("videos", [])
    for v in videos:
        if v["id"] == video_id:
            for s in v["scenes"]:
                if s["id"] == scene_id:
                    s["image"] = rel
    db.patch_data(pid, videos=videos)
    return get_project(pid)


@app.get("/files/{pid}/{path:path}")
def files(pid: str, path: str, download: int = 0):
    base = _pdir(pid).resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name if download else None)


# ------------------------------------------------------------------ static UI
if DIST.exists():
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = (DIST / path).resolve()
        if path and DIST.resolve() in f.parents and f.is_file():
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
