# Single-container deploy (Hugging Face Spaces, Docker SDK): Typesense + FastAPI API + static frontend.
# The index is rebuilt from data/catalog.jsonl.gz at every start (Space disks are ephemeral).
FROM python:3.12-slim

COPY --from=typesense/typesense:30.2 /opt/typesense-server /usr/local/bin/typesense-server

RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user config.py ./
COPY --chown=user app ./app
COPY --chown=user scripts ./scripts
COPY --chown=user frontend ./frontend
COPY --chown=user data/catalog.jsonl.gz ./data/
COPY --chown=user docker/start.sh /start.sh
RUN chmod +x /start.sh

USER user
ENV PYTHONUNBUFFERED=1 SEMANTIC_USE_CASES=0 CATALOG_API=0 PORT=7860
EXPOSE 7860
CMD ["/start.sh"]
