"""
Step 16 — Load the 8 manual Market Movement Map workbooks into the map_* tables.

Source: data/market_maps/*.xlsx (manually downloaded from Google Sheets — see
BACKLOG.md "Maps Auto-Sync Infrastructure" for the post-Yatin replacement plan).

Coverage: 10 of 19 leagues. England and France workbooks each contain both tiers.
Three Netherlands 2 rows (Eerste Divisie — outside our 19-league universe) are
skipped with a warning.

Loaded tabs per workbook:
    Club Overview   → map_club_overview
    Club Tracker    → map_club_tracker (manual 11-bucket + derived 10)
    Club Requests   → map_club_requests (translated to canonical 10-bucket;
                       "Either" expands to two rows for FB / WB / Winger)

map_demand_signal is DERIVED from map_club_requests after ingest, not from
the workbook's stale Live Demand Signal tab.

Idempotent: deletes existing source='manual_workbook' rows before insert.
Run after 15_define_demand_schema.py.
"""

import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

MARKET_MAPS_DIR = Path("data/market_maps")
SOURCE_TAG = "manual_workbook"

# ─── League normalisation ───────────────────────────────────────────────────────
# Workbook column values (Club Overview / Club Requests use "England 1" form,
# Club Tracker uses "ENG-1" form) → our canonical league codes.
LEAGUE_NORMAL = {
    "England 1": "GB1",  "ENG-1": "GB1",
    "England 2": "GB2",  "ENG-2": "GB2",
    "France 1":  "FR1",  "FRA-1": "FR1",
    "France 2":  "FR2",  "FRA-2": "FR2",
    "Italy 1":   "IT1",  "ITA-1": "IT1",
    "Spain 1":   "ES1",  "ESP-1": "ES1",
    "Germany 1": "L1",   "DEU-1": "L1",
    "Germany 2": "L2",   "DEU-2": "L2",
    "Netherlands 1": "NL1", "NLD-1": "NL1",
    "Portugal 1": "PO1", "POR-1": "PO1",
    "Belgium 1":  "BE1", "BEL-1": "BE1",
}
# Leagues we deliberately skip.
# Netherlands 2 — outside the 19-league universe (Eredivisie 2 not covered).
# Germany 2   — the Germany workbook covers only 1 of 18 2.Bundesliga clubs
#               (Kaiserslautern). Partial coverage was noise rather than signal,
#               so we filter it out entirely. Restore by removing "Germany 2"
#               and "DEU-2" if a fuller L2 workbook becomes available.
LEAGUE_SKIP = {"Netherlands 2", "NLD-2", "Germany 2", "DEU-2"}

# ─── Position translation (workbook → canonical 10-bucket) ─────────────────────
# Tier-A positions: bucket is independent of side.
POSITION_CATEGORY_DIRECT = {
    "Goalkeeper":          "GK",
    "Centre Back":         "CB",
    "Defensive Midfield":  "DM",
    "Centre Midfield":     "CM",
    "Attacking Midfield":  "AM",
    "Centre Forward":      "ST_CF",
}
# Side-dependent positions: bucket depends on Preferred Side.
POSITION_CATEGORY_SIDED = {
    "Full Back":  {"Left": "LB",  "Right": "RB",  "Either": ("LB", "RB"), "Centre": ("LB", "RB")},
    "Wing Back":  {"Left": "LB",  "Right": "RB",  "Either": ("LB", "RB"), "Centre": ("LB", "RB")},
    "Winger":     {"Left": "LW",  "Right": "RW",  "Either": ("LW", "RW"), "Centre": ("LW", "RW")},
}

# Manual 11 → canonical 10 (RCB and LCB collapse to CB; rest are identity).
MANUAL11_TO_10 = {
    "GK": "GK",
    "RB": "RB", "LB": "LB",
    "RCB": "CB", "LCB": "CB",
    "DM": "DM", "CM": "CM", "AM": "AM",
    "RW": "RW", "LW": "LW",
    "CF": "ST_CF",
}
TRACKER_POSITION_ORDER = ["GK", "RB", "RCB", "LCB", "LB", "DM", "CM", "AM", "RW", "LW", "CF"]

