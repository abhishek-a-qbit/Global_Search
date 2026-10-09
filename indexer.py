"""Build the Typesense collection from the configured Cuspera sitemaps."""
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

import requests

from config import ALIAS, SITEMAPS, make_client
from scripts.Textutil import norm, pretty

BATCH_SIZE = 1000
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


PAGE_SLUGS = {"news": "News", "customer-story": "Customer Stories", "alternatives": "Alternatives"}


def _title(path: str) -> str:
    """Last path segment; a generic page slug ('/vendors/zoom/news') is named after its parent."""
    parts = [unquote(p) for p in path.strip("/").split("/")]
    if len(parts) > 1 and parts[-1] in PAGE_SLUGS:
        return f"{pretty(parts[-2])} {PAGE_SLUGS[parts[-1]]}"
    return pretty(parts[-1])


def _owner_popularity(path: str, names_by_first_word: dict[str, list[tuple[str, int]]]) -> int:
    """'/vendors/zoom/news' -> popularity of the vendor's most popular product ('Zoom Workplace')."""
    parts = path.strip("/").split("/")
    owner = norm(pretty(unquote(parts[-2]))) if len(parts) > 1 and parts[-1] in PAGE_SLUGS else ""
    if not owner:
        return 1
    return max((p for name, p in names_by_first_word.get(owner.split()[0], [])
                if name == owner or name.startswith(owner + " ")), default=1)


def build_documents() -> list[dict]:
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

    # Pages inherit their product's popularity, so "popular news" / "top comparisons" mean something
    for product_id, product in products_by_id.items():
        product["popularity"] = 1 + comparison_counts.get(product_id, 0)
    names_by_first_word = {}
    for p in products_by_id.values():
        name = norm(p["name"])
        if name:
            names_by_first_word.setdefault(name.split()[0], []).append((name, p["popularity"]))

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
            "popularity": product["popularity"],
        })

    product_page_suffix = {"news": "News", "customer_story": "Customer Stories", "alternatives": "Alternatives"}
    for sitemap_kind, document_type in (
        ("news", "news"),
        ("customer-story", "customer_story"),
        ("alternatives", "alternatives"),
        ("categories", "category"),
        ("industries", "industry"),
    ):
        for path in paths_by_kind[sitemap_kind]:
            path_hash = hashlib.sha1(path.encode("utf-8")).hexdigest()
            product_match = re.search(r"/products/[^/]*-x-(\d+)/(?:news|customer-story|alternatives)$", path)
            product_id = int(product_match.group(1)) if product_match else None
            product = products_by_id.get(product_id) if product_id is not None else None
            product_ids = [product_id] if product else []
            product_names = [product["name"]] if product else []
            title = (
                f"{product['name']} {product_page_suffix[document_type]}"
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
                "popularity": product["popularity"] if product else _owner_popularity(path, names_by_first_word),
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
            "popularity": sum(products_by_id.get(pid, {}).get("popularity", 1) for pid in pair),
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


def main() -> None:
    if not client.operations.is_healthy():
        raise RuntimeError("Typesense is not healthy")

    print("Fetching sitemap URLs...")
    documents = build_documents()
    if not documents:
        raise RuntimeError("No documents were found in the configured sitemaps")

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
