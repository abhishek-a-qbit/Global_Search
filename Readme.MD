# Cuspera global search

FastAPI serves the search API; the frontend (`frontend/`) is a separate static site
that calls the API over HTTP (CORS-enabled). The search engine is the official
Typesense server (v30.2), accessed through the official `typesense` Python client
(v2.0.0). Typesense runs locally on your machine (Docker or WSL); no cloud service
is needed.

## Prerequisite on Windows: enable virtualization

Both Docker Desktop and WSL 2 need the Windows "Virtual Machine Platform". In an
**administrator** PowerShell:

```powershell
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
```

Restart Windows. If WSL still reports that virtualization is disabled, turn on
Intel VT-x / AMD-V (SVM) in your BIOS/UEFI settings.

## Run Typesense

Option A, Docker, from the repository directory. On a normal Linux host:

```bash
docker compose up -d
```

On Windows with Docker Desktop, newer WSL kernels (e.g. WSL 3.x, kernel 6.18) have
no `/proc/self/io`, which makes `typesense-server` segfault at startup (exit code
139, no logs). Use the dev override, which builds a small preload shim on top of
the official image (`docker/wsl-procio-shim/`):

```powershell
docker compose -f docker-compose.yml -f docker-compose.wsl.yml up -d --build
```

Option B, WSL (Ubuntu) without Docker:

```bash
curl -O https://dl.typesense.org/releases/30.2/typesense-server-30.2-linux-amd64.tar.gz
tar -xzf typesense-server-30.2-linux-amd64.tar.gz
mkdir -p ~/typesense-data
./typesense-server --data-dir="$HOME/typesense-data" --api-key=xyz --enable-cors
```

WSL normally forwards port `8108` to Windows `localhost`. If that does not
work, get the WSL IP with `wsl hostname -I` and set
`$env:TYPESENSE_URL = "http://<WSL-IP>:8108"` before running the indexer and app.

For anything beyond local development, set a strong `TYPESENSE_API_KEY`
environment variable for both Typesense and the app instead of `xyz`.

## Index and run the app

In PowerShell, from the repository directory:

```powershell
cd C:\Users\abhis\OneDrive\Desktop\Global_Search
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe indexer.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

In a second PowerShell window, serve the frontend:

```powershell
.\.venv\Scripts\python.exe -m http.server 5500 --directory frontend
```

Open <http://localhost:5500> (FastAPI also serves the same page at <http://127.0.0.1:8000>).
The frontend calls the API at `API_BASE` (top of the
`<script>` in `frontend/index.html`, default `http://127.0.0.1:8000`). If you host
the frontend elsewhere, update `API_BASE` and add its origin to `CORS_ORIGINS`.

