# KathaCut build status (resume file)

If the session stops, say "continue" and work resumes from the first unchecked item.

## Done and tested on this machine (no API keys, RTX 3050)
- [x] Backend core: config, SQLite projects/jobs/usage, in-process worker (`backend/app`)
- [x] Shared render engine: timeline JSON -> MP4 (Ken Burns, crop tracks, captions, overlays, audio mix, NVENC)
- [x] Providers: LLM router, TTS, STT, images
- [x] Story Studio: idea -> script (Ollama) -> Kokoro narration -> Pollinations image -> 1080x1920 MP4 (47 s render)
- [x] Clip Studio: upload -> faster-whisper -> LLM picks -> face-tracked 9:16 clips + 16:9 best-of (47 s render)
- [x] PDF parsing with figure extraction (2-page generated PDF)
- [x] Web UI (dashboard, new story, new clips, script editor, clip editor, settings) builds and loads
- [x] Deploy files: Dockerfile, setup/run scripts, .env.example, README

## Not yet tested (needs keys or the user's files)
- [ ] Gemini, Groq (LLM + Whisper), Cloudflare FLUX, Pixabay, OpenRouter paths — written, never called with a real key
- [x] Full Story run from a real 138-page PDF with local Ollama (151 figures found; planner returned 1 of 3 shorts — weak local model)
- [x] YouTube link download from the home connection (4.4-minute video, 5 clips rendered)
- [ ] Docker image build (Docker is not installed here)
- [x] `setup.ps1` / `run.ps1` from a clean folder

## Next ideas (phase 2)
- [ ] Indic-script captions (Pillow on Windows lacks libraqm)
- [ ] Split-screen layout when two speakers are far apart (currently blurred-fit)
- [ ] Whisper re-alignment of Kokoro narration for tighter caption timing (currently estimated per sentence)
- [ ] Reaction cut with a webcam picture-in-picture slot; background music library; evaluation dashboard
- [ ] Auto-publish as private/draft (YouTube, TikTok inbox)

## Notes
- Windows long-path limit: keep the project in a short folder path; the venv broke inside the deep session folder.
- Keyless Pollinations allows 1 image per 45 s per IP; the app waits for the slot. Cloudflare keys remove the wait.
