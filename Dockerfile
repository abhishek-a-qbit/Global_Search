# Single-container deploy (e.g. Render free): Typesense + FastAPI API + static frontend.
# The index is loaded from data/documents.jsonl.gz at every start (free disks are ephemeral).
FROM python:3.12-slim

COPY --from=typesense/typesense:30.2 /opt/typesense-server /usr/local/bin/typesense-server

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py indexer.py export_index.py seed_index.py ./
COPY app ./app
COPY scripts ./scripts
COPY frontend ./frontend
COPY data/documents.jsonl.gz ./data/
COPY docker/start.sh /start.sh
RUN chmod +x /start.sh

ENV PYTHONUNBUFFERED=1 SEMANTIC_USE_CASES=0 CATALOG_API=0
CMD ["/start.sh"]
