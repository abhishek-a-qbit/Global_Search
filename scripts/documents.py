"""Search document builders, shared by the full indexer and the catalog API (same shapes)."""
import hashlib
import json
import re
from urllib.parse import unquote

from scripts.Textutil import norm, pretty

PAGE_SLUGS = {"news": "News", "customer-story": "Customer Stories", "alternatives": "Alternatives"}
PAGE_KINDS = {"news": "news", "customer_story": "customer-story", "alternatives": "alternatives"}   # type -> slug
PAGE_SUFFIX = {kind: PAGE_SLUGS[slug] for kind, slug in PAGE_KINDS.items()}
TOPIC_DIRS = {"category": "categories", "industry": "industries"}
CATEGORY_SLUG_FIXES = {"abm": "account-based-marketing"}
USE_CASE_TOP_PRODUCTS = 5


def path_id(kind: str, path: str) -> str:
    return f"{kind}-{hashlib.sha1(path.encode('utf-8')).hexdigest()}"


def use_case_id(uuid: str) -> str:
    return f"use_case-{uuid}"


def tool_id(product_id: int) -> str:
    return f"CFProdCode-{product_id}"


def page_title(path: str) -> str:
    """Last path segment; a generic page slug ('/vendors/zoom/news') is named after its parent."""
    parts = [unquote(p) for p in path.strip("/").split("/")]
    if len(parts) > 1 and parts[-1] in PAGE_SLUGS:
        parent = re.sub(r"-x-\d+$", "", parts[-2])       # '/products/acme-x-12/news' -> 'Acme News'
        return f"{pretty(parent)} {PAGE_SLUGS[parts[-1]]}"
    return pretty(parts[-1])


def page_owner(path: str) -> str:
    """'/vendors/zoom/news' -> 'zoom' (normalized vendor name); '' for other pages."""
    parts = path.strip("/").split("/")
    return norm(pretty(unquote(parts[-2]))) if len(parts) > 1 and parts[-1] in PAGE_SLUGS else ""


def category_slug_candidates(slug: str) -> list[str]:
    slug = CATEGORY_SLUG_FIXES.get(slug, slug)
    return [slug, f"{slug}s"]                          # 'payment-gateway' -> '/categories/payment-gateways'


def product_doc(product_id: int, name: str, path: str, popularity: int, category: dict | None = None) -> dict:
    doc = {
        "id": f"product-{product_id}",
        "type": "product",
        "title": name,
        "path": path,
        "product_names": [name],
        "product_ids": [product_id],
        "name_len": len(name),
        "popularity": popularity,
    }
    if category:                                       # the category page document
        doc.update(category_id=category["id"], category_title=category["title"], category_path=category["path"])
    return doc


def page_doc(kind: str, path: str, product: dict | None = None, popularity: int = 1) -> dict:
    """news / customer_story / alternatives (of a product, or vendor-level), category, industry."""
    title = f"{product['title']} {PAGE_SUFFIX[kind]}" if product else page_title(path)
    return {
        "id": path_id(kind, path),
        "type": kind,
        "title": title,
        "path": path,
        "product_names": list(product["product_names"]) if product else [],
        "product_ids": list(product["product_ids"]) if product else [],
        "name_len": len(title),
        "popularity": product["popularity"] if product else popularity,
    }


def compare_doc(path: str, pair: tuple[int, int], names: list[str], popularity: int) -> dict:
    title = f"{names[0]} vs {names[1]}"
    return {
        "id": path_id("compare", path),
        "type": "compare",
        "title": title,
        "path": path,
        "product_names": list(names),
        "product_ids": list(pair),
        "name_len": len(title),
        "popularity": popularity,
    }


def unique_use_cases(entries) -> list[dict]:
    """[{id, name}] without repeated ids or blank names, in order."""
    seen, out = set(), []
    for e in entries:
        uid, name = str(e["id"]).strip(), str(e["name"]).strip()
        if uid and norm(name) and uid not in seen:
            seen.add(uid)
            out.append({"id": uid, "name": name})
    return out


def record_doc(product_id: int, soft: str, cat: dict | None, cats: list[dict], category_link: str | None,
               use_cases: list[dict]) -> dict:
    """Hidden source record of a product (a prod.json entry + its product_usecases.json list), kept
    whether or not the product has a page. Searches never query this type."""
    data = {"tool_id": tool_id(product_id), "soft": soft, **({"cat": cat} if cat else {}), **({"cats": cats} if cats else {})}
    doc = {
        "id": f"record-{product_id}",
        "type": "record",
        "title": soft,
        "path": "",
        "product_names": [soft],
        "product_ids": [product_id],
        "name_len": len(soft),
        "popularity": 1,
        "record_json": json.dumps(data, ensure_ascii=False),
        "use_cases_json": json.dumps(use_cases, ensure_ascii=False),
        "use_case_ids": [e["id"] for e in use_cases],
        "category_permas": [c["perma"] for c in ([cat] if cat else []) + cats],
    }
    if category_link:                                  # category slug shown for the product
        doc["category_link"] = category_link
    return doc


def use_cases_doc(product: dict, use_cases: list[dict]) -> dict:
    """One per product with a page: its use-case names, linked to the page's use-case section."""
    title = f"{product['title']} Use Cases"
    names = {}
    for e in use_cases:
        names.setdefault(norm(e["name"]), e["name"])
    return {
        "id": f"use_cases-{product['product_ids'][0]}",
        "type": "use_cases",
        "title": title,
        "path": f"{product['path']}#use-cases",
        "product_names": product["product_names"],
        "product_ids": product["product_ids"],
        "name_len": len(title),
        "popularity": product["popularity"],
        "use_case_names": list(names.values()),
        "use_case_ids": [e["id"] for e in use_cases],
    }


def use_case_doc(uuid: str, name: str, products: list[dict], count: int | None = None) -> dict:
    """One per use case (UUID), with its most popular products (product-like dicts: product_ids,
    popularity, name_len, path). count: products having it, if only the top ones are passed."""
    top = sorted(products, key=lambda p: (-p["popularity"], p["name_len"]))[:USE_CASE_TOP_PRODUCTS]
    return {
        "id": use_case_id(uuid),
        "type": "use_case",
        "title": name,
        "path": f"{top[0]['path']}#use-cases",
        "product_names": [],
        "product_ids": [p["product_ids"][0] for p in top],
        "name_len": len(name),
        "popularity": count if count is not None else len(products),
        "use_case_text": name,                         # source of the semantic embedding (use_case docs only)
    }
