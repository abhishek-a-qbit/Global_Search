import time
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from scripts import search
from config import HEADERS, TS_URL

app = FastAPI(title="Cuspera global search")


@app.get("/api/health")
def health():
    try:
        r = requests.get(f"{TS_URL}/health", headers=HEADERS, timeout=3)
        return {"api": "ok", "typesense": r.json()}
    except requests.RequestException as e:
        raise HTTPException(503, f"Typesense unreachable: {e}")


@app.get("/api/search")
def api_search(q: str = Query("", max_length=200)):
    t0 = time.perf_counter()
    try:
        result = search.run(q)
    except requests.RequestException as e:
        raise HTTPException(503, f"Typesense error: {e}")
    result["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


# Serve the frontend at /
app.mount(
    "/",
    StaticFiles(directory=Path(__file__).resolve().parents[1] / "frontend", html=True),
    name="static",
)