"""
Step 15 — Define the demand-side schema (Day 4).

Creates the four `map_*` tables that the Market Movement Map workbooks feed into:

    map_club_overview   — one row per club    (formation, manager, agent prefs, wage cap, …)
    map_club_tracker    — one row per club × position (11-bucket manual vocab + derived 10)
    map_club_requests   — one row per stated request (the authoritative live signal)
    map_demand_signal   — derived aggregation (league × position_bucket → count + clubs list)

Schema is designed as the long-term contract. Today's source is `manual_workbook`
(snapshots downloaded from Google Sheets, see data/market_maps/). When the proper
Market Movement Maps build comes online post-Yatin, it will write into these same
tables for all 19 leagues — replacing the manual snapshot source.

Idempotent: drops and recreates every table on each run. Manual workbook data is
re-loaded by 16_load_market_maps.py, so nothing is lost.

Position bucket vocabularies in play:
    Manual 11 (Club Tracker source):    GK, RB, RCB, LCB, LB, DM, CM, AM, RW, LW, CF
    Canonical 10 (TM-derived, our std): GK, CB, LB, RB, DM, CM, AM, LW, RW, ST_CF

Trust hierarchy across tabs in the source workbooks:
    Club Requests       — AUTHORITATIVE live signal. Sheet 5 derives from this.
    Club Overview       — Authoritative descriptive context (manager, formation, wage cap).
    Club Tracker        — POTENTIALLY STALE human judgement; loaded for history, not consumed
                          by any Day 4 sheet.
    Live Demand Signal  — POTENTIALLY STALE; not ingested. We recompute from Club Requests
                          into map_demand_signal.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

SCHEMA = """
DROP TABLE IF EXISTS map_club_overview;
CREATE TABLE map_club_overview (
    club_id                       TEXT,           -- TM club_id (NULL if no match)
    club_name                     TEXT NOT NULL,  -- verbatim from workbook
    league                        TEXT NOT NULL,  -- canonical code (GB1, GB2, PO1, …)
    formation                     TEXT,
    manager                       TEXT,
    agent_preferences             TEXT,
    sci_rotation_level            INTEGER,
    sci_first_team_level          INTEGER,
    sci_key_player_level          INTEGER,
    -- Historical season-to-date figures from the workbook, NOT budget caps.
    -- (Budget caps live on map_club_requests.max_transfer_fee_eur / max_wage_pw_eur.)
    highest_transfer_fee_2526_eur INTEGER,
    highest_sale_2526_eur         INTEGER,
    max_salary_pw_2526_eur        INTEGER,
    source                        TEXT NOT NULL,  -- 'manual_workbook' for today's load
    source_file                   TEXT,           -- workbook filename for provenance
    snapshot_date                 DATE NOT NULL,
    PRIMARY KEY (club_name, league, snapshot_date)
);

DROP TABLE IF EXISTS map_club_tracker;
CREATE TABLE map_club_tracker (
    club_id          TEXT,           -- TM club_id (NULL if no match)
    club_name        TEXT NOT NULL,
    league           TEXT NOT NULL,
    position_bucket  TEXT NOT NULL,  -- manual 11-vocab verbatim: GK/RB/RCB/LCB/LB/DM/CM/AM/RW/LW/CF
    bucket_10        TEXT,           -- canonical 10-bucket (RCB+LCB collapsed to CB)
    status           TEXT NOT NULL,  -- 'Covered' or 'Open'
    source           TEXT NOT NULL,
    source_file      TEXT,
    snapshot_date    DATE NOT NULL,
    PRIMARY KEY (club_name, league, position_bucket, snapshot_date)
);

DROP TABLE IF EXISTS map_club_requests;
CREATE TABLE map_club_requests (
    request_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id                     TEXT,
    club_name                   TEXT NOT NULL,
    league                      TEXT NOT NULL,
    date_last_updated           DATE,
    position_category           TEXT,      -- raw workbook value: Goalkeeper / Centre Back / Full Back / …
    preferred_side              TEXT,      -- raw workbook value: Left / Right / Either / Centre
    position_bucket             TEXT,      -- derived canonical 10 (may differ from row to row when "Either"
                                           --   expands a single workbook request into two rows, e.g. LB and RB)
    role_notes                  TEXT,
    max_transfer_fee_eur        INTEGER,   -- per-request budget cap (this IS a cap, unlike Club Overview)
    max_wage_pw_eur             INTEGER,
    source                      TEXT,      -- workbook value: 'Intel' or 'Agent'
    validated                   TEXT,      -- 'YES' / 'NO' (and one stray 'Jouko' preserved verbatim)
    validated_by                TEXT,
    linked_shortlisted_player   TEXT,
    workbook_source             TEXT NOT NULL,  -- 'manual_workbook' (kept separate from 'source' to avoid clash)
    source_file                 TEXT,
    snapshot_date               DATE NOT NULL
);

DROP TABLE IF EXISTS map_demand_signal;
CREATE TABLE map_demand_signal (
    league           TEXT NOT NULL,
    position_bucket  TEXT NOT NULL,
    request_count    INTEGER NOT NULL,  -- distinct clubs with at least one request for this bucket
    clubs            TEXT,              -- comma-separated club names
    snapshot_date    DATE NOT NULL,
    PRIMARY KEY (league, position_bucket, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_mco_league ON map_club_overview(league);
CREATE INDEX IF NOT EXISTS idx_mco_club   ON map_club_overview(club_id);
CREATE INDEX IF NOT EXISTS idx_mct_league ON map_club_tracker(league);
CREATE INDEX IF NOT EXISTS idx_mct_club   ON map_club_tracker(club_id);
CREATE INDEX IF NOT EXISTS idx_mcr_league ON map_club_requests(league);
CREATE INDEX IF NOT EXISTS idx_mcr_club   ON map_club_requests(club_id);
CREATE INDEX IF NOT EXISTS idx_mcr_bucket ON map_club_requests(position_bucket);
CREATE INDEX IF NOT EXISTS idx_mds_league ON map_demand_signal(league);
"""


def main() -> None:
    db_path = Path(config.SQLITE_FILE)
    print(f"Defining demand-side schema at {db_path}")
    with sqlite3.connect(db_path) as con:
        con.executescript(SCHEMA)
        for table in ("map_club_overview", "map_club_tracker",
                      "map_club_requests", "map_demand_signal"):
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:22s} rows: {n}")
    print("Done.")


if __name__ == "__main__":
    main()
