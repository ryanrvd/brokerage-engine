"""Display label mappings for the Streamlit app.

Reusable across pages — keep raw codes (league_id, position_bucket) in the
data layer; only humanise at render time.
"""

import sys
from pathlib import Path

# Allow importing the project-root club_display module
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    import club_display as _cd  # noqa: E402
except Exception:
    _cd = None

try:
    import player_display as _pd  # noqa: E402
except Exception:
    _pd = None

# League code → full display name. Mirrors config.LEAGUE_DISPLAY but lives
# here so the Streamlit app doesn't need to reach into pipeline config.
LEAGUE_NAMES: dict[str, str] = {
    "GB1":  "Premier League",
    "GB2":  "Championship",
    "ES1":  "La Liga",
    "ES2":  "La Liga 2",
    "IT1":  "Serie A",
    "IT2":  "Serie B",
    "L1":   "Bundesliga",
    "L2":   "2. Bundesliga",
    "FR1":  "Ligue 1",
    "FR2":  "Ligue 2",
    "PO1":  "Primeira Liga",
    "NL1":  "Eredivisie",
    "BE1":  "Pro League",
    "TR1":  "Süper Lig",
    "DK1":  "Danish Superliga",
    "SC1":  "Scottish Premiership",
    "GR1":  "Super League Greece",
    "SA1":  "Saudi Pro League",
    "MLS1": "MLS",
}


def league_name(code: str | None) -> str:
    if not code:
        return ""
    return LEAGUE_NAMES.get(code, code)


