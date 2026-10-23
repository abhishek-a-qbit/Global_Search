"""Catalog changes on the live index, shaped like the source data:
- product records (prod.json entries) and their use-case lists (product_usecases.json entries),
  stored as hidden 'record' documents whether or not the product has a page;
- website pages, given by URL like a sitemap entry (product pages make products searchable).
Every change also rewrites what is derived from it: popularity (1 + comparisons, inherited by pages
and comparisons), titles, each use case's top products, vendor page popularity, product categories."""
import json
import re
import threading
from urllib.parse import urlsplit

from typesense.exceptions import ObjectNotFound

from config import ALIAS, BASE, make_client
from scripts import documents as D
from scripts import search
from scripts.Textutil import norm, pretty

client = make_client(timeout_seconds=60)
LOCK = threading.RLock()                   # one write at a time: derived data is read-modify-write
CHUNK = 100                                # ids per filter (export filters travel in the URL)
VENDOR_KINDS = ",".join(D.PAGE_KINDS)
PRODUCT_PAGES = f"{VENDOR_KINDS},use_cases"


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


UNSET = object()                                     # PATCH: field not sent (vs sent as null)


# ---- Typesense helpers -------------------------------------------------------
def _docs():
    return client.collections[ALIAS].documents


def _get(doc_id: str) -> dict | None:
    try:
        doc = _docs()[doc_id].retrieve()
    except ObjectNotFound:
        return None
    doc.pop("embedding", None)
    return doc


def _find(filter_by: str, include: str | None = None) -> list[dict]:
    params = {"filter_by": filter_by, "exclude_fields": "embedding", **({"include_fields": include} if include else {})}
    return [json.loads(line) for line in _docs().export(params).splitlines() if line.strip()]


def _msearch(searches: list[dict]) -> list[dict]:
    out = []
    for chunk in _chunks(searches, 50):
        res = client.multi_search.perform(
            {"searches": [{"collection": ALIAS, "exclude_fields": "embedding", **s} for s in chunk]}, {})
        for r in res["results"]:
            if "error" in r:
                raise RuntimeError(r["error"])
            out.append(r)
    return out


def _count(filter_by: str) -> int:
    return _msearch([dict(q="*", filter_by=filter_by, per_page=0)])[0]["found"]


def _write(docs: list[dict], action: str = "upsert") -> None:
    for chunk in _chunks(docs, 1000):
        for r in _docs().import_(chunk, {"action": action}):
            if not r.get("success"):
                raise RuntimeError(f"Typesense import failed: {r.get('error')}")


def _delete(filter_by: str) -> None:
    _docs().delete({"filter_by": filter_by})


def _delete_ids(ids) -> None:
    for chunk in _chunks(ids):
        _delete(f"id:[{_ids(chunk)}]")


def _chunks(items, n: int = CHUNK):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _ids(items) -> str:
    return ",".join(f"`{i}`" if isinstance(i, str) else str(i) for i in items)


def _clean_name(name: str) -> str:
    if not norm(name):
        raise ValueError("Name must contain letters or digits")
    return name.strip()


def page_view(d: dict) -> dict:
    return {"type": d["type"], "title": d["title"], "url": BASE + d["path"], "popularity": d["popularity"]}


# ---- records ------------------------------------------------------------------
def _record(pid: int) -> dict:
    if not (rec := _get(f"record-{pid}")):
        raise NotFound(f"Product {D.tool_id(pid)} not found")
    return rec


def _records(pids) -> dict[int, dict]:
    return {d["product_ids"][0]: d for chunk in _chunks(set(pids))
            for d in _find(f"type:=record && product_ids:[{_ids(chunk)}]")}


def _use_cases(rec: dict | None) -> list[dict]:
    return json.loads(rec["use_cases_json"]) if rec else []