# ─── Workbook discovery ─────────────────────────────────────────────────────────

def discover_workbooks() -> list[Path]:
    if not MARKET_MAPS_DIR.exists():
        raise FileNotFoundError(f"Missing directory {MARKET_MAPS_DIR.resolve()}")
    return sorted(MARKET_MAPS_DIR.glob("*.xlsx"))


# ─── Name matching ──────────────────────────────────────────────────────────────
# Workbook short names ("KAA Gent") vs DB legal names ("Koninklijke Atletiek
# Associatie Gent") rarely share full tokens. We normalise both sides (accent strip,
# lowercase, punctuation, drop ultra-common club-codes & filler tokens), then score
# by length-weighted token match. Full-token match scores 2×token_length; substring
# match (for tokens ≥4 chars) scores 1×. Tiebreak: prefer DB names with fewer
# non-matched extra tokens.
#
# Tokens like "real", "atletico", "athletic", "sporting", "royal", "olympique" are
# deliberately NOT stopworded — they're distinguishing within a league (Real Madrid
# vs Atlético Madrid, Sporting Braga vs Sporting Lisboa).

STOPWORDS = {
    # Pure club-code abbreviations — short prefixes/suffixes that carry no info
    "fc", "afc", "ac", "as", "sc", "cd", "cf", "ce", "gd", "sd", "sl", "sv", "sa",
    "rc", "rcd", "rsc", "rcde", "krc", "kaa", "kvc", "kvv", "kv", "fcv", "fco",
    "fcd", "ssc", "sps", "vfb", "vfl", "vfr", "spvgg", "ssv", "tsv", "tsg", "msv",
    "kfc", "fk", "nk", "rk", "dk", "ag", "ev", "ek", "ec", "ud", "hsc", "hsv",
    "spa", "ltd", "ltda", "sad", "ngv",
    # German cluster — extremely common, safe to drop
    "verein", "fussball", "fussballverein", "sportverein", "sport",
    # Dutch / Belgian cluster — extremely common, safe to drop
    "voetbalvereniging", "voetbal", "koninklijke", "sportvereniging",
    "atletiek", "associatie",
    # Filler prepositions / articles (post-NFKD strips accents)
    "de", "da", "do", "del", "la", "le", "el", "von", "fur", "und", "y", "e",
}

# Manual name overrides for clubs the fuzzy matcher genuinely can't disambiguate.
# Maps (workbook_name, canonical_league) → unique substring of the DB name.
# Add to this dict if a new ambiguous case surfaces.
MANUAL_NAME_OVERRIDES: dict[tuple[str, str], str] = {
    # "Sporting CP" and "Sporting Clube de Braga" tie on "sporting" + extras — "CP"
    # is the user-facing shorthand for "Clube de Portugal" but doesn't substring-match.
    ("Sporting CP", "PO1"): "Sporting Clube de Portugal",
    # "AJ Auxerre" — "AJ" is the shorthand for "Association de la Jeunesse"; "auxerre"
    # is not a substring of the DB's "auxerroise" (e/o differ at position 7).
    ("AJ Auxerre", "FR1"): "Association de la Jeunesse auxerroise",
}


def _normalise(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t and t not in STOPWORDS and not t.isdigit()]


def _score_candidates(
    wb_tokens: list[str],
    candidates: list[tuple[str, str]],
) -> list[tuple[int, int, int, str, str]]:
    """Returns sorted list of (score, extras, full_matches, club_id, db_name)."""
    scored: list[tuple[int, int, int, str, str]] = []
    for cid, dbn in candidates:
        db_tokens = _normalise(dbn)
        if not db_tokens:
            continue
        joined = " ".join(db_tokens)
        score = 0
        full_matches = 0
        matched_wb = 0
        for tok in wb_tokens:
            if tok in db_tokens:
                score += 2 * len(tok)
                full_matches += 1
                matched_wb += 1
            elif len(tok) >= 4 and tok in joined:
                score += len(tok)
                matched_wb += 1
        if score > 0:
            extras = len(db_tokens) - matched_wb
            scored.append((score, extras, full_matches, cid, dbn))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored


