"""Query understanding + grouped retrieval on top of Typesense."""
import re

import requests

from config import ALIAS, BASE, HEADERS, TS_URL
from scripts.Textutil import norm

MIN_CHARS = 2
MAX_SPLIT_TOKENS = 6
_session = requests.Session()

SECTION_LABELS = {
    "products": "Products",
    "news": "News",
    "customer_story": "Customer stories",
    "compare": "Comparisons",
    "category": "Categories",
}


def msearch(searches: list[dict]) -> list[list[dict]]:
    if not searches:
        return []
    r = _session.post(
        f"{TS_URL}/multi_search", headers=HEADERS, timeout=10,
        json={"searches": [{"collection": ALIAS, **s} for s in searches]},
    )
    r.raise_for_status()
    out = []
    for res in r.json()["results"]:
        if "error" in res:
            raise RuntimeError(res["error"])
        out.append([h["document"] for h in res["hits"]])
    return out


# ---- intent parsing ---------------------------------------------------------
INTENT_PHRASES = {
    "news": ["news", "press", "announcements"],
    "customer_story": ["story", "stories", "customer story", "customer stories",
                       "case study", "case studies"],
    "compare": ["compare", "comparison", "comparisons"],
}
PHRASE_INTENT = {ph: intent for intent, phrases in INTENT_PHRASES.items() for ph in phrases}
HARD_SEPS = {"vs", "versus", "v"}
SOFT_SEPS = {"and", "or", "with"}                    # only count as compare if both sides check out
COMPARE_LEADS = [("difference", "between"), ("differences", "between"),
                 ("compare",), ("comparing",), ("comparison",)]
FILLER = {"about", "for", "of", "on", "from", "by", "the", "latest", "recent"}


def match_intent(tail: str):
    if len(tail) < 2:
        return None
    for intent, phrases in INTENT_PHRASES.items():
        if any(ph.startswith(tail) for ph in phrases):     # prefix match => works while typing
            return intent
    return None


def _strip_filler(toks: list[str], lead: bool) -> list[str]:
    toks = list(toks)
    while toks and toks[0 if lead else -1] in FILLER:
        toks.pop(0 if lead else -1)
    return toks


def split_intent(toks: list[str]):
    """'news about 6sense' / '6sense latest ne' -> (intent, entity tokens)."""
    lead = _strip_filler(toks, True)
    for n in (2, 1):                                   # leading: whole words only
        if len(lead) > n and " ".join(lead[:n]) in PHRASE_INTENT:
            rest = _strip_filler(lead[n:], True)
            if rest:
                return PHRASE_INTENT[" ".join(lead[:n])], rest
    for n in (2, 1):                                   # trailing: prefix match on the word being typed
        if len(toks) > n:
            intent = match_intent(" ".join(toks[-n:]))
            rest = _strip_filler(toks[:-n], False)
            if intent and rest:
                return intent, rest
    return None, toks


def split_compare(toks: list[str]):
    """Returns (left, right, kind); kind is hard | soft | lead | None."""
    lead = False
    for phrase in COMPARE_LEADS:
        if tuple(toks[:len(phrase)]) == phrase and len(toks) > len(phrase):
            toks, lead = toks[len(phrase):], True
            break
    for i in range(1, len(toks)):
        t = toks[i]
        if t in HARD_SEPS:
            return toks[:i], toks[i + 1:], "hard"
        if t in SOFT_SEPS:
            return toks[:i], toks[i + 1:], "hard" if lead else "soft"
        if t == "compared" and i + 1 < len(toks) and toks[i + 1] in ("to", "with"):
            return toks[:i], toks[i + 2:], "hard"
    return toks, [], "lead" if lead else None


# ---- stage 1: resolve products ---------------------------------------------
def _product_query(nq: str, typos: int) -> dict:
    return dict(
        q=nq, query_by="product_names", filter_by="type:=product", prefix=True,
        num_typos=typos, drop_tokens_threshold=0, prioritize_exact_match=True,
        prioritize_token_position=True,
        sort_by="_text_match(buckets: 3):desc,name_len:asc,popularity:desc",
        per_page=10,
    )


def resolve_products(texts) -> dict[str, list[dict]]:
    """Product hits for many texts in one round trip (+1 for the typo fallback)."""
    texts = [t for t in dict.fromkeys(texts) if len(t) >= MIN_CHARS]
    found = dict(zip(texts, msearch([_product_query(t, 0) for t in texts])))
    retry = [t for t in texts if not found[t] and len(t) >= 4]   # typo fallback only when nothing matches
    found.update(zip(retry, msearch([_product_query(t, 1) for t in retry])))
    return found


