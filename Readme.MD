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

Open <http://localhost:5500>. The frontend calls the API at `API_BASE` (top of the
`<script>` in `frontend/index.html`, default `http://127.0.0.1:8000`). If you host
the frontend elsewhere, update `API_BASE` and add its origin to `CORS_ORIGINS`. The first indexing run downloads the configured
sitemaps and may take some time. It builds a new collection and switches the
search alias after a successful import, then deletes older build collections.
Re-run `indexer.py` whenever you want to refresh the data (and after changing how
documents are built, e.g. titles or `popularity`; restart the API afterwards, since
category/industry aliases are cached in-process). Product popularity is
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
a minute to wake up plus the index load. To ship fresh data, re-run `indexer.py`
locally, then `.\.venv\Scripts\python.exe export_index.py`, commit the updated
`data/documents.jsonl.gz` and push (Render redeploys automatically).

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

Environment overrides: `TYPESENSE_URL`, `TYPESENSE_API_KEY`,
`TYPESENSE_COLLECTION`, `CORS_ORIGINS` (comma-separated, default
`http://localhost:5500,http://127.0.0.1:5500`).