import os

TS_URL = os.getenv("TYPESENSE_URL", "http://localhost:8108")
TS_KEY = os.getenv("TYPESENSE_API_KEY", "xyz")
ALIAS = os.getenv("TYPESENSE_COLLECTION", "cuspera_pages")   # searches go through this alias

BASE = "https://www.cuspera.com"
SITEMAPS = {
    "products":       f"{BASE}/sitemap/products.xml",
    "customer-story": f"{BASE}/sitemap/customer-story.xml",
    "news":           f"{BASE}/sitemap/news.xml",
    "categories":     f"{BASE}/sitemap/categories.xml",
    "compare":        f"{BASE}/sitemap/special_compare_ss.xml",
}

HEADERS = {"X-TYPESENSE-API-KEY": TS_KEY}