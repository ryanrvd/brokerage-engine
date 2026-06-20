"""
Step 16b — Load the Market Movement Maps export interface into the map_* tables.

Replaces scripts/16_load_market_maps.py (Phase 6, matcher migration). The maps
repo is now the single source of truth: it resolves every club to a Transfermarkt
id upstream, applies the promotion/relegation league overrides at source, and
publishes a stable, versioned export at:

    ~/market-movement-maps/exports/latest/      (override via MAPS_EXPORTS_PATH)

We read `_manifest.json` first (the entry point), validate the contract, then
read the per-table JSON files and upsert into the matcher's four map_* tables.
The cross-league name-matcher that used to live here is GONE — the export carries
`tm_club_id` for every club (in club.json), so we just join on that.

What we read from the export
----------------------------
    club.json               maps club_id  →  tm_club_id + display_name        (join key)
    league.json             maps league_id (ENG1/BEL1/…) → dcaribou_code (GB1/BE1/…)
    club_overview.json   →  map_club_overview      (also the budget source, see below)
    club_requests.json   →  map_club_requests      (source_origin='workbook' rows only)
    club_tracker.json    →  map_club_tracker
    (map_demand_signal is DERIVED from the loaded requests, exactly as the old
     loader did — the export's live_demand_signal has an incompatible per-club
     ranked-list shape, so a direct copy would break Sheets 5/6/7 and the app.)

Three deliberate transforms worth knowing about
-----------------------------------------------
1. Budget. The export's club_request rows no longer carry a per-request fee/wage
   cap (the match engine's budget filter needs both). In the old workbook those
   were effectively the club-level figures (identical for 184/185 clubs), so we
   reconstruct them by joining club_overview.workbook_highest_transfer_fee_eur and
   .max_salary_pw_eur onto each request by club. (club_budget_derived carries a
   newer, multiplier-adjusted figure — a future enhancement, not used here, to
   keep the migration parity check apples-to-apples.)
2. Position vocabulary. The export's canonical 10 use "CF"; the matcher uses
   "ST_CF". Every other code (GK/CB/LB/RB/DM/CM/AM/LW/RW) is identical. We map
   CF → ST_CF on the way in so request.position_bucket joins player.position_bucket.
3. Inference. The export's club_request includes maps-side inference rows
   (source_origin='inference'). The matcher runs its OWN inference (script 21,
   inferred_club_requests). We load only source_origin='workbook' rows here so the
   two inference layers don't double up — preserving the pre-migration behaviour.

Manual league overrides (data/manual_league_overrides.csv) are now WARNING-ONLY:
the maps pipeline applies the same overrides upstream, so we just cross-check each
entry against the export and log any disagreement rather than re-applying.

Idempotent. Run after 15_define_demand_schema.py (which (re)creates the tables).
"""

import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

# ─── Contract the matcher expects from the export ────────────────────────────────
SUPPORTED_SCHEMA_MAJOR = 1                 # bump only when this loader is updated
DEFAULT_EXPORTS_PATH = "~/market-movement-maps/exports/latest"
CALIBRATION_MIN_PCT = 50.0                 # sanity band — a broken maps run falls outside
CALIBRATION_MAX_PCT = 80.0

# Retained verbatim so downstream filters keep working unchanged — 17_export_demand_sheets
# selects WHERE workbook_source = 'manual_workbook'. The tag now means "the curated/explicit
# request layer" (as opposed to inferred), regardless of which pipeline produced it.
SOURCE_TAG = "manual_workbook"

LEAGUE_OVERRIDES_CSV = Path("data/manual_league_overrides.csv")

# Maps canonical position → matcher position_bucket. Only CF differs.
_CANON_TO_MATCHER = {"CF": "ST_CF"}


def to_matcher_bucket(pos: str | None) -> str | None:
    if pos is None:
        return None
    return _CANON_TO_MATCHER.get(pos, pos)


# ─── Export discovery + reading ──────────────────────────────────────────────────