def _with(rec: dict, **changes) -> dict:
    """The record rebuilt with some fields replaced (soft, cat, cats, category_link, use_cases)."""
    data = json.loads(rec["record_json"])
    fields = {"soft": data["soft"], "cat": data.get("cat"), "cats": data.get("cats", []),
              "category_link": rec.get("category_link"), "use_cases": _use_cases(rec), **changes}
    return D.record_doc(rec["product_ids"][0], **fields)


# ---- derived data -------------------------------------------------------------
def _products(pids) -> dict[int, dict]:
    return {d["product_ids"][0]: d for chunk in _chunks(set(pids))
            for d in _find(f"type:=product && product_ids:[{_ids(chunk)}]")}


def _category_page(slug: str | None) -> dict | None:
    for s in D.category_slug_candidates(slug) if slug else []:
        if doc := _get(D.path_id("category", f"/categories/{s}")):
            return doc
    return None


def _slug_name(path: str) -> str:
    return pretty(re.sub(r"-x-\d+$", "", path.rstrip("/").rsplit("/", 1)[-1]))


def _product_doc(pid: int, path: str, popularity: int, rec: dict | None) -> dict:
    """Name and category from the record; without one, the name comes from the URL."""
    return D.product_doc(pid, rec["title"] if rec else _slug_name(path), path, popularity,
                         _category_page(rec.get("category_link")) if rec else None)


def _update_popularity(pids) -> None:
    """Popularity = 1 + number of comparisons; on change, rewrite everything that inherits it."""
    prods = list(_products(pids).values())
    counts = _msearch([dict(q="*", filter_by=f"type:=compare && product_ids:={p['product_ids'][0]}", per_page=0)
                       for p in prods])
    changed = [dict(p, popularity=1 + r["found"]) for p, r in zip(prods, counts) if 1 + r["found"] != p["popularity"]]
    _write(changed)
    _propagate(changed)


def _propagate(prods: list[dict], old_names=()) -> None:
    """Products changed (new page, name, path or popularity): rewrite their pages, use cases,
    comparisons and the vendor pages named after them."""
    by_id = {p["product_ids"][0]: p for p in prods}
    pages = [d for chunk in _chunks(by_id) for d in _find(f"type:[{VENDOR_KINDS}] && product_ids:[{_ids(chunk)}]")]
    _write([D.page_doc(d["type"], d["path"], by_id[d["product_ids"][0]]) for d in pages])
    recs = _records(by_id)
    for pid, prod in by_id.items():
        _sync_use_cases(prod, _use_cases(recs.get(pid)))
    _refresh_compares(by_id)
    _refresh_vendor_pages([p["title"] for p in prods] + list(old_names))


def _sync_use_cases(prod: dict, entries: list[dict], old_ids=()) -> None:
    """A searchable product's use-case list document, and the use cases it touches."""
    pid = prod["product_ids"][0]
    if entries:
        _write([D.use_cases_doc(prod, entries)])
    else:
        _delete_ids([f"use_cases-{pid}"])
    _refresh_use_cases([*old_ids, *(e["id"] for e in entries)], {e["id"]: e["name"] for e in entries})


def _refresh_compares(pids) -> None:
    comps = {d["id"]: d for chunk in _chunks(pids) for d in _find(f"type:=compare && product_ids:[{_ids(chunk)}]")}
    prods = _products({i for c in comps.values() for i in c["product_ids"]})
    out = []
    for c in comps.values():
        pair = c["product_ids"]
        names = [prods[i]["title"] if i in prods else c["product_names"][k] for k, i in enumerate(pair)]
        new = D.compare_doc(c["path"], tuple(pair), names, sum(prods[i]["popularity"] if i in prods else 1 for i in pair))
        if new != c:
            out.append(new)
    _write(out)


