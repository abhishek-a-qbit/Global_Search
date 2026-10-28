"""The catalog as one JSON Lines source file (a local path or an http(s) URL, optionally gzipped):
read and validate it, build the search documents from it, and sync the live index to it. A sync
writes only the documents that changed, so use cases whose name did not change keep their embedding.

One JSON object per line, by "type" (urls are cuspera.com page urls; only their path is used):
  {"type": "product", "id": 17370, "name": "Canva", "url": "https://www.cuspera.com/products/canva-x-17370",
   "category": "graphic-design", "cat": {"perma": "...", "name": "..."}, "cats": [{"perma": "...", "name": "..."}],
   "use_cases": [{"id": "<uuid>", "name": "..."}]}             url null / missing: kept, but not searchable
  {"type": "news" | "customer_story" | "alternatives", "url": "...", "product_id": 17370}   no product_id: vendor page
  {"type": "compare", "url": "...", "product_ids": [17370, 2345]}
  {"type": "category" | "industry", "url": "https://www.cuspera.com/categories/crm", "title": "CRM"}
Pages may also set "title" and "popularity", replacing the derived ones.

  python -m scripts.source export [file]          live index -> source file (default: SOURCE_URL)
  python -m scripts.source sync [file|url] [--dry-run]      only what changed
  python -m scripts.source rebuild [file|url]              a fresh index from the file alone"""
import gzip
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Iterable, Literal, Union
from urllib.parse import urlsplit

import requests
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from typesense.exceptions import ObjectNotFound

from config import ALIAS, BASE, EMBED_MODEL, SEMANTIC_USE_CASES, SOURCE_URL, TS_KEY, TS_URL, make_client
from scripts import catalog, search
from scripts import documents as D
from scripts.Textutil import norm, pretty

ROOT = Path(__file__).resolve().parents[1]
BATCH_SIZE = 1000
MAX_ERRORS = 20
client = make_client(timeout_seconds=1800)           # imports embed new use cases (and the first one downloads the model): can be slow


# ---- file format ------------------------------------------------------------------------------
def _text(s: str) -> str:
    if not norm(s):
        raise ValueError("must contain letters or digits")
    return s.strip()


def _url(s: str) -> str:
    if not urlsplit(s.strip()).path.startswith("/") or len(urlsplit(s.strip()).path) < 2:
        raise ValueError("must be a page url (https://www.cuspera.com/...) or path (/...)")
    return s.strip()


