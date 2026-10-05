import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import time
from indexer import build_documents
from scripts.Textutil import norm

t0 = time.time()
docs = build_documents()
print(f"Built {len(docs)} docs in {time.time() - t0:.2f}s")

by_type = {}
by_pid = {}
for d in docs:
    by_type.setdefault(d["type"], []).append(d)
    for p in d.get("product_ids", []):
        by_pid.setdefault((d["type"], p), []).append(d)

t0 = time.time()
q = "canva"
matches = []
for d in by_type.get("product", []):
    pn = norm(d["product_names"][0])
    if pn == q:
        b = 3
    elif pn.startswith(q):
        b = 2
    elif any(tok.startswith(q) for tok in pn.split()):
        b = 1
    else:
        continue
    matches.append((-b, d["name_len"], -d["popularity"], d))

matches.sort(key=lambda x: (x[0], x[1], x[2]))
dt_prod = (time.time() - t0) * 1000
top_doc = matches[0][3]
print(f"Prod search took: {dt_prod:.2f}ms, top: {top_doc['title']}")

aid = top_doc["product_ids"][0]
t0 = time.time()
comps = sorted(by_pid.get(("compare", aid), []), key=lambda d: -d["popularity"])[:5]
dt_comp = (time.time() - t0) * 1000
print(f"Compares took: {dt_comp:.2f}ms, top: {[c['title'] for c in comps]}")
