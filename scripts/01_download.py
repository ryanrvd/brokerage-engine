"""
Step 01 — Download the dcaribou Transfermarkt dataset.

This is the only script in the Day 1 pipeline that touches the network.
It downloads one DuckDB file (~few hundred MB) into ./data/ and then
prints the list of tables it contains so we can confirm the file is valid.

Idempotent: if the file already exists, we skip the download.
"""

import ssl
import sys
import urllib.request
from pathlib import Path

import certifi

# Make config.py importable when we run this script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

CHUNK_SIZE = 1024 * 1024  # download in 1 MB chunks


def main() -> None:
    dest = Path(config.DUCKDB_FILE)
    if dest.exists():
        size_mb = dest.stat().st_size / 1e6
        print(f"Already have {dest} ({size_mb:.0f} MB). Skipping download.")
        _list_tables(dest)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Download to a .part file and rename on success, so an interrupted run
    # never leaves a half-finished file pretending to be valid.
    tmp = dest.with_suffix(dest.suffix + ".part")

    print(f"Downloading: {config.DUCKDB_URL}")
    print(f"         -> {dest}")

    # Use certifi's CA bundle so HTTPS works under the python.org installer on macOS.
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    # Cloudflare R2 rejects the default Python User-Agent; send a normal one.
    request = urllib.request.Request(
        config.DUCKDB_URL,
        headers={"User-Agent": "Mozilla/5.0 (yatin-matcher; +dataset-download)"},
    )
    with urllib.request.urlopen(request, context=ssl_context) as response:
        total_bytes = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        with open(tmp, "wb") as out:
            while chunk := response.read(CHUNK_SIZE):
                out.write(chunk)
                downloaded += len(chunk)
                if total_bytes:
                    pct = 100 * downloaded / total_bytes
                    print(
                        f"\r  {downloaded/1e6:>6.1f} / {total_bytes/1e6:.0f} MB ({pct:5.1f}%)",
                        end="",
                        flush=True,
                    )
                else:
                    print(f"\r  {downloaded/1e6:>6.1f} MB", end="", flush=True)
        print()  # newline after the progress meter

    tmp.rename(dest)
    print(f"Done. {dest.stat().st_size / 1e6:.0f} MB written to {dest}.")
    _list_tables(dest)


def _list_tables(path: Path) -> None:
    """Open the downloaded file read-only and print the table list — proves the file is intact."""
    import duckdb
    with duckdb.connect(str(path), read_only=True) as con:
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    print(f"Tables in file ({len(tables)}): {tables}")


if __name__ == "__main__":
    main()
