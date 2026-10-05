# Cuspera global search

FastAPI serves the search API and the frontend. Typesense is the search engine and
must be running separately; it can run without Docker under WSL 2 on Windows.

## Run on Windows without Docker

Install WSL 2 and Ubuntu if you do not already have them:

```powershell
wsl --install -d Ubuntu
```

Restart Windows if prompted. In Ubuntu (WSL), download and run Typesense:

```bash
curl -O https://dl.typesense.org/releases/27.1/typesense-server-27.1-linux-amd64.tar.gz
tar -xzf typesense-server-27.1-linux-amd64.tar.gz
mkdir -p ~/typesense-data
./typesense-server --data-dir="$HOME/typesense-data" --api-key=xyz --enable-cors
```

Keep that Ubuntu terminal open while using the app. These commands follow the
[official Typesense Linux binary instructions](https://typesense.org/docs/guide/install-typesense.html).

WSL normally forwards port `8108` to Windows `localhost`. If that does not
work, get the WSL IP with `wsl hostname -I` and set
`$env:TYPESENSE_URL = "http://<WSL-IP>:8108"` in the PowerShell window before
running the indexer and app below.

In PowerShell, from the repository directory:

```powershell
cd C:\Users\abhis\OneDrive\Desktop\Global_Search
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe indexer.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open <http://localhost:8000>. The first indexing run downloads the configured
sitemaps and may take some time. It builds a new collection and switches the
search alias after a successful import. Keep the terminal running while using
the app. In a second PowerShell window, you can check Typesense with:

```powershell
Invoke-RestMethod http://localhost:8108/health
```

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
`TYPESENSE_COLLECTION`.