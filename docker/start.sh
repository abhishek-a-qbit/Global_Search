#!/bin/sh
# Typesense listens on localhost only; a random key per boot unless TYPESENSE_API_KEY is set.
set -e
export TYPESENSE_API_KEY="${TYPESENSE_API_KEY:-$(python -c 'import secrets; print(secrets.token_hex(24))')}"
export TYPESENSE_URL="http://127.0.0.1:8108"
mkdir -p /tmp/ts-data

typesense-server --data-dir /tmp/ts-data --api-key="$TYPESENSE_API_KEY" \
  --listen-address 127.0.0.1 --api-port 8108 &

(
  python - <<'EOF'
import time
from config import make_client
c = make_client(timeout_seconds=5)
for _ in range(300):
    try:
        if c.operations.is_healthy():
            break
    except Exception:
        pass
    time.sleep(1)
EOF
  python -m scripts.source rebuild data/catalog.jsonl.gz
) &

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}"