def match_club_id(
    workbook_name: str,
    league: str,
    db_clubs: list[tuple[str, str]],
    db_clubs_by_league: dict[str, list[tuple[str, str]]] | None = None,
) -> tuple[str | None, str]:
    """Return (club_id, status).
    status ∈ {'matched', 'matched-cross-league', 'unmatched', 'ambiguous'}.

    Tries the workbook-tagged league first (strict in-league match). If that
    fails, falls back to other leagues with a STRICTER threshold: every WB
    token must be a full-token match (no substring fallback). This catches
    point-in-time disagreements where a club was tagged to a different league
    in the workbook than what dcaribou + senior_roster show (e.g. Schalke
    tagged L1 in workbook, sits at L2 in the DB).
    """
    # Manual override path — wins outright if the named substring is found in the league.
    override = MANUAL_NAME_OVERRIDES.get((workbook_name, league))
    if override:
        needle = _normalise(override)
        for cid, dbn in db_clubs:
            db_norm = _normalise(dbn)
            if all(t in db_norm for t in needle):
                return cid, "matched"
        return None, "unmatched"

    wb_tokens = _normalise(workbook_name)
    if not wb_tokens:
        return None, "unmatched"

    # 1. In-league scoring (existing behaviour).
    scored = _score_candidates(wb_tokens, db_clubs)
    if scored:
        top = scored[0]
        if len(scored) == 1 or scored[1][0] != top[0] or scored[1][1] != top[1]:
            return top[3], "matched"
        # Tied → ambiguous in-league. Fall through to cross-league rescue
        # below — a clean cross-league hit can still resolve it.

    # 2. Cross-league fallback. Search every OTHER league. Strict acceptance:
    #    every WB token must be a full-token match (no substring fallback).
    if db_clubs_by_league is None:
        return None, "unmatched"
    n_wb = len(wb_tokens)
    cross: list[tuple[int, int, int, str, str, str]] = []  # (score, extras, fulls, cid, dbn, lg)
    for other_lg, other_clubs in db_clubs_by_league.items():
        if other_lg == league:
            continue
        for score, extras, fulls, cid, dbn in _score_candidates(wb_tokens, other_clubs):
            if fulls == n_wb:  # every WB token must match in full
                cross.append((score, extras, fulls, cid, dbn, other_lg))
    if not cross:
        return None, "ambiguous" if scored else "unmatched"
    cross.sort(key=lambda x: (-x[0], x[1]))
    top = cross[0]
    if len(cross) > 1 and cross[1][0] == top[0] and cross[1][1] == top[1]:
        return None, "ambiguous"
    return top[3], "matched-cross-league"


# ─── Workbook readers ───────────────────────────────────────────────────────────

def _read_rows(wb, tab: str, header_row: int) -> list[tuple]:
    """Returns rows after the header. header_row is 0-indexed."""
    ws = wb[tab]
    rows = list(ws.iter_rows(values_only=True))
    return rows[header_row + 1:]


def load_club_overview(wb, path: Path, snapshot: str, skip_log: list[str]) -> list[dict]:
    out = []
    for row in _read_rows(wb, "Club Overview", header_row=1):
        club_name = row[0]
        league_raw = row[1]
        if not club_name or not league_raw:
            continue
        if league_raw in LEAGUE_SKIP:
            skip_log.append(f"{path.name}: skipped Club Overview row '{club_name}' ({league_raw})")
            continue
        league = LEAGUE_NORMAL.get(league_raw)
        if not league:
            skip_log.append(f"{path.name}: unknown league '{league_raw}' for '{club_name}'")
            continue
        out.append({
            "club_name": str(club_name).strip(),
            "league": league,
            "formation": row[2],
            "manager": row[3],
            "agent_preferences": row[4],
            "sci_rotation_level": row[5],
            "sci_first_team_level": row[6],
            "sci_key_player_level": row[7],
            "highest_transfer_fee_2526_eur": row[8],
            "highest_sale_2526_eur": row[9],
            "max_salary_pw_2526_eur": row[10],
            "source_file": path.name,
            "snapshot_date": snapshot,
        })
    return out


