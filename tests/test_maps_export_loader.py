"""Tests for scripts/16b_load_maps_exports.py — the maps export interface loader.

Covers:
  * manifest schema_version validation (accept matching major; reject a major bump)
  * redaction check (accept un-redacted; reject club_request.validated_by redacted)
  * calibration sanity band (reject out-of-band)
  * per-table read into the four map_* tables from a mock export directory
  * tm_club_id direct-join replaces the old cross-league name matcher

The loader lives at a digit-prefixed path (not importable as a normal module),
so we load it (and the schema script) by file location.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(filename: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


loader = _load("16b_load_maps_exports.py", "loader16b")
schema = _load("15_define_demand_schema.py", "schema15")


# ─── Manifest validation ──────────────────────────────────────────────────────────

def _base_manifest(**overrides) -> dict:
    m = {
        "schema_version": "1.0.0",
        "redactions_applied": [],
        "calibration_agreement_pct": 61.8,
        "resolution_violations": [],
        "tables": {},
    }
    m.update(overrides)
    return m


def test_schema_version_matching_major_accepted():
    # Same major (1.x) must pass — no exception.
    loader.validate_manifest(_base_manifest(schema_version="1.4.2"))


def test_schema_version_major_bump_rejected():
    with pytest.raises(SystemExit):
        loader.validate_manifest(_base_manifest(schema_version="2.0.0"))


def test_redaction_absent_accepted():
    loader.validate_manifest(_base_manifest(redactions_applied=[]))


def test_redaction_validated_by_rejected():
    with pytest.raises(SystemExit):
        loader.validate_manifest(
            _base_manifest(redactions_applied=["club_request.validated_by"]))


def test_calibration_out_of_band_rejected():
    with pytest.raises(SystemExit):
        loader.validate_manifest(_base_manifest(calibration_agreement_pct=12.0))
    with pytest.raises(SystemExit):
        loader.validate_manifest(_base_manifest(calibration_agreement_pct=None))


def test_resolution_violations_warn_only(capsys):
    # Non-empty violations must NOT raise — warn and continue.
    loader.validate_manifest(_base_manifest(resolution_violations=["GB1: 12%"]))
    assert "resolution-guard violation" in capsys.readouterr().out


# ─── Per-table read + tm_club_id join ───────────────────────────────────────────────

def _write_export(dirpath: Path) -> None:
    """Mock export dir: 4 table JSONs + club/league xref + manifest."""
    def dump(name, rows):
        (dirpath / name).write_text(
            json.dumps({"snapshot_date": "2026-06-19", "rows": rows}), encoding="utf-8")

    # Maps club_id 1 → tm 100; club_id 2 → tm 200; club_id 3 (NL2, must skip) → tm 300.
    dump("club.json", [
        {"club_id": 1, "tm_club_id": 100, "display_name": "Alpha FC"},
        {"club_id": 2, "tm_club_id": 200, "display_name": "Beta FC"},
        {"club_id": 3, "tm_club_id": 300, "display_name": "Dutch2 FC"},
    ])
    dump("league.json", [
        {"league_id": "ENG1", "dcaribou_code": "GB1"},
        {"league_id": "NLD2", "dcaribou_code": "NL2"},   # outside matcher universe → skipped
    ])
    dump("club_overview.json", [
        {"club_id": 1, "league_id": "ENG1", "formation": "433", "manager": "M1",
         "agent_preferences": "N/A", "sci_skill_rotation": 41.0, "sci_skill_first_team": 58.0,
         "sci_skill_key_player": 66.0, "workbook_highest_transfer_fee_eur": 17000000,
         "workbook_highest_sale_eur": 9000000, "max_salary_pw_eur": 50000},
        {"club_id": 2, "league_id": "ENG1", "formation": "442", "manager": "M2",
         "agent_preferences": None, "sci_skill_rotation": None, "sci_skill_first_team": None,
         "sci_skill_key_player": None, "workbook_highest_transfer_fee_eur": 8000000,
         "workbook_highest_sale_eur": None, "max_salary_pw_eur": 30000},
        {"club_id": 3, "league_id": "NLD2", "formation": None, "manager": None,
         "agent_preferences": None, "sci_skill_rotation": None, "sci_skill_first_team": None,
         "sci_skill_key_player": None, "workbook_highest_transfer_fee_eur": 1000000,
         "workbook_highest_sale_eur": None, "max_salary_pw_eur": 5000},
    ])
    dump("club_tracker.json", [
        {"club_id": 1, "position": "GK", "status": "Open"},
        {"club_id": 1, "position": "CF", "status": "Covered"},  # CF → ST_CF in bucket_10
    ])
    # club_budget_derived: club 1 has a derived fee distinct from its workbook fee
    # (17m) so the test proves the loader uses derived. club 2 absent → workbook fallback.
    dump("club_budget_derived.json", [
        {"club_id": 1, "derived_highest_transfer_fee_eur": 25000000},
    ])
    dump("club_requests.json", [
        # workbook request — must load, CF → ST_CF, budget joined from overview
        {"club_id": 1, "position": "CF", "source": "Agent", "validated": "YES",
         "validated_by": "Jane Agent", "role_notes": "needs a 9",
         "linked_shortlisted_players": None, "date_last_updated": "02/04/2026",
         "workbook_position_category": "Centre Forward", "workbook_preferred_side": None,
         "source_origin": "workbook"},
        # operational DUPLICATE of the request above (different request_id, same
        # club/bucket/side/source/validated, less info) — must collapse to one row,
        # keeping the richer row (with validated_by) above.
        {"club_id": 1, "position": "CF", "source": "Agent", "validated": "YES",
         "validated_by": None, "role_notes": None,
         "linked_shortlisted_players": None, "date_last_updated": "02/04/2026",
         "workbook_position_category": "Centre Forward", "workbook_preferred_side": None,
         "source_origin": "workbook"},
        # inference-origin request — must be SKIPPED (matcher runs its own inference)
        {"club_id": 1, "position": "CB", "source": "Intel", "validated": "NO",
         "validated_by": None, "role_notes": None, "linked_shortlisted_players": None,
         "date_last_updated": None, "workbook_position_category": "Centre Back",
         "workbook_preferred_side": "Left", "source_origin": "inference"},
        # workbook request for club 2 (absent from club_budget_derived) — budget
        # must fall back to the workbook fee (8m).
        {"club_id": 2, "position": "GK", "source": "Agent", "validated": "NO",
         "validated_by": None, "role_notes": None, "linked_shortlisted_players": None,
         "date_last_updated": None, "workbook_position_category": "Goalkeeper",
         "workbook_preferred_side": "Either", "source_origin": "workbook"},
        # workbook request for an NL2 club — skipped (no matcher-universe league)
        {"club_id": 3, "position": "LB", "source": "Agent", "validated": "NO",
         "validated_by": None, "role_notes": None, "linked_shortlisted_players": None,
         "date_last_updated": None, "workbook_position_category": "Full Back",
         "workbook_preferred_side": "Left", "source_origin": "workbook"},
    ])
    manifest = {
        "schema_version": "1.0.0", "redactions_applied": [],
        "calibration_agreement_pct": 61.8, "resolution_violations": [],
        "tables": {
            "club": {"json": "club.json"},
            "league": {"json": "league.json"},
            "club_overview": {"json": "club_overview.json"},
            "club_tracker": {"json": "club_tracker.json"},
            "club_request": {"json": "club_requests.json"},
            "club_budget_derived": {"json": "club_budget_derived.json"},
        },
    }
    (dirpath / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _run_loader(tmp_path, monkeypatch) -> sqlite3.Connection:
    export_dir = tmp_path / "latest"
    export_dir.mkdir()
    _write_export(export_dir)

    db = tmp_path / "test.db"
    with sqlite3.connect(db) as con:
        con.executescript(schema.SCHEMA)

    monkeypatch.setenv("MAPS_EXPORTS_PATH", str(export_dir))
    monkeypatch.setattr(loader.config, "SQLITE_FILE", str(db))
    monkeypatch.setattr(loader.config, "SNAPSHOT_DATE", "2026-06-20")
    # Avoid touching the repo's real overrides CSV from a temp cwd.
    monkeypatch.setattr(loader, "LEAGUE_OVERRIDES_CSV", tmp_path / "nope.csv")
    monkeypatch.chdir(tmp_path)

    loader.main()
    return sqlite3.connect(db)


def test_per_table_read_populates_all_four(tmp_path, monkeypatch):
    con = _run_loader(tmp_path, monkeypatch)
    n_ov = con.execute("SELECT COUNT(*) FROM map_club_overview").fetchone()[0]
    n_tr = con.execute("SELECT COUNT(*) FROM map_club_tracker").fetchone()[0]
    n_rq = con.execute("SELECT COUNT(*) FROM map_club_requests").fetchone()[0]
    n_ds = con.execute("SELECT COUNT(*) FROM map_demand_signal").fetchone()[0]
    assert n_ov == 2          # Alpha + Beta; NL2 club skipped
    assert n_tr == 2          # both Alpha tracker rows
    assert n_rq == 2          # club1 ST_CF (dup collapsed) + club2 GK; inference + NL2 skipped
    assert n_ds == 2          # derived: (GB1, ST_CF) and (GB1, GK)


def test_request_dedup_keeps_richest(tmp_path, monkeypatch):
    """Two operational duplicates collapse to one row, keeping the row with validated_by."""
    con = _run_loader(tmp_path, monkeypatch)
    rows = con.execute(
        "SELECT validated_by, role_notes FROM map_club_requests "
        "WHERE position_bucket='ST_CF'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Jane Agent"     # richer row won over the bare duplicate
    assert rows[0][1] == "needs a 9"


def test_tm_club_id_direct_join(tmp_path, monkeypatch):
    """The matcher's club_id must be the export's tm_club_id, not the maps id."""
    con = _run_loader(tmp_path, monkeypatch)
    overview_ids = {r[0] for r in con.execute("SELECT club_id FROM map_club_overview")}
    assert overview_ids == {"100", "200"}      # tm ids, NOT maps ids 1/2
    req = con.execute(
        "SELECT club_id, league, position_bucket, max_transfer_fee_eur, "
        "max_wage_pw_eur, validated_by FROM map_club_requests "
        "WHERE position_bucket='ST_CF'").fetchone()
    assert req[0] == "100"                     # joined via club.json tm_club_id
    assert req[1] == "GB1"                     # league via league.json dcaribou_code
    assert req[2] == "ST_CF"                   # CF → ST_CF
    assert req[3] == 25000000                  # budget = club_budget_derived (Phase 6.1), not workbook 17m
    assert req[4] == 50000                     # wage still from club_overview.max_salary_pw_eur
    assert req[5] == "Jane Agent"              # un-redacted validator preserved


def test_nl2_league_skipped(tmp_path, monkeypatch):
    con = _run_loader(tmp_path, monkeypatch)
    leagues = {r[0] for r in con.execute("SELECT DISTINCT league FROM map_club_overview")}
    assert "NL2" not in leagues