The first indexing run downloads the configured
sitemaps and may take some time. It builds a new collection and switches the
search alias after a successful import, then deletes older build collections.
`indexer.py` is the initial build: after that, keep the data up to date through the
catalog source file or the catalog API (below). If you already have a source file
(`data/catalog.jsonl.gz`), you can skip `indexer.py` and build from the file instead
(`python -m scripts.source rebuild`). An `indexer.py` rebuild replaces the whole index and discards API changes, so
it refuses to run while the index exists unless you pass `--force` (also needed after
changing how documents are built; restart the API afterwards). Product popularity is
1 + the number of comparisons a product appears in; news / customer story / alternatives
pages inherit it from their product (vendor pages from the vendor's most popular product),
and a comparison gets the sum of both products'. Keep the terminal
running while using the app. In a second PowerShell window, you can check Typesense with:

```powershell
Invoke-RestMethod http://localhost:8108/health
```

## Deploy for free on Render

The root `Dockerfile` runs Typesense, the API and the frontend in one container
(FastAPI serves `frontend/` at `/`). Free disks are ephemeral, so on every start
`seed_index.py` loads the bundled `data/documents.jsonl.gz` into Typesense (about
1.5 minutes on Render's free 0.1 CPU; searches return a "still loading" 503 until
then and the page retries automatically). Typesense listens on localhost only,
with a random API key per boot.

1. Push the repository (including `data/documents.jsonl.gz`) to GitHub.
2. On <https://render.com>, sign up (no card needed), then New > Blueprint and
   pick the repository; `render.yaml` creates a free Docker web service.
3. Open the `https://<name>.onrender.com` URL once the deploy is live.

Free services sleep after 15 minutes without traffic; the next visit takes about
a minute to wake up plus the index load. To ship fresh data, re-run `indexer.py --force`
locally, then `.\.venv\Scripts\python.exe export_index.py`, commit the updated
`data/documents.jsonl.gz` and push (Render redeploys automatically). The export leaves out
the hidden source `record` documents and the embedding vectors.

## API

`GET /api/search?q=canva%20vs%20figma` returns grouped search results:

```json
{"query": "...", "rule": "exact|parent|dominant|ambiguous|none|too_short",
 "anchor": "Canva", "intent": "compare",
 "sections": [{"key": "compare", "label": "Comparisons",
               "items": [{"title": "Canva vs Figma", "path": "...", "url": "...", "type": "compare"}]}],
 "took_ms": 8.2}
```

`GET /api/health` checks Typesense connectivity.

`GET /api/collections` lists the index builds (`cuspera_pages_build_<timestamp>`, newest
first) and which one is live. `GET /api/search?q=...&collection=cuspera_pages_build_<timestamp>`
searches that build instead of the live one (e.g. to compare with the previous build; 404 if it does
not exist). Optional `semantic=0|1` overrides semantic use-case search for one request.

## Catalog source file (JSON Lines)

The whole catalog can live in one JSON Lines file: one JSON object per line, each a
product or a page. The file is the source of truth: it is read only when you ask for a
sync or rebuild (nothing is synced automatically). It is set by `SOURCE_URL`: a path relative to the
repository (default `data/catalog.jsonl.gz`, committed gzipped to keep the repository small) or an
`http(s)` URL, e.g. the S3 object's URL; plain and gzipped files both work (detected by content).
To edit by hand, work on a plain copy and pass it explicitly: `python -m scripts.source export
data/catalog.jsonl` writes one from the live index (`data/catalog.jsonl` is gitignored), and
`python -m scripts.source sync data/catalog.jsonl` reads it; export to `data/catalog.jsonl.gz`
again before committing.

| `type` | Fields |
|---|---|
| `product` | `id` (the number of `CFProdCode-<id>`), `name`, `url` (product page; `null` or missing: kept but not searchable), `category` (category slug shown for the product), `cat` / `cats` (`{"perma": "<category slug>", "name": "..."}` as in `prod.json`), `use_cases` (`[{"id": "<uuid>", "name": "..."}]`) |
| `news`, `customer_story`, `alternatives` | `url`, `product_id` (missing: a vendor-level page such as `/vendors/zoom/news`) |
| `compare` | `url`, `product_ids` (exactly two) |
| `category`, `industry` | `url`, `title` |

Pages may also set `title` and `popularity`, which replace the derived ones. Everything
else is derived at build time, as with `indexer.py`: popularity (1 + comparisons, inherited by
pages and comparisons), page and comparison titles from the product names, each product's
category page, each use case's top products, and the use-case embeddings (computed by Typesense,
the file has no vectors).

```jsonl
{"type":"product","id":17370,"name":"Canva","url":"https://www.cuspera.com/products/canva-x-17370","category":"graphic-design","cat":{"perma":"graphic-design","name":"Graphic Design"},"use_cases":[{"id":"62eef0e3-6428-46cb-8149-c679c47d9ab6","name":"Social media graphics"}]}
{"type":"compare","url":"https://www.cuspera.com/compare/canva-vs-figma/17370/2345","product_ids":[17370,2345]}
{"type":"news","url":"https://www.cuspera.com/products/canva-x-17370/news","product_id":17370}
{"type":"category","url":"https://www.cuspera.com/categories/crm","title":"CRM"}
```

