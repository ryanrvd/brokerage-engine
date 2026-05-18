"""
Yatin Matcher — Day 1 configuration.

Every tunable lives here. Edit a value, re-run scripts/03_build_universe.py,
and the filtered universe updates without touching any other code.
"""

from datetime import date

# Snapshot date — every filter is anchored to this.
# Default is today; override (e.g. date(2026, 5, 1)) to lock the build to a past date.
SNAPSHOT_DATE = date.today()


def end_of_season_plus(snapshot: date, years: int) -> date:
    """Return June 30 of `years` seasons out from the snapshot.

    European football seasons run July → June. A snapshot in May-June 2026 sits in
    the 25/26 season (ends 2026-06-30); a snapshot in August 2026 sits in 26/27
    (ends 2027-06-30). Adding N seasons returns the end-of-season date N years on.

    This avoids calendar-month rounding bugs where e.g. a 2029-06-30 contract sits
    just outside a `snapshot + 3 years` cutoff calculated as `2026-05-12 + 3y =
    2029-05-12` — even though that contract represents the end of the season 3
    years out, which is precisely what the filter intends.
    """
    base_year = snapshot.year if snapshot.month <= 6 else snapshot.year + 1
    return date(base_year + years, 6, 30)

# Age band, inclusive both ends.
AGE_MIN = 17
AGE_MAX = 24

# Current Transfermarkt market value band, in euros.
# 5m buffer on each side of the €20–40m brokerage target to catch players near the edges.
TM_VALUE_MIN_EUR = 8_000_000
TM_VALUE_MAX_EUR = 45_000_000

# Contract must end on or before SNAPSHOT_DATE + CONTRACT_MAX_YEARS_AHEAD years.
CONTRACT_MAX_YEARS_AHEAD = 3

# Contract is considered "leveraged" if it ends within this many years of snapshot
# (tighter than the inclusion filter — this is the column flag for downstream sheets).
CONTRACT_LEVERAGED_YEARS = 2

# Minutes-played filter — look back this many months and require this share of available minutes.
MINUTES_LOOKBACK_MONTHS = 18
MIN_MINUTES_SHARE = 0.50

# Leagues we filter on. competition_id matches dcaribou's `competitions.competition_id`.
# `relaxed_minutes=True` means the dataset has no `appearances` data for that league —
# skip the 50% minutes filter and flag finished_product=False for those rows.
LEAGUES = [
    # Day 1 — eight Tier-A European leagues
    {"id": "GB1",  "country": "England",      "display": "Premier League",       "relaxed_minutes": False},
    {"id": "ES1",  "country": "Spain",        "display": "La Liga",              "relaxed_minutes": False},
    {"id": "IT1",  "country": "Italy",        "display": "Serie A",              "relaxed_minutes": False},
    {"id": "L1",   "country": "Germany",      "display": "Bundesliga",           "relaxed_minutes": False},
    {"id": "FR1",  "country": "France",       "display": "Ligue 1",              "relaxed_minutes": False},
    {"id": "PO1",  "country": "Portugal",     "display": "Primeira Liga",        "relaxed_minutes": False},
    {"id": "NL1",  "country": "Netherlands",  "display": "Eredivisie",           "relaxed_minutes": False},
    {"id": "BE1",  "country": "Belgium",      "display": "Belgian Pro League",   "relaxed_minutes": False},
    # Day 2 — four clean adds (in dcaribou, with appearances)
    {"id": "TR1",  "country": "Türkiye",      "display": "Süper Lig",            "relaxed_minutes": False},
    {"id": "DK1",  "country": "Denmark",      "display": "Danish Superliga",     "relaxed_minutes": False},
    {"id": "SC1",  "country": "Scotland",     "display": "Scottish Premiership", "relaxed_minutes": False},
    {"id": "GR1",  "country": "Greece",       "display": "Super League Greece",  "relaxed_minutes": False},
    # Day 2 — partial coverage (in dcaribou but no appearances data → relaxed filter)
    {"id": "SA1",  "country": "Saudi Arabia", "display": "Saudi Pro League",     "relaxed_minutes": True},
    {"id": "MLS1", "country": "USA",          "display": "MLS",                  "relaxed_minutes": True},
    # Day 2 — five second tiers (NOT in dcaribou — populated by external scrape)
    {"id": "GB2",  "country": "England",      "display": "Championship",         "relaxed_minutes": False, "external": True},
    {"id": "FR2",  "country": "France",       "display": "Ligue 2",              "relaxed_minutes": False, "external": True},
    {"id": "ES2",  "country": "Spain",        "display": "La Liga 2",            "relaxed_minutes": False, "external": True},
    {"id": "IT2",  "country": "Italy",        "display": "Serie B",              "relaxed_minutes": False, "external": True},
    {"id": "L2",   "country": "Germany",      "display": "2. Bundesliga",        "relaxed_minutes": False, "external": True},
]

