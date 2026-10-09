import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typesense.exceptions import TypesenseClientError

from config import CORS_ORIGINS
from scripts import search

app = FastAPI(title="Cuspera global search")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET"], allow_headers=["*"])


@app.get("/api/health")
def health():
    try:
        healthy = search.client.operations.is_healthy()
    except TypesenseClientError as e:
        raise HTTPException(503, f"Typesense unreachable: {e}")
    if not healthy:
        raise HTTPException(503, "Typesense is not healthy")
    return {"api": "ok", "typesense": {"ok": True}}


@app.get("/api/search")
def api_search(q: str = Query("", max_length=200)):
    t0 = time.perf_counter()
    try:
        result = search.run(q)
    except (TypesenseClientError, RuntimeError) as e:
        raise HTTPException(503, f"Typesense error: {e}")
    result["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result
