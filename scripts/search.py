"""Query understanding + grouped retrieval on top of Typesense."""
import functools
import re

from config import ALIAS, BASE, make_client
from scripts.Textutil import norm

MIN_CHARS = 2
MAX_SPLIT_TOKENS = 6
MAX_EXTRACT_TOKENS = 8
MAX_SPAN_TOKENS = 4
client = make_client(timeout_seconds=2)             # search-as-you-type: fail fast

SECTION_LABELS = {
    "products": "Products",
    "news": "News",
    "customer_story": "Customer stories",
    "alternatives": "Alternatives",
    "compare": "Comparisons",
    "category": "Categories",
    "industry": "Industries",
}
PRODUCT_PAGE_TYPES = ("news", "customer_story", "alternatives")   # one page per product
TOPIC_TYPES = ("category", "industry")                             # matched by title
TOPIC_ALIASES = {        # extra names for categories / industries, keyed by normalized title
    "crm": ["customer relationship management"],
    "cpq": ["configure price quote"],
    "pos": ["point of sale"],
    "seo": ["search engine optimization"],
    "business intelligence": ["bi"],
    "public relations": ["pr"],
    "human resources": ["hr"],
    "information technology and services": ["it", "it services"],
    "digital experience platform": ["dxp"],
    "customer experience management": ["cx", "cxm"],
    "employee experience management": ["ex", "exm"],
    "e commerce platform": ["ecommerce"],
    "e commerce software": ["ecommerce"],
}
INITIALS_SKIP = {"and", "or", "of"}
INITIALS_BLOCKLIST = {"its"}                     # real words: would hijack plain queries


def msearch(searches: list[dict]) -> list[list[dict]]:
    if not searches:
        return []
    response = client.multi_search.perform({"searches": [{"collection": ALIAS, **s} for s in searches]})
    out = []
    for res in response["results"]:
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
    "alternatives": ["alternatives", "alternative", "competitors", "competitor"],
}
PHRASE_INTENT = {ph: intent for intent, phrases in INTENT_PHRASES.items() for ph in phrases}
HARD_SEPS = {"vs", "versus", "v"}
SOFT_SEPS = {"and", "or", "with"}                    # only count as compare if both sides check out
COMPARE_LEADS = [("difference", "between"), ("differences", "between"),
                 ("compare",), ("comparing",), ("comparison",)]
FILLER = {"about", "for", "of", "on", "from", "by", "to", "the", "latest", "recent", "top", "best"}
NON_ENTITY_WORDS = HARD_SEPS | SOFT_SEPS | FILLER | {w for p in COMPARE_LEADS for w in p} | {"compared", "to"}


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
TEXT_POOL, POPULAR_POOL = 20, 30


def _product_queries(nq: str, typos: int) -> list[dict]:
    """Best text matches + most popular matches: Typesense ranks a whole-word hit ('Zed Sales')
    above a prefix hit ('Salesforce'), so popular prefix matches need their own pool."""
    base = dict(q=nq, query_by="product_names", filter_by="type:=product", prefix=True,
                num_typos=typos, drop_tokens_threshold=0)
    return [
        dict(base, prioritize_exact_match=True, prioritize_token_position=True,
             sort_by="_text_match(buckets: 3):desc,name_len:asc,popularity:desc", per_page=TEXT_POOL),
        dict(base, sort_by="popularity:desc", per_page=POPULAR_POOL),
    ]


def match_tier(name: str, nq: str) -> int:
    """3 exact name, 2 name starts with the query, 1 a later word does, 0 other (typos, scattered words)."""
    if name == nq:
        return 3
    if name.startswith(nq):
        return 2
    return 1 if f" {nq}" in f" {name}" else 0


def rank_products(nq: str, hits: list[dict]) -> list[dict]:
    """Match tier, then popularity; within a tier one product per family (first word) comes
    first, so 'sales' lists Salesforce, Salesmate, ... before a second Salesforce product."""
    hits = sorted(_dedupe(hits), key=lambda h: (-match_tier(norm(h["product_names"][0]), nq),
                                                -h["popularity"], h["name_len"]))
    seen, keyed = {}, []
    for rank, h in enumerate(hits):
        name = norm(h["product_names"][0])
        family = (match_tier(name, nq), name.split()[0] if name else "")
        keyed.append((-family[0], seen.get(family, 0), rank, h))
        seen[family] = seen.get(family, 0) + 1
    return [h for *_, h in sorted(keyed, key=lambda k: k[:3])]


