"""
Reconcile `data/scisports_ratings.xlsx` against the current sellable cohort.

Sci Sports talent layer — manual CA (current ability) and PA (potential
ability) ratings, entered by the user and preserved across weekly TM refreshes.

User workflow:
  1. After the main pipeline (scripts 08 → 09 → 11 → 12 → 13), run
       python scripts/reconcile_scisports.py
     which creates the file on first run or updates it idempotently thereafter.
  2. Open `data/scisports_ratings.xlsx`, filter on status=pending — that's the
     worklist. Fill in current_ability + potential_ability from the Sci Sports
     dashboard.
  3. Save and run
       python scripts/load_scisports_ratings.py
     to push the values into the `player_ratings` SQLite table.

Reconciliation rules:
  • New player in cohort, not in file → append with empty CA/PA, status=pending
  • Existing rated player still in cohort → preserve CA/PA, mark active
  • Existing rated player NOT in cohort → mark departed, preserve CA/PA so
    ratings survive a transient drop-out (player returns next week → status
    flips back automatically)
  • Player on Kill List → mark killed (still visible in file, flagged)

`last_updated` is auto-stamped only on rows whose status changes.

Workbook formatting:
  • Editable cells (current_ability, potential_ability, notes) get a pale
    yellow fill so the user knows where to type.
  • Read-only cells are left default.
  • Top row frozen, brand-blue header band, sensible column widths.
  • Rows sorted: pending → active → departed → killed (worklist surfaces
    first); within each band, by player name.

File-lock safety: if the user has the workbook open in Excel, openpyxl will
hit PermissionError on save and we exit with a clear message rather than
corrupting the file.
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
import kill_list  # project-root module

FILE_PATH = Path(config.DATA_DIR) / "scisports_ratings.xlsx"

COLUMNS: list[str] = [
    "tm_player_id",
    "player_name",
    "parent_club",
    "position_bucket",
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
    "position_bucket":   10,
    "current_ability":   16,
    "potential_ability": 16,
    "status":            12,
    "last_updated":      14,
    "notes":             42,
}
STATUS_ORDER = {"pending": 0, "active": 1, "departed": 2, "killed": 3}


# ─── DB helpers ──────────────────────────────────────────────────────────────
def get_current_cohort(con: sqlite3.Connection) -> dict[int, dict]:
    """Return {tm_player_id: {player_name, parent_club, position_bucket}} for
    every player in the current sellable cohort. Same filter Sheet 4 / Kill
    List / Targets all use."""
    rows = con.execute("""
        SELECT player_id, name, parent_club, position_bucket
        FROM player_universe
        WHERE (right_priced=1 OR finished_product=1
               OR finished_product IS NULL OR contract_leveraged=1)
    """).fetchall()
    return {
        int(pid): {
            "player_name":     name or "",
            "parent_club":     parent_club or "",
            "position_bucket": position_bucket or "",
        }
        for pid, name, parent_club, position_bucket in rows
    }


def get_killed_ids(con: sqlite3.Connection) -> set[int]:
    """Players on the Kill List (manual + agency-rule, deduped)."""
    state = kill_list.compute_kill_list_state(con)
    return {int(pid) for pid in state["excluded_ids"]}


# ─── File I/O ────────────────────────────────────────────────────────────────
def read_existing(file_path: Path) -> dict[int, dict]:
    """Read the existing workbook into {tm_player_id: row_dict}. Empty dict
    if the file doesn't exist yet (first run)."""
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
    """Treat any non-empty CA OR PA value as 'rated'."""
    for v in (ca, pa):
        if v is None: continue
        s = str(v).strip()
        if s and s.lower() not in ("none", "nan"):
            return True
    return False


def write_workbook(file_path: Path, rows: list[dict]) -> None:
    """Write reconciled rows out with header band, editable-cell tint,
    frozen top row, and sensible column widths."""
    wb = Workbook()
    ws = wb.active
    ws.title = "scisports_ratings"

    # Header row
    ws.append(COLUMNS)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    for col_idx, _ in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left", vertical="center")

    # Data rows
    editable_fill = PatternFill("solid", fgColor="FFFBEA")  # very light yellow
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, col_name in enumerate(COLUMNS, start=1):
            value = row.get(col_name)
            if isinstance(value, date):
                value = value.isoformat()
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if col_name in EDITABLE_COLS:
                cell.fill = editable_fill

    # Column widths + frozen header
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
    """Return (sorted_rows, counts)."""
    reconciled: dict[int, dict] = {}
    cohort_ids = set(cohort.keys())
    existing_ids = set(existing.keys())

    # Players in the current cohort
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
            # New player or previously rated-then-cleared: pending
            new_status = "pending"

        last_updated = prev.get("last_updated") or today_iso
        if new_status != prev_status:
            last_updated = today_iso

        reconciled[pid] = {
            "tm_player_id":      pid,
            "player_name":       meta["player_name"],
            "parent_club":       meta["parent_club"],
            "position_bucket":   meta["position_bucket"],
            "current_ability":   ca,
            "potential_ability": pa,
            "status":            new_status,
            "last_updated":      last_updated,
            "notes":             prev.get("notes") or "",
        }

    # Players in the existing file but NOT in the current cohort → departed
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
            "position_bucket":   prev.get("position_bucket") or "",
            "current_ability":   prev.get("current_ability"),
            "potential_ability": prev.get("potential_ability"),
            "status":            new_status,
            "last_updated":      last_updated,
            "notes":             prev.get("notes") or "",
        }

    # Sort: pending first (worklist), then active, departed, killed; within
    # each band by player name (case-insensitive)
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
    sorted_rows, counts = reconcile(cohort, killed_ids, existing, today_iso)

    write_workbook(FILE_PATH, sorted_rows)

    print(
        f"Reconciled {FILE_PATH} — "
        f"{counts['pending']} pending (new), "
        f"{counts['active']} active, "
        f"{counts['departed']} departed, "
        f"{counts['killed']} killed"
    )


if __name__ == "__main__":
    main()
