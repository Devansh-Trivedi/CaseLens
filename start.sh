#!/usr/bin/env bash
set -euo pipefail
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ ! -x .venv/bin/python ]; then
  python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10 or newer is required"'
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import fastapi, httpx, dotenv, uvicorn, pymupdf' 2>/dev/null; then
  .venv/bin/python -m pip install -r backend/requirements.txt
fi

if [ ! -f frontend/dist/index.html ]; then
  if command -v npm >/dev/null 2>&1; then
    (cd frontend && npm install && npm run build)
  else
    echo 'Frontend build missing. Install Node.js, then run: cd frontend && npm install && npm run build'
    exit 1
  fi
fi

if [ "$(uname)" = Darwin ] && [ ! -x .local/ocr ] && command -v swiftc >/dev/null 2>&1; then
  mkdir -p .local
  chmod 700 .local
  echo 'Preparing local OCR for scanned PDFs (first run only)…'
  if ! swiftc -module-cache-path .local/swift-cache backend/ocr.swift -o .local/ocr; then
    echo 'OCR is unavailable. Scanned pages will be clearly marked as unread.'
  fi
fi

# Never terminate another app or print credentials when checking the port.
if .venv/bin/python -c 'import urllib.request,json; r=json.load(urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=2)); assert r.get("version")=="0.2.0"' 2>/dev/null; then
  echo 'CaseLens is already running. Open http://127.0.0.1:8000/'
  exit 0
fi
echo 'CaseLens: http://127.0.0.1:8000/ — keep this terminal open. Ctrl+C stops the app.'
# No reload: an edit must not interrupt an in-progress ingestion job.
# Access logging is off so OAuth callback authorization codes are not logged.
exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --no-access-log