# League code → (country name, flag emoji). Surfaced on Club View identity
# strip so the user gets geographic context at a glance.
LEAGUE_COUNTRY: dict[str, tuple[str, str]] = {
    "GB1":  ("England",      "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    "GB2":  ("England",      "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    "ES1":  ("Spain",        "🇪🇸"),
    "ES2":  ("Spain",        "🇪🇸"),
    "IT1":  ("Italy",        "🇮🇹"),
    "IT2":  ("Italy",        "🇮🇹"),
    "L1":   ("Germany",      "🇩🇪"),
    "L2":   ("Germany",      "🇩🇪"),
    "FR1":  ("France",       "🇫🇷"),
    "FR2":  ("France",       "🇫🇷"),
    "PO1":  ("Portugal",     "🇵🇹"),
    "NL1":  ("Netherlands",  "🇳🇱"),
    "BE1":  ("Belgium",      "🇧🇪"),
    "TR1":  ("Türkiye",      "🇹🇷"),
    "DK1":  ("Denmark",      "🇩🇰"),
    "SC1":  ("Scotland",     "🏴󠁧󠁢󠁳󠁣󠁴󠁿"),
    "GR1":  ("Greece",       "🇬🇷"),
    "SA1":  ("Saudi Arabia", "🇸🇦"),
    "MLS1": ("USA",          "🇺🇸"),
}


def country_for_league(code: str | None) -> tuple[str, str]:
    """Returns (country_name, flag_emoji) for a league code; ("", "") if unknown."""
    if not code:
        return ("", "")
    return LEAGUE_COUNTRY.get(code, ("", ""))


def talent_band(ca: float | None) -> str:
    """Sci Sports CA → interpretive band label (display only). Bands are
    fixed thresholds on the CA scale; matching always uses the raw CA value."""
    if ca is None:
        return "Not yet rated"
    if ca >= 140: return "Generational"
    if ca >= 120: return "Elite"
    if ca >= 100: return "First-team level"
    if ca >= 80:  return "Squad level"
    if ca >= 60:  return "Development"
    return "Below cohort"


def potential_band(pa: float | None) -> str:
    """PA → interpretive ceiling band (display only)."""
    if pa is None:
        return "Not yet rated"
    if pa >= 140: return "Generational ceiling"
    if pa >= 120: return "Elite ceiling"
    if pa >= 100: return "First-team ceiling"
    if pa >= 80:  return "Squad ceiling"
    if pa >= 60:  return "Development ceiling"
    return "Below cohort ceiling"


def format_formation(value) -> str:
    """Render a tactical formation like 343 → '3-4-3', 4231 → '4-2-3-1'.

    Accepts int, float, or string. Numbers with existing dashes pass through.
    Non-numeric strings pass through unchanged. None → '—'."""
    if value is None:
        return "—"
    try:
        if isinstance(value, float):
            s = str(int(value))
        else:
            s = str(value).strip()
    except (TypeError, ValueError):
        return "—"
    if not s or s in ("—", "nan", "None"):
        return "—"
    if "-" in s:
        return s  # already formatted
    if not s.isdigit():
        return s  # e.g. "4-3-3" with characters we don't recognise — pass through
    return "-".join(s)


# Position bucket → super-bucket tier. Used for the bucket badge column and
# the sidebar bucket-tier filter.
BUCKET_TIER: dict[str, str] = {
    "GK":    "GK",
    "CB":    "DEF",
    "LB":    "DEF",
    "RB":    "DEF",
    "DM":    "MID",
    "CM":    "MID",
    "AM":    "MID",
    "LW":    "ATT",
    "RW":    "ATT",
    "ST_CF": "ATT",
}

# Reverse: tier → constituent buckets.
TIER_POSITIONS: dict[str, list[str]] = {
    "GK":  ["GK"],
    "DEF": ["CB", "LB", "RB"],
    "MID": ["DM", "CM", "AM"],
    "ATT": ["LW", "RW", "ST_CF"],
}

TIER_ORDER = ["GK", "DEF", "MID", "ATT"]


# Display-layer bucket rename. Internal canonical code is ST_CF (covers
# Centre-Forward + Second Striker — see scripts/_position_buckets.py); user-
# facing surfaces render as "CF". Apply everywhere the bucket is shown:
# table cells, multiselect dropdowns, page headers. The DB / matcher / filter
# logic keep ST_CF unchanged.
_BUCKET_DISPLAY: dict[str, str] = {
    "ST_CF": "CF",
}


def display_bucket(position_bucket: str | None) -> str:
    """Map internal bucket code → user-facing label (ST_CF → CF; others identity)."""
    if not position_bucket:
        return ""
    return _BUCKET_DISPLAY.get(position_bucket, position_bucket)


def super_bucket(position_bucket: str | None) -> str:
    if not position_bucket:
        return ""
    return BUCKET_TIER.get(position_bucket, position_bucket)


# Colour palette for bucket badges (pandas Styler background).
TIER_COLOURS: dict[str, str] = {
    "GK":  "#fef3c7",  # muted yellow
    "DEF": "#d1fae5",  # muted green
    "MID": "#dbeafe",  # muted blue
    "ATT": "#fee2e2",  # muted red/orange
}


# ─── Club display names (single source of truth via club_display.py) ─────────

# Aliased for readability. Streamlit calls these helpers everywhere a club name
# is rendered. The underlying state lives in BrokerageWorkbook.xlsx →
# "Club Display Names" tab. Read live each session (no need to re-run pipeline
# after editing display names — just refresh the page).

LEAGUE_DISPLAY_NAMES = LEAGUE_NAMES  # alias for the new request's naming


def _load_display_map() -> dict[int, str]:
    """Cached at first call. Returns {club_id: display_name}."""
    if _cd is None:
        return {}
    return _cd.load_display_map()


_DISPLAY_MAP_CACHE: dict[int, str] | None = None


def _get_display_map() -> dict[int, str]:
    global _DISPLAY_MAP_CACHE
    if _DISPLAY_MAP_CACHE is None:
        _DISPLAY_MAP_CACHE = _load_display_map()
    return _DISPLAY_MAP_CACHE


def invalidate_display_map_cache() -> None:
    """Call from a Streamlit page if you want to force a re-read of the tab."""
    global _DISPLAY_MAP_CACHE
    _DISPLAY_MAP_CACHE = None


def club_display_name(club_id: int | None, fallback: str | None = None) -> str:
    """Returns the short display name for a club, falling back to `fallback`
    (typically the official name) if no mapping exists."""
    if club_id is None:
        return fallback or ""
    return _get_display_map().get(int(club_id), fallback or "")


# ─── Player display names ────────────────────────────────────────────────────

_PLAYER_MAP_CACHE: dict[int, str] | None = None


def _get_player_display_map() -> dict[int, str]:
    global _PLAYER_MAP_CACHE
    if _PLAYER_MAP_CACHE is None:
        _PLAYER_MAP_CACHE = _pd.load_display_map() if _pd is not None else {}
    return _PLAYER_MAP_CACHE


def invalidate_player_display_map_cache() -> None:
    global _PLAYER_MAP_CACHE
    _PLAYER_MAP_CACHE = None


def player_display_name(player_id: int | None, fallback: str | None = None) -> str:
    """Returns the short display name for a player, falling back to `fallback`
    (typically the official name) if no mapping exists."""
    if player_id is None:
        return fallback or ""
    return _get_player_display_map().get(int(player_id), fallback or "")