The file must be the complete catalog, not a list of changes: anything not in it is removed
from search. Every line is validated first (required fields, no unknown fields, names with
letters or digits, page URLs, no duplicate products or URLs); an invalid file changes nothing
and the errors are listed with line numbers. A sync or rebuild also replaces any changes made
through the catalog API that are not in the file.

| Command | API | What it does |
|---|---|---|
| `python -m scripts.source sync --dry-run` | `POST /api/sync?dry_run=true` | Reports what a sync would create / update / delete, by document type; writes nothing |
| `python -m scripts.source sync` | `POST /api/sync` | Writes only the documents that changed; a use case keeps its embedding unless renamed |
| `python -m scripts.source rebuild` | `POST /api/sync?rebuild=true` | A fresh index from the file alone, in a new collection |
| `python -m scripts.source export` | - | Writes the live index as a source file (e.g. to capture catalog API changes) |

The commands take an optional file or URL instead of `SOURCE_URL`
(`python -m scripts.source sync other.jsonl.gz`). Run them with `.\.venv\Scripts\python.exe`
from the repository directory. The endpoint is part of the catalog API, so it is off when
`CATALOG_API=0`. Requests block until done; with the API, `rebuild=true` takes precedence over
`dry_run=true`, so that combination rebuilds.

- A sync compares the file with the live index, which takes 2-3 minutes for the current
  catalog (about 300k documents), plus the writes.
- A rebuild takes about 12 minutes, mostly embedding the ~146k use cases. Search keeps using the
  current collection until the new one is complete, then the `cuspera_pages` alias switches to it
  in one step; if the import fails, the new collection is deleted and nothing changes. The
  collection that was live before is kept (older ones are deleted) for comparing and rollback:

```powershell
Invoke-RestMethod -Method Put "http://127.0.0.1:8108/aliases/cuspera_pages" -Headers @{"X-TYPESENSE-API-KEY"="xyz"} -ContentType application/json -Body '{"collection_name":"cuspera_pages_build_<timestamp>"}'
```

## Catalog API (create / update / delete)

Changes go live immediately, and everything derived from them is updated too: popularity
(1 + comparisons; inherited by pages and comparisons), page and comparison titles after a
rename, each use case's top products, vendor page popularity, and product categories.
Interactive docs: <http://127.0.0.1:8000/docs>. There is no authentication, so the write
API is for local use only; set `CATALOG_API=0` to switch it off (the Docker image does).

The API is shaped like the source data: product records as in `prod.json` (keyed by
`CFProdCode-<id>`), use-case lists as in `product_usecases.json` (identified by their real
UUIDs), and pages as website URLs. `POST` creates (201, or 409 if it already exists), `PATCH`
changes part of a record, `PUT` replaces a whole list, `DELETE` removes.

**Products**

| Endpoint | Body |
|---|---|
| `POST /api/products` | `{"tool_id": "CFProdCode-99999", "name": "Acme Signals", "url": "https://www.cuspera.com/products/acme-signals-x-99999"}` (`url` optional: without it the product is stored but hidden) |
| `GET /api/products/CFProdCode-{id}` | - (the record, its use cases, and whether it is searchable) |
| `PATCH /api/products/CFProdCode-{id}` | `name` and/or `url` (omitted fields are kept; `"url": null` removes the page) |
| `DELETE /api/products/CFProdCode-{id}` | - (also deletes its use cases, pages and comparisons) |

**Categories and industries**

