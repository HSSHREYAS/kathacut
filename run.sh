#!/usr/bin/env sh
exec .venv/bin/python -m uvicorn app.main:app --app-dir backend --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