def resolve_export_dir() -> Path:
    raw = os.environ.get("MAPS_EXPORTS_PATH", DEFAULT_EXPORTS_PATH)
    path = Path(os.path.expanduser(raw)).resolve()
    if not path.exists():
        sys.exit(
            f"FATAL: maps export path does not exist: {path}\n"
            f"  Set MAPS_EXPORTS_PATH or generate the export in ~/market-movement-maps "
            f"(PYTHONPATH=src python -m mmm.rules.run_phase_2_4b)."
        )
    if not (path / "_manifest.json").exists():
        sys.exit(
            f"FATAL: no _manifest.json in {path}\n"
            f"  This directory is not a maps export. Check MAPS_EXPORTS_PATH."
        )
    return path


def read_manifest(export_dir: Path) -> dict:
    return json.loads((export_dir / "_manifest.json").read_text(encoding="utf-8"))


def read_table(export_dir: Path, manifest: dict, key: str) -> list[dict]:
    """Read one export table's JSON rows via the filename the manifest advertises."""
    fname = manifest["tables"][key]["json"]
    payload = json.loads((export_dir / fname).read_text(encoding="utf-8"))
    return payload["rows"]


# ─── Manifest validation (the contract guard) ────────────────────────────────────

def validate_manifest(manifest: dict) -> None:
    """Hard-fail on contract violations; warn-and-continue on resolution gaps."""
    # 1. Schema major version must match what this loader understands.
    version = str(manifest.get("schema_version", ""))
    try:
        major = int(version.split(".")[0])
    except (ValueError, IndexError):
        sys.exit(f"FATAL: unparseable schema_version {version!r} in manifest.")
    if major != SUPPORTED_SCHEMA_MAJOR:
        sys.exit(
            f"FATAL: maps export schema_version is {version} (major {major}); this loader "
            f"supports major {SUPPORTED_SCHEMA_MAJOR}. The export schema bumped — "
            f"scripts/16b_load_maps_exports.py needs updating before it can read it."
        )

    # 2. validated_by must NOT be redacted — the matcher wants the raw Agent identity.
    redactions = manifest.get("redactions_applied") or []
    if "club_request.validated_by" in redactions:
        sys.exit(
            "FATAL: the maps export has club_request.validated_by REDACTED.\n"
            "  The matcher needs the unredacted validator identity. Regenerate the maps "
            "export with MMM_LOCAL_MODE=1 set on the maps' pipeline run."
        )

    # 3. Calibration agreement must be in a sane band (broken run → out of range).
    cal = manifest.get("calibration_agreement_pct")
    if cal is None or not (CALIBRATION_MIN_PCT <= float(cal) <= CALIBRATION_MAX_PCT):
        sys.exit(
            f"FATAL: calibration_agreement_pct={cal} is outside the sane band "
            f"[{CALIBRATION_MIN_PCT}, {CALIBRATION_MAX_PCT}]. The maps pipeline run looks "
            f"broken — refusing to load it. Inspect the maps' calibration report."
        )

    # 4. Resolution guard violations → warn per league, but continue (some matcher
    #    use cases tolerate a residual gap).
    violations = manifest.get("resolution_violations") or []
    if violations:
        print(f"WARNING: maps export reports {len(violations)} resolution-guard violation(s):")
        for v in violations:
            print(f"  - {v}")
        print("  (continuing — the resolution guard is advisory for the matcher.)")


# ─── Build cross-reference maps from club.json + league.json + club_overview ──────

def build_club_xref(club_rows: list[dict]) -> dict[int, dict]:
    """maps club_id → {tm_club_id, display_name}."""
    return {
        r["club_id"]: {"tm_club_id": r["tm_club_id"], "display_name": r["display_name"]}
        for r in club_rows
    }


def build_league_xref(league_rows: list[dict], valid_codes: set[str]) -> dict[str, str | None]:
    """export league_id (ENG1) → matcher code (GB1), or None if outside the universe."""
    out: dict[str, str | None] = {}
    for r in league_rows:
        code = r.get("dcaribou_code")
        out[r["league_id"]] = code if code in valid_codes else None
    return out


# ─── Row transforms (export shape → matcher table shape) ──────────────────────────

