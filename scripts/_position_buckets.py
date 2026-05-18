"""Shared position-bucket mapping. Imported by 08_compute_pressure.py and 09_compute_sellability.py."""

POSITION_BUCKETS: dict[str, str] = {
    "Goalkeeper":          "GK",
    "Centre-Back":         "CB",
    "Left-Back":           "LB",
    "Right-Back":          "RB",
    "Defensive Midfield":  "DM",
    "Central Midfield":    "CM",
    "Attacking Midfield":  "AM",
    "Left Winger":         "LW",
    "Right Winger":        "RW",
    "Centre-Forward":      "ST_CF",
    "Second Striker":      "ST_CF",   # judgment: bundled with strikers, not AM
    "Left Midfield":       "LW",      # judgment: modern usage
    "Right Midfield":      "RW",      # judgment: modern usage
}


def bucket_for(sub_position: str | None) -> str | None:
    if sub_position is None:
        return None
    return POSITION_BUCKETS.get(sub_position.strip())


# Display-layer rename. Internal canonical key is ST_CF (covers Centre-Forward
# + Second Striker); user-facing surfaces render as "CF". Mirror of the helper
# in app/labels.py — kept in sync manually since scripts/ and app/ don't
# cross-import. Apply in export scripts at the boundary where position_bucket
# gets written to xlsx cells.
_BUCKET_DISPLAY: dict[str, str] = {
    "ST_CF": "CF",
}


def display_bucket(b: str | None) -> str:
    """Map internal bucket code → user-facing label (ST_CF → CF; others identity)."""
    if not b:
        return ""
    return _BUCKET_DISPLAY.get(b, b)