def _refresh_use_cases(ids, names: dict[str, str] | None = None, rename: dict[str, str] | None = None) -> None:
    """Recompute each use case's product count and top products (searchable products only); drop it
    when none has it. Title: rename > existing > names. 'emplace' keeps the embedding unless renamed."""
    ids = list(dict.fromkeys(ids))
    existing = {d["id"]: d for chunk in _chunks([D.use_case_id(i) for i in ids]) for d in _find(f"id:[{_ids(chunk)}]")}
    results = _msearch([dict(q="*", filter_by=f"type:=use_cases && use_case_ids:=[`{i}`]",
                             sort_by="popularity:desc,name_len:asc", per_page=D.USE_CASE_TOP_PRODUCTS,
                             include_fields="product_ids,product_names,popularity,path") for i in ids])
    writes, deletes = [], []
    for uid, r in zip(ids, results):
        old = existing.get(D.use_case_id(uid))
        if not r["found"]:
            deletes += [old["id"]] if old else []
            continue
        top = [{"product_ids": h["document"]["product_ids"], "popularity": h["document"]["popularity"],
                "name_len": len(h["document"]["product_names"][0]),
                "path": h["document"]["path"].removesuffix("#use-cases")} for h in r["hits"]]
        title = (rename or {}).get(uid) or (old["title"] if old else (names or {}).get(uid, uid))
        new = D.use_case_doc(uid, title, top, count=r["found"])
        if new != old:
            writes.append(new)
    _write(writes, "emplace")
    _delete_ids(deletes)


def _vendor_popularity(owner: str) -> int:
    """Popularity of the vendor's most popular product ('zoom' -> 'Zoom Workplace')."""
    r = _msearch([dict(q=owner, query_by="product_names", filter_by="type:=product", prefix=False, num_typos=0,
                       drop_tokens_threshold=0, sort_by="popularity:desc", per_page=250,
                       include_fields="title,popularity")])[0]
    return max((h["document"]["popularity"] for h in r["hits"]
                if (n := norm(h["document"]["title"])) == owner or n.startswith(owner + " ")), default=1)


def _refresh_vendor_pages(product_names) -> None:
    names = {n for n in map(norm, product_names) if n}
    if not names:
        return
    results = _msearch([dict(q=w, query_by="title", filter_by=f"type:[{VENDOR_KINDS}]", prefix=False, num_typos=0,
                             per_page=100) for w in sorted({n.split()[0] for n in names})])
    pages = {h["document"]["id"]: h["document"] for r in results for h in r["hits"] if not h["document"]["product_ids"]}
    out = []
    for d in pages.values():
        owner = D.page_owner(d["path"])
        if owner and any(n == owner or n.startswith(owner + " ") for n in names):
            pop = _vendor_popularity(owner)
            if pop != d["popularity"]:
                out.append(dict(d, popularity=pop))
    _write(out)


def _relink_category(page: dict, category_id: str | None = None) -> None:
    """Products whose category page was added / renamed / removed: refresh their category link."""
    if category_id:                        # removed: products pointing at it lose the link
        prods = _find(f"type:=product && category_id:=`{category_id}`")
        _write([D.product_doc(p["product_ids"][0], p["title"], p["path"], p["popularity"]) for p in prods])
    else:                                  # added / renamed: products whose record names this category
        slug = page["path"].rsplit("/", 1)[-1]
        links = {slug, slug.removesuffix("s"), *(k for k, v in D.CATEGORY_SLUG_FIXES.items() if v == slug)}
        recs = _find(f"type:=record && category_link:[{_ids(links)}]", include="product_ids")
        prods = _products(r["product_ids"][0] for r in recs).values()
        _write([D.product_doc(p["product_ids"][0], p["title"], p["path"], p["popularity"], page) for p in prods])
    search.topic_aliases.cache_clear()     # abbreviations ('abm') are built from titles


def _name_categories(categories: list[dict]) -> None:
    """Category pages take their names from product records ({perma, name})."""
    for c in categories:
        page = _category_page(c["perma"])
        if page and norm(c["name"]) and page["title"] != c["name"].strip():
            page = dict(page, title=c["name"].strip(), name_len=len(c["name"].strip()))
            _write([page])
            _relink_category(page)


