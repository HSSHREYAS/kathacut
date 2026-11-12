#!/usr/bin/env sh
# One-time setup on Linux/macOS (needs Python 3.11+, Node 18+, ffmpeg optional).
set -e
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
(cd frontend && npm install --no-audit --no-fund && npm run build)
[ -f .env ] || cp .env.example .env
echo "Setup finished. Start with: ./run.sh"
