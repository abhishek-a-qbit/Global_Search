"""Export the live search index to data/documents.jsonl.gz (bundled into the Docker image)."""
import gzip
from pathlib import Path

from config import ALIAS, make_client

EXPORT_FILE = Path(__file__).parent / "data" / "documents.jsonl.gz"


def main() -> None:
    jsonl = make_client(timeout_seconds=300).collections[ALIAS].documents.export(
        {"exclude_fields": "embedding",             # vectors: ~1.5 KB/doc, regenerated from use_case_text
         "filter_by": "type:!=record"})             # source records: only the (local) catalog API needs them
    EXPORT_FILE.parent.mkdir(exist_ok=True)
    with gzip.open(EXPORT_FILE, "wt", encoding="utf-8") as f:
        f.write(jsonl.rstrip("\n") + "\n")
    print(f"Exported {jsonl.count(chr(10)) + 1} documents to {EXPORT_FILE} "
          f"({EXPORT_FILE.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
