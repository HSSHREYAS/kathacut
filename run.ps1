# Starts KathaCut at http://localhost:8000  (UI + API + worker in one process)
& .venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
