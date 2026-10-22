"""Build the Typesense collection from the configured Cuspera sitemaps (initial build).

After that, changes go through the catalog API (app/catalog_api.py): a rebuild replaces the
whole index, so it refuses to run while the search alias exists unless --force is given."""
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

import requests
from typesense.exceptions import ObjectNotFound

from config import ALIAS, EMBED_MODEL, SEMANTIC_USE_CASES, SITEMAPS, make_client
from scripts.documents import (category_slug_candidates, compare_doc, page_doc, page_owner, product_doc,
                               record_doc, unique_use_cases, use_case_doc, use_cases_doc)
from scripts.Textutil import norm, pretty

BATCH_SIZE = 1000
PRODUCTS_FILE = Path(__file__).parent / "prod.json"   # {"CFProdCode-<id>": {"tool_id", "soft", "cat"?, "cats"?}}
USE_CASES_FILE = Path(__file__).parent / "product_usecases.json"   # {"CFProdCode-<product id>": [{"id", "name"}, ...]}
CATEGORIES_FILE = Path(__file__).parent / "product_categories.json"   # {"CFProdCode-<product id>": "<category slug>"}
SESSION = requests.Session()                         # sitemap downloads only
client = make_client(timeout_seconds=120)            # bulk imports can be slow


def sitemap_paths(url: str) -> list[str]:
    """Page paths in a sitemap, following nested sitemap indexes."""
    response = SESSION.get(url, timeout=60)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    locs = [
        element.text.strip()
        for element in root.iter()
        if element.tag.endswith("loc") and element.text and urlsplit(element.text.strip()).netloc
    ]
    if root.tag.endswith("sitemapindex"):
        return list(dict.fromkeys(path for loc in locs for path in sitemap_paths(loc)))
    return list(dict.fromkeys(urlsplit(loc).path for loc in locs))


def _owner_popularity(path: str, names_by_first_word: dict[str, list[tuple[str, int]]]) -> int:
    """'/vendors/zoom/news' -> popularity of the vendor's most popular product ('Zoom Workplace')."""
    owner = page_owner(path)
    if not owner:
        return 1
    return max((p for name, p in names_by_first_word.get(owner.split()[0], [])
                if name == owner or name.startswith(owner + " ")), default=1)


def build_records() -> dict[int, dict]:
    """Hidden source records: every prod.json product with its use cases and category link
    (product_categories.json), whether or not it has a page."""
    def load(path):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    products, use_cases, links = load(PRODUCTS_FILE), load(USE_CASES_FILE), load(CATEGORIES_FILE)
    records = {}
    for key, r in products.items():
        pid = int(key.rsplit("-", 1)[-1])
        records[pid] = record_doc(pid, r["soft"].strip(), r.get("cat"), r.get("cats", []), links.get(key),
                                  unique_use_cases(use_cases.get(key, [])))
    return records


def category_names(records: dict[int, dict]) -> dict[str, str]:
    """Category slug -> name, from the products' cat / cats."""
    names = {}
    for rec in records.values():
        data = json.loads(rec["record_json"])
        for c in ([data["cat"]] if "cat" in data else []) + data.get("cats", []):
            names.setdefault(c["perma"], c["name"].strip())
    return names


