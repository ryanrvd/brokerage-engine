"""Shared demand-provenance module — single source of truth.

Every demand row the matcher surfaces comes from one of three layers. The UI
and the workbooks show which one with a one-letter badge so a maps-inferred
signal is never mistaken for a curated, validated request:

    W  Workbook        map_club_requests, source_origin='workbook'
                       (curated in the Market Movement Maps workbooks)
    M  Maps inference  map_club_requests, source_origin='inference'
                       (MMM rule output: squad thinness, loan-no-buy, outbound)
    Y  Yatin inference inferred_club_requests, source='Inferred'
                       (this repo's scripts/21_infer_demand.py, roster thinness)

Consumers:
  • app/components.py + app pages        — badges, labels, W/M/Y count lines
  • app/db.py                             — demand_mapped_leagues() for the UI filters
  • scripts/17_export_demand_sheets.py    — Sheet 5 W/M/Y columns, Sheet 7 Src column
  • scripts/23_export_brokerage_sheet.py  — Sheet 1 Demand label

Also owns the demand-coverage set (`derive_demand_mapped_leagues`), which
replaced the hard-coded config.DEMAND_MAPPED_LEAGUES from the manual-workbook era.
"""

from __future__ import annotations

import sqlite3

import config

WORKBOOK = "W"
MAPS_INFERENCE = "M"
YATIN_INFERENCE = "Y"
UNKNOWN = "?"

SOURCE_CODES = (WORKBOOK, MAPS_INFERENCE, YATIN_INFERENCE)

SOURCE_NAMES = {
    WORKBOOK:        "Workbook",
    MAPS_INFERENCE:  "Maps inference",
    YATIN_INFERENCE: "Matcher inference",
    UNKNOWN:         "Unknown origin",
}

SOURCE_DESCRIPTIONS = {
    WORKBOOK:        "Workbook — curated request from the Market Movement Maps workbooks",
    MAPS_INFERENCE:  "Maps inference — Market Movement Maps rule output (not validated)",
    YATIN_INFERENCE: "Matcher inference — squad-thinness gap from scripts/21 (not validated)",
    UNKNOWN:         "Unknown origin",
}

# Badge colours rank by trust: curated green, maps-rule amber, matcher-rule grey.
SOURCE_COLOURS = {
    WORKBOOK:        ("#DCFCE7", "#166534"),
    MAPS_INFERENCE:  ("#FEF3C7", "#92400E"),
    YATIN_INFERENCE: ("#F3F4F6", "#374151"),
    UNKNOWN:         ("#FEE2E2", "#991B1B"),
}


def source_code(source_origin: str | None, source: str | None = None) -> str:
    """Map a demand row's (source_origin, source) to W / M / Y / ?."""
    if source_origin == "inference":
        return MAPS_INFERENCE
    if source_origin == "workbook":
        return WORKBOOK
    if (source or "").strip() == "Inferred":
        return YATIN_INFERENCE
    return UNKNOWN


def count_line(counts: dict[str, int]) -> str:
    """'0 W · 2 M · 2 Y' — always lists all three, in trust order."""
    return " · ".join(f"{counts.get(c, 0)} {c}" for c in SOURCE_CODES)


def count_codes(codes) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in codes:
        out[c] = out.get(c, 0) + 1
    return out


def derive_demand_mapped_leagues(con: sqlite3.Connection) -> tuple[str, ...]:
    """Leagues with at least one map_club_requests row, in config.LEAGUE_IDS order.

    Replaces the hard-coded 10-league config.DEMAND_MAPPED_LEAGUES: coverage now
    follows whatever the maps export actually carries (18/19 on the 2026-10-18
    export — no SA1).
    """
    present = {r[0] for r in con.execute(
        "SELECT DISTINCT league FROM map_club_requests WHERE league IS NOT NULL"
    )}
    return tuple(lg for lg in config.LEAGUE_IDS if lg in present)


def demand_label(source: str | None, validated: str | None,
                 source_origin: str | None = None) -> str:
    """Badge-prefixed demand label for match rows: 'M · Maps inference',
    'Y · Matcher inference (squad gap)', 'W · Validated agent', …"""
    code = source_code(source_origin, source)
    if code == MAPS_INFERENCE:
        return f"{code} · Maps inference"
    if code == YATIN_INFERENCE:
        return f"{code} · Matcher inference (squad gap)"
    src = (source or "").strip()
    val = (validated or "").strip().upper()
    if src == "Agent" and val == "YES":
        tier = "Validated agent"
    elif src == "Agent":
        tier = "Agent (partial)"
    elif src == "Intel":
        tier = "Intel-derived"
    else:
        tier = "Unverified"
    return f"{code} · {tier}"