| Endpoint | Body |
|---|---|
| `POST /api/categories`, `POST /api/industries` | `{"slug": "demand-generation", "name": "Demand Generation"}` |
| `GET /api/categories/{slug}`, `GET /api/industries/{slug}` | - |
| `PATCH /api/categories/{slug}`, `PATCH /api/industries/{slug}` | `{"name": "..."}` (renames) |
| `DELETE /api/categories/{slug}`, `DELETE /api/industries/{slug}` | - |

**News, customer stories, alternatives, comparisons**

| Endpoint | Body |
|---|---|
| `POST /api/pages` | `{"type": "news\|customer-story\|alternatives\|compare", "url": "...", "title": "...", "product_ids": [...], "popularity": 5}` — one `product_ids` entry links the page to a product, two make a comparison, none a vendor page (`title`, `product_ids`, `popularity` optional) |
| `GET /api/pages?url=...` | - (one page by URL) |
| `DELETE /api/pages?url=...` | - |

**Use cases**

| Endpoint | Body |
|---|---|
| `PUT /api/products/CFProdCode-{id}/use-cases` | a `product_usecases.json` list: `[{"id": "<uuid>", "name": "Lead scoring"}]` (replaces it) |
| `POST /api/products/CFProdCode-{id}/use-cases` | `{"id": "<uuid>", "name": "..."}` (appends one; returns the list) |
| `DELETE /api/products/CFProdCode-{id}/use-cases/{uuid}` | - (removes one; returns the list) |
| `PATCH /api/use-cases/{uuid}` | `{"name": "..."}` (renames it for every product) |
| `DELETE /api/use-cases/{uuid}` | - (removes it from every product) |

Deleting a product page (`PATCH` `"url": null`, or `DELETE /api/pages` on its URL) hides the
product but keeps its record and use cases. Errors: 404 not found, 409 already exists,
422 invalid input.

```powershell
$api = "http://127.0.0.1:8000/api"
Invoke-RestMethod -Method Post "$api/products" -ContentType application/json -Body '{"tool_id": "CFProdCode-99999", "name": "Acme Signals", "url": "https://www.cuspera.com/products/acme-signals-x-99999"}'
Invoke-RestMethod -Method Post "$api/products/CFProdCode-99999/use-cases" -ContentType application/json -Body '{"id": "62eef0e3-6428-46cb-8149-c679c47d9ab6", "name": "Lead scoring"}'
Invoke-RestMethod -Method Post "$api/categories" -ContentType application/json -Body '{"slug": "signal-analytics", "name": "Signal Analytics"}'
Invoke-RestMethod -Method Post "$api/pages" -ContentType application/json -Body '{"type": "news", "url": "https://www.cuspera.com/products/acme-signals-x-99999/news", "product_ids": [99999]}'
Invoke-RestMethod -Method Delete "$api/products/CFProdCode-99999"
```

Environment overrides: `TYPESENSE_URL`, `TYPESENSE_API_KEY`,
`TYPESENSE_COLLECTION`, `CORS_ORIGINS` (comma-separated, default
`http://localhost:5500,http://127.0.0.1:5500`), `SOURCE_URL` (catalog source file, default
`data/catalog.jsonl.gz`).

## Semantic use-case search

Use-case names (`use_case` documents) are also searched by meaning: Typesense embeds them
with its built-in `ts/all-MiniLM-L12-v2` model at index time and embeds each query, so
"find out which companies visit my site" finds "Website visitor identification". This is
pure vector search (no keyword matching), closest meaning first. The first indexer run downloads
the model (~1 minute); embedding all use cases adds a few minutes.

Settings: `SEMANTIC_USE_CASES` (`1`/`0`, default `1`; the Render image sets `0`, too slow and
too much memory on the free plan; use cases then fall back to keyword search), `EMBED_MODEL`,
`SEMANTIC_MAX_DISTANCE` (cosine distance cut-off, default `0.45`). Changing
`SEMANTIC_USE_CASES` or `EMBED_MODEL` requires re-running `indexer.py` (or a source-file rebuild); `export_index.py`
leaves the vectors out.