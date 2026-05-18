"""
Step 20 — Scrape current manager + tenure + contract for every club in club_pressure.

For each of the ~354 clubs, fetch the TM Mitarbeiter (Staff) page:
    https://www.transfermarkt.com/--/mitarbeiter/verein/{club_id}

Parse the head-coach row (first row of the "Coaching Staff" section):
    name | title (Manager / Interim Manager / Caretaker Manager / ...)
         | appointment date (DD/MM/YYYY) | contract end (DD.MM.YYYY)

Politeness: 1.5s sleep between live fetches, 30s backoff on 429/503.
Caching: per-club HTML at data/tm_cache/manager_{club_id}.html — re-runs are instant.
Stores results in a new SQLite table `club_manager` (PK: club_id).

Feeds into 18_manual_flags_excel.py which derives manager_change_flag via
a four-rule auto-derivation (see CLAUDE.md "Manual flag pattern").
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


# ─── HTTP + cache ──────────────────────────────────────────────────────────────

def fetch(club_id: str) -> str | None:
    """Return staff-page HTML for a club_id; cached at data/tm_cache/manager_{cid}.html."""
    cache_path = CACHE_DIR / f"manager_{club_id}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    url = f"https://www.transfermarkt.com/--/mitarbeiter/verein/{club_id}"
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
    """Handles both DD/MM/YYYY (appointment) and DD.MM.YYYY (contract end)."""
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


def parse_head_coach(html: str) -> dict:
    """Extract head coach details from a Mitarbeiter page.

    Returns a dict with keys: manager_name, manager_title, appointment_date,
    contract_end_date. Values are None if not parsed. Missing 'Coaching Staff'
    section yields all-None.
    """
    out = {"manager_name": None, "manager_title": None,
           "appointment_date": None, "contract_end_date": None}
    soup = BeautifulSoup(html, "lxml")
    for h2 in soup.find_all("h2"):
        if h2.get_text(strip=True).lower() != "coaching staff":
            continue
        box = h2.find_parent("div", class_="box")
        if not box:
            return out
        rt = box.find("div", class_="responsive-table")
        if not rt:
            return out
        table = rt.find("table")
        if not table:
            return out
        tbody = table.find("tbody") or table
        first_row = tbody.find("tr")
        if not first_row:
            return out
        cells = first_row.find_all("td", recursive=False)
        if not cells:
            return out
        # Cell 0: inline-table with name + title
        inline = cells[0].find("table", class_="inline-table")
        if inline:
            link = inline.find("a")
            if link:
                out["manager_name"] = link.get_text(strip=True) or None
            title_rows = inline.find_all("tr")
            if len(title_rows) >= 2:
                title_td = title_rows[1].find("td")
                if title_td:
                    out["manager_title"] = title_td.get_text(strip=True) or None
        # Cell 3: appointment date; Cell 4: contract end date.
        # (Defend against TM column shuffles by checking cell count.)
        if len(cells) > 3:
            out["appointment_date"] = _parse_date_flexible(cells[3].get_text(strip=True))
        if len(cells) > 4:
            out["contract_end_date"] = _parse_date_flexible(cells[4].get_text(strip=True))
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
    manager_title        TEXT,
    appointment_date     DATE,
    contract_end_date    DATE,
    tenure_months        INTEGER,   -- computed: months between appointment_date and snapshot
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
                            if (CACHE_DIR / f"manager_{cid}.html").exists())
        print(f"Scraping manager data for {n_total} clubs "
              f"({cached_before} already cached, {n_total - cached_before} fresh fetches at "
              f"~{(n_total - cached_before) * SLEEP_BETWEEN / 60:.1f} min wall time)")
        print()

        _ensure_schema(con)
        con.commit()

        rows_to_insert: list[tuple] = []
        n_parsed = 0
        n_no_section = 0  # 'Coaching Staff' section missing
        n_no_name = 0     # section present but no name parsed (vacancy?)
        n_fetch_fail = 0

        for i, (cid, name, lid) in enumerate(clubs, start=1):
            html = fetch(cid)
            if html is None:
                n_fetch_fail += 1
                rows_to_insert.append((
                    cid, name, lid, None, None, None, None, None,
                    f"https://www.transfermarkt.com/--/mitarbeiter/verein/{cid}",
                    snapshot.isoformat(),
                ))
                continue
            parsed = parse_head_coach(html)
            if parsed["manager_name"] is None and parsed["manager_title"] is None:
                # Section likely missing or parse-fail
                if "Coaching Staff" in html:
                    n_no_name += 1
                else:
                    n_no_section += 1
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
                f"https://www.transfermarkt.com/--/mitarbeiter/verein/{cid}",
                snapshot.isoformat(),
            ))
            if i % 50 == 0 or i == n_total:
                print(f"  [{i}/{n_total}] {name[:36]:36s}  "
                      f"{parsed['manager_name'] or '(no manager)':28s}  "
                      f"{parsed['manager_title'] or '':24s}")

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
    print(f"Done. {n_parsed} clubs parsed with manager info, "
          f"{n_no_name} clubs with 'Coaching Staff' section but no name (likely vacancy), "
          f"{n_no_section} clubs missing 'Coaching Staff' section, "
          f"{n_fetch_fail} fetch failures.")
    # Show a quick title-distribution sanity check
    with sqlite3.connect(config.SQLITE_FILE) as con:
        print()
        print("Title distribution (top 15):")
        for title, n in con.execute("""
            SELECT manager_title, COUNT(*) FROM club_manager
            WHERE manager_title IS NOT NULL
            GROUP BY manager_title
            ORDER BY COUNT(*) DESC
            LIMIT 15
        """).fetchall():
            print(f"  {n:>4d}  {title}")
        n_interim = con.execute("""
            SELECT COUNT(*) FROM club_manager
            WHERE manager_title IS NOT NULL
              AND (lower(manager_title) LIKE '%interim%'
                   OR lower(manager_title) LIKE '%caretaker%'
                   OR lower(manager_title) LIKE '%acting%')
        """).fetchone()[0]
        print(f"\nClubs with interim/caretaker/acting title: {n_interim}")


if __name__ == "__main__":
    main()