def _int_or_none(v) -> int | None:
    return None if v is None else int(round(float(v)))


def transform_overview(rows, club_xref, league_xref, snapshot, skip_log):
    """club_overview.json → map_club_overview rows. Returns (out_rows, budget_by_tm, league_by_tm)."""
    out, budget_by_tm, league_by_tm = [], {}, {}
    for r in rows:
        xref = club_xref.get(r["club_id"])
        if not xref or xref["tm_club_id"] is None:
            skip_log.append(f"overview: club_id={r['club_id']} has no tm_club_id — skipped")
            continue
        league = league_xref.get(r.get("league_id"))
        if league is None:
            skip_log.append(
                f"overview: '{xref['display_name']}' league {r.get('league_id')} "
                f"outside matcher universe — skipped"
            )
            continue
        tm = int(xref["tm_club_id"])
        fee = r.get("workbook_highest_transfer_fee_eur")
        wage = r.get("max_salary_pw_eur")
        budget_by_tm[tm] = (fee, wage)
        league_by_tm[tm] = league
        out.append({
            "club_id": str(tm),
            "club_name": xref["display_name"],
            "league": league,
            "formation": r.get("formation"),
            "manager": r.get("manager"),
            "agent_preferences": r.get("agent_preferences"),
            "sci_rotation_level": _int_or_none(r.get("sci_skill_rotation")),
            "sci_first_team_level": _int_or_none(r.get("sci_skill_first_team")),
            "sci_key_player_level": _int_or_none(r.get("sci_skill_key_player")),
            "highest_transfer_fee_2526_eur": fee,
            "highest_sale_2526_eur": r.get("workbook_highest_sale_eur"),
            "max_salary_pw_2526_eur": wage,
            "source": SOURCE_TAG,
            "source_file": "maps_export",
            "snapshot_date": snapshot,
        })
    return out, budget_by_tm, league_by_tm


def transform_tracker(rows, club_xref, league_by_tm, snapshot, skip_log):
    """club_tracker.json → map_club_tracker rows."""
    out = []
    for r in rows:
        xref = club_xref.get(r["club_id"])
        if not xref or xref["tm_club_id"] is None:
            continue
        tm = int(xref["tm_club_id"])
        league = league_by_tm.get(tm)
        if league is None:           # club not in (matcher-universe) overview
            continue
        out.append({
            "club_id": str(tm),
            "club_name": xref["display_name"],
            "league": league,
            "position_bucket": r["position"],                 # canonical-10 verbatim
            "bucket_10": to_matcher_bucket(r["position"]),     # CF → ST_CF
            "status": r["status"],
            "source": SOURCE_TAG,
            "source_file": "maps_export",
            "snapshot_date": snapshot,
        })
    return out


def _richness(row: dict) -> int:
    """How many descriptive fields a request row carries (for dedup tiebreak)."""
    return sum(row.get(k) is not None for k in
               ("validated_by", "linked_shortlisted_player", "role_notes"))