Text = Annotated[str, Field(min_length=1, max_length=300), AfterValidator(_text)]
Url = Annotated[str, Field(min_length=1, max_length=1000), AfterValidator(_url)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")         # a misspelt field fails instead of being ignored


class Category(_Strict):
    perma: str = Field(min_length=1)
    name: str


class UseCase(_Strict):
    id: str = Field(min_length=1, max_length=100)
    name: Text


class Product(_Strict):
    type: Literal["product"]
    id: int = Field(ge=0)
    name: Text
    url: Url | None = None
    category: str | None = None                        # category page slug shown for the product
    cat: Category | None = None
    cats: list[Category] = []
    use_cases: list[UseCase] = []


class Page(_Strict):
    type: Literal["news", "customer_story", "alternatives"]
    url: Url
    product_id: int | None = None
    title: Text | None = None
    popularity: int | None = Field(None, ge=0)


class Compare(_Strict):
    type: Literal["compare"]
    url: Url
    product_ids: Annotated[list[int], Field(min_length=2, max_length=2)]
    title: Text | None = None
    popularity: int | None = Field(None, ge=0)


class Topic(_Strict):
    type: Literal["category", "industry"]
    url: Url
    title: Text | None = None
    popularity: int | None = Field(None, ge=0)


Line = Annotated[Union[Product, Page, Compare, Topic], Field(discriminator="type")]
LINE = TypeAdapter(Line)


def _path(url: str) -> str:
    return urlsplit(url).path


def read_source(source: str = SOURCE_URL) -> list[str]:
    """The file's lines; gzip is detected by content."""
    if re.match(r"https?://", source):
        response = requests.get(source, timeout=300)
        response.raise_for_status()
        data = response.content
    else:
        data = (ROOT / source).read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8-sig").splitlines()


def parse(rows: Iterable[str | dict]) -> list:
    """Validated lines; raises ValueError listing the first errors (line numbers are 1-based)."""
    lines, errors, seen = [], [], set()
    for n, row in enumerate(rows, start=1):
        if isinstance(row, str) and not row.strip():
            continue
        try:
            line = LINE.validate_json(row) if isinstance(row, str) else LINE.validate_python(row)
        except ValidationError as e:
            errors.append(f"line {n}: " + "; ".join(
                f"{'.'.join(map(str, err['loc']))}: {err['msg']}" if err["loc"] else err["msg"] for err in e.errors()))
        else:
            key = ("product", line.id) if line.type == "product" else (line.type, _path(line.url))
            if key in seen:
                errors.append(f"line {n}: duplicate {key[0]} {key[1]}")
            seen.add(key)
            lines.append(line)
        if len(errors) >= MAX_ERRORS:
            break
    if errors:
        raise ValueError("Invalid source file:\n" + "\n".join(errors))
    if not any(line.type == "product" for line in lines):
        raise ValueError("Invalid source file: no products")
    return lines


# ---- search documents ---------------------------------------------------------------------------
def _owner_popularity(path: str, names_by_first_word: dict[str, list[tuple[str, int]]]) -> int:
    """'/vendors/zoom/news' -> popularity of the vendor's most popular product ('Zoom Workplace')."""
    owner = D.page_owner(path)
    if not owner:
        return 1
    return max((p for name, p in names_by_first_word.get(owner.split()[0], [])
                if name == owner or name.startswith(owner + " ")), default=1)


def _override(doc: dict, line) -> dict:
    if line.title:
        doc.update(title=line.title, name_len=len(line.title))
    if line.popularity is not None:
        doc["popularity"] = line.popularity
    return doc


def build_documents(lines: list) -> list[dict]:
    """All search documents for a source: products, pages, comparisons, use cases and the hidden
    product records (the catalog API's source of truth), as the catalog API would build them."""
    products = [line for line in lines if line.type == "product"]
    records = {p.id: D.record_doc(p.id, p.name, p.cat.model_dump() if p.cat else None,
                                  [c.model_dump() for c in p.cats], p.category,
                                  D.unique_use_cases(u.model_dump() for u in p.use_cases)) for p in products}

    comparison_counts = {}
    for line in lines:
        if line.type == "compare":
            for pid in line.product_ids:
                comparison_counts[pid] = comparison_counts.get(pid, 0) + 1
    # Pages inherit their product's popularity, so "popular news" / "top comparisons" mean something
    product_docs = {p.id: D.product_doc(p.id, p.name, _path(p.url), 1 + comparison_counts.get(p.id, 0))
                    for p in products if p.url}
    names_by_first_word = {}
    for p in product_docs.values():
        if name := norm(p["title"]):
            names_by_first_word.setdefault(name.split()[0], []).append((name, p["popularity"]))
    topic_names = {}                                   # category slug -> name, from the products' cat / cats
    for p in products:
        for c in ([p.cat] if p.cat else []) + p.cats:
            topic_names.setdefault(c.perma, c.name.strip())

    documents = list(product_docs.values())
    for line in lines:
        if line.type in ("news", "customer_story", "alternatives", "category", "industry"):
            path = _path(line.url)
            doc = D.page_doc(line.type, path, product_docs.get(getattr(line, "product_id", None)),
                             popularity=_owner_popularity(path, names_by_first_word))
            if line.type == "category" and (title := topic_names.get(path.rstrip("/").rsplit("/", 1)[-1])):
                doc.update(title=title, name_len=len(title))
            documents.append(_override(doc, line))
        elif line.type == "compare":
            path = _path(line.url)
            parts = path.strip("/").split("/")
            slug_names = parts[-3].split("-vs-", maxsplit=1) if len(parts) >= 3 else []
            names = []
            for index, pid in enumerate(line.product_ids):
                name = product_docs[pid]["title"] if pid in product_docs else None
                names.append(name or (pretty(slug_names[index]) if len(slug_names) == 2 else f"Product {pid}"))
            documents.append(_override(D.compare_doc(path, tuple(line.product_ids), names, sum(
                product_docs[pid]["popularity"] if pid in product_docs else 1 for pid in line.product_ids)), line))

    add_product_categories(documents, records)
    return documents + build_use_case_documents(documents, records) + list(records.values())


def add_product_categories(documents: list[dict], records: dict[int, dict]) -> None:
    """Store each product's category page (id, title, path) on its product document."""
    pages = {d["path"].rstrip("/").rsplit("/", 1)[-1]: d for d in documents if d["type"] == "category"}
    for d in documents:
        rec = records.get(d["product_ids"][0]) if d["type"] == "product" else None
        slug = rec.get("category_link") if rec else None
        page = next((pages[s] for s in D.category_slug_candidates(slug) if s in pages), None) if slug else None
        if page:
            d.update(category_id=page["id"], category_title=page["title"], category_path=page["path"])


def build_use_case_documents(documents: list[dict], records: dict[int, dict]) -> list[dict]:
    """Use cases live on the product page ('/products/...#use-cases'), so two document types:
    'use_cases', one per product with its names in order; 'use_case', one per use case (UUID) with
    its most popular products. Products without a page are skipped (their records keep the list)."""
    out, by_id = [], {}
    for d in documents:
        rec = records.get(d["product_ids"][0]) if d["type"] == "product" else None
        entries = json.loads(rec["use_cases_json"]) if rec else []
        if not entries:
            continue
        out.append(D.use_cases_doc(d, entries))
        for e in entries:
            by_id.setdefault(e["id"], (e["name"], []))[1].append(d)
    out += [D.use_case_doc(uid, name, prods) for uid, (name, prods) in by_id.items()]
    return out


# ---- Typesense ----------------------------------------------------------------------------------
def schema(collection_name: str) -> dict:
    return {
        "name": collection_name,
        "fields": [
            {"name": "type", "type": "string", "facet": True},
            {"name": "title", "type": "string"},
            {"name": "path", "type": "string"},
            {"name": "product_names", "type": "string[]"},
            {"name": "product_ids", "type": "int32[]", "facet": True},
            {"name": "name_len", "type": "int32"},
            {"name": "popularity", "type": "int32"},
            # catalog API lookups: products having a use case / in a category / records naming a category
            {"name": "use_case_ids", "type": "string[]", "optional": True},
            {"name": "category_id", "type": "string", "optional": True},
            {"name": "category_link", "type": "string", "optional": True},
            {"name": "category_permas", "type": "string[]", "optional": True},
        ] + ([
            {"name": "use_case_text", "type": "string", "optional": True, "index": False},
            {"name": "embedding", "type": "float[]", "optional": True,   # docs without use_case_text: no vector
             "embed": {"from": ["use_case_text"], "model_config": {"model_name": EMBED_MODEL}}},
        ] if SEMANTIC_USE_CASES else []),
        "default_sorting_field": "popularity",
    }


def alias_exists() -> bool:
    try:
        client.aliases[ALIAS].retrieve()
        return True
    except ObjectNotFound:
        return False


def import_documents(collection_name: str, documents: list[dict], action: str = "upsert", log=print) -> None:
    for start in range(0, len(documents), BATCH_SIZE):
        batch = documents[start:start + BATCH_SIZE]
        results = client.collections[collection_name].documents.import_(batch, {"action": action})
        for line_number, result in enumerate(results, start=1):
            if not result.get("success"):
                raise RuntimeError(f"Typesense failed to import document in batch at line "
                                   f"{line_number}: {result.get('error', 'unknown error')}")
        log(f"{action}: {min(start + len(batch), len(documents))}/{len(documents)} documents")


def full_build(documents: list[dict], log=print, keep_previous: bool = False) -> str:
    """A new collection with every document, then the alias switches to it; older builds are deleted
    (keep_previous: all but the one that was live, so it can be switched back to)."""
    collection_name = f"{ALIAS}_build_{int(time.time())}"
    previous = client.aliases[ALIAS].retrieve()["collection_name"] if keep_previous and alias_exists() else None
    client.collections.create(schema(collection_name))
    try:
        import_documents(collection_name, documents, log=log)
        client.aliases.upsert(ALIAS, {"collection_name": collection_name})
    except Exception:
        client.collections[collection_name].delete()
        raise
    if previous:
        log(f"Previous collection kept: {previous}")
    for collection in client.collections.retrieve():
        if collection["name"].startswith(f"{ALIAS}_build_") and collection["name"] not in (collection_name, previous):
            client.collections[collection["name"]].delete()
            log(f"Deleted old collection {collection['name']}")
    return collection_name


def live_documents(**params):
    """Streams the live index (without embeddings)."""
    with requests.get(f"{TS_URL}/collections/{ALIAS}/documents/export", headers={"X-TYPESENSE-API-KEY": TS_KEY},
                      params={"exclude_fields": "embedding", **params}, stream=True, timeout=300) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line:
                yield json.loads(line)


def _fingerprint(doc: dict) -> bytes:
    return hashlib.sha1(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")).digest()


# ---- sync and export ----------------------------------------------------------------------------
def sync(source: str = SOURCE_URL, dry_run: bool = False, log=print) -> dict:
    """Make the live index match the source file. Only changed documents are written; a use case
    keeps its embedding unless renamed. Without an index yet, a full build. An invalid file
    changes nothing (ValueError)."""
    t0 = time.perf_counter()
    documents = build_documents(parse(read_source(source)))
    with catalog.LOCK:                                 # not while a catalog API change is running
        if not alias_exists():
            if not dry_run:
                full_build(documents, log)
            return {"mode": "full", "dry_run": dry_run, "documents": len(documents),
                    "took_s": round(time.perf_counter() - t0, 1)}

        current = {d["id"]: (d["type"], _fingerprint(d), d.get("use_case_text")) for d in live_documents()}
        creates, upserts, updates, unchanged = [], [], [], 0
        for doc in documents:
            old = current.pop(doc["id"], None)
            if old is None:
                creates.append(doc)
            elif old[1] == _fingerprint(doc):
                unchanged += 1
            elif doc.get("use_case_text") is not None and doc["use_case_text"] == old[2]:
                updates.append({k: v for k, v in doc.items() if k != "use_case_text"})   # keeps the embedding
            else:
                upserts.append(doc)                    # whole document replaced (renamed use case: re-embedded)
        deletes = list(current)

        if not dry_run:
            import_documents(ALIAS, creates + upserts, "upsert", log)
            import_documents(ALIAS, updates, "update", log)
            catalog._delete_ids(deletes)
            if deletes:
                log(f"delete: {len(deletes)} documents")
            search.topic_aliases.cache_clear()

    def by_type(types):
        counts = {}
        for t in types:
            counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items()))

    return {"mode": "diff", "dry_run": dry_run, "documents": len(documents),
            "created": by_type(d["type"] for d in creates),
            "updated": by_type(d["type"] for d in upserts + [{"type": u["type"]} for u in updates]),
            "deleted": by_type(old[0] for old in current.values()),
            "unchanged": unchanged, "took_s": round(time.perf_counter() - t0, 1)}


