#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
./.venv/bin/python -m pip install -r requirements.txt

# Optional HTTPS for local dev: set SSL_KEYFILE + SSL_CERTFILE (self-signed)
if [ -n "${SSL_KEYFILE:-}" ] && [ -n "${SSL_CERTFILE:-}" ]; then
  ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8021}" \
    --ssl-keyfile "$SSL_KEYFILE" --ssl-certfile "$SSL_CERTFILE"
else
  ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8021}"
fi