# ---- products (prod.json entries) ---------------------------------------------
def get_product(pid: int) -> dict:
    rec = _record(pid)
    prod = _get(f"product-{pid}")
    pages = _find(f"type:[{VENDOR_KINDS}] && product_ids:={pid}", include="type")
    return {**json.loads(rec["record_json"]), "category": rec.get("category_link"),
            "use_cases": _use_cases(rec), "searchable": bool(prod),
            "url": BASE + prod["path"] if prod else None,
            "popularity": prod["popularity"] if prod else None,
            "pages": [D.PAGE_KINDS[p["type"]] for p in pages],
            "comparisons": _count(f"type:=compare && product_ids:={pid}")}


def _save_record(pid: int, soft: str, cat: dict | None, cats: list[dict],
                 category_link: str | None, use_cases: list[dict]) -> dict:
    """Write the record and, if the product has a page, the product document (and whatever the
    new name / category changes). Also lets category pages pick up the record's names."""
    rec = D.record_doc(pid, soft, cat, cats, category_link, use_cases)
    _write([rec])
    if prod := _get(f"product-{pid}"):
        doc = _product_doc(pid, prod["path"], prod["popularity"], rec)
        _write([doc])
        if doc["title"] != prod["title"]:
            _propagate([doc], old_names=[prod["title"]])
    _name_categories(([cat] if cat else []) + cats)
    return rec


def _publish_product(pid: int, path: str) -> None:
    """Create or move the product's page; the product becomes searchable."""
    old = _get(f"product-{pid}")
    pop = old["popularity"] if old else 1 + _count(f"type:=compare && product_ids:={pid}")
    doc = _product_doc(pid, path, pop, _get(f"record-{pid}"))
    _write([doc])
    orphans = [p for kind, slug in D.PAGE_KINDS.items()                # its pages added before it
               if (p := _get(D.path_id(kind, f"{path}/{slug}"))) and not p["product_ids"]]
    _write([D.page_doc(o["type"], o["path"], doc) for o in orphans])
    _propagate([doc], old_names=[old["title"]] if old else [])


def _unpublish_product(doc: dict) -> None:
    """Remove the product page: the product is hidden again; its sub-pages become un-owned."""
    pid = doc["product_ids"][0]
    pages = _find(f"type:[{VENDOR_KINDS}] && product_ids:={pid}")
    _write([D.page_doc(p["type"], p["path"]) for p in pages])
    _delete_ids([f"use_cases-{pid}", doc["id"]])
    rec = _get(f"record-{pid}")
    _refresh_use_cases(rec["use_case_ids"] if rec else [])
    _refresh_vendor_pages([doc["title"]])


def create_product(pid: int, soft: str, url: str | None = None) -> dict:
    """Create the product record (an empty use-case list). With a url, its page is published
    and it is searchable at once."""
    with LOCK:
        if _get(f"record-{pid}"):
            raise Conflict(f"Product {D.tool_id(pid)} already exists")
        _save_record(pid, _clean_name(soft), None, [], None, [])
        if url:
            _publish_product(pid, _path_of(url))
        return get_product(pid)


def update_product(pid: int, soft: str | None = None, url=UNSET) -> dict:
    """Change fields of the record. url omitted: kept; url null: the product page is removed
    (hidden, record kept)."""
    with LOCK:
        rec = _record(pid)
        data = json.loads(rec["record_json"])
        _save_record(pid, _clean_name(soft) if soft is not None else data["soft"],
                     data.get("cat"), data.get("cats", []), rec.get("category_link"), _use_cases(rec))
        if url is not UNSET:
            if url is None:
                if prod := _get(f"product-{pid}"):
                    _unpublish_product(prod)
            else:
                _publish_product(pid, _path_of(url))
        return get_product(pid)


