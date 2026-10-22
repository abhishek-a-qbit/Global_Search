import os
from urllib.parse import urlsplit

import typesense

TS_URL = os.getenv("TYPESENSE_URL", "http://127.0.0.1:8108")   # not "localhost": avoids a slow IPv6 attempt on Windows
TS_KEY = os.getenv("TYPESENSE_API_KEY", "xyz")
ALIAS = os.getenv("TYPESENSE_COLLECTION", "cuspera_pages")   # searches go through this alias
# Comma-separated browser origins allowed to call the API (where the frontend is hosted)
CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500").split(",") if o.strip()]

# Semantic (pure vector) use-case search: Typesense embeds use-case names with a built-in model.
# Off in the Render image (too slow / too much memory there); requires re-running indexer.py.
SEMANTIC_USE_CASES = os.getenv("SEMANTIC_USE_CASES", "1") == "1"
EMBED_MODEL = os.getenv("EMBED_MODEL", "ts/all-MiniLM-L12-v2")
SEMANTIC_MAX_DISTANCE = float(os.getenv("SEMANTIC_MAX_DISTANCE", "0.45"))    # cosine distance cut-off
# (relevant matches measured at <= ~0.44, gibberish at ~0.78)

# Catalog write API (create / update / delete). No auth: local use only; off in the Docker image.
CATALOG_API = os.getenv("CATALOG_API", "1") == "1"

BASE = "https://www.cuspera.com"
# From {BASE}/sitemap.xml, excluding directory, dimension and vendors. Entries may be
# sitemap indexes (e.g. compare-index.xml); the indexer follows them recursively.
SITEMAPS = {
    "products":       [f"{BASE}/sitemap/products.xml"],
    "compare":        [f"{BASE}/sitemap/compare-index.xml",
                       f"{BASE}/sitemap/special_compare_ss.xml"],   # not in the index, but has ~20k extra pairs
    "customer-story": [f"{BASE}/sitemap/customer-story.xml"],
    "news":           [f"{BASE}/sitemap/news.xml"],
    "alternatives":   [f"{BASE}/sitemap/alternatives.xml"],
    "categories":     [f"{BASE}/sitemap/categories.xml"],
    "industries":     [f"{BASE}/sitemap/industries.xml"],
}


def make_client(timeout_seconds: float) -> typesense.Client:
    url = urlsplit(TS_URL)
    return typesense.Client({
        "api_key": TS_KEY,
        "nodes": [{
            "host": url.hostname,
            "port": url.port or (443 if url.scheme == "https" else 80),
            "protocol": url.scheme,
        }],
        "connection_timeout_seconds": timeout_seconds,
        "num_retries": 2,
        "retry_interval_seconds": 0.1,
    })
