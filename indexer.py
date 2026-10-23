"""Build the Typesense collection from the configured Cuspera sitemaps (initial build).

The sitemaps and prod.json / product_usecases.json / product_categories.json are turned into
source-file lines (scripts/source.py) and indexed like a source file. After that, changes go through
the catalog API (app/catalog_api.py) or a source-file sync (POST /api/sync): a rebuild replaces the
whole index, so it refuses to run while the search alias exists unless --force is given."""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

import requests

from config import ALIAS, BASE, SITEMAPS
from scripts.documents import unique_use_cases
from scripts.source import alias_exists, build_documents, client, full_build, parse
from scripts.Textutil import pretty

PRODUCTS_FILE = Path(__file__).parent / "prod.json"   # {"CFProdCode-<id>": {"tool_id", "soft", "cat"?, "cats"?}}
USE_CASES_FILE = Path(__file__).parent / "product_usecases.json"   # {"CFProdCode-<product id>": [{"id", "name"}, ...]}
CATEGORIES_FILE = Path(__file__).parent / "product_categories.json"   # {"CFProdCode-<product id>": "<category slug>"}
SESSION = requests.Session()                         # sitemap downloads only


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


def source_lines() -> list[dict]:
    """The sitemaps and the product JSON files as source-file lines."""
    def load(path):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    paths_by_kind = {
        kind: list(dict.fromkeys(path for url in urls for path in sitemap_paths(url)))
        for kind, urls in SITEMAPS.items()
    }
    print(", ".join(f"{kind}: {len(paths)}" for kind, paths in paths_by_kind.items()))

    page_paths = {}                                    # product id -> its product page
    for path in paths_by_kind["products"]:
        if match := re.search(r"-x-(\d+)$", path.rstrip("/").rsplit("/", 1)[-1]):
            page_paths[int(match.group(1))] = path

    products, use_cases, links = load(PRODUCTS_FILE), load(USE_CASES_FILE), load(CATEGORIES_FILE)
    lines = []
    for key, r in products.items():
        pid = int(key.rsplit("-", 1)[-1])
        path = page_paths.pop(pid, None)
        lines.append({"type": "product", "id": pid, "name": r["soft"], "url": BASE + path if path else None,
                      **({"category": links[key]} if links.get(key) else {}),
                      **({k: r[k] for k in ("cat", "cats") if r.get(k)}),
                      "use_cases": unique_use_cases(use_cases.get(key, []))})
    for pid, path in page_paths.items():               # product pages without a prod.json record: named by url
        lines.append({"type": "product", "id": pid, "url": BASE + path,
                      "name": pretty(re.sub(r"-x-\d+$", "", path.rstrip("/").rsplit("/", 1)[-1]))})

    for sitemap_kind, page_type in (("news", "news"), ("customer-story", "customer_story"),
                                    ("alternatives", "alternatives"), ("categories", "category"),
                                    ("industries", "industry")):
        for path in paths_by_kind[sitemap_kind]:
            match = re.search(r"/products/[^/]*-x-(\d+)/(?:news|customer-story|alternatives)$", path)
            lines.append({"type": page_type, "url": BASE + path,
                          **({"product_id": int(match.group(1))} if match else {})})

    for path in paths_by_kind["compare"]:
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
            lines.append({"type": "compare", "url": BASE + path, "product_ids": [int(parts[-2]), int(parts[-1])]})
    return lines


def main() -> None:
    if not client.operations.is_healthy():
        raise RuntimeError("Typesense is not healthy")
    if alias_exists() and "--force" not in sys.argv:
        raise SystemExit(f"Alias '{ALIAS}' exists: a rebuild would discard changes made through the catalog API. "
                         f"Re-run with --force to rebuild from the sitemaps anyway.")

    print("Fetching sitemap URLs...")
    documents = build_documents(parse(source_lines()))
    full_build(documents)
    print(f"Indexed {len(documents)} documents; search alias '{ALIAS}' is ready.")


if __name__ == "__main__":
    main()