def rebuild(source: str = SOURCE_URL, log=print) -> dict:
    """A fresh index from the source file alone: a new collection (every use case embedded again);
    search switches to it when complete. The previous collection is kept for switching back."""
    t0 = time.perf_counter()
    documents = build_documents(parse(read_source(source)))
    with catalog.LOCK:
        collection_name = full_build(documents, log, keep_previous=True)
        search.topic_aliases.cache_clear()
    return {"mode": "rebuild", "collection": collection_name, "documents": len(documents),
            "took_s": round(time.perf_counter() - t0, 1)}


def export_source(path: str | Path = SOURCE_URL) -> int:
    """Write the live index as a source file. Titles are written only where they differ from the
    derived ones (so a product rename still renames its pages); derived popularity is not written."""
    records, products, pages = {}, {}, []
    for d in live_documents(filter_by="type:[record,product,news,customer_story,alternatives,compare,category,industry]"):
        if d["type"] == "record":
            records[d["product_ids"][0]] = d
        elif d["type"] == "product":
            products[d["product_ids"][0]] = d
        else:
            pages.append(d)

    def valid_path(d):
        try:
            return bool(_url(d["path"]))
        except ValueError:
            print(f"Warning: {d['id']} has no valid page path ({d['path']!r}): "
                  + ("written without url" if d["type"] == "product" else "left out"))
            return False

    pages = [d for d in pages if valid_path(d)]
    lines = []
    for pid in list(products) + [pid for pid in records if pid not in products]:   # index order: same derived data
        rec, prod = records.get(pid), products.get(pid)
        data = json.loads(rec["record_json"]) if rec else {}
        line = {"type": "product", "id": pid, "name": data.get("soft") or prod["title"],
                "url": BASE + prod["path"] if prod and valid_path(prod) else None}
        if rec and rec.get("category_link"):
            line["category"] = rec["category_link"]
        line.update({k: data[k] for k in ("cat", "cats") if data.get(k)})
        if rec and (use_cases := json.loads(rec["use_cases_json"])):
            line["use_cases"] = use_cases
        lines.append(line)
    for d in pages:
        line = {"type": d["type"], "url": BASE + d["path"]}
        if d["type"] == "compare":
            line["product_ids"] = d["product_ids"]
        elif d["product_ids"]:
            line["product_id"] = d["product_ids"][0]
        lines.append(line)

    derived = {d["id"]: d["title"] for d in build_documents(parse(lines)) if d["type"] in D.TOPIC_DIRS
               or d["type"] in ("news", "customer_story", "alternatives", "compare")}
    for d, line in zip(pages, lines[-len(pages):] if pages else []):
        if derived.get(d["id"]) != d["title"]:
            line["title"] = d["title"]

    out = ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if out.suffix == ".gz" else open
    with opener(out, "wt", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(lines)


def main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    if not args or args[0] not in ("export", "sync", "rebuild"):
        raise SystemExit(__doc__.split("\n\n")[-1])
    target = args[1] if len(args) > 1 else SOURCE_URL
    if args[0] == "export":
        print(f"Wrote {export_source(target)} lines to {ROOT / target}")
    elif args[0] == "rebuild":
        print(json.dumps(rebuild(target), indent=2))
    else:
        print(json.dumps(sync(target, dry_run="--dry-run" in argv), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
