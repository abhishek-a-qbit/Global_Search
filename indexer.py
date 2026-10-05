"""Build the Typesense collection from the configured Cuspera sitemaps."""
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

import requests

from config import ALIAS, HEADERS, SITEMAPS, TS_URL
from scripts.Textutil import pretty

BATCH_SIZE = 1000
SESSION = requests.Session()


def sitemap_paths(url: str) -> list[str]:
    response = SESSION.get(url, timeout=60)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    paths = [
        urlsplit(element.text.strip()).path
        for element in root.iter()
        if element.tag.endswith("loc") and element.text and urlsplit(element.text.strip()).netloc
    ]
    return list(dict.fromkeys(paths))


def _title(path: str) -> str:
    slug = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return pretty(slug)


def build_documents() -> list[dict]:
    paths_by_kind = {
        kind: sitemap_paths(url)
        for kind, url in SITEMAPS.items()
    }

    products_by_id = {}
    comparison_pairs = []
    for path in paths_by_kind["products"]:
        match = re.search(r"-x-(\d+)$", path.rstrip("/").rsplit("/", 1)[-1])
        if not match:
            continue
        product_id = int(match.group(1))
        name = pretty(re.sub(r"-x-\d+$", "", path.rstrip("/").rsplit("/", 1)[-1]))
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

    documents = []
    for product_id, product in products_by_id.items():
        documents.append({
            "id": f"product-{product_id}",
            "type": "product",
            "title": product["name"],
            "path": product["path"],
            "product_names": [product["name"]],
            "product_ids": [product_id],
            "name_len": len(product["name"]),
            "popularity": 1 + comparison_counts.get(product_id, 0),
        })

    for sitemap_kind, document_type in (
        ("news", "news"),
        ("customer-story", "customer_story"),
        ("categories", "category"),
    ):
        for path in paths_by_kind[sitemap_kind]:
            path_hash = hashlib.sha1(path.encode("utf-8")).hexdigest()
            product_match = re.search(r"/products/[^/]*-x-(\d+)/(?:news|customer-story)$", path)
            product_id = int(product_match.group(1)) if product_match else None
            product = products_by_id.get(product_id) if product_id is not None else None
            product_ids = [product_id] if product else []
            product_names = [product["name"]] if product else []
            title = (
                f"{product['name']} {('News' if document_type == 'news' else 'Customer Stories')}"
                if product
                else _title(path)
            )
            documents.append({
                "id": f"{document_type}-{path_hash}",
                "type": document_type,
                "title": title,
                "path": path,
                "product_names": product_names,
                "product_ids": product_ids,
                "name_len": len(title),
                "popularity": 1,
            })

    for path, pair in comparison_pairs:
        comparison_slug = path.strip("/").split("/")[-3]
        slug_names = comparison_slug.split("-vs-", maxsplit=1)
        names = []
        for index, product_id in enumerate(pair):
            name = products_by_id.get(product_id, {}).get("name")
            if not name:
                name = pretty(slug_names[index]) if len(slug_names) == 2 else f"Product {product_id}"
            names.append(name)
        title = f"{names[0]} vs {names[1]}"
        path_hash = hashlib.sha1(path.encode("utf-8")).hexdigest()
        documents.append({
            "id": f"compare-{path_hash}",
            "type": "compare",
            "title": title,
            "path": path,
            "product_names": names,
            "product_ids": list(pair),
            "name_len": len(title),
            "popularity": 1,
        })

    return documents


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
        ],
        "default_sorting_field": "popularity",
    }


def _index_documents(collection_name: str, documents: list[dict]) -> None:
    for start in range(0, len(documents), BATCH_SIZE):
        batch = documents[start:start + BATCH_SIZE]
        response = SESSION.post(
            f"{TS_URL}/collections/{collection_name}/documents/import",
            params={"action": "upsert"},
            headers={**HEADERS, "Content-Type": "text/plain"},
            data="\n".join(json.dumps(document) for document in batch),
            timeout=120,
        )
        response.raise_for_status()
        for line_number, line in enumerate(response.text.splitlines(), start=1):
            result = json.loads(line)
            if not result.get("success"):
                raise RuntimeError(
                    f"Typesense failed to import document in batch at line "
                    f"{line_number}: {result.get('error', 'unknown error')}"
                )
        print(f"Indexed {min(start + len(batch), len(documents))}/{len(documents)} documents")


def main() -> None:
    health = SESSION.get(f"{TS_URL}/health", timeout=5)
    health.raise_for_status()

    print("Fetching sitemap URLs...")
    documents = build_documents()
    if not documents:
        raise RuntimeError("No documents were found in the configured sitemaps")

    collection_name = f"{ALIAS}_build_{int(time.time())}"
    create = SESSION.post(
        f"{TS_URL}/collections",
        headers=HEADERS,
        json=_schema(collection_name),
        timeout=10,
    )
    create.raise_for_status()

    try:
        _index_documents(collection_name, documents)
        alias = SESSION.put(
            f"{TS_URL}/aliases/{ALIAS}",
            headers=HEADERS,
            json={"collection_name": collection_name},
            timeout=10,
        )
        alias.raise_for_status()
    except Exception:
        SESSION.delete(
            f"{TS_URL}/collections/{collection_name}",
            headers=HEADERS,
            timeout=10,
        )
        raise

    print(f"Indexed {len(documents)} documents; search alias '{ALIAS}' is ready.")


if __name__ == "__main__":
    main()
