"""
Step 20 — Scrape current manager + tenure for every club in club_pressure.

For each of the ~354 clubs, fetch the TM manager-history page:
    https://www.transfermarkt.com/--/mitarbeiterhistorie/verein/{club_id}/personalie/Trainer

This page is static HTML and lists head coaches newest-first in a single history
table (headers: Name/DoB | Nat | Appointed | End of time in post | Time in post |
Matches | PPG). The FIRST (newest) row is the current head coach. We take:
    manager_name      — from the trainer-profile link in the first row
    appointment_date  — the first row's "Appointed" date (drives tenure_months)

Why this page (Phase A fix — lifted from the maps repo's
src/mmm/sync/scrape_tm_manager.py): the old /mitarbeiter (Staff) page + "first
row of Coaching Staff" heuristic was wrong and fragile. For clubs where the head
coach isn't the first Coaching-Staff row — or isn't on that page at all
(Liverpool's /mitarbeiter lists only goalkeeping coaches) — it grabbed an
assistant. The history page's newest entry is canonical and TM-updated within
hours of any change.

What this page does NOT carry (so these columns are now NULL, where the old —
wrong-person — scraper used to guess them):
    manager_title       — no Manager/Interim/Caretaker column on the history page
    contract_end_date   — "End of time in post" is the departure date (blank for
                          the incumbent), not the manager's contract expiry

Politeness: 1.5s sleep between live fetches, 30s backoff on 429/503.
Caching: per-club HTML at data/tm_cache/manager_trainer_{club_id}.html — re-runs
are instant. (Distinct filename from the retired Staff-page cache so stale
wrong-page HTML is never reused.)
Stores results in SQLite table `club_manager` (PK: club_id).

Feeds into 18_manual_flags_excel.py which derives manager_change_flag (tenure ≤ 6mo
and vacancy rules still fire off manager_name + tenure_months; the interim-title
and contract-expiry rules no longer fire — they relied on the wrong-page data).
"""

