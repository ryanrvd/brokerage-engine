"""
Step 02 — Initialise the SQLite database with our two tables.

Idempotent: drops and recreates the tables on each run. Safe because the
authoritative data lives in the DuckDB source file; re-running step 03
repopulates SQLite from scratch.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

SCHEMA = """
DROP TABLE IF EXISTS player_universe;
CREATE TABLE player_universe (
    player_id              INTEGER PRIMARY KEY,
    name                   TEXT NOT NULL,
    current_club           TEXT,
    current_club_id        TEXT,
    league                 TEXT,
    league_id              TEXT,
    primary_position       TEXT,    -- broad: Defender / Midfield / Attack / Goalkeeper
    sub_position           TEXT,    -- granular: e.g. Centre-Forward, Left Winger
    age                    INTEGER, -- completed years as of snapshot_date
    date_of_birth          DATE,
    current_tm_value_eur   INTEGER,
    contract_end_date      DATE,
    last_fee_paid_eur      INTEGER, -- most recent transfer with fee > 0; NULL = academy
    last_fee_paid_date     DATE,
    minutes_last_18m       INTEGER,
    appearances_last_18m   INTEGER,
    minutes_available_18m  INTEGER,
    minutes_share_pct      REAL,
    agency                 TEXT,
    foot                   TEXT,
    height_cm              INTEGER,
    nationality            TEXT,
    -- Boolean flag columns (stored as 0/1 in SQLite)
    right_priced           INTEGER,    -- TM value ≥ last fee paid (NULL fee passes)
    finished_product       INTEGER,    -- ≥ 50% minutes share over the window (NULL = unknown)
    contract_leveraged     INTEGER,    -- contract ends within CONTRACT_LEVERAGED_YEARS
    -- Day 3 additions
    position_bucket        TEXT,       -- mapped 10-bucket position (GK/CB/LB/RB/DM/CM/AM/LW/RW/ST_CF)
    sellability_score      REAL,       -- 0-100; populated by 09_compute_sellability.py
    -- Day 3.5 loan additions (populated by 11_patch_loans.py from TM profile pages)
    parent_club            TEXT,       -- legal owner; equals current_club for non-loaned
    parent_club_id         TEXT,       -- owner's club_id (for joining to club_pressure)
    on_loan                INTEGER,    -- 1 if parent_club != current_club, else 0
    -- Source provenance — useful when we merge scraped second-tier data
    data_source            TEXT NOT NULL DEFAULT 'dcaribou',
    snapshot_date          DATE NOT NULL,
    -- Day 8: sellability as tag (not filter) + loan metadata
    sellability_status     TEXT,       -- sellable_now / sellable_with_caveat / not_sellable / out_of_scope
    loan_end_date          DATE        -- populated from TM scrape's loans-out page when on_loan=1
);

DROP TABLE IF EXISTS club_pressure;
CREATE TABLE club_pressure (
    club_id                    TEXT PRIMARY KEY,
    name                       TEXT NOT NULL,
    league                     TEXT,
    league_id                  TEXT,
    -- 5 pressure components (0-100 each), populated by 08_compute_pressure.py
    contract_leverage_score    REAL,   -- 25% weight
    squad_oversupply_score     REAL,   -- 20% weight
    net_spend_score            REAL,   -- 20% weight
    manager_change_flag        INTEGER,-- 15% weight (manual 0/1)
    public_must_sell_flag      INTEGER,-- 20% weight (manual 0/1)
    total_pressure_score       REAL,   -- weighted total, 0-100
    top_3_likely_to_move       TEXT,   -- "Name1; Name2; Name3" from 09_compute_sellability.py
    scoring_basis              TEXT,   -- notes e.g. "headcount-weighted; net_spend unavailable"
    snapshot_date              DATE NOT NULL
);

DROP TABLE IF EXISTS senior_roster;
CREATE TABLE senior_roster (
    player_id            INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    club_id              TEXT,
    club_name            TEXT,
    league_id            TEXT,
    league               TEXT,
    sub_position         TEXT,
    position_bucket      TEXT,         -- mapped via 08_compute_pressure.py POSITION_BUCKETS
    age                  INTEGER,
    contract_end_date    DATE,
    minutes_last_18m     INTEGER,      -- NULL for second-tier scraped rows
    contract_leveraged   INTEGER,      -- 1 if contract_end ≤ snapshot + CONTRACT_LEVERAGED_YEARS
    data_source          TEXT NOT NULL,-- 'dcaribou' or 'tm_squad_scrape'
    snapshot_date        DATE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pu_league ON player_universe(league_id);
CREATE INDEX IF NOT EXISTS idx_pu_club   ON player_universe(current_club_id);
CREATE INDEX IF NOT EXISTS idx_cp_league ON club_pressure(league_id);
CREATE INDEX IF NOT EXISTS idx_sr_club   ON senior_roster(club_id);
CREATE INDEX IF NOT EXISTS idx_sr_league ON senior_roster(league_id);

-- Day 8: SciSports linkage + recently-relegated/promoted flags.
-- Added via ALTER so existing positional INSERTs in scripts 03/06/28 keep
-- working (these columns default to NULL and are populated by 19/29/30).
ALTER TABLE player_universe ADD COLUMN scisports_player_id           INTEGER;
ALTER TABLE player_universe ADD COLUMN parent_club_recently_relegated INTEGER;
ALTER TABLE player_universe ADD COLUMN mandate_priority_multiplier   REAL DEFAULT 1.0;
ALTER TABLE club_pressure   ADD COLUMN recently_relegated INTEGER DEFAULT 0;
ALTER TABLE club_pressure   ADD COLUMN recently_promoted  INTEGER DEFAULT 0;
"""


def main() -> None:
    db_path = Path(config.SQLITE_FILE)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Initialising schema at {db_path}")
    with sqlite3.connect(db_path) as con:
        con.executescript(SCHEMA)
        for table in ("player_universe", "club_pressure", "senior_roster"):
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:18s} rows: {n}")
    print("Done.")


if __name__ == "__main__":
    main()
