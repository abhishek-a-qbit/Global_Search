"""Load data/documents.jsonl.gz into an empty Typesense (container start on Render)."""
import gzip
import itertools
import time

from typesense.exceptions import ObjectNotFound, TypesenseClientError

from config import ALIAS, make_client
from export_index import EXPORT_FILE
from scripts.source import schema as _schema

BATCH_LINES = 2000                                   # streamed: keeps memory low on a 512 MB instance
client = make_client(timeout_seconds=300)


def wait_for_typesense(timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if client.operations.is_healthy():
                return
        except TypesenseClientError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("Typesense did not become healthy")
        time.sleep(1)


def main() -> None:
    wait_for_typesense()
    try:
        client.aliases[ALIAS].retrieve()
        print(f"Alias '{ALIAS}' already exists; skipping seed.")
        return
    except ObjectNotFound:
        pass

    t0 = time.monotonic()
    collection_name = f"{ALIAS}_build_{int(time.time())}"
    client.collections.create(_schema(collection_name))
    count = 0
    with gzip.open(EXPORT_FILE, "rt", encoding="utf-8") as f:
        while batch := list(itertools.islice(f, BATCH_LINES)):
            results = client.collections[collection_name].documents.import_("".join(batch), {"action": "upsert"})
            failed = [line for line in results.splitlines() if '"success":false' in line]
            if failed:
                raise RuntimeError(f"Typesense import failed: {failed[0]}")
            count += len(batch)
    client.aliases.upsert(ALIAS, {"collection_name": collection_name})
    print(f"Seeded {count} documents in {time.monotonic() - t0:.0f}s; search alias '{ALIAS}' is ready.")


if __name__ == "__main__":
    main()