def delete_product(pid: int) -> None:
    """Removes the product with its use cases, pages and comparisons."""
    with LOCK:
        rec = _record(pid)
        partners = {i for c in _find(f"type:=compare && product_ids:={pid}", include="product_ids")
                    for i in c["product_ids"]} - {pid}
        _delete(f"type:=compare && product_ids:={pid}")
        _delete(f"type:[{PRODUCT_PAGES}] && product_ids:={pid}")
        _delete_ids([f"product-{pid}", rec["id"]])
        _refresh_use_cases(rec["use_case_ids"])
        _update_popularity(partners)
        _refresh_vendor_pages([rec["title"]])


# ---- use cases (product_usecases.json entries, by UUID) -------------------------
def _write_use_cases(rec: dict, entries: list[dict]) -> None:
    """Persist a record's new use-case list and, if the product is searchable, its list document
    and the use cases it adds or drops."""
    _write([_with(rec, use_cases=entries)])
    if prod := _get(f"product-{rec['product_ids'][0]}"):
        _sync_use_cases(prod, entries, old_ids=rec["use_case_ids"])


def set_use_cases(pid: int, entries: list[dict]) -> list[dict]:
    """Replaces the product's use-case list (repeated ids dropped; [] removes all)."""
    with LOCK:
        rec = _record(pid)
        new = D.unique_use_cases(entries)
        _write_use_cases(rec, new)
        return new


def add_use_case(pid: int, entry: dict) -> list[dict]:
    """Appends one use case to the product's list."""
    with LOCK:
        rec = _record(pid)
        entries = _use_cases(rec)
        if any(e["id"] == entry["id"] for e in entries):
            raise Conflict(f"Use case {entry['id']} is already on {D.tool_id(pid)}")
        new = D.unique_use_cases([*entries, entry])
        _write_use_cases(rec, new)
        return new


def remove_use_case(pid: int, uid: str) -> list[dict]:
    """Removes one use case from the product's list."""
    with LOCK:
        rec = _record(pid)
        new = [e for e in _use_cases(rec) if e["id"] != uid]
        if len(new) == len(_use_cases(rec)):
            raise NotFound(f"Use case {uid} is not on {D.tool_id(pid)}")
        _write_use_cases(rec, new)
        return new


def _edit_use_case(uid: str, change) -> list[dict]:
    """Applies change(entries) -> entries to every record having the use case."""
    recs = _find(f"type:=record && use_case_ids:=[`{uid}`]")
    if not recs:
        raise NotFound(f"Use case {uid} not found")
    updated = [_with(r, use_cases=change(_use_cases(r))) for r in recs]
    _write(updated)
    prods = _products(r["product_ids"][0] for r in recs)
    for r in updated:
        if (prod := prods.get(r["product_ids"][0])) and (entries := _use_cases(r)):
            _write([D.use_cases_doc(prod, entries)])
        elif prod:
            _delete_ids([f"use_cases-{prod['product_ids'][0]}"])
    return recs


def rename_use_case(uid: str, name: str) -> dict:
    """Renames the use case for every product that has it."""
    with LOCK:
        name = _clean_name(name)
        recs = _edit_use_case(uid, lambda es: [dict(e, name=name) if e["id"] == uid else e for e in es])
        _refresh_use_cases([uid], rename={uid: name})
        return {"id": uid, "name": name, "products": len(recs)}


def delete_use_case(uid: str) -> None:
    """Removes the use case from every product."""
    with LOCK:
        _edit_use_case(uid, lambda es: [e for e in es if e["id"] != uid])
        _refresh_use_cases([uid])


# ---- website pages (type and url given explicitly) ---------------------------------
def _path_of(url: str) -> str:
    path = urlsplit(url.strip()).path.rstrip("/")
    if not path.startswith("/"):                        # 'chess.com' (no scheme) is not a page url
        raise ValueError(f"Not a page URL: {url}")
    return path


def _by_path(url: str) -> dict:
    path = _path_of(url)
    if not (docs := _find(f'path:="{path}"')):
        raise NotFound(f"Page not found: {path}")
    return docs[0]


def get_page(url: str) -> dict:
    return page_view(_by_path(url))


