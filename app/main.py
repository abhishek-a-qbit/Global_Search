import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from typesense.exceptions import ObjectNotFound, TypesenseClientError

from app.catalog_api import router as catalog_router
from config import ALIAS, CATALOG_API, CORS_ORIGINS
from scripts import catalog, search

app = FastAPI(title="Cuspera global search")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET"], allow_headers=["*"])
if CATALOG_API:
    app.include_router(catalog_router)

for _exc, _status in ((catalog.NotFound, 404), (catalog.Conflict, 409), (ValueError, 422)):
    app.add_exception_handler(_exc, lambda request, e, status=_status: JSONResponse({"detail": str(e)}, status))


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
def api_search(q: str = Query("", max_length=200),
               semantic: bool | None = Query(None, description="Override semantic use-case search "
                                              "for this request (0/1); default: the server's setting")):
    t0 = time.perf_counter()
    try:
        result = search.run(q, semantic)
    except (TypesenseClientError, RuntimeError) as e:
        if _index_missing():
            raise HTTPException(503, "Search index is still loading (a minute or two after a cold start)")
        raise HTTPException(503, f"Typesense error: {e}")
    result["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


def _index_missing() -> bool:
    try:
        search.client.aliases[ALIAS].retrieve()
        return False
    except ObjectNotFound:
        return True
    except TypesenseClientError:
        return False


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "frontend", html=True), name="frontend")