def load_club_tracker(wb, path: Path, snapshot: str, skip_log: list[str]) -> list[dict]:
    out = []
    for row in _read_rows(wb, "Club Tracker", header_row=0):
        club_name = row[0]
        league_raw = row[1]
        if not club_name or not league_raw:
            continue
        if league_raw in LEAGUE_SKIP:
            skip_log.append(f"{path.name}: skipped Club Tracker row '{club_name}' ({league_raw})")
            continue
        league = LEAGUE_NORMAL.get(league_raw)
        if not league:
            skip_log.append(f"{path.name}: unknown tracker league '{league_raw}' for '{club_name}'")
            continue
        # Position columns are row[2:13] in order: GK, RB, RCB, LCB, LB, DM, CM, AM, RW, LW, CF
        for i, bucket in enumerate(TRACKER_POSITION_ORDER):
            status = row[2 + i]
            if status not in ("Covered", "Open"):
                continue
            out.append({
                "club_name": str(club_name).strip(),
                "league": league,
                "position_bucket": bucket,
                "bucket_10": MANUAL11_TO_10[bucket],
                "status": status,
                "source_file": path.name,
                "snapshot_date": snapshot,
            })
    return out


def _translate_request_buckets(category: str | None, side: str | None) -> list[str]:
    """workbook (Position Category, Preferred Side) → list of 10-buckets.

    Returns a list because "Either" on side-dependent positions expands to two
    rows (LB + RB for Full Back/Wing Back, LW + RW for Winger).
    """
    if not category:
        return []
    cat = str(category).strip()
    if cat in POSITION_CATEGORY_DIRECT:
        return [POSITION_CATEGORY_DIRECT[cat]]
    if cat in POSITION_CATEGORY_SIDED:
        side_key = (side or "Either").strip() if side else "Either"
        mapping = POSITION_CATEGORY_SIDED[cat]
        result = mapping.get(side_key, mapping["Either"])
        return list(result) if isinstance(result, tuple) else [result]
    return []  # unknown category — surfaced via miss-log


def load_club_requests(wb, path: Path, snapshot: str, skip_log: list[str]) -> list[dict]:
    out = []
    unknown_cats: set[str] = set()
    for row in _read_rows(wb, "Club Requests", header_row=0):
        club_name = row[1]
        league_raw = row[2]
        if not club_name or not league_raw:
            continue
        league_raw = str(league_raw).strip()
        if league_raw in LEAGUE_SKIP:
            skip_log.append(f"{path.name}: skipped Club Requests row '{club_name}' ({league_raw})")
            continue
        league = LEAGUE_NORMAL.get(league_raw)
        if not league:
            skip_log.append(f"{path.name}: unknown request league '{league_raw}' for '{club_name}'")
            continue
        category = row[3]
        side = row[4]
        buckets = _translate_request_buckets(category, side)
        if not buckets and category:
            unknown_cats.add(str(category))
        date_last_updated = row[0]
        if hasattr(date_last_updated, "date"):
            date_last_updated = date_last_updated.date().isoformat()
        elif date_last_updated:
            date_last_updated = str(date_last_updated)
        base = {
            "club_name": str(club_name).strip(),
            "league": league,
            "date_last_updated": date_last_updated,
            "position_category": category,
            "preferred_side": side,
            "role_notes": row[5],
            "max_transfer_fee_eur": row[6],
            "max_wage_pw_eur": row[7],
            "source": row[8],
            "validated": row[9],
            "validated_by": row[10],
            "linked_shortlisted_player": row[11],
            "source_file": path.name,
            "snapshot_date": snapshot,
        }
        if not buckets:
            # Preserve the row with NULL position_bucket so it's still queryable.
            out.append({**base, "position_bucket": None})
        else:
            for b in buckets:
                out.append({**base, "position_bucket": b})
    for c in sorted(unknown_cats):
        skip_log.append(f"{path.name}: unknown Position Category '{c}' — loaded with position_bucket=NULL")
    return out


# ─── Database writes ────────────────────────────────────────────────────────────