def add_page(kind: str, url: str, title: str | None = None,
             product_ids: list[int] = (), popularity: int | None = None) -> dict:
    """Add a news / customer-story / alternatives / compare page. Optional product_ids link it to
    a product (or two, for a comparison); no link means a vendor-level page."""
    with LOCK:
        path = _path_of(url)
        if kind == "compare":
            if len(product_ids) != 2:
                raise ValueError("A comparison needs exactly two product_ids")
            prods = _products(product_ids)
            names = [prods[i]["title"] if i in prods else f"Product {i}" for i in product_ids]
            doc = D.compare_doc(path, tuple(product_ids), names,
                                popularity if popularity is not None
                                else sum(prods[i]["popularity"] if i in prods else 1 for i in product_ids))
        elif kind in D.PAGE_KINDS:
            if len(product_ids) > 1:
                raise ValueError(f"A {kind} page links to at most one product")
            product = _get(f"product-{product_ids[0]}") if product_ids else None
            if product_ids and not product:
                raise NotFound(f"Product {D.tool_id(product_ids[0])} has no page")
            doc = D.page_doc(kind, path, product, popularity or 1)
        else:
            raise ValueError(f"Unknown page type: {kind} (news, customer_story, alternatives, compare)")
        if title:
            doc.update(title=_clean_name(title), name_len=len(title.strip()))
        if _get(doc["id"]):
            raise Conflict(f"Page already exists: {url}")
        _write([doc])
        if kind == "compare":
            _update_popularity(doc["product_ids"])
        return page_view(doc)


def remove_page(url: str) -> None:
    """Removes a page. Cascades by its type: a product page hides the product (record kept),
    a comparison recalculates popularity, a category unlinks its products."""
    with LOCK:
        doc = _by_path(url)
        if doc["type"] == "product":
            _unpublish_product(doc)
        else:
            _delete_ids([doc["id"]])
            if doc["type"] == "compare":
                _update_popularity(doc["product_ids"])
            elif doc["type"] == "category":
                _relink_category(doc, category_id=doc["id"])
            elif doc["type"] == "industry":
                search.topic_aliases.cache_clear()


# ---- categories and industries --------------------------------------------------------
TOPIC_PAGES = {folder: kind for kind, folder in D.TOPIC_DIRS.items()}


def _topic(folder: str, slug: str) -> dict:
    doc = _get(D.path_id(TOPIC_PAGES[folder], f"/{folder}/{slug}"))
    if not doc:
        raise NotFound(f"{TOPIC_PAGES[folder].title()} not found: /{folder}/{slug}")
    return doc


def create_topic(folder: str, slug: str, name: str) -> dict:
    with LOCK:
        doc = D.page_doc(TOPIC_PAGES[folder], f"/{folder}/{slug}", popularity=1)
        doc.update(title=_clean_name(name), name_len=len(name.strip()))
        if _get(doc["id"]):
            raise Conflict(f"{TOPIC_PAGES[folder].title()} already exists: /{folder}/{slug}")
        _write([doc])
        if doc["type"] == "category":
            _relink_category(doc)
        else:
            search.topic_aliases.cache_clear()
        return page_view(doc)


def get_topic(folder: str, slug: str) -> dict:
    return page_view(_topic(folder, slug))


def update_topic(folder: str, slug: str, name: str) -> dict:
    with LOCK:
        doc = _topic(folder, slug)
        name = _clean_name(name)
        doc = dict(doc, title=name, name_len=len(name))
        _write([doc])
        if doc["type"] == "category":
            _relink_category(doc)
        else:
            search.topic_aliases.cache_clear()
        return page_view(doc)


def delete_topic(folder: str, slug: str) -> None:
    with LOCK:
        doc = _topic(folder, slug)
        _delete_ids([doc["id"]])
        if doc["type"] == "category":
            _relink_category(doc, category_id=doc["id"])
        else:
            search.topic_aliases.cache_clear()