def pick_anchor(text: str, hits: list[dict]):
    """Returns (doc, rule). Rules: exact | parent | dominant | ambiguous | none."""
    nq = norm(text)
    if not hits:
        return None, "none"
    names = [norm(h["product_names"][0]) for h in hits]
    for h, n in zip(hits, names):                  # 1. exact name
        if n == nq:
            return h, "exact"
    first_words = {n.split()[0] for n in names if n}
    if len(hits) > 1 and len(first_words) == 1:    # 2. a "parent" product named by the shared first word
        fw = next(iter(first_words))
        for h, n in zip(hits, names):
            if n == fw:
                return h, "parent"
    if len(nq) >= 3:                               # 3. clear popularity winner
        p0 = hits[0]["popularity"]
        p1 = hits[1]["popularity"] if len(hits) > 1 else 0
        if len(hits) == 1 or p0 >= 3 * max(p1, 1):
            return hits[0], "dominant"
    return None, "ambiguous"                       # 4. show a product list only


RULE_RANK = {"exact": 3, "parent": 2, "dominant": 1}


# ---- interpretation ---------------------------------------------------------
def _other_name(d: dict, aid: int) -> str:
    ids = d["product_ids"]
    k = ids.index(aid) if aid in ids else 0
    return norm(d["product_names"][1 - k]) if len(d["product_names"]) > 1 else ""


def _compare_candidates(toks: list[str]):
    """Yields (group, left, right, kind). group 0 = explicit compare, 2 = bare pair."""
    left, right, kind = split_compare(toks)
    if kind in ("hard", "soft"):
        yield 0, " ".join(left), " ".join(right), kind
        return
    if kind == "lead" or len(toks) <= MAX_SPLIT_TOKENS:
        group = 0 if kind == "lead" else 2
        if len(left) <= MAX_SPLIT_TOKENS:
            for i in range(1, len(left)):
                yield group, " ".join(left[:i]), " ".join(left[i:]), "bare"


def interpret(nq: str) -> dict:
    toks = nq.split()
    _, _, ckind = split_compare(toks)
    intent, rest = split_intent(toks) if ckind is None else (None, toks)
    pairs = list(_compare_candidates(toks)) if intent is None else []
    lead_entity = " ".join(split_compare(toks)[0]) if ckind == "lead" else None

    found = resolve_products(
        [nq, " ".join(rest)] + [t for _, l, r, _ in pairs for t in (l, r)] + ([lead_entity] if lead_entity else [])
    )

    def anchor(text):
        hits = found.get(text, [])
        return (*pick_anchor(text, hits), hits)

    a0, r0, hits0 = anchor(nq)
    plain = {"intent": None, "anchor": a0, "rule": r0, "hits": hits0}
    if r0 == "exact" or any(norm(h["product_names"][0]).startswith(nq) for h in hits0):
        return plain                                # the whole query is (the start of) a real product name

    # Compare candidates: anchor on a confident side, the other side filters its comparisons.
    cands = []
    for group, ltext, rtext, kind in pairs:
        (la, lr, lh), (ra, rr, rh) = anchor(ltext), (anchor(rtext) if rtext else (None, "none", []))
        if lr not in RULE_RANK and rr in RULE_RANK:
            (la, lr, lh), (ra, rr, rh), ltext, rtext = (ra, rr, rh), (la, lr, lh), rtext, ltext
        if lr not in RULE_RANK:
            continue
        if ra and ra["id"] == la["id"]:
            ra, rr = None, "none"
        cands.append({"group": group, "kind": kind, "anchor": la, "rule": lr, "hits": lh, "e2": rtext,
                      "b": ra if rr in RULE_RANK else None, "b_rule": rr})

    searches = []
    for c in cands:
        c["e2q"] = norm(c["b"]["product_names"][0]) if c["b"] else c["e2"]
        if c["e2q"]:
            searches.append(dict(q=c["e2q"], query_by="product_names", prefix=True, num_typos=0,
                                 filter_by=f"type:=compare && product_ids:={c['anchor']['product_ids'][0]}",
                                 per_page=50))
    partner_results = iter(msearch(searches))
    for c in cands:
        c["pair"], c["partners"] = [], []
        if not c["e2q"]:
            continue
        aid = c["anchor"]["product_ids"][0]
        bid = c["b"]["product_ids"][0] if c["b"] else None
        pat = re.compile(("^" if c["kind"] == "bare" else r"(^| )") + re.escape(c["e2q"]))
        for d in next(partner_results):
            if bid is not None and bid in d["product_ids"]:
                c["pair"].append(d)
            elif pat.search(_other_name(d, aid)):
                c["partners"].append(d)

    def valid(c):
        if c["kind"] == "hard":
            return True
        if not c["e2"]:
            return c["kind"] == "soft"              # 'canva and' while the second name is being typed
        return bool(c["pair"] or c["partners"] or c["b"])

    def score(c):
        return (bool(c["pair"]), RULE_RANK[c["rule"]] + RULE_RANK.get(c["b_rule"], 0), len(c["partners"]))

    def compare_result(c):
        return {"intent": "compare", "anchor": c["anchor"], "rule": c["rule"], "hits": c["hits"],
                "anchor2": c["b"], "e2": c["e2"], "pair": c["pair"], "partners": c["partners"]}

    explicit = [c for c in cands if c["group"] == 0 and valid(c)]
    if explicit:
        return compare_result(max(explicit, key=score))
    if intent:
        a, r, h = anchor(" ".join(rest))
        if a:
            return {"intent": intent, "anchor": a, "rule": r, "hits": h}
    bare = [c for c in cands if c["group"] == 2 and valid(c)]
    if bare:
        return compare_result(max(bare, key=score))
    if lead_entity:
        a, r, h = anchor(lead_entity)
        if a:
            return {"intent": "compare", "anchor": a, "rule": r, "hits": h}
    if intent and not hits0:                        # 'news cr': no confident product yet, list candidates
        a, r, h = anchor(" ".join(rest))
        return {"intent": None, "anchor": None, "rule": r, "hits": h}
    return plain