def build_documents(records: dict[int, dict]) -> list[dict]:
    paths_by_kind = {
        kind: list(dict.fromkeys(path for url in urls for path in sitemap_paths(url)))
        for kind, urls in SITEMAPS.items()
    }
    print(", ".join(f"{kind}: {len(paths)}" for kind, paths in paths_by_kind.items()))

    products_by_id = {}
    comparison_pairs = []
    for path in paths_by_kind["products"]:
        match = re.search(r"-x-(\d+)$", path.rstrip("/").rsplit("/", 1)[-1])
        if not match:
            continue
        product_id = int(match.group(1))
        name = records[product_id]["title"] if product_id in records else \
            pretty(re.sub(r"-x-\d+$", "", path.rstrip("/").rsplit("/", 1)[-1]))
        products_by_id[product_id] = {"name": name, "path": path}

    comparison_counts = {}
    for path in paths_by_kind["compare"]:
        parts = path.strip("/").split("/")
        if len(parts) < 3 or not parts[-1].isdigit() or not parts[-2].isdigit():
            continue
        pair = (int(parts[-2]), int(parts[-1]))
        comparison_pairs.append((path, pair))
        for product_id in pair:
            comparison_counts[product_id] = comparison_counts.get(product_id, 0) + 1

    # Pages inherit their product's popularity, so "popular news" / "top comparisons" mean something
    for product_id, product in products_by_id.items():
        product["popularity"] = 1 + comparison_counts.get(product_id, 0)
    names_by_first_word = {}
    for p in products_by_id.values():
        name = norm(p["name"])
        if name:
            names_by_first_word.setdefault(name.split()[0], []).append((name, p["popularity"]))

    topic_names = category_names(records)
    product_docs = {pid: product_doc(pid, p["name"], p["path"], p["popularity"]) for pid, p in products_by_id.items()}
    documents = list(product_docs.values())

    for sitemap_kind, document_type in (
        ("news", "news"),
        ("customer-story", "customer_story"),
        ("alternatives", "alternatives"),
        ("categories", "category"),
        ("industries", "industry"),
    ):
        for path in paths_by_kind[sitemap_kind]:
            product_match = re.search(r"/products/[^/]*-x-(\d+)/(?:news|customer-story|alternatives)$", path)
            product = product_docs.get(int(product_match.group(1))) if product_match else None
            doc = page_doc(document_type, path, product, popularity=_owner_popularity(path, names_by_first_word))
            if document_type == "category" and (title := topic_names.get(path.rstrip("/").rsplit("/", 1)[-1])):
                doc.update(title=title, name_len=len(title))
            documents.append(doc)

    for path, pair in comparison_pairs:
        comparison_slug = path.strip("/").split("/")[-3]
        slug_names = comparison_slug.split("-vs-", maxsplit=1)
        names = []
        for index, product_id in enumerate(pair):
            name = products_by_id.get(product_id, {}).get("name")
            if not name:
                name = pretty(slug_names[index]) if len(slug_names) == 2 else f"Product {product_id}"
            names.append(name)
        documents.append(compare_doc(path, pair, names,
                                     sum(products_by_id.get(pid, {}).get("popularity", 1) for pid in pair)))

    return documents


def add_product_categories(documents: list[dict], records: dict[int, dict]) -> None:
    """Store each product's category page (id, title, path) on its product document."""
    pages = {d["path"].rstrip("/").rsplit("/", 1)[-1]: d for d in documents if d["type"] == "category"}
    for d in documents:
        rec = records.get(d["product_ids"][0]) if d["type"] == "product" else None
        slug = rec.get("category_link") if rec else None
        page = next((pages[s] for s in category_slug_candidates(slug) if s in pages), None) if slug else None
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
        out.append(use_cases_doc(d, entries))
        for e in entries:
            by_id.setdefault(e["id"], (e["name"], []))[1].append(d)
    out += [use_case_doc(uid, name, prods) for uid, (name, prods) in by_id.items()]
    return out


def _schema(collection_name: str) -> dict:
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


def _index_documents(collection_name: str, documents: list[dict]) -> None:
    for start in range(0, len(documents), BATCH_SIZE):
        batch = documents[start:start + BATCH_SIZE]
        results = client.collections[collection_name].documents.import_(batch, {"action": "upsert"})
        for line_number, result in enumerate(results, start=1):
            if not result.get("success"):
                raise RuntimeError(
                    f"Typesense failed to import document in batch at line "
                    f"{line_number}: {result.get('error', 'unknown error')}"
                )
        print(f"Indexed {min(start + len(batch), len(documents))}/{len(documents)} documents")


def _delete_old_builds(keep: str) -> None:
    for collection in client.collections.retrieve():
        name = collection["name"]
        if name.startswith(f"{ALIAS}_build_") and name != keep:
            client.collections[name].delete()
            print(f"Deleted old collection {name}")


def _alias_exists() -> bool:
    try:
        client.aliases[ALIAS].retrieve()
        return True
    except ObjectNotFound:
        return False


def main() -> None:
    if not client.operations.is_healthy():
        raise RuntimeError("Typesense is not healthy")
    if _alias_exists() and "--force" not in sys.argv:
        raise SystemExit(f"Alias '{ALIAS}' exists: a rebuild would discard changes made through the catalog API. "
                         f"Re-run with --force to rebuild from the sitemaps anyway.")

    print("Fetching sitemap URLs...")
    records = build_records()
    documents = build_documents(records)
    if not documents:
        raise RuntimeError("No documents were found in the configured sitemaps")
    add_product_categories(documents, records)
    documents += build_use_case_documents(documents, records) + list(records.values())

    collection_name = f"{ALIAS}_build_{int(time.time())}"
    client.collections.create(_schema(collection_name))

    try:
        _index_documents(collection_name, documents)
        client.aliases.upsert(ALIAS, {"collection_name": collection_name})
    except Exception:
        client.collections[collection_name].delete()
        raise

    _delete_old_builds(keep=collection_name)
    print(f"Indexed {len(documents)} documents; search alias '{ALIAS}' is ready.")


if __name__ == "__main__":
    main()
