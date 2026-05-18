"""
Step 19 — Apply manual league overrides across all tables.

Some clubs have recently been promoted or relegated between league tiers, but
neither dcaribou (weekly snapshot) nor our TM scrape have caught up. The
workbook source may also be lagging on the same clubs. This script applies
hand-curated overrides so the league displayed on every output matches the
user's current ground truth.

Cascades the override across every table that carries a league_id:
    player_universe        (current_club_id     → set league/league_id)
    senior_roster          (club_id             → set league/league_id)
    club_pressure          (club_id             → set league/league_id)
    map_club_overview      (club_id             → set league)
    map_club_tracker       (club_id             → set league)
    map_club_requests      (club_id             → set league)
And re-derives map_demand_signal from the corrected map_club_requests.

Overrides are stored in data/manual_league_overrides.csv (seeded on first run,
user-editable thereafter). Each row is:
    club_id, club_name, override_league_id, override_league_display, reason

To add or remove an override, edit the CSV and re-run. Empty CSV = no-op.

Pipeline position:
    07_extend_roster.py → 19_apply_league_overrides.py → 08_compute_pressure.py
    (and again as a tail step after 16/17/18 if you re-load workbook data)
"""

import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

OVERRIDES_CSV = Path("data/manual_league_overrides.csv")

SEED_OVERRIDES: list[dict] = [
    # Promoted from GB2 → GB1 (Championship → Premier League)
    {"club_id": "990",  "club_name": "Coventry City",
     "override_league_id": "GB1", "override_league_display": "Premier League",
     "reason": "promoted from Championship for 26/27 (workbook/dcaribou still stale)"},
    {"club_id": "677",  "club_name": "Ipswich Town",
     "override_league_id": "GB1", "override_league_display": "Premier League",
     "reason": "promoted from Championship for 26/27 (workbook/dcaribou still stale)"},
    # Relegated from GB1 → GB2 (Premier League → Championship)
    {"club_id": "543",  "club_name": "Wolverhampton Wanderers Football Club",
     "override_league_id": "GB2", "override_league_display": "Championship",
     "reason": "relegated from Premier League for 26/27 (workbook/dcaribou still stale)"},
    {"club_id": "1132", "club_name": "Burnley Football Club",
     "override_league_id": "GB2", "override_league_display": "Championship",
     "reason": "relegated from Premier League for 26/27 (workbook/dcaribou still stale)"},
]


def _seed_csv_if_missing() -> None:
    if OVERRIDES_CSV.exists():
        return
    OVERRIDES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OVERRIDES_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "club_id", "club_name",
            "override_league_id", "override_league_display", "reason",
        ])
        writer.writeheader()
        for row in SEED_OVERRIDES:
            writer.writerow(row)
    print(f"  Seeded {OVERRIDES_CSV} with {len(SEED_OVERRIDES)} default override(s)")


def _load_overrides() -> list[dict]:
    if not OVERRIDES_CSV.exists():
        return []
    rows = []
    with OVERRIDES_CSV.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("club_id") or not r.get("override_league_id"):
                continue
            rows.append({
                "club_id": str(r["club_id"]).strip(),
                "club_name": r.get("club_name", "").strip(),
                "league_id": r["override_league_id"].strip(),
                "league_display": r.get("override_league_display", "").strip(),
                "reason": r.get("reason", "").strip(),
            })
    return rows


# Tables to update: (table, league_col, lid_col, key_col)
CASCADE_TABLES = [
    ("player_universe",   "league", "league_id", "current_club_id"),
    ("senior_roster",     "league", "league_id", "club_id"),
    ("club_pressure",     "league", "league_id", "club_id"),
    ("map_club_overview", "league", None,        "club_id"),
    ("map_club_tracker",  "league", None,        "club_id"),
    ("map_club_requests", "league", None,        "club_id"),
]


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def apply_overrides(con: sqlite3.Connection, overrides: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ov in overrides:
        cid = ov["club_id"]
        new_lid = ov["league_id"]
        new_display = ov["league_display"] or new_lid
        # For map_* tables, we store the canonical league CODE in the `league`
        # column (loader normalised "England 1" → "GB1"). For first-party tables,
        # `league` holds the display name (e.g. "Premier League").
        for table, lg_col, lid_col, key_col in CASCADE_TABLES:
            if not _table_exists(con, table):
                continue
            value_for_lg = new_lid if table.startswith("map_") else new_display
            sql_set = f"{lg_col} = ?"
            params: list = [value_for_lg]
            if lid_col:
                sql_set += f", {lid_col} = ?"
                params.append(new_lid)
            params.append(cid)
            cur = con.execute(
                f"UPDATE {table} SET {sql_set} WHERE {key_col} = ?",
                params,
            )
            counts[table] = counts.get(table, 0) + cur.rowcount
    return counts


def _rederive_demand_signal(con: sqlite3.Connection) -> int:
    """Same logic as scripts/16_load_market_maps.py:derive_demand_signal."""
    if not _table_exists(con, "map_demand_signal") or not _table_exists(con, "map_club_requests"):
        return 0
    snapshot = str(config.SNAPSHOT_DATE)
    con.execute("DELETE FROM map_demand_signal WHERE snapshot_date = ?", (snapshot,))
    rows = con.execute("""
        SELECT league, position_bucket,
               COUNT(DISTINCT club_name) AS request_count,
               GROUP_CONCAT(DISTINCT club_name) AS clubs
        FROM map_club_requests
        WHERE position_bucket IS NOT NULL
          AND snapshot_date = ?
        GROUP BY league, position_bucket
        ORDER BY league, position_bucket
    """, (snapshot,)).fetchall()
    con.executemany("""
        INSERT INTO map_demand_signal (league, position_bucket, request_count, clubs, snapshot_date)
        VALUES (?, ?, ?, ?, ?)
    """, [(lg, b, n, clubs, snapshot) for lg, b, n, clubs in rows])
    return len(rows)


def main() -> None:
    _seed_csv_if_missing()
    overrides = _load_overrides()
    if not overrides:
        print(f"No overrides in {OVERRIDES_CSV} — nothing to do.")
        return

    print(f"Applying {len(overrides)} league override(s) from {OVERRIDES_CSV}:")
    for ov in overrides:
        print(f"  {ov['club_name']:48s} → {ov['league_id']} ({ov['league_display']})"
              f"   [{ov['reason']}]")
    print()

    with sqlite3.connect(config.SQLITE_FILE) as con:
        counts = apply_overrides(con, overrides)
        n_ds = _rederive_demand_signal(con)
        con.commit()

    print("Rows updated:")
    for table, _, _, _ in CASCADE_TABLES:
        n = counts.get(table, 0)
        print(f"  {table:22s} {n}")
    if n_ds:
        print(f"  map_demand_signal      re-derived ({n_ds} rows)")
    print()
    print("NOTE: this script only rewrites labels. It does not recompute pressure")
    print("scores. If a club's league change affects per-league net_spend pooling,")
    print("re-run 08_compute_pressure.py AFTER this script to refresh component 3.")


if __name__ == "__main__":
    main()
