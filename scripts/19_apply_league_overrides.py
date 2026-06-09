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
    # Promoted Championship → Premier League for 26/27
    {"club_id": "990",  "club_name": "Coventry City",
     "override_league_id": "GB1", "override_league_display": "Premier League",
     "reason": "promoted to Premier League for 26/27"},
    {"club_id": "677",  "club_name": "Ipswich Town",
     "override_league_id": "GB1", "override_league_display": "Premier League",
     "reason": "promoted to Premier League for 26/27"},
    {"club_id": "3008", "club_name": "Hull City",
     "override_league_id": "GB1", "override_league_display": "Premier League",
     "reason": "promoted to Premier League for 26/27"},
    # Relegated Premier League → Championship end of 25/26
    {"club_id": "543",  "club_name": "Wolverhampton Wanderers Football Club",
     "override_league_id": "GB2", "override_league_display": "Championship",
     "reason": "relegated from Premier League end of 25/26"},
    {"club_id": "1132", "club_name": "Burnley Football Club",
     "override_league_id": "GB2", "override_league_display": "Championship",
     "reason": "relegated from Premier League end of 25/26"},
    {"club_id": "379",  "club_name": "West Ham United Football Club",
     "override_league_id": "GB2", "override_league_display": "Championship",
     "reason": "relegated from Premier League end of 25/26"},
]


# Recently-relegated/promoted classification — derived from the override
# direction in the CSV's `reason` text. Drives club_pressure.recently_relegated
# and recently_promoted, which feed mandate_priority_multiplier downstream.
def _classify_movement(reason: str) -> tuple[int, int]:
    """Return (recently_relegated, recently_promoted) booleans (0/1)."""
    r = (reason or "").lower()
    if "relegat" in r:
        return 1, 0
    if "promot" in r:
        return 0, 1
    return 0, 0


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
    # Additional cascade on parent_club_id catches loaned-out players whose
    # current_club_id is a different (loan-destination) club. Without this,
    # e.g. a Wolves loaned-out player would keep league_id='GB1' after Wolves
    # is overridden to GB2.
    ("player_universe",   "league", "league_id", "parent_club_id"),
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


def apply_recent_movement_flags(
    con: sqlite3.Connection, overrides: list[dict],
) -> dict[str, int]:
    """Stamp recently_relegated / recently_promoted on club_pressure and
    cascade parent_club_recently_relegated + mandate_priority_multiplier
    onto player_universe via parent_club_id.

    mandate_priority_multiplier (sell-side):
      1.3 — recently relegated (Wolves, Burnley, West Ham). Elevated mandate
            priority: structural sell pressure, more valuable squad than the
            typical relegated cohort.
      1.0 — everyone else, INCLUDING recently-promoted clubs.

    Note (2026-06-04): promoted clubs (Coventry/Ipswich/Hull + the 10
    European newly-promoted) used to carry 1.1 here, but they are strategic
    BUYERS, not mandate sellers — boosting their squad's sell-side priority
    is wrong. Promoted clubs surface on the buyer side via Mandate
    Territory's "promoted-buyer panel" instead. See CLAUDE.md §Mandate
    priority.
    """
    if not _table_exists(con, "club_pressure"):
        return {}
    # Zero out previous run so removed overrides revert to 0.
    con.execute("UPDATE club_pressure SET recently_relegated = 0, recently_promoted = 0")
    if _table_exists(con, "player_universe"):
        con.execute(
            "UPDATE player_universe SET parent_club_recently_relegated = 0, "
            "mandate_priority_multiplier = 1.0"
        )

    counts = {"club_pressure_rel": 0, "club_pressure_pro": 0, "player_universe": 0}
    for ov in overrides:
        cid = ov["club_id"]
        rel, pro = _classify_movement(ov.get("reason", ""))
        if rel == 0 and pro == 0:
            continue
        cur = con.execute(
            "UPDATE club_pressure SET recently_relegated = ?, recently_promoted = ? "
            "WHERE club_id = ?",
            (rel, pro, cid),
        )
        if rel:
            counts["club_pressure_rel"] += cur.rowcount
        if pro:
            counts["club_pressure_pro"] += cur.rowcount

        if _table_exists(con, "player_universe"):
            # Relegated → 1.3 sell-side boost. Promoted → 1.0 (no sell-side boost;
            # they surface on the buyer side via Mandate Territory).
            multiplier = 1.3 if rel else 1.0
            cur2 = con.execute(
                "UPDATE player_universe SET parent_club_recently_relegated = ?, "
                "mandate_priority_multiplier = ? WHERE parent_club_id = ?",
                (rel, multiplier, cid),
            )
            counts["player_universe"] += cur2.rowcount
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
        movement = apply_recent_movement_flags(con, overrides)
        n_ds = _rederive_demand_signal(con)
        con.commit()

    print("Rows updated:")
    for table, _, _, _ in CASCADE_TABLES:
        n = counts.get(table, 0)
        print(f"  {table:22s} {n}")
    if n_ds:
        print(f"  map_demand_signal      re-derived ({n_ds} rows)")
    if movement:
        print()
        print("Recently-relegated/promoted flags applied:")
        print(f"  club_pressure   recently_relegated set on {movement['club_pressure_rel']} club(s)")
        print(f"  club_pressure   recently_promoted  set on {movement['club_pressure_pro']} club(s)")
        print(f"  player_universe mandate_priority_multiplier set on {movement['player_universe']} row(s)")
    print()
    print("NOTE: this script only rewrites labels. It does not recompute pressure")
    print("scores. If a club's league change affects per-league net_spend pooling,")
    print("re-run 08_compute_pressure.py AFTER this script to refresh component 3.")


if __name__ == "__main__":
    main()