import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import certifi
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SLEEP_BETWEEN = 1.5
CACHE_DIR = Path("data/tm_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_ssl_context = ssl.create_default_context(cafile=certifi.where())

_TRAINER_HREF = re.compile(r"/profil/trainer/\d+")


def _manager_history_url(club_id: str) -> str:
    return (f"https://www.transfermarkt.com/--/mitarbeiterhistorie/verein/"
            f"{club_id}/personalie/Trainer")


# ─── HTTP + cache ──────────────────────────────────────────────────────────────

def fetch(club_id: str) -> str | None:
    """Return manager-history-page HTML; cached at manager_trainer_{cid}.html."""
    cache_path = CACHE_DIR / f"manager_trainer_{club_id}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    url = _manager_history_url(club_id)
    time.sleep(SLEEP_BETWEEN)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            print(f"  ! HTTP {e.code} on club {club_id}, backing off 30s")
            time.sleep(30)
            try:
                with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e2:
                print(f"  ! HTTP {e2.code} again on club {club_id}: skipping")
                return None
        else:
            print(f"  ! HTTP {e.code} on club {club_id}: skipping")
            return None
    except Exception as e:
        print(f"  ! {type(e).__name__} on club {club_id}: {e}")
        return None
    cache_path.write_text(html, encoding="utf-8")
    return html


# ─── Parser ────────────────────────────────────────────────────────────────────

def _parse_date_flexible(text: str | None) -> date | None:
    """Handles DD/MM/YYYY and DD.MM.YYYY."""
    if not text:
        return None
    text = text.strip()
    if text in {"", "-", "—", "?"}:
        return None
    m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _row_manager_name(row) -> str | None:
    """First trainer-profile link with a non-empty name (title attr or text)."""
    for a in row.find_all("a", href=_TRAINER_HREF):
        name = (a.get("title") or a.get_text(strip=True) or "").strip()
        if name:
            return name
    return None


def parse_current_manager(html: str) -> dict:
    """Extract current head coach from a manager-history page.

    Returns {manager_name, manager_title, appointment_date, contract_end_date}.
    The history table is newest-first, so its first data row is the incumbent.
    title/contract_end are always None (not present on this page) — see module docstring.
    """
    out = {"manager_name": None, "manager_title": None,
           "appointment_date": None, "contract_end_date": None}
    soup = BeautifulSoup(html, "lxml")

    # Locate the history table by its header set (contains an "Appointed" column).
    table = None
    for tbl in soup.find_all("table"):
        heads = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
        if "appointed" in heads:
            table = tbl
            break

    if table is None:
        # Page-structure fallback: maps' doc-wide first non-empty trainer link.
        # Recovers the name even if the history table can't be located.
        for a in soup.find_all("a", href=_TRAINER_HREF):
            name = (a.get("title") or a.get_text(strip=True) or "").strip()
            if name:
                out["manager_name"] = name
                break
        return out

    header_idx: dict[str, int] = {}
    for tr in table.find_all("tr"):
        ths = tr.find_all("th")
        if ths:
            header_idx = {th.get_text(strip=True).lower(): i for i, th in enumerate(ths)}
            continue
        # First non-header row that carries a trainer link = newest manager.
        if not tr.find("a", href=_TRAINER_HREF):
            continue
        out["manager_name"] = _row_manager_name(tr)
        cells = tr.find_all("td", recursive=False)
        ai = header_idx.get("appointed")
        if ai is not None and ai < len(cells):
            out["appointment_date"] = _parse_date_flexible(cells[ai].get_text(strip=True))
        break
    return out


# ─── DB ────────────────────────────────────────────────────────────────────────

SCHEMA = """
DROP TABLE IF EXISTS club_manager;
CREATE TABLE club_manager (
    club_id              TEXT PRIMARY KEY,
    club_name            TEXT NOT NULL,
    league_id            TEXT,
    manager_name         TEXT,
    manager_title        TEXT,       -- NULL: not on the manager-history page
    appointment_date     DATE,
    contract_end_date    DATE,       -- NULL: not on the manager-history page
    tenure_months        INTEGER,    -- computed: months between appointment_date and snapshot
    source_url           TEXT,
    scraped_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cm_league ON club_manager(league_id);
"""


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def _months_between(a: date, b: date) -> int:
    """Whole-month count between a (older) and b (newer). Negative if a > b."""
    return (b.year - a.year) * 12 + (b.month - a.month) + (1 if b.day >= a.day else 0) - 1


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    snapshot = config.SNAPSHOT_DATE

    with sqlite3.connect(config.SQLITE_FILE) as con:
        clubs = con.execute("""
            SELECT club_id, name, league_id
            FROM club_pressure
            ORDER BY league_id, name
        """).fetchall()

        n_total = len(clubs)
        cached_before = sum(1 for cid, *_ in clubs
                            if (CACHE_DIR / f"manager_trainer_{cid}.html").exists())
        print(f"Scraping manager data for {n_total} clubs "
              f"({cached_before} already cached, {n_total - cached_before} fresh fetches at "
              f"~{(n_total - cached_before) * SLEEP_BETWEEN / 60:.1f} min wall time)")
        print()

        _ensure_schema(con)
        con.commit()

        rows_to_insert: list[tuple] = []
        n_parsed = 0
        n_no_name = 0     # history page reached but no trainer parsed (vacancy / new club)
        n_fetch_fail = 0

        for i, (cid, name, lid) in enumerate(clubs, start=1):
            html = fetch(cid)
            if html is None:
                n_fetch_fail += 1
                rows_to_insert.append((
                    cid, name, lid, None, None, None, None, None,
                    _manager_history_url(cid), snapshot.isoformat(),
                ))
                continue
            parsed = parse_current_manager(html)
            if parsed["manager_name"] is None:
                n_no_name += 1
            else:
                n_parsed += 1
            tenure_months = None
            if parsed["appointment_date"]:
                tm = _months_between(parsed["appointment_date"], snapshot)
                tenure_months = max(0, tm)
            rows_to_insert.append((
                cid, name, lid,
                parsed["manager_name"], parsed["manager_title"],
                parsed["appointment_date"].isoformat() if parsed["appointment_date"] else None,
                parsed["contract_end_date"].isoformat() if parsed["contract_end_date"] else None,
                tenure_months,
                _manager_history_url(cid), snapshot.isoformat(),
            ))
            if i % 50 == 0 or i == n_total:
                print(f"  [{i}/{n_total}] {name[:36]:36s}  "
                      f"{parsed['manager_name'] or '(no manager)':28s}  "
                      f"appt={parsed['appointment_date'] or '—'}")

        con.executemany("""
            INSERT INTO club_manager
                (club_id, club_name, league_id,
                 manager_name, manager_title,
                 appointment_date, contract_end_date, tenure_months,
                 source_url, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows_to_insert)
        con.commit()

    print()
    print(f"Done. {n_parsed} clubs parsed with a manager, "
          f"{n_no_name} clubs with no manager parsed (vacancy / new club / parse-miss), "
          f"{n_fetch_fail} fetch failures.")
    with sqlite3.connect(config.SQLITE_FILE) as con:
        n_with_appt = con.execute(
            "SELECT COUNT(*) FROM club_manager WHERE appointment_date IS NOT NULL"
        ).fetchone()[0]
        print(f"{n_with_appt} clubs have an appointment date (→ tenure_months).")
        print("\nSample (first 10 by league):")
        for nm, mgr, appt in con.execute("""
            SELECT club_name, manager_name, appointment_date FROM club_manager
            WHERE manager_name IS NOT NULL ORDER BY league_id, club_name LIMIT 10
        """).fetchall():
            print(f"  {nm[:34]:34s}  {mgr[:26]:26s}  {appt or '—'}")


if __name__ == "__main__":
    main()
