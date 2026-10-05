"""Local Typesense server emulator for Windows.

Provides a lightweight, in-memory Typesense HTTP API on port 8108
so that the Cuspera Global Search indexer, API, and frontend can run
completely natively on Windows without requiring Docker or WSL.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from scripts.Textutil import norm

DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_FILE = DATA_DIR / "typesense_cache.json"

app = FastAPI(title="Local Typesense Emulator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage
collections: Dict[str, Dict[str, Any]] = {}
aliases: Dict[str, str] = {}


def edit_distance_le1(s1: str, s2: str) -> bool:
    if abs(len(s1) - len(s2)) > 1:
        return False
    if s1 == s2:
        return True
    i = j = diffs = 0
    while i < len(s1) and j < len(s2):
        if s1[i] != s2[j]:
            diffs += 1
            if diffs > 1:
                return False
            if len(s1) > len(s2):
                i += 1
                continue
            elif len(s1) < len(s2):
                j += 1
                continue
        i += 1
        j += 1
    return True


def is_one_typo(word: str, target: str) -> bool:
    if abs(len(word) - len(target)) <= 1 and edit_distance_le1(word, target):
        return True
    for prefix_len in (len(target) - 1, len(target), len(target) + 1):
        if 1 <= prefix_len <= len(word):
            if edit_distance_le1(word[:prefix_len], target):
                return True
    return False


def build_collection_indices(col_data: Dict[str, Any]):
    docs = col_data["documents"]
    by_type: Dict[str, List[dict]] = {}
    by_pid: Dict[tuple, List[dict]] = {}
    by_id: Dict[str, dict] = {}
    for d in docs:
        by_id[d["id"]] = d
        by_type.setdefault(d["type"], []).append(d)
        for pid in d.get("product_ids", []):
            by_pid.setdefault((d["type"], pid), []).append(d)
    col_data["indices"] = {
        "by_type": by_type,
        "by_pid": by_pid,
        "by_id": by_id,
    }


def save_cache():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    serializable = {
        "aliases": aliases,
        "collections": {
            k: {
                "schema": v["schema"],
                "documents": v["documents"],
            }
            for k, v in collections.items()
        },
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f)


def load_cache():
    if not CACHE_FILE.exists():
        return
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        aliases.update(data.get("aliases", {}))
        for k, v in data.get("collections", {}).items():
            col_data = {
                "schema": v["schema"],
                "documents": v["documents"],
            }
            build_collection_indices(col_data)
            collections[k] = col_data
        print(f"Loaded {len(collections)} collection(s) from cache.")
    except Exception as e:
        print(f"Error loading cache: {e}")


# Initialize cache at startup
load_cache()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/collections")
async def create_collection(request: Request):
    schema = await request.json()
    name = schema["name"]
    col_data = {
        "schema": schema,
        "documents": [],
    }
    build_collection_indices(col_data)
    collections[name] = col_data
    return schema


@app.delete("/collections/{collection_name}")
def delete_collection(collection_name: str):
    if collection_name in collections:
        del collections[collection_name]
    save_cache()
    return {"name": collection_name}


@app.put("/aliases/{alias_name}")
async def create_alias(alias_name: str, request: Request):
    body = await request.json()
    target_collection = body["collection_name"]
    aliases[alias_name] = target_collection
    save_cache()
    return {"name": alias_name, "collection_name": target_collection}


@app.post("/collections/{collection_name}/documents/import")
async def import_documents(collection_name: str, request: Request):
    if collection_name not in collections:
        raise HTTPException(status_code=404, detail="Collection not found")
    raw_body = await request.body()
    lines = raw_body.decode("utf-8").splitlines()
    col = collections[collection_name]
    imported = []
    for line in lines:
        if line.strip():
            doc = json.loads(line)
            col["documents"].append(doc)
            imported.append({"success": True})
    build_collection_indices(col)
    save_cache()
    return Response(
        content="\n".join(json.dumps(res) for res in imported) + "\n",
        media_type="text/plain",
    )


def execute_single_search(col: dict, s: dict) -> list[dict]:
    docs = col["documents"]
    indices = col.get("indices", {})
    by_type = indices.get("by_type", {})
    by_pid = indices.get("by_pid", {})

    q = s.get("q", "*")
    filter_by = s.get("filter_by", "")
    per_page = int(s.get("per_page", 10))
    sort_by = s.get("sort_by", "")
    query_by = [f.strip() for f in s.get("query_by", "").split(",") if f.strip()]
    prefix = s.get("prefix", True)
    num_typos = int(s.get("num_typos", 0))

    # Parse filter conditions
    doc_type = None
    target_pid = None
    if filter_by:
        for part in filter_by.split("&&"):
            part = part.strip()
            if ":=" in part:
                k, v = part.split(":=", 1)
                k, v = k.strip(), v.strip()
                if k == "type":
                    doc_type = v
                elif k == "product_ids":
                    target_pid = int(v)

    # Fast index lookup
    if doc_type and target_pid is not None:
        candidates = by_pid.get((doc_type, target_pid), [])
    elif doc_type:
        candidates = by_type.get(doc_type, [])
    elif target_pid is not None:
        candidates = [d for d in docs if target_pid in d.get("product_ids", [])]
    else:
        candidates = docs

    if q == "*":
        matched = list(candidates)
        if "popularity:desc" in sort_by:
            matched.sort(key=lambda d: -d.get("popularity", 0))
        return matched[:per_page]

    nq = norm(q)
    if not nq:
        return []

    scored_candidates = []
    for d in candidates:
        bucket = -1
        text_values = []
        for field in query_by:
            val = d.get(field)
            if isinstance(val, list):
                text_values.extend(val)
            elif isinstance(val, str):
                text_values.append(val)
        if not text_values and "title" in d:
            text_values.append(d["title"])

        for tv in text_values:
            nt = norm(tv)
            if nt == nq:
                bucket = max(bucket, 3)
            elif prefix and nt.startswith(nq):
                bucket = max(bucket, 2)
            elif prefix and any(tok.startswith(nq) for tok in nt.split()):
                bucket = max(bucket, 1)
            elif num_typos > 0 and len(nq) >= 4:
                if is_one_typo(nt, nq) or any(is_one_typo(tok, nq) for tok in nt.split()):
                    bucket = max(bucket, 0)

        if bucket >= 0:
            scored_candidates.append((bucket, d))

    if "_text_match" in sort_by:
        scored_candidates.sort(
            key=lambda item: (
                -item[0],
                item[1].get("name_len", len(item[1].get("title", ""))),
                -item[1].get("popularity", 0),
            )
        )
    elif "popularity:desc" in sort_by:
        scored_candidates.sort(key=lambda item: (-item[0], -item[1].get("popularity", 0)))
    else:
        scored_candidates.sort(key=lambda item: -item[0])

    return [item[1] for item in scored_candidates[:per_page]]


@app.post("/multi_search")
async def multi_search(request: Request):
    body = await request.json()
    searches = body.get("searches", [])
    results = []
    for s in searches:
        col_name = s.get("collection", "")
        # Resolve alias
        resolved = aliases.get(col_name, col_name)
        col = collections.get(resolved)
        if not col:
            results.append({"hits": [], "found": 0, "out_of": 0})
            continue
        hits = execute_single_search(col, s)
        results.append({
            "hits": [{"document": h} for h in hits],
            "found": len(hits),
            "out_of": len(col["documents"]),
        })
    return {"results": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("local_typesense:app", host="127.0.0.1", port=8108, reload=False)