def resolve_products(texts) -> dict[str, list[dict]]:
    """Ranked product hits for many texts in one round trip (+1 for the typo fallback)."""
    def run_pools(ts, typos):
        res = iter(msearch([s for t in ts for s in _product_queries(t, typos)]))
        return {t: rank_products(t, next(res) + next(res)) for t in ts}

    texts = [t for t in dict.fromkeys(texts) if len(t) >= MIN_CHARS]
    found = run_pools(texts, 0)
    found.update(run_pools([t for t in texts if not found[t] and len(t) >= 4], 1))   # typos only when nothing matches
    return found


def pick_anchor(text: str, hits: list[dict]):
    """Returns (doc, rule). Rules: exact | parent | dominant | ambiguous | none. Expects ranked hits."""
    nq = norm(text)
    if not hits:
        return None, "none"
    names = [norm(h["product_names"][0]) for h in hits]
    for h, n in zip(hits, names):                  # 1. exact name
        if n == nq:
            return h, "exact"
    starts = [n for n in names if n.startswith(nq)]
    group = starts or names
    if len(group) > 1:                             # 2. "parent": a name every other match extends ('Demandbase One')
        for h, n in sorted(zip(hits, names), key=lambda x: len(x[1])):
            if n in group and all(g.split()[:len(n.split())] == n.split() for g in group):
                return h, "parent"
        qw = nq.split()                            # ... or every match is '<query> ...' ('salesforce'): its top product
        if starts and all(n.split()[:len(qw)] == qw for n in names if match_tier(n, nq)):
            return hits[0], "parent"
    if len(nq) >= 3 and match_tier(names[0], nq) != 1:   # 3. clear popularity winner (not a later-word match like 'crm' -> 'C3 CRM')
        p0 = hits[0]["popularity"]
        p1 = max((h["popularity"] for h in hits[1:]), default=0)
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
    bare_intent = {"anchor": None, "rule": "none", "hits": hits0, "scope": None}   # scope None: across all products
    if r0 != "exact" and nq in PHRASE_INTENT:      # 'news', 'case studies': the intent alone
        return {"intent": PHRASE_INTENT[nq], **bare_intent}
    if r0 == "exact" or any(norm(h["product_names"][0]).startswith(nq) for h in hits0):
        return plain                                # the whole query is (the start of) a real product name
    if len(nq) >= 4 and match_intent(nq):           # 'alternat' while typing, if no product starts that way
        return {"intent": match_intent(nq), **bare_intent}

    def evaluate(pairs):
        """Compare candidates: anchor on a confident side, the other side filters its comparisons."""
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
        return cands

    def extract_products(ext_toks):
        """Up to 2 confident product mentions anywhere in the query, skipping unmatched words."""
        spans = [(i, j) for i in range(len(ext_toks))
                 for j in range(i + 1, min(len(ext_toks), i + MAX_SPAN_TOKENS) + 1)]
        texts = [" ".join(ext_toks[i:j]) for i, j in spans]
        found.update(resolve_products([t for t in texts if t not in found]))
        scored = sorted(((j - i, RULE_RANK[anchor(t)[1]], -i, i, j, t) for (i, j), t in zip(spans, texts)
                         if anchor(t)[1] in RULE_RANK), reverse=True)    # longer span, stronger rule first
        chosen = []
        for *_, i, j, t in scored:
            if all(j <= ci or i >= cj for ci, cj, _ in chosen) \
                    and all(anchor(t)[0]["id"] != anchor(ct)[0]["id"] for _, _, ct in chosen):
                chosen.append((i, j, t))
            if len(chosen) == 2:
                break
        return [t for _, _, t in sorted(chosen)]

    cands = evaluate(pairs)

    def valid(c):
        if c["kind"] == "hard":
            return True
        if not c["e2"]:
            return c["kind"] == "soft"              # 'canva and' while the second name is being typed
        return bool(c["pair"] or c["partners"] or c["b"])

    def score(c):
        return (bool(c["pair"]), RULE_RANK[c["rule"]] + RULE_RANK.get(c["b_rule"], 0), len(c["partners"]))

    def compare_result(c, intent=None):
        """Two products: compare intent only if asked for or a comparison page exists; otherwise
        ('demandbase outreach' without a page) both products' own pages lead."""
        has_page = bool(c["pair"] or c["partners"])
        return {"intent": intent or ("compare" if c["group"] == 0 or has_page else None),
                "anchor": c["anchor"], "rule": c["rule"], "hits": c["hits"],
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

    # Last resort: 'algolia xqyz landingi' / 'landingi xqzw' -> find the product names among unmatched words.
    ext_toks = [t for t in rest if t not in NON_ENTITY_WORDS]
    if 2 <= len(ext_toks) <= MAX_EXTRACT_TOKENS:
        mentions = extract_products(ext_toks)
        if len(mentions) == 2:
            c = evaluate([(3, mentions[0], mentions[1], "bare")])
            if c:                                   # 'outreach demandbase news': news of both products
                return compare_result(c[0], intent if ckind is None else None)
        if len(mentions) == 1 and anchor(mentions[0])[1] in ("exact", "parent"):
            a, r, h = anchor(mentions[0])
            return {"intent": intent if ckind is None else "compare", "anchor": a, "rule": r, "hits": h}

    if intent and not hits0:                        # 'news cr': no confident product yet, list candidates
        a, r, h = anchor(" ".join(rest))            # ... and lead with their intent pages
        return {"intent": intent, "anchor": None, "rule": r, "hits": h,
                "scope": [d["product_ids"][0] for d in h[:10]], "entity": " ".join(rest)}
    return plain


# ---- stage 2: grouped results ----------------------------------------------
def _item(d: dict) -> dict:
    return {"title": d["title"], "path": d["path"], "url": BASE + d["path"], "type": d["type"]}


def _dedupe(docs):
    return list({d["id"]: d for d in docs}.values())


PAGE_TITLE_SUFFIX = {"news": "news", "customer_story": "customer stories", "alternatives": "alternatives"}


def _vendor_of(d: dict, kind: str, product_name: str) -> bool:
    """A vendor-level page ('Demandbase News', no product) whose vendor name starts the product name."""
    vendor = norm(d["title"]).removesuffix(PAGE_TITLE_SUFFIX[kind]).strip()
    return not d["product_ids"] and bool(vendor) and f"{product_name} ".startswith(f"{vendor} ")


@functools.cache
def topic_aliases() -> dict[str, list[dict]]:
    """alias -> category/industry docs. Aliases: title initials ('abm') + TOPIC_ALIASES."""
    docs, page = [], 1
    while True:
        batch = msearch([dict(q="*", filter_by=f"type:[{','.join(TOPIC_TYPES)}]", per_page=250, page=page)])[0]
        docs += batch
        if len(batch) < 250:
            break
        page += 1
    out = {}
    for d in docs:
        title = norm(d["title"])
        words = [w for w in title.split() if w not in INITIALS_SKIP]
        initials = "".join(w[0] for w in words)
        names = list(TOPIC_ALIASES.get(title, []))
        if len(words) > 1 and len(initials) >= 3 and initials not in INITIALS_BLOCKLIST:
            names.append(initials)
        for name in names:
            out.setdefault(name, []).append(d)
    return out


def topic_alias_hits(nq: str):
    """(exact, inner, partial) alias matches: the whole query, a phrase inside it ('abm software'),
    or a multi-word alias being typed ('customer rel'). Inner skips 2-letter aliases ('it', 'pr')."""
    aliases = topic_aliases()
    toks = nq.split()
    spans = [" ".join(toks[i:j]) for i in range(len(toks))
             for j in range(i + 1, min(len(toks), i + MAX_SPAN_TOKENS) + 1)]
    inner = [d for s in spans if s != nq and len(s) >= 3 for d in aliases.get(s, [])]
    partial = [d for a, ds in aliases.items() if " " in nq and a != nq and a.startswith(nq) for d in ds]
    return aliases.get(nq, []), inner, partial


def run(q: str) -> dict:
    nq = norm(q)
    if len(nq) < MIN_CHARS:
        return {"query": q, "rule": "too_short", "anchor": None, "anchor2": None, "intent": None, "sections": []}

    p = interpret(nq)
    anchor, b, hits = p["anchor"], p.get("anchor2"), p["hits"]

    searches, keys = [], []
    if anchor:
        aid = anchor["product_ids"][0]
        for i, prod in enumerate([anchor] + ([b] if b else [])):   # group info for both products
            for kind in PRODUCT_PAGE_TYPES:
                searches.append(dict(q="*", filter_by=f"type:={kind} && product_ids:={prod['product_ids'][0]}",
                                     per_page=1))
                keys.append(f"{kind}_{i}")
                searches.append(dict(q=norm(prod["product_names"][0]).split()[0], query_by="title",
                                     filter_by=f"type:={kind}", num_typos=0, prefix=False, per_page=10))
                keys.append(f"{kind}_vendor_{i}")
        if not p.get("e2") or not (b or p["pair"] or p["partners"]):   # nothing matched the 2nd side: show anchor's
            searches.append(dict(q="*", filter_by=f"type:=compare && product_ids:={aid}",
                                 sort_by="popularity:desc", per_page=5))
            keys.append("compare")
        elif b and not p["pair"]:                   # both products known, no page for the pair
            for pid, key in ((aid, "compare_a"), (b["product_ids"][0], "compare_b")):
                searches.append(dict(q="*", filter_by=f"type:=compare && product_ids:={pid}",
                                     sort_by="popularity:desc", per_page=3))
                keys.append(key)
    elif p["intent"]:                               # intent, no confident product: candidates' pages, else the most popular
        scope, entity = p.get("scope"), p.get("entity")
        if scope or not entity:
            searches.append(dict(q="*", filter_by=f"type:={p['intent']}" + (
                f" && product_ids:[{','.join(map(str, scope))}]" if scope else ""),
                sort_by="popularity:desc", per_page=50 if scope else 5))
            keys.append(p["intent"])
        if entity:                                  # pages not tied to one product, e.g. '/vendors/salesforce/news'
            searches.append(dict(q=entity, query_by="title", filter_by=f"type:={p['intent']}", prefix=True,
                                 num_typos=0, sort_by="_text_match(buckets: 3):desc,popularity:desc", per_page=10))
            keys.append("intent_titles")
    if not p["intent"] and len(nq) >= 3:
        for kind in TOPIC_TYPES:
            searches.append(dict(q=nq, query_by="title", filter_by=f"type:={kind}",
                                 prefix=True, num_typos=0, per_page=2))
            keys.append(kind)
    got = dict(zip(keys, msearch(searches)))
    if not anchor and p.get("entity"):              # keep the candidates' ranking: 'news cr' -> Crayon News first
        rank = {pid: i for i, pid in enumerate(p["scope"])}
        scoped = sorted(got.get(p["intent"], []), key=lambda d: min(
            rank.get(pid, len(rank)) for pid in d["product_ids"]))
        got[p["intent"]] = sorted(_dedupe(scoped + got.pop("intent_titles")),
                                  key=lambda d: -match_tier(norm(d["title"]), p["entity"]))[:5]
    for kind in PRODUCT_PAGE_TYPES:
        for i, prod in enumerate([anchor, b] if anchor else []):
            own, vendor = got.pop(f"{kind}_{i}", []), got.pop(f"{kind}_vendor_{i}", [])
            if prod and not own:                    # no page of its own: 'Demandbase One' -> Demandbase News
                own = [d for d in vendor if _vendor_of(d, kind, norm(prod["product_names"][0]))][:1]
            got[kind] = got.get(kind, []) + own
    alias_exact, alias_inner, alias_partial = topic_alias_hits(nq)
    if not anchor:                                  # 'abm xyz': nothing else matched, the abbreviation leads
        alias_exact = alias_exact + alias_inner
    for kind in TOPIC_TYPES:                        # abbreviations / full forms: 'abm' -> Account Based Marketing
        exact = _dedupe(d for d in alias_exact + alias_inner if d["type"] == kind)
        partial = [d for d in alias_partial if d["type"] == kind]
        if exact or partial:
            got[kind] = _dedupe(exact + got.get(kind, []) + partial)[:max(2, len(exact))]
    alias_ids = {d["id"] for d in alias_exact}

    if p.get("e2") and (b or p["pair"] or p["partners"]):
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

    order = ["products", *PRODUCT_PAGE_TYPES, "compare", *TOPIC_TYPES]
    if p["intent"]:
        order.remove(p["intent"])
        order.insert(0, p["intent"])
    for kind in TOPIC_TYPES:                        # an exact category/industry name goes first
        if any(norm(c["title"]) == nq or c["id"] in alias_ids for c in got.get(kind, [])):
            order.remove(kind)
            order.insert(0, kind)

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