def _load_db_clubs_by_league(con: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """league_id → [(club_id, club_name), …] from club_pressure (all 19 leagues)."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for cid, name, lid in con.execute(
        "SELECT club_id, name, league_id FROM club_pressure"
    ).fetchall():
        out[lid].append((cid, name))
    return out


def _resolve_ids(
    rows: list[dict],
    db_clubs: dict[str, list[tuple[str, str]]],
    match_cache: dict[tuple[str, str], tuple[str | None, str]],
    miss_log: list[str],
    label: str,
) -> None:
    """Mutates rows in-place to add 'club_id'. match_cache shared across tabs/workbooks."""
    for r in rows:
        key = (r["club_name"], r["league"])
        if key not in match_cache:
            cid, status = match_club_id(
                r["club_name"], r["league"],
                db_clubs.get(r["league"], []),
                db_clubs_by_league=db_clubs,
            )
            match_cache[key] = (cid, status)
            if status not in ("matched", "matched-cross-league"):
                miss_log.append(f"  {label}: '{r['club_name']}' ({r['league']}) — {status}")
        r["club_id"] = match_cache[key][0]


def insert_overview(con: sqlite3.Connection, rows: list[dict]) -> int:
    con.executemany("""
        INSERT INTO map_club_overview
            (club_id, club_name, league, formation, manager, agent_preferences,
             sci_rotation_level, sci_first_team_level, sci_key_player_level,
             highest_transfer_fee_2526_eur, highest_sale_2526_eur, max_salary_pw_2526_eur,
             source, source_file, snapshot_date)
        VALUES (:club_id, :club_name, :league, :formation, :manager, :agent_preferences,
                :sci_rotation_level, :sci_first_team_level, :sci_key_player_level,
                :highest_transfer_fee_2526_eur, :highest_sale_2526_eur, :max_salary_pw_2526_eur,
                :source, :source_file, :snapshot_date)
    """, [{**r, "source": SOURCE_TAG} for r in rows])
    return len(rows)


def insert_tracker(con: sqlite3.Connection, rows: list[dict]) -> int:
    con.executemany("""
        INSERT INTO map_club_tracker
            (club_id, club_name, league, position_bucket, bucket_10, status,
             source, source_file, snapshot_date)
        VALUES (:club_id, :club_name, :league, :position_bucket, :bucket_10, :status,
                :source, :source_file, :snapshot_date)
    """, [{**r, "source": SOURCE_TAG} for r in rows])
    return len(rows)


def insert_requests(con: sqlite3.Connection, rows: list[dict]) -> int:
    con.executemany("""
        INSERT INTO map_club_requests
            (club_id, club_name, league, date_last_updated,
             position_category, preferred_side, position_bucket, role_notes,
             max_transfer_fee_eur, max_wage_pw_eur, source, validated,
             validated_by, linked_shortlisted_player, workbook_source,
             source_file, snapshot_date)
        VALUES (:club_id, :club_name, :league, :date_last_updated,
                :position_category, :preferred_side, :position_bucket, :role_notes,
                :max_transfer_fee_eur, :max_wage_pw_eur, :source, :validated,
                :validated_by, :linked_shortlisted_player, :workbook_source,
                :source_file, :snapshot_date)
    """, [{**r, "workbook_source": SOURCE_TAG} for r in rows])
    return len(rows)


def derive_demand_signal(con: sqlite3.Connection, snapshot: str) -> int:
    """Aggregate map_club_requests → map_demand_signal (league × position_bucket)."""
    con.execute(
        "DELETE FROM map_demand_signal WHERE snapshot_date = ?", (snapshot,)
    )
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


# ─── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    snapshot = str(config.SNAPSHOT_DATE)
    workbooks = discover_workbooks()
    if not workbooks:
        print(f"No workbooks found in {MARKET_MAPS_DIR.resolve()}")
        sys.exit(1)

    print(f"Loading {len(workbooks)} market map workbook(s) from {MARKET_MAPS_DIR}:")
    for p in workbooks:
        print(f"  - {p.name}")
    print()

    all_overview: list[dict] = []
    all_tracker: list[dict] = []
    all_requests: list[dict] = []
    skip_log: list[str] = []

    for path in workbooks:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        all_overview += load_club_overview(wb, path, snapshot, skip_log)
        all_tracker += load_club_tracker(wb, path, snapshot, skip_log)
        all_requests += load_club_requests(wb, path, snapshot, skip_log)
        wb.close()

    if skip_log:
        print(f"Skipped / unrecognised rows ({len(skip_log)} log entries):")
        for line in skip_log:
            print(f"  {line}")
        print()

    with sqlite3.connect(config.SQLITE_FILE) as con:
        # Wipe prior manual_workbook rows so re-runs are clean.
        con.execute("DELETE FROM map_club_overview WHERE source = ?", (SOURCE_TAG,))
        con.execute("DELETE FROM map_club_tracker  WHERE source = ?", (SOURCE_TAG,))
        con.execute("DELETE FROM map_club_requests WHERE workbook_source = ?", (SOURCE_TAG,))

        # Match workbook club names → TM club_id, cached across all tabs.
        db_clubs = _load_db_clubs_by_league(con)
        match_cache: dict[tuple[str, str], tuple[str | None, str]] = {}
        miss_log: list[str] = []
        _resolve_ids(all_overview, db_clubs, match_cache, miss_log, "overview")
        _resolve_ids(all_tracker,  db_clubs, match_cache, miss_log, "tracker")
        _resolve_ids(all_requests, db_clubs, match_cache, miss_log, "requests")

        n_ov = insert_overview(con, all_overview)
        n_tr = insert_tracker(con, all_tracker)
        n_rq = insert_requests(con, all_requests)
        n_ds = derive_demand_signal(con, snapshot)
        con.commit()

    # ── Reporting ──
    print(f"Inserted: {n_ov} club_overview / {n_tr} club_tracker / "
          f"{n_rq} club_requests / {n_ds} demand_signal rows")
    print()

    matched = sum(1 for v in match_cache.values() if v[1] == "matched")
    matched_xleague = sum(1 for v in match_cache.values() if v[1] == "matched-cross-league")
    unmatched = sum(1 for v in match_cache.values() if v[1] == "unmatched")
    ambiguous = sum(1 for v in match_cache.values() if v[1] == "ambiguous")
    total = len(match_cache)
    print(f"Club name → club_id matching: {matched + matched_xleague}/{total} matched "
          f"({matched_xleague} via cross-league fallback), "
          f"{unmatched} unmatched, {ambiguous} ambiguous")
    if miss_log:
        # Show one line per distinct miss (deduped across tabs).
        seen: set[str] = set()
        misses_unique = []
        for line in miss_log:
            key = line.split(":", 1)[1] if ":" in line else line
            if key not in seen:
                seen.add(key)
                misses_unique.append(line)
        print(f"Per-club misses ({len(misses_unique)} distinct):")
        for line in misses_unique:
            print(line)
    print()

    # Per-league counts (sanity print called for in the brief).
    with sqlite3.connect(config.SQLITE_FILE) as con:
        print("Clubs loaded into map_club_overview per league:")
        for lg, n in con.execute(
            "SELECT league, COUNT(*) FROM map_club_overview "
            "WHERE source=? GROUP BY league ORDER BY league", (SOURCE_TAG,)
        ).fetchall():
            print(f"  {lg:5s} {n}")
        print()
        print("Requests loaded into map_club_requests per league:")
        for lg, n in con.execute(
            "SELECT league, COUNT(*) FROM map_club_requests "
            "WHERE workbook_source=? GROUP BY league ORDER BY league", (SOURCE_TAG,)
        ).fetchall():
            print(f"  {lg:5s} {n}")
        print()
        print("Live Demand Signal — clubs requesting each position (across all 10 leagues):")
        print(f"  {'bucket':8s} {'clubs':>6s}")
        for b, n in con.execute("""
            SELECT position_bucket, SUM(request_count)
            FROM map_demand_signal
            GROUP BY position_bucket
            ORDER BY SUM(request_count) DESC
        """).fetchall():
            print(f"  {b:8s} {n:>6d}")


if __name__ == "__main__":
    main()
