# Cuspera global search (Typesense + FastAPI)

```
cuspera-search/
  docker-compose.yml   Typesense server
  config.py            URLs, API key, sitemap list
  textutil.py          norm() / pretty()
  indexer.py           sitemaps -> documents -> Typesense (alias swap)
  search.py            intent parsing, anchor selection, grouped retrieval
  main.py              FastAPI app (+ serves the frontend)
  static/index.html    demo UI
```

## Run

```bash
docker compose up -d                       # Typesense on :8108 (api key "xyz")

python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python indexer.py                          # fetch sitemaps, build docs, index
uvicorn main:app --reload                  # http://localhost:8000
```

No Docker? Download the Typesense binary and run:
`./typesense-server --data-dir=./ts-data --api-key=xyz --enable-cors`

## API

`GET /api/search?q=canva vs fi` returns:

```json
{"query": "...", "rule": "exact|parent|dominant|ambiguous|none|too_short",
 "anchor": "Canva", "intent": "compare",
 "sections": [{"key": "compare", "label": "Comparisons",
               "items": [{"title": "Canva vs Figma", "path": "...", "url": "...", "type": "compare"}]}],
 "took_ms": 8.2}
```

`GET /api/health` checks Typesense connectivity.

## Notes

- `popularity` is a heuristic derived from the sitemaps (compare-page counts + news/story bonuses). Put real
  traffic data or manual overrides in `BOOSTS` (indexer.py) when you have them.
- Environment overrides: `TYPESENSE_URL`, `TYPESENSE_API_KEY`, `TYPESENSE_COLLECTION`.