# Convenience lookups derived from LEAGUES.
LEAGUE_IDS = [l["id"] for l in LEAGUES]
LEAGUE_DISPLAY = {l["id"]: l["display"] for l in LEAGUES}
RELAXED_MINUTES_IDS = {l["id"] for l in LEAGUES if l.get("relaxed_minutes")}
EXTERNAL_LEAGUE_IDS = {l["id"] for l in LEAGUES if l.get("external")}

# File paths (relative to repo root).
DATA_DIR = "data"
DB_DIR = "db"
DUCKDB_FILE = f"{DATA_DIR}/transfermarkt-datasets.duckdb"
DUCKDB_URL = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/transfermarkt-datasets.duckdb"
SQLITE_FILE = f"{DB_DIR}/yatin.db"


# ─── Demand-side league coverage ──────────────────────────────────────────────
# Demand-side coverage is limited to the 10 leagues we've manually mapped via
# the Google Sheets workflow (see scripts/16_load_market_maps.py). Demand
# signals from outside this set — MLS, Saudi Pro League, Süper Lig, Greek
# Super League, Scottish Premiership, Danish Superliga, and the second-tier
# leagues other than FRA-2 — are NOT trusted at present.
#
# Every demand display surface (Position View counts/charts/commentary,
# matches tables on Targets / All Matches / Player View / Club View) filters
# buyers to this set. Supply (player_universe) stays drawn from the full
# 19-league universe — only demand is restricted.
DEMAND_MAPPED_LEAGUES: tuple[str, ...] = (
    "GB1", "GB2",       # England Premier League + Championship
    "ES1",              # Spain La Liga
    "IT1",              # Italy Serie A
    "FR1", "FR2",       # France Ligue 1 + Ligue 2
    "L1",               # Germany Bundesliga
    "PO1",              # Portugal Primeira Liga
    "NL1",              # Netherlands Eredivisie
    "BE1",              # Belgium Pro League
)


# ─── Day 5 — match engine constants ───────────────────────────────────────────
#
# Internal scoring constants. The matcher identifies moves; it does NOT price
# the commercial economics — that judgement is applied by Yatin on review of
# Sheet 1, so no commission/sell-on/lifecycle math is surfaced anywhere.

# TM market value → indicative transfer fee. TM consistently understates
# realised fees, BUT the gap is non-uniform: it's biggest for low-TM
# contract-leveraged prospects (TM €8m can land €20m+ fee, 2-3× lift) and
# shrinks for established players (TM €40m sells around €40m, 1× lift).
# Empirical evidence: recent real-world transfers (Wirtz, Olise, Mbeumo,
# etc.) sit in the 1.0–1.5× range; only contract-leveraged prospects hit 2×+.
#
# Used internally by budget_fit only — NOT surfaced as a visible column.
TM_TO_FEE_BANDS: list[tuple[int, float]] = [
    # (upper_bound_inclusive_TM_eur, multiplier_applied_when_TM_<_bound)
    (15_000_000, 2.0),
    (25_000_000, 1.5),
    (10**12,     1.2),  # sentinel ceiling — all higher TMs use 1.2×
]


def tm_to_fee_multiplier(tm_eur: int | float) -> float:
    """Tiered indicative-fee multiplier. See TM_TO_FEE_BANDS."""
    for ceiling, mult in TM_TO_FEE_BANDS:
        if tm_eur < ceiling:
            return mult
    return TM_TO_FEE_BANDS[-1][1]


# Minimum max_transfer_fee_eur for a buyer to be considered a credible brokerage
# candidate. Clubs below this threshold are not in the conversation regardless
# of position need. Surfaces clubs that can "stretch" — they may not match the
# full indicative fee but ≥€15m signals real intent to spend on a single signing.
MIN_BROKERAGE_FEE = 15_000_000

# League-tier hierarchy used by the matcher's "lateral or upward only" rule.
# Lower tier number = higher tier. Tier-D self-contained (SA1/MLS1 moves only
# permitted out of D if the player is already in D).
LEAGUE_TIERS: dict[str, int] = {
    "GB1": 1, "ES1": 1, "IT1": 1, "L1": 1, "FR1": 1,
    "PO1": 2, "NL1": 2, "BE1": 2, "TR1": 2,
    "GB2": 2, "FR2": 2, "ES2": 2, "IT2": 2, "L2": 2,
    "DK1": 3, "SC1": 3, "GR1": 3,
    "SA1": 4, "MLS1": 4,
}
