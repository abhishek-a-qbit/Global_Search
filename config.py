import os
from urllib.parse import urlsplit

import typesense

TS_URL = os.getenv("TYPESENSE_URL", "http://127.0.0.1:8108")   # not "localhost": avoids a slow IPv6 attempt on Windows
TS_KEY = os.getenv("TYPESENSE_API_KEY", "xyz")
ALIAS = os.getenv("TYPESENSE_COLLECTION", "cuspera_pages")   # searches go through this alias

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