# ---- stage 2: grouped results ----------------------------------------------
def _item(d: dict) -> dict:
    return {"title": d["title"], "path": d["path"], "url": BASE + d["path"], "type": d["type"]}


def _dedupe(docs):
    return list({d["id"]: d for d in docs}.values())


def run(q: str) -> dict:
    nq = norm(q)
    if len(nq) < MIN_CHARS:
        return {"query": q, "rule": "too_short", "anchor": None, "anchor2": None, "intent": None, "sections": []}

    p = interpret(nq)
    anchor, b, hits = p["anchor"], p.get("anchor2"), p["hits"]

    searches, keys = [], []
    if anchor:
        aid = anchor["product_ids"][0]
        for i, pid in enumerate([aid] + ([b["product_ids"][0]] if b else [])):   # group info for both products
            for kind in ("news", "customer_story"):
                searches.append(dict(q="*", filter_by=f"type:={kind} && product_ids:={pid}", per_page=1))
                keys.append(f"{kind}_{i}")
        if not p.get("e2"):
            searches.append(dict(q="*", filter_by=f"type:=compare && product_ids:={aid}",
                                 sort_by="popularity:desc", per_page=5))
            keys.append("compare")
        elif b and not p["pair"]:                   # both products known, no page for the pair
            for pid, key in ((aid, "compare_a"), (b["product_ids"][0], "compare_b")):
                searches.append(dict(q="*", filter_by=f"type:=compare && product_ids:={pid}",
                                     sort_by="popularity:desc", per_page=3))
                keys.append(key)
    if not p["intent"] and len(nq) >= 3:
        searches.append(dict(q=nq, query_by="title", filter_by="type:=category",
                             prefix=True, num_typos=0, per_page=2))
        keys.append("category")
    got = dict(zip(keys, msearch(searches)))
    for kind in ("news", "customer_story"):
        got[kind] = got.pop(f"{kind}_0", []) + got.pop(f"{kind}_1", [])

    if p.get("e2"):
        if p["pair"] or not b:
            got["compare"] = (p["pair"] + p["partners"])[:5]
        else:
            got["compare"] = _dedupe(got.pop("compare_a") + got.pop("compare_b"))

    if b:
        prods = [anchor, b]
    elif anchor:
        prods = ([anchor] + [h for h in hits if h["id"] != anchor["id"]])[:3]
    else:
        prods = hits[:5]
    sections = {"products": prods, **got}

    order = ["products", "news", "customer_story", "compare", "category"]
    if p["intent"]:
        order.remove(p["intent"])
        order.insert(0, p["intent"])
    if any(norm(c["title"]) == nq for c in got.get("category", [])):
        order.remove("category")
        order.insert(0, "category")

    return {
        "query": q,
        "rule": p["rule"],
        "intent": p["intent"],
        "anchor": anchor["title"] if anchor else None,
        "anchor2": b["title"] if b else None,
        "sections": [
            {"key": k, "label": SECTION_LABELS[k], "items": [_item(d) for d in sections[k]]}
            for k in order if sections.get(k)
        ],
    }