def transform_requests(rows, club_xref, league_by_tm, budget_by_tm, snapshot, skip_log):
    """club_requests.json (workbook origin only) → deduped map_club_requests rows.

    The export is REQUEST-grained: the same logical demand (e.g. club 595 wants a
    GK, Intel/NO) is recorded under several request_ids (different source_row_index
    or re-emitted rows), up to 4× per cell. The matcher needs CLUB-BUCKET-grained
    demand — the match engine emits one (player, buyer) candidate per request row,
    so request-level multiplicity would inflate matches ~2× with exact-duplicate
    buyer rows. We collapse to one row per
        (club, position_bucket, preferred_side, source, validated)
    keeping the descriptively-richest row. This is the granularity the retired
    workbook loader produced (pre-migration: 1058 distinct vs 2140 raw export rows).
    """
    by_key: dict[tuple, dict] = {}
    skipped_no_league = 0
    for r in rows:
        if r.get("source_origin") != "workbook":
            continue                  # maps-side inference handled by script 21 instead
        xref = club_xref.get(r["club_id"])
        if not xref or xref["tm_club_id"] is None:
            continue
        tm = int(xref["tm_club_id"])
        league = league_by_tm.get(tm)
        if league is None:
            skipped_no_league += 1    # club has no overview row → no league/budget
            continue
        fee, wage = budget_by_tm.get(tm, (None, None))
        bucket = to_matcher_bucket(r.get("position"))
        side = r.get("workbook_preferred_side")
        source = r.get("source")
        validated = r.get("validated")
        row = {
            "club_id": str(tm),
            "club_name": xref["display_name"],
            "league": league,
            "date_last_updated": r.get("date_last_updated"),
            "position_category": r.get("workbook_position_category"),
            "preferred_side": side,
            "position_bucket": bucket,
            "role_notes": r.get("role_notes"),
            "max_transfer_fee_eur": fee,
            "max_wage_pw_eur": wage,
            "source": source,
            "validated": validated,
            "validated_by": r.get("validated_by"),
            "linked_shortlisted_player": r.get("linked_shortlisted_players"),
            "workbook_source": SOURCE_TAG,
            "source_file": "maps_export",
            "snapshot_date": snapshot,
        }
        key = (tm, bucket, side, source, validated)
        prev = by_key.get(key)
        if prev is None or _richness(row) > _richness(prev):
            by_key[key] = row     # keep richest; first-seen wins on tie (stable order)
    if skipped_no_league:
        skip_log.append(
            f"requests: {skipped_no_league} workbook request(s) skipped — club not in "
            f"matcher-universe club_overview (no league/budget resolvable)"
        )
    n_raw = sum(1 for r in rows if r.get("source_origin") == "workbook")
    skip_log.append(
        f"requests: collapsed {n_raw} request-grained export rows → {len(by_key)} "
        f"club-bucket-grained demand rows (deduped operational duplicates)"
    )
    return list(by_key.values())


# ─── Database writes ──────────────────────────────────────────────────────────────

def insert_overview(con, rows):
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
    """, rows)
    return len(rows)


def insert_tracker(con, rows):
    con.executemany("""
        INSERT INTO map_club_tracker
            (club_id, club_name, league, position_bucket, bucket_10, status,
             source, source_file, snapshot_date)
        VALUES (:club_id, :club_name, :league, :position_bucket, :bucket_10, :status,
                :source, :source_file, :snapshot_date)
    """, rows)
    return len(rows)


def insert_requests(con, rows):
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
    """, rows)
    return len(rows)


def derive_demand_signal(con, snapshot):
    """Aggregate map_club_requests → map_demand_signal (league × position_bucket).

    Identical logic to the retired loader's derive step — Sheets 5/6/7 and the
    Streamlit app read this aggregate shape, so it cannot be a direct copy of the
    export's per-club live_demand_signal.
    """
    con.execute("DELETE FROM map_demand_signal WHERE snapshot_date = ?", (snapshot,))
    rows = con.execute("""
        SELECT league, position_bucket,
               COUNT(DISTINCT club_name) AS request_count,
               GROUP_CONCAT(DISTINCT club_name) AS clubs
        FROM map_club_requests
        WHERE position_bucket IS NOT NULL AND snapshot_date = ?
        GROUP BY league, position_bucket
        ORDER BY league, position_bucket
    """, (snapshot,)).fetchall()
    con.executemany("""
        INSERT INTO map_demand_signal (league, position_bucket, request_count, clubs, snapshot_date)
        VALUES (?, ?, ?, ?, ?)
    """, [(lg, b, n, clubs, snapshot) for lg, b, n, clubs in rows])
    return len(rows)


# ─── Warning-only league-override divergence check ────────────────────────────────

