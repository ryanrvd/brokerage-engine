"""
Reconcile `data/scisports_ratings.xlsx` against the current player universe.

Day 8 expansion: scope widened from "sellable cohort only" (~157 players) to
"every PL squad player (parent club in PL), including loaned-out players",
plus the original sellable cohort across all 19 leagues. This gives the
worksheet ~600+ rows for the PL phase; Championship follows in a later prompt.

User workflow:
  1. Run the pipeline through scripts 09 → reconcile_scisports.py
  2. Open `data/scisports_ratings.xlsx`, filter status=pending — that's the
     worklist. Fill in current_ability + potential_ability from Sci Sports.
  3. Save and run `python scripts/load_scisports_ratings.py`.

Reconciliation rules (unchanged in spirit):
  • New player in scope, not in file → append with empty CA/PA, status=pending
  • Existing rated player still in scope → preserve CA/PA, mark active
  • Existing rated player NOT in scope → mark departed, preserve CA/PA
  • Player on Kill List → mark killed (still visible in file, flagged)
  • Reactivation: previously departed player back in scope → restore status

Scope definition (Day 8):
  "in scope" = parent_club is a PL club (league_id = 'GB1' in club_pressure)
               OR the player is in the original sellable cohort (sellability_status
               = 'sellable_now')

File-lock safety: if the workbook is open in Excel, openpyxl hits
PermissionError and we exit with a clear message.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
import kill_list

FILE_PATH = Path(config.DATA_DIR) / "scisports_ratings.xlsx"

PL_LEAGUE_ID = "GB1"

COLUMNS: list[str] = [
    "tm_player_id",
    "player_name",
    "parent_club",
    "current_club",
    "position_bucket",
    "age",
    "is_on_loan",
    "current_ability",
    "potential_ability",
    "status",
    "last_updated",
    "notes",
]
EDITABLE_COLS: set[str] = {"current_ability", "potential_ability", "notes"}
COLUMN_WIDTHS: dict[str, int] = {
    "tm_player_id":      14,
    "player_name":       28,
    "parent_club":       30,
    "current_club":      30,
    "position_bucket":   10,
    "age":                6,
    "is_on_loan":         12,
    "current_ability":   16,
    "potential_ability": 16,
    "status":            12,
    "last_updated":      14,
    "notes":             42,
}
STATUS_ORDER = {"pending": 0, "active": 1, "departed": 2, "killed": 3}


# ─── DB helpers ──────────────────────────────────────────────────────────────
def get_current_cohort(con: sqlite3.Connection) -> dict[int, dict]:
    """Return {tm_player_id: {...}} for every player in scope.

    Scope = PL-parented players (parent club in GB1) UNION the original
    sellable cohort (sellability_status = 'sellable_now') from all leagues.
    """
    # PL club IDs (from club_pressure, post-override)
    pl_club_ids = set(
        r[0] for r in con.execute(
            "SELECT club_id FROM club_pressure WHERE league_id = ?", (PL_LEAGUE_ID,)
        ).fetchall()
    )

    rows = con.execute("""
        SELECT pu.player_id, pu.name, pu.parent_club, pu.current_club,
               pu.position_bucket, pu.age, pu.on_loan, pu.parent_club_id,
               pu.sellability_status
        FROM player_universe pu
    """).fetchall()

    out: dict[int, dict] = {}
    for (pid, name, parent_club, current_club, position_bucket,
         age, on_loan, parent_club_id, sellability_status) in rows:
        # Include if: parent is a PL club OR player is sellable_now
        is_pl_parented = str(parent_club_id) in pl_club_ids if parent_club_id else False
        is_sellable = sellability_status == "sellable_now"
        if not (is_pl_parented or is_sellable):
            continue
        out[int(pid)] = {
            "player_name":     name or "",
            "parent_club":     parent_club or "",
            "current_club":    current_club or "",
            "position_bucket": position_bucket or "",
            "age":             age,
            "is_on_loan":      True if on_loan else False,
        }
    return out


def get_killed_ids(con: sqlite3.Connection) -> set[int]:
    state = kill_list.compute_kill_list_state(con)
    return {int(pid) for pid in state["excluded_ids"]}


# ─── File I/O ────────────────────────────────────────────────────────────────
def read_existing(file_path: Path) -> dict[int, dict]:
    if not file_path.exists():
        return {}
    wb = load_workbook(file_path)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}
    headers = list(rows[0])
    out: dict[int, dict] = {}
    for raw in rows[1:]:
        if not raw or raw[0] is None:
            continue
        d = dict(zip(headers, raw))
        try:
            pid = int(d.get("tm_player_id"))
        except (TypeError, ValueError):
            continue
        out[pid] = d
    return out


def _is_rated(ca, pa) -> bool:
    for v in (ca, pa):
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("none", "nan"):
            return True
    return False


def write_workbook(file_path: Path, rows: list[dict]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "scisports_ratings"

    ws.append(COLUMNS)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    for col_idx, _ in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left", vertical="center")

    editable_fill = PatternFill("solid", fgColor="FFFBEA")
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, col_name in enumerate(COLUMNS, start=1):
            value = row.get(col_name)
            if isinstance(value, date):
                value = value.isoformat()
            if isinstance(value, bool):
                value = "TRUE" if value else "FALSE"
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if col_name in EDITABLE_COLS:
                cell.fill = editable_fill

    for col_idx, col_name in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = COLUMN_WIDTHS.get(col_name, 16)
    ws.freeze_panes = "A2"

    try:
        wb.save(file_path)
    except PermissionError as e:
        raise PermissionError(
            f"Cannot write {file_path} — file may be open in Excel. "
            "Close it and re-run."
        ) from e


# ─── Reconciliation ──────────────────────────────────────────────────────────
def reconcile(cohort: dict[int, dict],
              killed_ids: set[int],
              existing: dict[int, dict],
              today_iso: str) -> tuple[list[dict], dict[str, int]]:
    reconciled: dict[int, dict] = {}
    cohort_ids = set(cohort.keys())
    existing_ids = set(existing.keys())

    for pid, meta in cohort.items():
        prev = existing.get(pid, {})
        prev_status = (prev.get("status") or "").strip().lower()
        ca = prev.get("current_ability")
        pa = prev.get("potential_ability")

        if pid in killed_ids:
            new_status = "killed"
        elif _is_rated(ca, pa):
            new_status = "active"
        else:
            new_status = "pending"

        last_updated = prev.get("last_updated") or today_iso
        if new_status != prev_status:
            last_updated = today_iso

        reconciled[pid] = {
            "tm_player_id":      pid,
            "player_name":       meta["player_name"],
            "parent_club":       meta["parent_club"],
            "current_club":      meta["current_club"],
            "position_bucket":   meta["position_bucket"],
            "age":               meta["age"],
            "is_on_loan":        meta["is_on_loan"],
            "current_ability":   ca,
            "potential_ability": pa,
            "status":            new_status,
            "last_updated":      last_updated,
            "notes":             prev.get("notes") or "",
        }

    for pid in existing_ids - cohort_ids:
        prev = existing[pid]
        prev_status = (prev.get("status") or "").strip().lower()
        new_status = "departed"
        last_updated = prev.get("last_updated") or today_iso
        if new_status != prev_status:
            last_updated = today_iso
        reconciled[pid] = {
            "tm_player_id":      pid,
            "player_name":       prev.get("player_name") or "",
            "parent_club":       prev.get("parent_club") or "",
            "current_club":      prev.get("current_club") or "",
            "position_bucket":   prev.get("position_bucket") or "",
            "age":               prev.get("age"),
            "is_on_loan":        prev.get("is_on_loan") or False,
            "current_ability":   prev.get("current_ability"),
            "potential_ability": prev.get("potential_ability"),
            "status":            new_status,
            "last_updated":      last_updated,
            "notes":             prev.get("notes") or "",
        }

    sorted_rows = sorted(
        reconciled.values(),
        key=lambda r: (
            STATUS_ORDER.get(r["status"], 9),
            str(r["player_name"] or "").lower(),
        ),
    )

    counts = {
        "pending":  sum(1 for r in sorted_rows if r["status"] == "pending"),
        "active":   sum(1 for r in sorted_rows if r["status"] == "active"),
        "departed": sum(1 for r in sorted_rows if r["status"] == "departed"),
        "killed":   sum(1 for r in sorted_rows if r["status"] == "killed"),
    }
    return sorted_rows, counts


def main() -> None:
    today_iso = date.today().isoformat()
    con = sqlite3.connect(config.SQLITE_FILE)
    try:
        cohort = get_current_cohort(con)
        killed_ids = get_killed_ids(con)
    finally:
        con.close()

    existing = read_existing(FILE_PATH)
    existing_count = len(existing)
    sorted_rows, counts = reconcile(cohort, killed_ids, existing, today_iso)

    write_workbook(FILE_PATH, sorted_rows)

    new_rows = len(sorted_rows) - existing_count
    # Departures = existing rows not in new cohort
    departed_from_existing = sum(
        1 for pid in existing
        if pid not in set(cohort.keys()) and existing[pid].get("status") != "departed"
    )
    reactivated = sum(
        1 for pid in existing
        if (existing[pid].get("status") or "").strip().lower() == "departed"
        and pid in cohort
    )

    print(f"Reconciled {FILE_PATH}:")
    print(f"  Existing rows preserved: {existing_count}")
    print(f"  New rows added:          {max(0, new_rows)}")
    print(f"  Departures marked:       {departed_from_existing}")
    print(f"  Reactivations:           {reactivated}")
    print(f"  Total rows:              {len(sorted_rows)}")
    print()
    print("By status:")
    for status in ("active", "killed", "pending", "departed"):
        label = {
            "active": "active (rated)",
            "killed": "killed",
            "pending": "pending (awaiting rating)",
            "departed": "departed",
        }[status]
        print(f"  {label:35s} {counts.get(status, 0)}")


if __name__ == "__main__":
    main()
