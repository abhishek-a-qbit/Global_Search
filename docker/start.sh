#!/bin/sh
# Typesense listens on localhost only; a random key per boot unless TYPESENSE_API_KEY is set.
# The index is built from the catalog only when the data dir has none yet (embedding every use case
# takes a while); with TS_DATA_DIR on a volume, later restarts reuse it.
set -e
export TYPESENSE_API_KEY="${TYPESENSE_API_KEY:-$(python -c 'import secrets; print(secrets.token_hex(24))')}"
export TYPESENSE_URL="http://127.0.0.1:8108"
DATA_DIR="${TS_DATA_DIR:-/tmp/ts-data}"
mkdir -p "$DATA_DIR"

typesense-server --data-dir "$DATA_DIR" --api-key="$TYPESENSE_API_KEY" \
  --listen-address 127.0.0.1 --api-port 8108 &

(
  rc=0
  python - <<'EOF' || rc=$?
import sys
import time

from typesense.exceptions import ObjectNotFound

from config import ALIAS, make_client

c = make_client(timeout_seconds=5)
for _ in range(600):
    try:
        c.aliases[ALIAS].retrieve()
        sys.exit(0)
    except ObjectNotFound:
        sys.exit(1)
    except Exception:
        time.sleep(1)
sys.exit(2)
EOF
  if [ "$rc" = 1 ]; then
    python -m scripts.source rebuild "${CATALOG_SOURCE:-data/catalog.jsonl.gz}"
  else
    echo "Index already present (check exit code $rc); not rebuilding."
  fi
) &

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}"
