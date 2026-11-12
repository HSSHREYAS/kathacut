# One-time setup on Windows. Run from the project folder:  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
if ((Get-Location).Path.Length -gt 90) { Write-Warning "This folder path is long. Windows may fail to load some libraries; a short path like C:\kathacut is safer." }
if (Get-Command uv -ErrorAction SilentlyContinue) {
  uv venv .venv --python 3.13
  uv pip install --python .venv\Scripts\python.exe -r backend\requirements.txt
} else {
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
}
Push-Location frontend
npm install --no-audit --no-fund
npm run build
Pop-Location
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Write-Host "`nSetup finished. Start the app with:  .\run.ps1" -ForegroundColor Green