def check_league_overrides(league_by_tm: dict[int, str]) -> None:
    """Cross-check data/manual_league_overrides.csv against the export's leagues.

    The maps pipeline applies these overrides upstream, so this is a sanity check,
    not an active override layer. Log a warning per disagreement; never fail.
    """
    if not LEAGUE_OVERRIDES_CSV.exists():
        return
    disagreements, unverifiable = [], 0
    with open(LEAGUE_OVERRIDES_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                tm = int(row["club_id"])
            except (ValueError, KeyError):
                continue
            want = row["override_league_id"].strip()
            got = league_by_tm.get(tm)
            if got is None:
                unverifiable += 1
            elif got != want:
                disagreements.append((row.get("club_name", ""), tm, want, got))
    if disagreements:
        print(f"WARNING: {len(disagreements)} manual_league_overrides.csv entry/entries "
              f"DISAGREE with the maps export:")
        for name, tm, want, got in disagreements:
            print(f"  - {name} (tm={tm}): CSV says {want}, export says {got}")
        print("  The maps pipeline is the source of truth; update the export upstream or "
              "retire the stale CSV row.")
    else:
        print(f"manual_league_overrides.csv: all entries agree with the export "
              f"({unverifiable} not present in export, unverifiable).")


# ─── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    snapshot = str(config.SNAPSHOT_DATE)
    valid_codes = set(config.LEAGUE_IDS)

    export_dir = resolve_export_dir()
    manifest = read_manifest(export_dir)
    print(f"Reading maps export from {export_dir}")
    print(f"  schema_version={manifest.get('schema_version')}  "
          f"git_commit={str(manifest.get('git_commit'))[:10]}  "
          f"snapshot={manifest.get('snapshot_date')}  "
          f"calibration={manifest.get('calibration_agreement_pct')}%")
    validate_manifest(manifest)

    club_rows = read_table(export_dir, manifest, "club")
    league_rows = read_table(export_dir, manifest, "league")
    overview_rows = read_table(export_dir, manifest, "club_overview")
    tracker_rows = read_table(export_dir, manifest, "club_tracker")
    request_rows = read_table(export_dir, manifest, "club_request")

    club_xref = build_club_xref(club_rows)
    league_xref = build_league_xref(league_rows, valid_codes)

    skip_log: list[str] = []
    overview, budget_by_tm, league_by_tm = transform_overview(
        overview_rows, club_xref, league_xref, snapshot, skip_log)
    tracker = transform_tracker(tracker_rows, club_xref, league_by_tm, snapshot, skip_log)
    requests = transform_requests(
        request_rows, club_xref, league_by_tm, budget_by_tm, snapshot, skip_log)

    if skip_log:
        print(f"\nSkip log ({len(skip_log)} entries):")
        for line in skip_log:
            print(f"  {line}")

    with sqlite3.connect(config.SQLITE_FILE) as con:
        # Idempotent: clear this source's rows before insert. (Tables are also
        # recreated by 15_define_demand_schema.py, so this is belt-and-braces.)
        con.execute("DELETE FROM map_club_overview WHERE source = ?", (SOURCE_TAG,))
        con.execute("DELETE FROM map_club_tracker  WHERE source = ?", (SOURCE_TAG,))
        con.execute("DELETE FROM map_club_requests WHERE workbook_source = ?", (SOURCE_TAG,))
        n_ov = insert_overview(con, overview)
        n_tr = insert_tracker(con, tracker)
        n_rq = insert_requests(con, requests)
        n_ds = derive_demand_signal(con, snapshot)
        con.commit()

    print(f"\nInserted: {n_ov} club_overview / {n_tr} club_tracker / "
          f"{n_rq} club_requests / {n_ds} demand_signal rows")
    print()
    check_league_overrides(league_by_tm)

    # Per-league sanity prints (mirror the retired loader's reporting).
    with sqlite3.connect(config.SQLITE_FILE) as con:
        print("\nRequests loaded into map_club_requests per league:")
        for lg, n in con.execute(
            "SELECT league, COUNT(*) FROM map_club_requests "
            "WHERE workbook_source=? GROUP BY league ORDER BY league", (SOURCE_TAG,)
        ).fetchall():
            print(f"  {lg:5s} {n}")
        print("\nLive Demand Signal — clubs requesting each position:")
        for b, n in con.execute("""
            SELECT position_bucket, SUM(request_count)
            FROM map_demand_signal GROUP BY position_bucket
            ORDER BY SUM(request_count) DESC
        """).fetchall():
            print(f"  {b:8s} {n:>6d}")


if __name__ == "__main__":
    main()
