"""
Step 12 — patch parent_contract_end_date on player_universe.

dcaribou's `contract_expiration_date` (and our derived `contract_end_date`)
records the LOAN-period contract for loaned players, not the parent-club
contract. That's wrong for Bosman / contract-leverage reasoning: a loaned
player returning to his parent club isn't a Bosman risk, he just goes home.

TM player profile HTML has both:
  - "Contract expires"        → loan-period contract (same as what we store)
  - "Contract there expires"  → PARENT CLUB contract  ← we want this

For every loaned player in player_universe, parse the cached profile HTML
and populate a new column `parent_contract_end_date` with the "Contract there
expires" value. For non-loaned players, parent_contract_end_date is just
their existing contract_end_date.

Downstream consumers (sellability scoring, Player View narrative + Contract
Leverage flag card) should switch their Bosman-window check from
contract_end_date → parent_contract_end_date.

Cached HTML at data/tm_cache/profile_{player_id}.html. No network calls.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

CACHE_DIR = Path("data/tm_cache")


def _parse_date_dmy(text: str) -> date | None:
    if not text:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _info_table(html: str) -> dict[str, str]:
    """Build a label→value map from the TM profile page's info table."""
    soup = BeautifulSoup(html, "lxml")
    info: dict[str, str] = {}
    for lbl in soup.select(".info-table__content--regular"):
        val = (lbl.find_next_sibling("span", class_="info-table__content--bold")
               or lbl.find_next("span", class_="info-table__content--bold"))
        if val:
            info[lbl.get_text(strip=True).rstrip(":").strip()] = val.get_text(" ", strip=True)
    return info


def _ensure_column(con: sqlite3.Connection) -> None:
    cols = [r[1] for r in con.execute("PRAGMA table_info(player_universe)").fetchall()]
    if "parent_contract_end_date" not in cols:
        con.execute("ALTER TABLE player_universe ADD COLUMN parent_contract_end_date TEXT")
        con.commit()
        print("  + added column parent_contract_end_date to player_universe")


def main() -> None:
    with sqlite3.connect(config.SQLITE_FILE) as con:
        _ensure_column(con)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT player_id, name, on_loan, contract_end_date, parent_club
            FROM player_universe
        """).fetchall()

    # Baseline: every non-loaned row's parent contract == its existing date.
    # Updates are applied as a single batch at the end.
    updates: list[tuple[str | None, int]] = []
    loaned_changed: list[dict] = []
    loaned_missing_html: list[tuple[int, str]] = []
    loaned_missing_field: list[tuple[int, str]] = []

    for r in rows:
        pid = r["player_id"]
        on_loan = r["on_loan"] == 1
        contract_end = r["contract_end_date"]

        if not on_loan:
            # Non-loaned: parent_contract_end_date IS contract_end_date.
            updates.append((contract_end, pid))
            continue

        # Loaned — read cached profile HTML and look for "Contract there expires".
        html_path = CACHE_DIR / f"profile_{pid}.html"
        if not html_path.exists():
            loaned_missing_html.append((pid, r["name"]))
            updates.append((contract_end, pid))  # safe fallback — flag in summary
            continue

        info = _info_table(html_path.read_text(encoding="utf-8"))
        there_text = info.get("Contract there expires", "")
        parent_date = _parse_date_dmy(there_text)
        if parent_date is None:
            loaned_missing_field.append((pid, r["name"]))
            updates.append((contract_end, pid))  # safe fallback
            continue

        parent_iso = parent_date.isoformat()
        updates.append((parent_iso, pid))
        if parent_iso != contract_end:
            loaned_changed.append({
                "player_id":      pid,
                "name":           r["name"],
                "parent_club":    r["parent_club"],
                "loan_end_date":  contract_end,
                "parent_contract_end": parent_iso,
            })

    with sqlite3.connect(config.SQLITE_FILE) as con:
        con.executemany(
            "UPDATE player_universe SET parent_contract_end_date = ? WHERE player_id = ?",
            updates,
        )
        con.commit()

    # Recompute contract_leveraged based on the NEW parent_contract_end_date.
    # The flag is the Bosman-window check: ends within 2 years of end-of-season.
    # Before this patch the flag used contract_end_date — which for loaned
    # players was the loan end, falsely making every loanee a Bosman risk.
    leverage_cutoff = config.end_of_season_plus(
        config.SNAPSHOT_DATE, config.CONTRACT_LEVERAGED_YEARS
    ).isoformat()

    with sqlite3.connect(config.SQLITE_FILE) as con:
        # Snapshot before for diff
        before = {
            r[0]: r[1] for r in con.execute(
                "SELECT player_id, contract_leveraged FROM player_universe"
            ).fetchall()
        }
        con.execute("""
            UPDATE player_universe
               SET contract_leveraged = CASE
                   WHEN parent_contract_end_date IS NULL THEN 0
                   WHEN parent_contract_end_date <= ? THEN 1
                   ELSE 0
               END
        """, (leverage_cutoff,))
        con.commit()
        after = {
            r[0]: r[1] for r in con.execute(
                "SELECT player_id, contract_leveraged FROM player_universe"
            ).fetchall()
        }

    flipped_to_0 = [pid for pid in before if before[pid] == 1 and after[pid] == 0]
    flipped_to_1 = [pid for pid in before if before[pid] == 0 and after[pid] == 1]
    print()
    print(f"contract_leveraged recomputed using parent_contract_end_date (cutoff: {leverage_cutoff})")
    print(f"  flipped 1 → 0 (no longer Bosman): {len(flipped_to_0)}")
    print(f"  flipped 0 → 1 (newly Bosman):     {len(flipped_to_1)}")

    # ── Sanity summary ──
    total = len(rows)
    loaned_total = sum(1 for r in rows if r["on_loan"] == 1)
    print(f"player_universe rows: {total}")
    print(f"loaned players:       {loaned_total}")
    print(f"  parent_contract_end_date populated from TM HTML: "
          f"{loaned_total - len(loaned_missing_html) - len(loaned_missing_field)}")
    print(f"  loan players with date CHANGED from loan-end:    {len(loaned_changed)}")
    if loaned_missing_html:
        print(f"  loaned players w/ NO cached HTML (fallback to loan end): {len(loaned_missing_html)}")
        for pid, nm in loaned_missing_html[:8]: print(f"    {pid:>9d}  {nm}")
    if loaned_missing_field:
        print(f"  loaned players w/ no 'Contract there expires' in cache:  {len(loaned_missing_field)}")
        for pid, nm in loaned_missing_field[:8]: print(f"    {pid:>9d}  {nm}")

    print()
    print("Per-player parent-contract corrections (loan end → parent contract):")
    print(f"  {'player':<28s} {'parent club':<28s} {'loan end':<12s} {'parent ends':<12s}")
    print(f"  {'-'*28} {'-'*28} {'-'*12} {'-'*12}")
    for L in loaned_changed:
        print(f"  {L['name'][:28]:<28s} {(L['parent_club'] or '')[:28]:<28s} "
              f"{(L['loan_end_date'] or '')[:12]:<12s} {L['parent_contract_end'][:12]:<12s}")


if __name__ == "__main__":
    main()
