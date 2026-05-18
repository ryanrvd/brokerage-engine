"""Position View — market-per-position drill-in.

Default state (no ?bucket= param): overview grid of 10 cards, one per
position bucket, summarising availability + demand + market tension. Click a
card → drilled-in state.

Detail state (?bucket=XX): full per-position market view — KPI tiles, a
plain-English commentary panel, two mini supply-vs-demand bar charts, then
three tables (available players, clubs looking, all matches at the position).
Kill List players appear in the tables with a pale red tint + ⊘ Excluded
badge, but are excluded from KPI tile picks and commentary named-player
references (those are operational signals; non-actionable picks would mislead
the reader).
"""

from __future__ import annotations

from datetime import datetime
import pandas as pd
import streamlit as st

import db
import labels
import components as ui

# config.DEMAND_MAPPED_LEAGUES — single source of truth for which leagues we
# trust as demand sources (the 10 manually mapped via Google Sheets).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
import config

# SQL placeholders for the "league IN (...)" clauses.
_DML_PLACEHOLDERS = ",".join("?" * len(config.DEMAND_MAPPED_LEAGUES))

st.set_page_config(
    page_title="Brokerage Engine",
    page_icon="📍",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# ─── Sidebar — wordmark + global search (matches other pages) ────────────────
with st.sidebar:
    st.markdown(
        """
        <div class="rvc-wordmark">
            <div class="rvc-wordmark-title">Brokerage Engine</div>
            <div class="rvc-wordmark-subtitle">
                <span class="rvc-wordmark-attribution">RMC</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())


# ─── Position name dictionaries ──────────────────────────────────────────────
# Used everywhere the user sees a position — page titles, card labels,
# commentary narratives. Internal bucket key stays ST_CF / etc.
POSITION_NAMES = {
    "GK":    "Goalkeeper",
    "CB":    "Centre-back",
    "LB":    "Left-back",
    "RB":    "Right-back",
    "DM":    "Defensive midfielder",
    "CM":    "Central midfielder",
    "AM":    "Attacking midfielder",
    "LW":    "Left winger",
    "RW":    "Right winger",
    "ST_CF": "Striker",
}
POSITION_NAMES_LOWER = {k: v.lower() for k, v in POSITION_NAMES.items()}
ALL_BUCKETS = ["GK", "CB", "LB", "RB", "DM", "CM", "AM", "LW", "RW", "ST_CF"]


def _tension_label(clubs: int, players: int) -> tuple[str, str, str]:
    """Return (label, css-colour, broker-opportunity-framing) for the
    clubs-per-player ratio. Plain broker-vernacular labels — drop the
    real-estate "tight sellers' market" idiom in favour of explicitly naming
    who has the upper hand. The opportunity-framing line stays explanatory."""
    if players <= 0 or clubs <= 0:
        return (
            "No clear signal",
            "#9ca3af",
            "Insufficient activity to call a direction.",
        )
    ratio = clubs / players
    if ratio > 5:
        return (
            "Sellers in control",
            "#b91c1c",
            "Supply-constrained — buyers competing, sellers in strong position. "
            "High broker opportunity.",
        )
    if ratio >= 2:
        return (
            "Balanced",
            "#15803d",
            "Supply and demand roughly matched. Standard deal flow.",
        )
    return (
        "Buyers in control",
        "#1d4ed8",
        "Oversupplied — buyers in control. Sellers will need flexibility on "
        "fee or structure.",
    )


def _money(x) -> str:
    if x is None or pd.isna(x): return "—"
    x = float(x)
    if abs(x) >= 1_000_000: return f"€{x/1_000_000:.1f}m"
    if abs(x) >= 1_000:     return f"€{x/1_000:.0f}k"
    return f"€{int(x)}"


# ─── Cached helpers for the overview grid ────────────────────────────────────
@st.cache_data(ttl=60)
def _overview_data() -> dict[str, dict]:
    """For each bucket, count available players (sellable cohort) and
    distinct clubs requesting that bucket. Returns {bucket: {n_players, n_clubs}}."""
    con = db.get_connection()
    out: dict[str, dict] = {}
    excluded = db.get_excluded_ids()
    for b in ALL_BUCKETS:
        n_players_universe = con.execute("""
            SELECT COUNT(*) FROM player_universe
            WHERE position_bucket = ?
              AND (right_priced=1 OR finished_product=1
                   OR finished_product IS NULL OR contract_leveraged=1)
        """, (b,)).fetchone()[0]
        n_players_excluded = 0
        if excluded:
            ids_sql = "(" + ",".join(str(int(i)) for i in excluded) + ")"
            n_players_excluded = con.execute(f"""
                SELECT COUNT(*) FROM player_universe
                WHERE position_bucket = ? AND player_id IN {ids_sql}
                  AND (right_priced=1 OR finished_product=1
                       OR finished_product IS NULL OR contract_leveraged=1)
            """, (b,)).fetchone()[0]
        n_players = n_players_universe - n_players_excluded

        # Distinct clubs looking at this position — union of explicit +
        # inferred, restricted to the 10 demand-mapped leagues only.
        n_clubs_rows = con.execute(f"""
            SELECT COUNT(DISTINCT club_id) FROM (
                SELECT club_id FROM map_club_requests
                WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
                UNION
                SELECT club_id FROM inferred_club_requests
                WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
            )
        """, (b, *config.DEMAND_MAPPED_LEAGUES,
              b, *config.DEMAND_MAPPED_LEAGUES)).fetchone()[0]
        out[b] = {
            "n_players": n_players,
            "n_players_universe": n_players_universe,
            "n_clubs": n_clubs_rows,
        }
    return out


# ─── Resolve query param ─────────────────────────────────────────────────────
raw_bucket = st.query_params.get("bucket")
bucket = raw_bucket if raw_bucket in ALL_BUCKETS else None


# ═════════════════════════════════════════════════════════════════════════════
# OVERVIEW STATE (no bucket selected) — 10-card grid
# ═════════════════════════════════════════════════════════════════════════════
if bucket is None:
    st.markdown(
        """
        <div class="rvc-page-title">
            <h1>Position markets</h1>
            <div class="rvc-page-subtitle">
                Pick a position to see who's available, who's looking, and where
                the arbitrage corridors are.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    data = _overview_data()

    # CSS for the card grid — auto-fit responsive, capped at 1400px so cards
    # don't shrink too narrow on ultra-wide displays.
    st.markdown(
        """
        <style>
        .rvc-pos-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
            max-width: 1400px;
            margin: 14px 0 8px 0;
        }
        .rvc-pos-card {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 16px 18px;
            text-decoration: none;
            color: inherit;
            display: block;
            transition: border-color 0.15s, transform 0.15s, box-shadow 0.15s;
            cursor: pointer;
        }
        .rvc-pos-card:hover {
            border-color: #1F3864;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.08);
            text-decoration: none;
        }
        .rvc-pos-card-pill {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 10px;
            font-weight: 600;
            font-size: 0.78rem;
            color: #1f2937;
        }
        .rvc-pos-card-title {
            font-weight: 700;
            font-size: 1.15rem;
            color: #111827;
            margin: 6px 0 12px 0;
            line-height: 1.2;
        }
        .rvc-pos-card-stat {
            font-size: 0.85rem;
            color: #374151;
            line-height: 1.5;
        }
        .rvc-pos-card-stat strong { color: #111827; font-weight: 700; }
        .rvc-pos-card-tension {
            font-weight: 600;
            font-size: 0.85rem;
            margin-top: 4px;
        }
        .rvc-pos-card-ratio {
            color: #6b7280;
            font-size: 0.75rem;
            margin-top: 8px;
            padding-top: 8px;
            border-top: 1px solid #f3f4f6;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    cards_html = []
    for b in ALL_BUCKETS:
        d = data[b]
        tier = labels.super_bucket(b)
        tier_colour = labels.TIER_COLOURS.get(tier, "#e5e7eb")
        # Overview cards show just label + colour; opportunity-framing is
        # reserved for the drilled-in KPI tile + commentary panel.
        tension_lbl, tension_colour, _ = _tension_label(d["n_clubs"], d["n_players"])
        ratio_str = (f"{d['n_clubs'] / d['n_players']:.1f}× demand"
                     if d["n_players"] > 0 else "no supply")
        cards_html.append(
            f'<a class="rvc-pos-card" href="/position_view?bucket={b}" target="_self">'
            f'  <span class="rvc-pos-card-pill" style="background:{tier_colour};">'
            f'    ● {labels.display_bucket(b)}'
            f'  </span>'
            f'  <div class="rvc-pos-card-title">{POSITION_NAMES[b]}</div>'
            f'  <div class="rvc-pos-card-stat"><strong>{d["n_players"]}</strong> available</div>'
            f'  <div class="rvc-pos-card-stat"><strong>{d["n_clubs"]}</strong> clubs looking</div>'
            f'  <div class="rvc-pos-card-tension" style="color:{tension_colour};">{tension_lbl}</div>'
            f'  <div class="rvc-pos-card-ratio">→ {ratio_str}</div>'
            f'</a>'
        )
    st.markdown(f'<div class="rvc-pos-grid">{"".join(cards_html)}</div>',
                unsafe_allow_html=True)
    st.stop()


# ═════════════════════════════════════════════════════════════════════════════
# DETAIL STATE (bucket selected) — full market view
# ═════════════════════════════════════════════════════════════════════════════

# Top spacer so the Back button has breathing room from the nav bar
st.markdown("<div style='margin-top:0.5rem;'></div>", unsafe_allow_html=True)
if st.button("← Back to all positions", key="back_to_positions"):
    try:
        del st.query_params["bucket"]
    except KeyError:
        pass
    st.rerun()

position_name = POSITION_NAMES[bucket]
position_name_lower = POSITION_NAMES_LOWER[bucket]
tier = labels.super_bucket(bucket)
tier_full = {"GK": "Goalkeeping", "DEF": "Defensive", "MID": "Midfield", "ATT": "Attacking"}.get(tier, "")
tier_colour = labels.TIER_COLOURS.get(tier, "#e5e7eb")

st.markdown(f"## {position_name} market")
st.markdown(
    f'<div style="color:#374151; font-size:0.95rem; margin-bottom:6px;">'
    f'<span style="display:inline-block; padding:2px 10px; border-radius:10px; '
    f'background:{tier_colour}; font-weight:600; font-size:0.85rem; color:#1f2937;">'
    f'● {labels.display_bucket(bucket)}</span>'
    f' · {tier_full} bucket'
    f'</div>',
    unsafe_allow_html=True,
)

# ─── Pull data ───────────────────────────────────────────────────────────────
data = db.get_position(bucket)
players_all = data["players"].copy()    # sellable cohort at this bucket
matches_all = data["matches"]           # every match row at this bucket

# Restrict matches to buyers in the 10 demand-mapped leagues only. Same
# principle as the demand counts above — we don't trust demand signal from
# leagues outside that set, so matches with non-mapped buyers shouldn't
# appear here either.
matches_all = matches_all[
    matches_all["buyer_league_id"].isin(config.DEMAND_MAPPED_LEAGUES)
].reset_index(drop=True)

excluded_ids = db.get_excluded_ids()
con = db.get_connection()

# Attach parent_league to players (db.get_position doesn't join club_pressure,
# so we derive parent_league from parent_club_id ourselves). club_id is TEXT
# in both tables — coerce to str on both sides of the lookup.
_parent_league_lookup = {
    str(cid): lg for cid, lg in
    con.execute("SELECT club_id, league_id FROM club_pressure").fetchall()
}
players_all["parent_league"] = players_all["parent_club_id"].apply(
    lambda cid: _parent_league_lookup.get(str(int(cid))) if pd.notna(cid) else None
)

# ─── Sidebar filters ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("---")
    # Use the larger of player + match league sets for the filter options
    league_options = sorted(set(
        list(players_all["parent_league"].dropna().unique())
        + list(matches_all["parent_league"].dropna().unique())
        + list(matches_all["buyer_league_id"].dropna().unique())
    ))
    st.markdown('<div class="rvc-filter-section-label">League</div>', unsafe_allow_html=True)
    sel_leagues = st.multiselect(
        "League", options=league_options,
        key=f"pv_filter_league_{bucket}",
        format_func=labels.league_name,
        placeholder="All leagues",
        label_visibility="collapsed",
    )

    sl_lo = int(players_all["sellability_score"].min()) if len(players_all) else 0
    sl_hi = int(players_all["sellability_score"].max()) + 1 if len(players_all) else 100
    st.markdown('<div class="rvc-filter-section-label">Sellability</div>', unsafe_allow_html=True)
    sel_sell = st.slider(
        "Sellability", min_value=sl_lo, max_value=sl_hi,
        value=(sl_lo, sl_hi), step=1,
        key=f"pv_filter_sell_{bucket}",
        label_visibility="collapsed",
    )

    ms_lo = int(matches_all["match_score"].min()) if len(matches_all) else 0
    ms_hi = int(matches_all["match_score"].max()) + 1 if len(matches_all) else 100
    st.markdown('<div class="rvc-filter-section-label">Match score</div>', unsafe_allow_html=True)
    sel_match = st.slider(
        "Match score", min_value=ms_lo, max_value=ms_hi,
        value=(ms_lo, ms_hi), step=1,
        key=f"pv_filter_match_{bucket}",
        label_visibility="collapsed",
    )

    st.markdown('<div class="rvc-filter-section-label">Kill List</div>', unsafe_allow_html=True)
    show_kill_list = st.toggle(
        "Show Kill List players",
        value=True,
        key=f"pv_show_kill_{bucket}",
        help="ON: Kill List players visible in tables with the ⊘ Excluded badge. "
             "KPI tiles and commentary always exclude them from named picks.",
    )

# ─── Apply filters to working copies ─────────────────────────────────────────
players_view = players_all.copy()
matches_view = matches_all.copy()

if sel_leagues:
    players_view = players_view[players_view["parent_league"].isin(sel_leagues)]
    matches_view = matches_view[
        matches_view["parent_league"].isin(sel_leagues)
        | matches_view["buyer_league_id"].isin(sel_leagues)
    ]
players_view = players_view[players_view["sellability_score"].between(sel_sell[0], sel_sell[1])]
matches_view = matches_view[matches_view["match_score"].between(sel_match[0], sel_match[1])]

# Kill-list-aware cohorts
players_actionable = players_view[~players_view["player_id"].isin(excluded_ids)].copy()
matches_actionable = matches_view[~matches_view["player_id"].isin(excluded_ids)].copy()

# Universe count BEFORE Kill List exclusion (for the KPI tooltip)
n_avail_actionable = len(players_actionable)
n_avail_universe = len(players_view)

# Distinct clubs looking at this position — restricted to the 10 demand-
# mapped leagues only (MLS/Saudi/Greek/Scottish/Danish/Turkish + non-FRA
# second tiers are excluded since our demand-side coverage stops there).
n_clubs = con.execute(f"""
    SELECT COUNT(DISTINCT club_id) FROM (
        SELECT club_id FROM map_club_requests
        WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
        UNION
        SELECT club_id FROM inferred_club_requests
        WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
    )
""", (bucket, *config.DEMAND_MAPPED_LEAGUES,
      bucket, *config.DEMAND_MAPPED_LEAGUES)).fetchone()[0]

# Top match — Kill-List-excluded, post-filter
top_match_row = None
if len(matches_actionable):
    top_match_row = matches_actionable.iloc[matches_actionable["match_score"].idxmax()]

# ─── KPI tiles ───────────────────────────────────────────────────────────────
tension_lbl, tension_colour, tension_opportunity = _tension_label(n_clubs, n_avail_actionable)
avail_subline = (
    f"({n_avail_universe} before Kill List)"
    if n_avail_universe != n_avail_actionable else ""
)
ratio_str = (f"{n_clubs / n_avail_actionable:.1f} / player"
             if n_avail_actionable > 0 else "—")

if top_match_row is not None:
    top_player_name = labels.player_display_name(
        int(top_match_row["player_id"]), top_match_row["player_name"])
    top_buyer_name = labels.club_display_name(
        int(top_match_row["buyer_club_id"]) if pd.notna(top_match_row["buyer_club_id"]) else None,
        top_match_row["buyer_club_name"])
    top_match_value = f"{float(top_match_row['match_score']):.1f}"
    top_match_subline = f"{top_player_name} → {top_buyer_name}"
else:
    top_match_value = "—"
    top_match_subline = ""


def _tile(label: str, value: str, *, subline: str = "", title: str = "") -> str:
    sub_html = f'<div class="rvc-tile-sub">{subline}</div>' if subline else ""
    return (
        f'<div class="rvc-tile" title="{title}">'
        f'<div class="rvc-tile-label">{label}</div>'
        f'<div class="rvc-tile-value">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


tiles = [
    _tile("Available players", str(n_avail_actionable),
          subline=avail_subline,
          title="Players at this position passing the sellable filters. Excludes "
                "Kill List players; subline shows the unfiltered universe count "
                "when the Kill List removes anyone."),
    _tile("Clubs looking", str(n_clubs),
          title="Distinct buyer clubs with a stated need at this position "
                "(explicit demand + inferred squad-thinness)."),
    _tile("Market tension", ratio_str,
          subline=(
              f'<span style="color:{tension_colour}; font-weight:700;">{tension_lbl}</span>'
              f'<div style="color:#4b5563; font-weight:500; font-size:0.78rem; '
              f'margin-top:4px; line-height:1.35; white-space:normal;">'
              f'{tension_opportunity}</div>'
          ),
          title="Clubs looking ÷ available players. Demand restricted to the "
                "10 mapped leagues (England 1-2, Spain 1, Italy 1, France 1-2, "
                "Germany 1, Portugal 1, Netherlands 1, Belgium 1)."),
    _tile("Top match", top_match_value,
          subline=top_match_subline,
          title="Highest match score at this position (Kill List excluded)."),
]
st.markdown(f'<div class="rvc-tile-row">{"".join(tiles)}</div>', unsafe_allow_html=True)


# ─── Market commentary panel ─────────────────────────────────────────────────
def _league_count(df: pd.DataFrame, col: str) -> dict[str, int]:
    """Group rows by league code → count, sorted descending. Dedup if `col` is buyer."""
    if not len(df):
        return {}
    s = df[col].dropna()
    if not len(s):
        return {}
    # For demand side, we want unique clubs per league, not raw rows.
    # Caller handles dedup before passing.
    return s.value_counts().to_dict()


def _build_commentary() -> str:
    """Compose the 3-5 sentence market narrative. Named players are Kill-List-
    excluded. Demand counts restricted to the 10 mapped leagues. Uses league
    display names throughout. Finishes with the broker-operational implication
    so the reader doesn't have to interpret a tension ratio in isolation."""
    avail_actionable = n_avail_actionable
    universe_total = n_avail_universe
    clubs_total = n_clubs
    ratio = (clubs_total / avail_actionable) if avail_actionable else 0

    # Lowercase label for mid-sentence rendering after the em-dash.
    label_inline = {
        "Sellers in control": "sellers in control",
        "Balanced":           "balanced",
        "Buyers in control":  "buyers in control",
        "No clear signal":    "no clear signal",
    }.get(tension_lbl, "unusual market")
    # Standalone opportunity sentence — full stop, capitalised, follows the
    # label clause directly. Drops the "is a [LABEL]" pattern in favour of
    # "[Position] — [label]. [Opportunity]." which reads cleaner.
    opportunity_sentence = {
        "Sellers in control": "Supply-constrained with strong broker opportunity.",
        "Balanced":           "Supply and demand roughly matched.",
        "Buyers in control":  "Oversupplied with the buyer side holding leverage.",
        "No clear signal":    "Not enough activity to call a direction.",
    }.get(tension_lbl, "")

    # Sentence 1 — headline tension as standalone clause, then opportunity,
    # then availability data.
    excl_clause = (f" ({universe_total} before Kill List)"
                   if universe_total != avail_actionable else "")
    if avail_actionable > 0:
        s1 = (f"{position_name} — {label_inline}."
              + (f" {opportunity_sentence}" if opportunity_sentence else "")
              + f" {avail_actionable} players available{excl_clause}, "
                f"{clubs_total} clubs looking across our demand-mapped leagues "
                f"— roughly {ratio:.1f} buyers per player.")
    else:
        s1 = (f"{position_name} has no available players right now — "
              f"{clubs_total} clubs in our demand-mapped leagues are looking, "
              f"but the supply side is empty.")

    # Sentence 2 — demand concentration (unique clubs by league, mapped-only)
    demand_pairs = con.execute(f"""
        SELECT league, COUNT(DISTINCT club_id) AS n_clubs
        FROM (
            SELECT league, club_id FROM map_club_requests
            WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
            UNION
            SELECT league, club_id FROM inferred_club_requests
            WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
        )
        GROUP BY league
        ORDER BY n_clubs DESC
    """, (bucket, *config.DEMAND_MAPPED_LEAGUES,
          bucket, *config.DEMAND_MAPPED_LEAGUES)).fetchall()
    demand_top = [(labels.league_name(lg), n) for lg, n in demand_pairs[:3] if lg]
    if demand_top:
        demand_str = ", ".join([f"{name} ({n})" for name, n in demand_top])
        s2 = f"Demand sits heaviest in {demand_str}."
    else:
        s2 = ""

    # Sentence 3 — supply concentration + arbitrage corridor
    supply_pairs = (
        players_actionable.groupby("parent_league").size().sort_values(ascending=False)
        if len(players_actionable) else pd.Series(dtype=int)
    )
    supply_top = [(labels.league_name(lg), int(n)) for lg, n in supply_pairs.head(3).items() if lg]
    s3 = ""
    if supply_top:
        supply_str = ", ".join([f"{name} ({n})" for name, n in supply_top])
        demand_top_names = {n for n, _ in demand_top}
        supply_only_leagues = [n for n, _ in supply_top if n not in demand_top_names]
        if supply_only_leagues:
            top_destination_names = [n for n, _ in demand_top[:2]]
            dest_str = " and ".join(top_destination_names) if top_destination_names else "top-tier buyers"
            src_str = " and ".join(supply_only_leagues[:2])
            corridor_clause = (
                f" — clear pathways from {src_str} up to {dest_str} buyers."
            )
            s3 = f"Supply is concentrated in {supply_str}{corridor_clause}"
        else:
            shared = supply_top[0][0]
            s3 = (f"Supply is concentrated in {supply_str}. "
                  f"Supply and demand overlap heavily in {shared} — "
                  f"fewer cross-league pathways than typical.")

    # Sentence 4 — strongest named players (top 2 by sellability, Kill List excluded)
    s4 = ""
    if len(players_actionable):
        top2 = players_actionable.nlargest(2, "sellability_score")
        names = []
        for _, r in top2.iterrows():
            pname = labels.player_display_name(int(r["player_id"]), r["name"])
            club_disp = labels.club_display_name(
                int(r["parent_club_id"]) if pd.notna(r.get("parent_club_id")) else None,
                r.get("parent_club") or r.get("current_club") or "")
            sell = r.get("sellability_score") or 0
            names.append(f"<strong>{pname}</strong> ({club_disp}, sellability {sell:.1f})")
        if names:
            s4 = "Strongest available: " + " and ".join(names) + "."

    # Sentence 5 — closing line, tied to broker implication
    closing = {
        "Sellers in control": (
            f"Available {position_name_lower}s will move at premium fees; "
            "our negotiating position with selling clubs is strong."),
        "Balanced": (
            "Deals will require careful matching of player fit and club budgets "
            "— no structural pricing tailwind in either direction."),
        "Buyers in control": (
            f"Selling clubs at {position_name_lower} will need flexibility on "
            "fee or deal structure; buyers can be selective."),
        "No clear signal": "",
    }.get(tension_lbl, "")

    return " ".join(s for s in (s1, s2, s3, s4, closing) if s)


commentary_html = _build_commentary()
st.markdown(
    f'<div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px; '
    f'padding:16px 18px; margin:18px 0 6px 0; line-height:1.6; color:#374151; '
    f'font-size:0.95rem;">{commentary_html}</div>',
    unsafe_allow_html=True,
)


# ─── Supply vs Demand mini bar charts ────────────────────────────────────────
def _render_bar_chart(title: str, pairs: list[tuple[str, int]], unit_label: str) -> str:
    """Build a small horizontal-bar HTML chart from (league_name, count) pairs."""
    if not pairs:
        return (
            f'<div class="rvc-mini-chart">'
            f'<div class="rvc-mini-chart-title">{title}</div>'
            f'<div style="color:#6b7280; font-size:0.85rem; padding:8px 0;">No data.</div>'
            f'</div>'
        )
    max_n = max(n for _, n in pairs) or 1
    rows = []
    for name, n in pairs:
        width_pct = (n / max_n) * 100
        rows.append(
            f'<div class="rvc-bar-row">'
            f'  <div class="rvc-bar-label">{name}</div>'
            f'  <div class="rvc-bar-track">'
            f'    <div class="rvc-bar-fill" style="width:{width_pct:.1f}%;"></div>'
            f'  </div>'
            f'  <div class="rvc-bar-count">{n}</div>'
            f'</div>'
        )
    return (
        f'<div class="rvc-mini-chart">'
        f'<div class="rvc-mini-chart-title">{title}</div>'
        + "".join(rows) +
        f'<div class="rvc-bar-foot">{unit_label}</div>'
        f'</div>'
    )


# Supply (players grouped by parent_league, Kill-List-excluded — same cohort
# as the named players in commentary)
_supply_groups = (
    players_actionable.groupby("parent_league").size().sort_values(ascending=False)
    if len(players_actionable) else pd.Series(dtype=int)
)
_supply_top6 = [(labels.league_name(lg), int(n)) for lg, n in _supply_groups.head(6).items() if lg]
_supply_extra = len(_supply_groups) - len(_supply_top6)
if _supply_extra > 0:
    _supply_top6.append((f"+ {_supply_extra} more leagues", 0))

# Demand (distinct clubs per league) — restricted to the 10 demand-mapped leagues
_demand_groups = con.execute(f"""
    SELECT league, COUNT(DISTINCT club_id) AS n_clubs FROM (
        SELECT league, club_id FROM map_club_requests
        WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
        UNION
        SELECT league, club_id FROM inferred_club_requests
        WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
    )
    GROUP BY league ORDER BY n_clubs DESC
""", (bucket, *config.DEMAND_MAPPED_LEAGUES,
      bucket, *config.DEMAND_MAPPED_LEAGUES)).fetchall()
_demand_top6 = [(labels.league_name(lg), int(n)) for lg, n in _demand_groups[:6] if lg]
_demand_extra = max(0, len(_demand_groups) - len(_demand_top6))
if _demand_extra > 0:
    _demand_top6.append((f"+ {_demand_extra} more leagues", 0))

st.markdown(
    """
    <style>
    .rvc-charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 6px 0 18px 0; }
    .rvc-mini-chart { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 18px; }
    .rvc-mini-chart-title { font-size: 0.78rem; font-weight: 700; color: #6B7280;
        text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
    .rvc-bar-row { display: grid; grid-template-columns: 130px 1fr 36px;
        gap: 8px; align-items: center; padding: 3px 0; font-size: 0.85rem; color: #374151; }
    .rvc-bar-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .rvc-bar-track { background: #f3f4f6; border-radius: 4px; height: 16px; overflow: hidden; }
    .rvc-bar-fill { background: #1F3864; height: 100%; border-radius: 4px; min-width: 2px; }
    .rvc-bar-count { font-weight: 600; color: #111827; text-align: right; font-size: 0.85rem; }
    .rvc-bar-foot { color: #9ca3af; font-size: 0.72rem; margin-top: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="rvc-charts-row">'
    + _render_bar_chart("Where the supply is", _supply_top6, "players available per league")
    + _render_bar_chart("Where the demand is", _demand_top6, "distinct clubs looking per league")
    + '</div>',
    unsafe_allow_html=True,
)


# ─── Helpers for the three tables ────────────────────────────────────────────
def _player_cell(row: pd.Series) -> str:
    """Player name as same-tab anchor + ⊘ Excluded badge on Kill List rows."""
    pid = int(row["player_id"])
    display_name = labels.player_display_name(pid, row.get("name") or row.get("player_name") or "")
    anchor = ui.player_link(pid, display_name)
    if pid in excluded_ids:
        reason = ""
        anchor += f' <span class="rvc-excluded-badge" title="{reason or "Excluded from Targets"}">⊘ Excluded</span>'
    return anchor


def _fmt_yrs(ce) -> str:
    if not ce or pd.isna(ce): return "—"
    try:
        snap = db.get_snapshot_date()
        yrs = round((datetime.fromisoformat(str(ce)).date() - snap).days / 365.25, 1)
        return f"{yrs:.1f}"
    except (TypeError, ValueError):
        return "—"


# ─── Available players table ─────────────────────────────────────────────────
players_for_table = players_view.copy()  # keep Kill List rows visible (with badge)
if not show_kill_list:
    players_for_table = players_for_table[~players_for_table["player_id"].isin(excluded_ids)]

st.markdown(
    f"### Available players ({len(players_for_table)}) {ui.level_fit_info_icon()}",
    unsafe_allow_html=True,
)
if not len(players_for_table):
    st.info("No players match the current filters at this position.")
else:
    pdf = players_for_table.copy()
    # Effective parent contract (loan-corrected when present)
    pdf["yrs_str"] = pdf.apply(
        lambda r: _fmt_yrs(r.get("parent_contract_end_date") or r.get("contract_end_date")),
        axis=1,
    )
    pdf["current_club_display"] = pdf.apply(
        lambda r: labels.club_display_name(
            int(r["current_club_id"]) if pd.notna(r.get("current_club_id")) else None,
            r.get("current_club")),
        axis=1,
    )
    pdf["parent_club_display"] = pdf.apply(
        lambda r: labels.club_display_name(
            int(r["parent_club_id"]) if pd.notna(r.get("parent_club_id")) else None,
            r.get("parent_club")),
        axis=1,
    )
    pdf["parent_with_lg"] = pdf.apply(
        lambda r: f"{r['parent_club_display']} · {labels.league_name(r.get('parent_league'))}"
        if r.get("parent_league") else r["parent_club_display"],
        axis=1,
    )
    pdf["mv_str"] = pdf["current_tm_value_eur"].apply(_money)
    pdf["bucket_tier"] = pdf["position_bucket"].apply(labels.super_bucket)
    # Match counts per player (already in sellable list but matches may exclude some)
    if len(pdf):
        match_counts = dict(con.execute(
            f"SELECT player_id, COUNT(*) FROM matches "
            f"WHERE player_id IN ({','.join(str(int(x)) for x in pdf['player_id'])}) "
            f"GROUP BY player_id"
        ).fetchall())
    else:
        match_counts = {}
    pdf["buyer_matches"] = pdf["player_id"].apply(lambda pid: match_counts.get(int(pid), 0))
    # Sort by sellability desc (default; clickable headers handle the rest)
    pdf = pdf.sort_values("sellability_score", ascending=False, kind="stable").reset_index(drop=True)
    pdf["Player_html"] = pdf.apply(_player_cell, axis=1)
    pdf.insert(0, "rank", range(1, len(pdf) + 1))
    # Top-5 by sellability (identity-based, Kill-List-included so stars don't shift on toggle)
    pdf["is_top5"] = pdf["sellability_score"].rank(method="first", ascending=False) <= 5
    _top5_mask = pdf["is_top5"].tolist()
    _excluded_mask = pdf["player_id"].isin(excluded_ids).tolist()

    # Sci Sports CA / PA + Level fit (vs the player's top-scoring buyer)
    pdf["ca_str"] = pdf["player_id"].apply(lambda pid: (
        f"{r[0]:.0f}" if (r := con.execute(
            "SELECT current_ability FROM player_ratings WHERE tm_player_id=? AND current_ability IS NOT NULL",
            (int(pid),)).fetchone()) else "—"
    ))
    pdf["pa_str"] = pdf["player_id"].apply(lambda pid: (
        f"{r[0]:.0f}" if (r := con.execute(
            "SELECT potential_ability FROM player_ratings WHERE tm_player_id=? AND potential_ability IS NOT NULL",
            (int(pid),)).fetchone()) else "—"
    ))
    # Level fit relative to the player's HIGHEST-scoring buyer match.
    _top_lf: dict[int, str | None] = {}
    for pid_int in pdf["player_id"]:
        row = con.execute(
            "SELECT level_fit FROM matches WHERE player_id=? "
            "ORDER BY match_score DESC LIMIT 1",
            (int(pid_int),)).fetchone()
        _top_lf[int(pid_int)] = row[0] if row else None
    pdf["level_fit_pill"] = pdf["player_id"].apply(
        lambda pid: ui.level_fit_pill(_top_lf.get(int(pid))))

    show_df = pdf[[
        "rank", "Player_html", "age", "bucket_tier",
        "current_club_display", "parent_with_lg",
        "yrs_str", "mv_str", "ca_str", "pa_str", "level_fit_pill",
        "sellability_score", "buyer_matches",
    ]].rename(columns={
        "rank":                 "#",
        "Player_html":          "Player",
        "age":                  "Age",
        "bucket_tier":          "Bucket",
        "current_club_display": "Current Club",
        "parent_with_lg":       "Parent Club",
        "yrs_str":              "Years Remaining",
        "mv_str":               "Market Value",
        "ca_str":               "CA",
        "pa_str":               "PA",
        "level_fit_pill":       "Level fit (top buyer)",
        "sellability_score":    "Sellability",
        "buyer_matches":        "Buyer Matches",
    })
    show_df["Sellability"] = show_df["Sellability"].apply(lambda v: f"{v:.1f}")
    show_df["#"] = [(f"⭐ {i + 1}" if _top5_mask[i] else str(i + 1)) for i in range(len(show_df))]

    def _bucket_bg(val):
        colour = labels.TIER_COLOURS.get(val, "")
        return f"background-color: {colour}; font-weight: 600;" if colour else ""

    def _highlight_top_rows(row):
        idx = int(row.name)
        return ['background-color: #fffbf0;' if (_top5_mask[idx] if 0 <= idx < len(_top5_mask) else False) else '' for _ in row]

    def _highlight_excluded(row):
        idx = int(row.name)
        return ['background-color: #FEF2F2;' if (_excluded_mask[idx] if 0 <= idx < len(_excluded_mask) else False) else '' for _ in row]

    def _bg_sell(v):
        try: return ui.green_gradient(float(v), 15, 100)
        except (TypeError, ValueError): return ""

    styled = (
        show_df.style
        .apply(_highlight_top_rows,  axis=1)
        .apply(_highlight_excluded,  axis=1)  # red beats cream on conflict
        .map(_bucket_bg, subset=["Bucket"])
        .map(_bg_sell,   subset=["Sellability"])
    )
    # Static headers — multi-table sort on a single page needs per-table
    # query-param namespacing (deferred). Default sort: Sellability DESC.
    ui.render_html_table(styled, max_height_px=min(620, 60 + 38 * len(show_df)))

# ─── Clubs looking table ─────────────────────────────────────────────────────
# Pulls explicit demand only (map_club_requests) AND restricts to the 10
# demand-mapped leagues, so the table never surfaces buyer demand we don't
# trust as actionable signal.
clubs_rows = con.execute(f"""
    SELECT club_id, club_name, league, preferred_side, validated,
           max_transfer_fee_eur, max_wage_pw_eur, linked_shortlisted_player
    FROM map_club_requests
    WHERE position_bucket = ? AND league IN ({_DML_PLACEHOLDERS})
    ORDER BY max_transfer_fee_eur DESC NULLS LAST
""", (bucket, *config.DEMAND_MAPPED_LEAGUES)).fetchall()

st.markdown(f"### Clubs looking for a {position_name_lower} ({len(clubs_rows)})")
if not clubs_rows:
    st.info(f"No stated buyer demand for {position_name_lower} in our coverage.")
else:
    clubs_df = pd.DataFrame(clubs_rows, columns=[
        "club_id", "club_name", "league", "preferred_side", "validated",
        "max_transfer_fee_eur", "max_wage_pw_eur", "linked_shortlisted_player",
    ])
    clubs_df["club_display"] = clubs_df.apply(
        lambda r: labels.club_display_name(
            int(r["club_id"]) if pd.notna(r["club_id"]) else None, r["club_name"]),
        axis=1,
    )
    clubs_df["Club_html"] = clubs_df.apply(
        lambda r: ui.club_link(int(r["club_id"]) if pd.notna(r["club_id"]) else None,
                               str(r["club_display"])),
        axis=1,
    )
    clubs_df["league_display"] = clubs_df["league"].apply(labels.league_name)
    clubs_df["budget_str"] = clubs_df["max_transfer_fee_eur"].apply(_money)
    clubs_df["wage_str"]   = clubs_df["max_wage_pw_eur"].apply(_money)
    clubs_df["shortlist_str"] = clubs_df["linked_shortlisted_player"].fillna("").astype(str)

    # League filter
    if sel_leagues:
        clubs_df = clubs_df[clubs_df["league"].isin(sel_leagues)]

    clubs_show = clubs_df[[
        "Club_html", "league_display", "preferred_side", "validated",
        "budget_str", "wage_str", "shortlist_str",
    ]].rename(columns={
        "Club_html":       "Club",
        "league_display":  "League",
        "preferred_side":  "Side",
        "validated":       "Validated",
        "budget_str":      "Buyer Budget",
        "wage_str":        "Wage Cap",
        "shortlist_str":   "Confirmed players of interest",
    })
    # Default sort: Buyer Budget DESC (already applied by query ORDER BY).
    styled = clubs_show.style
    ui.render_html_table(styled, max_height_px=min(540, 60 + 38 * len(clubs_show)))

# ─── All matches at this position table ──────────────────────────────────────
matches_for_table = matches_view.copy()
if not show_kill_list:
    matches_for_table = matches_for_table[~matches_for_table["player_id"].isin(excluded_ids)]

st.markdown(f"### All matches at {position_name_lower} ({len(matches_for_table)})")
if not len(matches_for_table):
    st.info("No matches at this position with the current filters.")
else:
    mdf = matches_for_table.copy()
    mdf["current_club_display"] = mdf.apply(
        lambda r: labels.club_display_name(
            int(r["current_club_id"]) if pd.notna(r.get("current_club_id")) else None,
            r.get("current_club")),
        axis=1,
    )
    mdf["parent_club_display"] = mdf.apply(
        lambda r: labels.club_display_name(
            int(r["parent_club_id"]) if pd.notna(r.get("parent_club_id")) else None,
            r.get("parent_club")),
        axis=1,
    )
    mdf["buyer_display"] = mdf.apply(
        lambda r: labels.club_display_name(
            int(r["buyer_club_id"]) if pd.notna(r.get("buyer_club_id")) else None,
            r["buyer_club_name"]),
        axis=1,
    )
    mdf["buyer_league_display"] = mdf["buyer_league_id"].apply(labels.league_name)
    mdf["mv_str"] = mdf["current_tm_value_eur"].apply(_money)
    mdf["budget_str"] = mdf["max_transfer_fee_eur"].apply(_money)
    mdf["rationale"] = mdf.apply(ui.build_rationale, axis=1)
    mdf["Player_html"] = mdf.apply(_player_cell, axis=1)
    mdf["Buyer_html"] = mdf.apply(
        lambda r: ui.club_link(int(r["buyer_club_id"]) if pd.notna(r.get("buyer_club_id")) else None,
                                str(r["buyer_display"])),
        axis=1,
    )
    mdf = mdf.sort_values("match_score", ascending=False, kind="stable").reset_index(drop=True)

    SHOW_LIMIT = 20
    show_all_key = f"pv_show_all_matches_{bucket}"
    show_all = st.session_state.get(show_all_key, False)
    table_df = mdf if (show_all or len(mdf) <= SHOW_LIMIT) else mdf.head(SHOW_LIMIT)
    table_df = table_df.reset_index(drop=True)

    _excluded_match_mask = table_df["player_id"].isin(excluded_ids).tolist()

    show_df = table_df[[
        "Player_html", "current_club_display", "parent_club_display",
        "mv_str", "Buyer_html", "buyer_league_display", "budget_str",
        "match_score", "sellability_score", "rationale",
    ]].rename(columns={
        "Player_html":          "Player",
        "current_club_display": "Current Club",
        "parent_club_display":  "Parent Club",
        "mv_str":               "Market Value",
        "Buyer_html":           "Buyer",
        "buyer_league_display": "Buyer League",
        "budget_str":           "Buyer Budget",
        "match_score":          "Match Score",
        "sellability_score":    "Sellability",
        "rationale":            "Rationale",
    })
    show_df["Match Score"] = show_df["Match Score"].apply(lambda v: f"{v:.1f}")
    show_df["Sellability"] = show_df["Sellability"].apply(lambda v: f"{v:.1f}")

    def _highlight_excluded_mx(row):
        idx = int(row.name)
        return ['background-color: #FEF2F2;' if (_excluded_match_mask[idx] if 0 <= idx < len(_excluded_match_mask) else False) else '' for _ in row]

    def _bg_match(v):
        try: return ui.green_gradient(float(v), 10, 100)
        except (TypeError, ValueError): return ""
    def _bg_sell(v):
        try: return ui.green_gradient(float(v), 15, 100)
        except (TypeError, ValueError): return ""

    styled = (
        show_df.style
        .apply(_highlight_excluded_mx, axis=1)
        .map(_bg_match, subset=["Match Score"])
        .map(_bg_sell,  subset=["Sellability"])
    )
    # Default sort: Match Score DESC.
    ui.render_html_table(styled, max_height_px=min(640, 60 + 38 * len(show_df)))

    if len(mdf) > SHOW_LIMIT and not show_all:
        if st.button(f"Show all {len(mdf)} matches", key=f"toggle_{bucket}"):
            st.session_state[show_all_key] = True
            st.rerun()
    elif show_all and len(mdf) > SHOW_LIMIT:
        if st.button("Collapse to top 20", key=f"toggle_{bucket}_collapse"):
            st.session_state[show_all_key] = False
            st.rerun()
