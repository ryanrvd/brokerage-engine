"""Player View — full drill-in page for one player.

URL: /player_view?player_id=X

Layout (top → bottom):
  1. Kill List banner (conditional)
  2. Header — display name, identity strip, club context panel (right)
  3. 4 metric tiles — Sellability, Top Match, # Buyers, Owning-Club Pressure
  4. Sellability breakdown — narrative + 4 component cards with evidence numbers
  5. Loan Status panel (conditional)
  6. All Matches table — sorted by Match Score DESC, heat-mapped, top-20 + expand
  7. Wage Info panel (conditional)
"""

from __future__ import annotations

from datetime import datetime
import pandas as pd
import streamlit as st

import db
import labels
import components as ui

# config.py — TM-to-fee multiplier (Stage 1 tiered constant; see CLAUDE.md
# "Calibration constants"). Imported via the project-root config module that
# sits one level above app/.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
import config

st.set_page_config(page_title="Brokerage Engine", page_icon="👤", layout="wide")
ui.inject_css()
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())

# ─── Resolve query param ─────────────────────────────────────────────────────

raw_pid = st.query_params.get("player_id")

# ═════════════════════════════════════════════════════════════════════════════
# BROWSER VIEW (no query param) — pick-a-player table + sidebar filters
# ═════════════════════════════════════════════════════════════════════════════
if not raw_pid:
    st.title("Players")
    st.caption("Pick a player to see their full profile, sellability breakdown, and all buyer matches.")

    # Pull all sellable players + their match counts in one go
    matches_all = db.get_all_matches()
    match_counts = matches_all.groupby("player_id").size().to_dict()

    sellable_rows = db.get_player_search_options()  # has display_name, league_id, current_club
    # Enrich each row with metadata from player_universe
    con = db.get_connection()
    extra = {r[0]: r for r in con.execute("""
        SELECT player_id, age, position_bucket, current_club, current_club_id,
               parent_club, parent_club_id, sellability_score, on_loan
        FROM player_universe
        WHERE sellability_status = 'sellable_now'
    """).fetchall()}

    rows = []
    for p in sellable_rows:
        e = extra.get(p["player_id"])
        if not e:
            continue
        _, age, bucket, current_club, current_club_id, parent_club, parent_club_id, sell, on_loan = e
        rows.append({
            "player_id":          p["player_id"],
            "display_name":       p["display_name"],
            "age":                age,
            "position_bucket":    bucket,
            "super_bucket":       labels.super_bucket(bucket or ""),
            "current_club_display": labels.club_display_name(current_club_id, current_club),
            "parent_club_display":  labels.club_display_name(parent_club_id, parent_club),
            "parent_league_id":   p["league_id"],
            "sellability":        round(sell or 0.0, 1),
            "n_matches":          match_counts.get(p["player_id"], 0),
            "on_loan":            bool(on_loan),
        })
    df_all = pd.DataFrame(rows)

    # ─── Sidebar filters ─────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### Filters")
        sel_tiers = st.pills(
            "Bucket tier",
            options=labels.TIER_ORDER, default=labels.TIER_ORDER,
            selection_mode="multi",
        )
        positions_via_tier = [p for t in sel_tiers for p in labels.TIER_POSITIONS.get(t, [])]
        sel_positions = st.multiselect(
            "Position (granular)", options=positions_via_tier,
            default=positions_via_tier,
        )
        parent_leagues = sorted(df_all["parent_league_id"].dropna().unique().tolist())
        sel_leagues = st.multiselect(
            "League of parent club",
            options=parent_leagues, default=parent_leagues,
            format_func=labels.league_name,
        )
        sell_min = int(df_all["sellability"].min()) if len(df_all) else 0
        sell_max = int(df_all["sellability"].max()) + 1 if len(df_all) else 100
        sel_sell = st.slider("Sellability score", sell_min, sell_max, (sell_min, sell_max), 1)
        loan_filter = st.radio(
            "On loan", options=["All", "Loaned only", "Not loaned"],
            index=0, horizontal=True,
        )

    # ─── Apply filters ───────────────────────────────────────────────────
    filtered = df_all.copy()
    if sel_positions:
        filtered = filtered[filtered["position_bucket"].isin(sel_positions)]
    else:
        filtered = filtered.iloc[0:0]
    if sel_leagues:
        filtered = filtered[filtered["parent_league_id"].fillna("").isin(sel_leagues + [""])]
    filtered = filtered[filtered["sellability"].between(sel_sell[0], sel_sell[1])]
    if loan_filter == "Loaned only":
        filtered = filtered[filtered["on_loan"]]
    elif loan_filter == "Not loaned":
        filtered = filtered[~filtered["on_loan"]]
    # Browser default sort: alphabetical by player display name (per user request).
    # Easy to scan for a specific name, complements the dropdown selector above.
    filtered = filtered.sort_values("display_name", ascending=True, kind="stable").reset_index(drop=True)

    # ─── Selector (prominent, top) ───────────────────────────────────────
    selector_options = ["— pick a player —"] + filtered["display_name"].tolist()

    def _on_select():
        sel = st.session_state.get("player_browser_select")
        if sel and sel != "— pick a player —":
            match = filtered[filtered["display_name"] == sel]
            if len(match):
                st.query_params["player_id"] = str(int(match.iloc[0]["player_id"]))

    st.selectbox(
        "Jump to player",
        options=selector_options,
        index=0,
        key="player_browser_select",
        on_change=_on_select,
    )

    # ─── Top stats ───────────────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    c1.metric("Players shown", len(filtered))
    c2.metric("Total in universe", len(df_all))
    c3.metric("Median sellability", f"{filtered['sellability'].median():.1f}" if len(filtered) else "—")

    st.markdown("")

    # ─── Browser table ──────────────────────────────────────────────────
    if len(filtered) == 0:
        st.info("No players match the current filters.")
        st.stop()

    # Pre-build clickable player names with <a target="_self">
    filtered["Player_html"] = filtered.apply(
        lambda r: ui.player_link(int(r["player_id"]), str(r["display_name"])),
        axis=1,
    )
    filtered.insert(0, "rank", range(1, len(filtered) + 1))

    display = filtered[[
        "rank", "Player_html", "age", "super_bucket", "position_bucket",
        "current_club_display", "parent_club_display", "parent_league_id",
        "sellability", "n_matches",
    ]].rename(columns={
        "rank":                  "#",
        "Player_html":           "Player",
        "age":                   "Age",
        "super_bucket":          "Bucket",
        "position_bucket":       "Position",
        "current_club_display":  "Current Club",
        "parent_club_display":   "Parent Club",
        "parent_league_id":      "League",
        "sellability":           "Sellability",
        "n_matches":             "Matches",
    })
    display["League"] = display["League"].apply(labels.league_name)

    # ─── Sort state from query params (drives clickable column headers) ──
    # Headers wrapped by render_html_table become anchors that update
    # ?sort=KEY&dir=desc|asc. Click to sort, click again to flip direction.
    _PV_SORTABLE = {
        "Player":      "name",
        "Sellability": "sellability",
        "Matches":     "matches",
        "Age":         "age",
    }
    pv_sort_key = st.query_params.get("sort", "name")
    pv_sort_dir = st.query_params.get("dir", "asc")
    if pv_sort_key not in _PV_SORTABLE.values():
        pv_sort_key, pv_sort_dir = "name", "asc"
    pv_ascending = (pv_sort_dir == "asc")
    # Map key → sort column. "name" uses raw display_name (the Player column
    # holds anchor HTML, which would sort alphabetically by the tag prefix).
    if pv_sort_key == "name":
        display["_sort_key"] = filtered["display_name"].values
        display = display.sort_values(
            "_sort_key", ascending=pv_ascending, kind="stable", na_position="last"
        ).drop(columns=["_sort_key"]).reset_index(drop=True)
    else:
        _key_to_col = {
            "sellability": "Sellability",
            "matches":     "Matches",
            "age":         "Age",
        }
        display = display.sort_values(
            _key_to_col[pv_sort_key], ascending=pv_ascending,
            kind="stable", na_position="last",
        ).reset_index(drop=True)
    display["#"] = range(1, len(display) + 1)

    # Pre-format numeric columns to strings + bucket display rename
    display["Sellability"] = display["Sellability"].apply(lambda v: f"{v:.1f}")
    display["Position"]    = display["Position"].apply(labels.display_bucket)

    def _bucket_bg(val):
        colour = labels.TIER_COLOURS.get(val, "")
        return f"background-color: {colour}; font-weight: 600;" if colour else ""

    def _bg_sell(v):
        try: return ui.green_gradient(float(v), 15, 100)
        except (TypeError, ValueError): return ""

    styled = (
        display.style
        .map(_bucket_bg, subset=["Bucket"])
        .map(_bg_sell, subset=["Sellability"])
    )
    ui.render_html_table(
        styled, max_height_px=640,
        sortable=_PV_SORTABLE,
        current_sort=(pv_sort_key, pv_sort_dir),
    )
    st.stop()

# ═════════════════════════════════════════════════════════════════════════════
# DETAIL VIEW (query param present) — full profile
# ═════════════════════════════════════════════════════════════════════════════
try:
    pid = int(raw_pid)
except (TypeError, ValueError):
    st.error(f"Bad player_id: {raw_pid!r}")
    st.stop()

# Back button — clears query param and returns to browser
if st.button("← Back to all players", key="back_to_players"):
    try:
        del st.query_params["player_id"]
    except KeyError:
        pass
    st.rerun()

data = db.get_player(pid)
if data["profile"] is None:
    st.error(f"No player with player_id={pid}")
    st.stop()

p = data["profile"]
matches = data["matches"]
# Demand-side restriction: drop matches whose buyer is in a league we don't
# trust as demand signal. Keeps the per-player matches table consistent with
# Targets / All Matches / Position View.
matches = matches[matches["buyer_league_id"].isin(config.DEMAND_MAPPED_LEAGUES)].reset_index(drop=True)
excluded = data["excluded"]
snapshot = db.get_snapshot_date()

# Display layer lookups
player_name = labels.player_display_name(pid, p["name"])
current_club_id = int(p["current_club_id"]) if pd.notna(p.get("current_club_id")) else None
parent_club_id  = int(p["parent_club_id"])  if pd.notna(p.get("parent_club_id"))  else None
current_club_display = labels.club_display_name(current_club_id, p.get("current_club"))
parent_club_display  = labels.club_display_name(parent_club_id,  p.get("parent_club"))
parent_league = labels.league_name(p.get("parent_league_id"))
player_league = labels.league_name(p.get("league_id"))
sb = labels.super_bucket(p.get("position_bucket") or "")

# ─── Kill List banner (top, prominent) ──────────────────────────────────────

if excluded:
    excluded_df = db.get_excluded()
    reason = ""
    source = ""
    if len(excluded_df):
        row = excluded_df[excluded_df["player_id"] == pid]
        if len(row):
            reason = str(row.iloc[0].get("reason") or "")
            source = str(row.iloc[0].get("source") or "")
    st.error(f"🚫 **Kill List** — {reason}" + (f"  _({source})_" if source else ""))

# ─── Header ──────────────────────────────────────────────────────────────────

col_main, col_aside = st.columns([3, 2])
with col_main:
    st.markdown(f"## {player_name}")
    age_str       = f"{int(p['age'])}" if pd.notna(p.get("age")) else "—"
    sub_pos       = p.get("sub_position") or ""
    agency        = p.get("agency") or "—"
    pill_html     = ui.bucket_pill(sb)
    st.markdown(
        f"<div style='color:#374151; font-size:0.95rem;'>"
        f"{age_str} · {sub_pos} · {pill_html} · {agency}"
        f"</div>",
        unsafe_allow_html=True,
    )
    # Sci Sports CA / PA strip — interpretive band as a small tag.
    p_ca = p.get("player_ca")
    p_pa = p.get("player_pa")
    ca_str = f"{float(p_ca):.1f}" if pd.notna(p_ca) else "—"
    pa_str = f"{float(p_pa):.1f}" if pd.notna(p_pa) else "—"
    ca_band = labels.talent_band(float(p_ca) if pd.notna(p_ca) else None)
    pa_band = labels.potential_band(float(p_pa) if pd.notna(p_pa) else None)
    st.markdown(
        f"<div style='color:#374151; font-size:0.9rem; margin-top:4px;'>"
        f"<strong style='color:#1F3864;'>CA {ca_str}</strong> · "
        f"<span style='color:#6b7280;'>{ca_band}</span>"
        f" &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"<strong style='color:#1F3864;'>PA {pa_str}</strong> · "
        f"<span style='color:#6b7280;'>{pa_band}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

with col_aside:
    cc_url = f"/club_view?club_id={current_club_id}" if current_club_id is not None else ""
    pc_url = f"/club_view?club_id={parent_club_id}"  if parent_club_id  is not None else ""
    on_loan = bool(p.get("on_loan"))
    # Raw HTML anchors with target="_self" so clicks stay in the same tab.
    def _club_link(url: str, label: str) -> str:
        if not url:
            return label
        return f'<a href="{ui.with_auth(url)}" target="_self" style="color:#1F3864; font-weight:600;">{label}</a>'

    cc_line = f"<strong>Current:</strong> {_club_link(cc_url, current_club_display)}"
    if on_loan and parent_club_display and parent_club_display != current_club_display:
        cc_line += f"<br><span style='color:#6b7280; font-size:0.85rem;'>(on loan from {parent_club_display})</span>"
    pc_line = ""
    if parent_club_id is not None:
        pc_line = f"<strong>Parent:</strong> {_club_link(pc_url, parent_club_display)} · {parent_league}"
    elif p.get("parent_club"):
        pc_line = f"<strong>Parent:</strong> {p['parent_club']} (outside coverage)"
    st.markdown(
        f"<div style='background:#f9fafb; border:1px solid #e5e7eb; border-radius:6px; padding:12px;'>"
        f"{cc_line}<br><br>{pc_line}"
        f"</div>",
        unsafe_allow_html=True,
    )

st.markdown("")

# ─── 4 metric tiles ─────────────────────────────────────────────────────────

sellability = p.get("sellability_score") or 0.0
n_matches   = len(matches)
top_match   = float(matches["match_score"].max()) if n_matches else None
top_buyer_id = None
top_buyer_name = ""
if n_matches:
    top_row = matches.iloc[matches["match_score"].idxmax()]
    top_buyer_id   = int(top_row["buyer_club_id"]) if pd.notna(top_row["buyer_club_id"]) else None
    top_buyer_name = labels.club_display_name(top_buyer_id, top_row["buyer_club_name"])

owning_pressure   = p.get("total_pressure_score")
parent_pressure_link = f"/club_view?club_id={parent_club_id}" if parent_club_id is not None else ""

# All four tiles use the shared .rvc-tile HTML structure (same as Targets) so
# they line up regardless of whether a tile carries a subtitle line.
def _pv_tile(
    label: str, value: str, *,
    subline: str = "",
    title: str = "",
    href: str = "",
) -> str:
    sub_html = f'<div class="rvc-tile-sub">{subline}</div>' if subline else ""
    tile_html = (
        f'<div class="rvc-tile" title="{title}">'
        f'<div class="rvc-tile-label">{label}</div>'
        f'<div class="rvc-tile-value">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )
    if href:
        return (
            f'<a href="{ui.with_auth(href)}" target="_self" '
            f'style="text-decoration:none; color:inherit;">{tile_html}</a>'
        )
    return tile_html


_pv_tiles = [
    _pv_tile(
        "Sellability", f"{sellability:.1f}",
        title="0-100 score: quality × 50 + parent_club pressure × 0.5 (+ loan bonus). Higher = stronger sell signal.",
    ),
    _pv_tile(
        "Top match",
        f"{top_match:.1f}" if top_match else "—",
        subline=f"Best buyer: {top_buyer_name}" if top_buyer_name else "",
        title="Highest match_score across all buyer pairs.",
    ),
    _pv_tile(
        "Buyer matches", f"{n_matches}",
        title="Number of buyer clubs surviving all matcher filters for this player.",
    ),
    _pv_tile(
        "Owning-club pressure",
        f"{owning_pressure:.1f}" if pd.notna(owning_pressure) else "—",
        subline=f"{parent_club_display} →" if pd.notna(owning_pressure) and parent_pressure_link else "",
        title="Click to open owning-club Club View." if (pd.notna(owning_pressure) and parent_pressure_link)
              else "Parent club outside our 19-league coverage.",
        href=parent_pressure_link if pd.notna(owning_pressure) and parent_pressure_link else "",
    ),
]
st.markdown(f'<div class="rvc-tile-row">{"".join(_pv_tiles)}</div>', unsafe_allow_html=True)

# ─── ETV + Wage paired panels (immediately below the KPI tile row) ──────────
# Both panels are derived player-economics numbers, so they sit as a matched
# pair: ETV on the left, Wage on the right. If wage data is missing for this
# player, ETV expands to full width — cleaner than rendering a "—" placeholder.

def _fmt_money(x: float) -> str:
    if abs(x) >= 1_000_000: return f"€{x/1_000_000:.1f}m"
    if abs(x) >= 1_000:     return f"€{x/1_000:.0f}k"
    return f"€{int(x)}"


# Wage lookup — same logic as the old bottom-of-page panel. Profile row from
# player_universe doesn't carry wage; the matcher joined manual_wages.xlsx
# onto the matches table, so we pull from there.
player_wage = None
if n_matches:
    pws = matches["player_wage_pw_eur"].dropna().unique()
    if len(pws):
        player_wage = float(pws[0])

# Build ETV HTML (if TM value present)
_tm_val = p.get("current_tm_value_eur")
_etv_html = ""
if _tm_val and not pd.isna(_tm_val) and float(_tm_val) > 0:
    _tm_val_f = float(_tm_val)
    _mult = config.tm_to_fee_multiplier(_tm_val_f)
    _indicative_fee = _tm_val_f * _mult

    _tier_rows = ""
    for _ceil, _m in config.TM_TO_FEE_BANDS:
        if _ceil >= 10**11:
            label = "≥ €25m (established)"
        elif _ceil == 15_000_000:
            label = "< €15m (contract-leveraged prospects)"
        else:
            label = "€15m – €25m (mid-band)"
        active = " ← this player" if _mult == _m else ""
        _tier_rows += (
            f"<tr><td style='padding:2px 10px;'>{label}</td>"
            f"<td style='padding:2px 10px; font-weight:600;'>{_m:.1f}×{active}</td></tr>"
        )

    _etv_html = f"""
        <div style="background:#f5f3ff; border:1px solid #ddd6fe; border-radius:8px;
                    padding:14px 18px; min-height:110px; box-sizing:border-box;">
            <div style="font-size:0.72rem; font-weight:600; color:#6B7280;
                        text-transform:uppercase; letter-spacing:0.05em;">
                Estimated Transfer Value
            </div>
            <div style="font-size:1.875rem; font-weight:800; color:#111827;
                        line-height:1.15; margin-top:2px;">
                {_fmt_money(_indicative_fee)}
            </div>
            <div style="font-size:0.85rem; color:#374151; margin-top:4px;">
                TM {_fmt_money(_tm_val_f)} × <strong>{_mult:.1f}×</strong>
                <details style="display:inline-block; margin-left:8px;">
                    <summary style="display:inline; cursor:pointer; color:#1F3864;
                                    font-weight:600; font-size:0.8rem;
                                    list-style:none;">methodology ⓘ</summary>
                    <div style="margin-top:10px; padding:12px 14px; background:#ffffff;
                                border:1px solid #e5e7eb; border-radius:6px;
                                font-size:0.85rem; color:#374151; line-height:1.5;
                                max-width:560px;">
                        <p style="margin:0 0 8px 0;">
                            Transfermarkt market valuations systematically lag the
                            actual fees observed in transfers. We multiply TM value
                            by a <strong>tiered factor by TM band</strong> to produce
                            an indicative fee — the same number the matcher uses
                            internally for budget-fit scoring.
                        </p>
                        <table style="border-collapse:collapse; margin:8px 0; font-size:0.85rem;">
                            <thead>
                                <tr style="background:#f9fafb; color:#6B7280; text-align:left;">
                                    <th style="padding:4px 10px;">TM band</th>
                                    <th style="padding:4px 10px;">Multiplier</th>
                                </tr>
                            </thead>
                            <tbody>{_tier_rows}</tbody>
                        </table>
                    </div>
                </details>
            </div>
        </div>
    """

# Build Wage HTML (if wage present)
_wage_html = ""
if player_wage:
    _wage_html = f"""
        <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px;
                    padding:14px 18px; min-height:110px; box-sizing:border-box;">
            <div style="font-size:0.72rem; font-weight:600; color:#6B7280;
                        text-transform:uppercase; letter-spacing:0.05em;">
                Player wage / week
            </div>
            <div style="font-size:1.875rem; font-weight:800; color:#111827;
                        line-height:1.15; margin-top:2px;">
                €{player_wage:,.0f}
            </div>
        </div>
    """

# Render: pair if both present, otherwise ETV at full width.
if _etv_html and _wage_html:
    _ev_col, _wg_col = st.columns([1, 1])
    with _ev_col:
        st.markdown(_etv_html, unsafe_allow_html=True)
    with _wg_col:
        st.markdown(_wage_html, unsafe_allow_html=True)
elif _etv_html:
    st.markdown(_etv_html, unsafe_allow_html=True)
elif _wage_html:
    st.markdown(_wage_html, unsafe_allow_html=True)


st.markdown("")

# ─── Sellability breakdown ──────────────────────────────────────────────────

st.markdown("### Why this player is sellable")

# Auto-generated narrative
def _narrative() -> str:
    age = int(p["age"]) if pd.notna(p.get("age")) else None
    pos = p.get("sub_position") or "player"
    where = current_club_display
    parent = parent_club_display if (on_loan and parent_club_display != current_club_display) else None
    parts = []
    age_str = f"{age}-year-old " if age else ""
    parts.append(f"{age_str}{pos.lower()} at {where}" + (f" on loan from {parent}" if parent else ""))

    mins = p.get("minutes_share_pct")
    if pd.notna(mins) and float(mins) > 0:
        # Cap at 100 in the user-facing string — extra-time inflates the raw
        # share above 100 occasionally (denominator counts 90 min per game),
        # which reads as a data bug. Underlying value stays as-is.
        mins_display = min(float(mins), 100.0)
        parts.append(f"plays {mins_display:.0f}% of available minutes over the last 18 months")

    # Use the PARENT-club contract date for Bosman reasoning. For non-loaned
    # players this is identical to contract_end_date; for loaned players it's
    # populated from TM's "Contract there expires" field (see 12_patch_parent_contract.py).
    pce = p.get("parent_contract_end_date") or p.get("contract_end_date")
    if p.get("contract_leveraged") == 1 and pce:
        parts.append(f"parent-club contract ends {pce} — within Bosman range")

    drivers = []
    if p.get("public_must_sell_flag") == 1:
        drivers.append("public must-sell flag (FFP / parachute)")
    if p.get("manager_change_flag") == 1:
        drivers.append("recent manager change")
    if drivers:
        parts.append("owning club under " + " and ".join(drivers))

    sentence = ". ".join(s.strip().rstrip(",") for s in parts) + "."
    return sentence[0].upper() + sentence[1:]

st.markdown(f"_{_narrative()}_")
st.markdown("")

# 4 component cards (using columns)
def _flag_cell(flag_value: int | None) -> str:
    if flag_value == 1: return "✓"
    if flag_value == 0: return "✗"
    return "?"

def _fmt_eur(v):
    if v is None or pd.isna(v): return "—"
    v = float(v)
    if abs(v) >= 1_000_000: return f"€{v/1_000_000:.1f}m"
    if abs(v) >= 1_000:     return f"€{v/1_000:.0f}k"
    return f"€{int(v)}"

mv      = p.get("current_tm_value_eur")
last_fee = p.get("last_fee_paid_eur")
mins_pct = p.get("minutes_share_pct")
fp_raw   = p.get("finished_product")
cl_flag  = p.get("contract_leveraged")
rp_flag  = p.get("right_priced")
loan_end_date    = p.get("contract_end_date")             # for loaned players this is the LOAN end
parent_end_date  = p.get("parent_contract_end_date") or loan_end_date  # PARENT-club contract end
ce       = parent_end_date  # Contract Leverage card uses the parent date
yrs_left = None
if ce:
    try:
        yrs_left = round((datetime.fromisoformat(str(ce)).date() - snapshot).days / 365.25, 2)
    except (TypeError, ValueError):
        yrs_left = None

cc1, cc2, cc3 = st.columns(3)
with cc1:
    head = f"{_flag_cell(rp_flag)} Right Priced"
    body = (
        f"Market Value <strong>{_fmt_eur(mv)}</strong>, last fee paid <strong>{_fmt_eur(last_fee)}</strong>.<br>"
        + ("Current market value is at or above what the club paid — no balance-sheet drag."
           if rp_flag == 1
           else "Owning club paid above current market value — paper loss may inhibit sale.")
    )
    st.markdown(
        f"<div style='background:#f9fafb; border:1px solid #e5e7eb; border-radius:6px; padding:12px; height:140px;'>"
        f"<div style='font-weight:600; font-size:1rem;'>{head}</div>"
        f"<div style='font-size:0.85rem; color:#374151; margin-top:6px;'>{body}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

with cc2:
    head = f"{_flag_cell(fp_raw)} Established"
    if pd.notna(mins_pct) and float(mins_pct) > 0:
        _mins_capped = min(float(mins_pct), 100.0)
        body = (
            f"Played <strong>{_mins_capped:.0f}%</strong> of available minutes over the last 18 months. "
            + ("Confirmed first-team regular." if fp_raw == 1 else "Below the 50% threshold.")
        )
    else:
        body = "Minutes data unavailable (second-tier scrape or relaxed-minutes league)."
    st.markdown(
        f"<div style='background:#f9fafb; border:1px solid #e5e7eb; border-radius:6px; padding:12px; height:140px;'>"
        f"<div style='font-weight:600; font-size:1rem;'>{head}</div>"
        f"<div style='font-size:0.85rem; color:#374151; margin-top:6px;'>{body}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

with cc3:
    head = f"{_flag_cell(cl_flag)} Contract Leverage"
    if ce:
        yrs_str = f"<strong>{yrs_left:.1f} years</strong> remaining" if yrs_left is not None else "duration unknown"
        body = (
            f"Contract ends <strong>{ce}</strong>, {yrs_str}.<br>"
            + ("Within 2 years of expiry — Bosman freedom of movement window." if cl_flag == 1
               else "More than 2 years remaining — owning club retains negotiating leverage.")
        )
    else:
        body = "Contract date unavailable."
    st.markdown(
        f"<div style='background:#f9fafb; border:1px solid #e5e7eb; border-radius:6px; padding:12px; height:140px;'>"
        f"<div style='font-weight:600; font-size:1rem;'>{head}</div>"
        f"<div style='font-size:0.85rem; color:#374151; margin-top:6px;'>{body}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

# Owning-club pressure row spanning full width
if pd.notna(owning_pressure):
    cl_s = p.get("contract_leverage_score") or 0
    so_s = p.get("squad_oversupply_score") or 0
    ns_s = p.get("net_spend_score") or 0
    mc_f = p.get("manager_change_flag") == 1
    ps_f = p.get("public_must_sell_flag") == 1
    top_drivers = []
    if ps_f: top_drivers.append("public must-sell flag")
    if mc_f: top_drivers.append("manager change")
    comps = sorted([("contract leverage", cl_s), ("squad oversupply", so_s), ("net-spend", ns_s)],
                   key=lambda x: -x[1])
    if comps[0][1] > 0:
        top_drivers.append(f"{comps[0][0]} ({comps[0][1]:.0f}/100)")
    drivers_text = ", ".join(top_drivers) if top_drivers else "low structural pressure"

    st.markdown(
        f"<div style='background:#eff6ff; border:1px solid #bfdbfe; border-radius:6px; padding:12px; margin-top:8px;'>"
        f"<div style='font-weight:600;'>Owning club selling pressure: {owning_pressure:.0f} / 100</div>"
        f"<div style='font-size:0.9rem; color:#374151; margin-top:4px;'>"
        f"Top drivers: {drivers_text} · "
        f"<a href='{ui.with_auth(parent_pressure_link)}' target='_self'>open {parent_club_display} Club View →</a>"
        f"</div></div>",
        unsafe_allow_html=True,
    )

st.markdown("")

# ─── Loan Status panel (conditional) ────────────────────────────────────────

if on_loan and parent_club_display and parent_club_display != current_club_display:
    loan_bonus = (fp_raw == 1)
    st.markdown("### Loan status")
    cc_a = f'<a href="{ui.with_auth(cc_url)}" target="_self" style="color:#1F3864; font-weight:600;">{current_club_display}</a>'
    pc_a = f'<a href="{ui.with_auth(pc_url)}" target="_self" style="color:#1F3864; font-weight:600;">{parent_club_display}</a>'
    loan_end_str   = loan_end_date or "—"
    parent_end_str = parent_end_date or "—"
    st.markdown(
        f"<div style='background:#fff7ed; border:1px solid #fed7aa; border-radius:6px; padding:12px;'>"
        f"<strong>At:</strong> {cc_a} · {player_league} "
        f"<span style='color:#6b7280; font-size:0.85rem;'>(on loan until <strong>{loan_end_str}</strong>)</span><br>"
        f"<strong>Owned:</strong> {pc_a} · {parent_league} "
        f"<span style='color:#6b7280; font-size:0.85rem;'>(parent contract until <strong>{parent_end_str}</strong>)</span><br>"
        + (f"<strong>Bonus:</strong> +15 loan bonus active — player is an established starter at loan club<br>" if loan_bonus else "")
        + "<span style='color:#6b7280; font-size:0.85rem;'>"
          "Loan-and-showcase pattern — owning club using the spell to demonstrate value to potential buyers."
          "</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.markdown("")

# ─── All matches table ──────────────────────────────────────────────────────

st.markdown(f"### All matches ({n_matches})")

if n_matches == 0:
    st.info("No buyer matches survive the matcher filters for this player.")
else:
    # Seller context — rendered ONCE above the table because every row shares
    # the same seller (this player's parent club). Per-row rationale below
    # focuses on buyer + trade only.
    seller_line = ui.build_seller_context_line(p, p.get("total_pressure_score"))
    st.markdown(
        f"<div style='background:#f9fafb; border:1px solid #e5e7eb; border-radius:6px; "
        f"padding:10px 14px; margin-bottom:10px; font-size:0.9rem; color:#374151;'>"
        f"<strong style='color:#111827;'>Seller context:</strong> {seller_line}"
        f"</div>",
        unsafe_allow_html=True,
    )

    matches_df = matches.copy()
    # Derive display fields
    matches_df["buyer_display"] = matches_df.apply(
        lambda r: labels.club_display_name(int(r["buyer_club_id"]), r["buyer_club_name"]),
        axis=1,
    )
    matches_df["buyer_league_display"] = matches_df["buyer_league_id"].apply(labels.league_name)
    matches_df["demand_label"] = matches_df.apply(
        lambda r: ui.demand_intensity_label(r["request_source"], r["request_validated"]),
        axis=1,
    )
    matches_df["budget_fit_pct"] = (matches_df["budget_fit"] * 100).round(1)
    # Buyer-only rationale — seller text moved to the context line above.
    matches_df["rationale"] = matches_df.apply(ui.build_buyer_rationale, axis=1)

    def _money(x):
        if pd.isna(x): return "—"
        x = float(x)
        if abs(x) >= 1_000_000: return f"€{x/1_000_000:.1f}m"
        if abs(x) >= 1_000:     return f"€{x/1_000:.0f}k"
        return f"€{int(x)}"

    matches_df["budget_str"] = matches_df["max_transfer_fee_eur"].apply(_money)
    matches_df["wage_cap_str"] = matches_df["max_wage_pw_eur"].apply(_money)

    SHOW_LIMIT = 20
    show_all_key = f"show_all_matches_{pid}"
    show_all = st.session_state.get(show_all_key, False)
    table_df = matches_df if (show_all or n_matches <= SHOW_LIMIT) else matches_df.head(SHOW_LIMIT)
    table_df = table_df.reset_index(drop=True)

    # Pre-build clickable Buyer cells
    table_df["Buyer_html"] = table_df.apply(
        lambda r: ui.club_link(int(r["buyer_club_id"]), str(r["buyer_display"])),
        axis=1,
    )

    # Pre-build the Sci Sports columns. Level fit becomes a coloured pill;
    # threshold is shown alongside as context ("82 / threshold 79" reads as
    # "this player is 3 points above what this club needs").
    table_df["level_fit_pill"] = table_df["level_fit"].apply(ui.level_fit_pill)
    table_df["threshold_str"]  = table_df["club_threshold_for_request"].apply(
        lambda v: f"{float(v):.0f}" if pd.notna(v) else "—"
    )

    display = table_df.assign(rank=range(1, len(table_df) + 1))[[
        "rank", "Buyer_html", "buyer_league_display", "budget_str", "wage_cap_str",
        "threshold_str", "level_fit_pill",
        "match_score", "budget_fit_pct", "demand_label", "rationale",
    ]].rename(columns={
        "rank":                 "#",
        "Buyer_html":           "Buyer",
        "buyer_league_display": "League",
        "budget_str":           "Buyer Budget",
        "wage_cap_str":         "Wage Cap",
        "threshold_str":        "Buyer CA threshold",
        "level_fit_pill":       "Level fit",
        "match_score":          "Match Score",
        "budget_fit_pct":       "Headroom",
        "demand_label":         "Demand",
        "rationale":            "Rationale",
    })
    display["Match Score"] = display["Match Score"].apply(lambda v: f"{v:.1f}")
    display["Headroom"]    = display["Headroom"].apply(lambda v: f"{v:.1f}%")

    def _bg_match(v):
        try: return ui.green_gradient(float(v), 10, 100)
        except (TypeError, ValueError): return ""

    styled = display.style.map(_bg_match, subset=["Match Score"])
    ui.render_html_table(styled, max_height_px=520)

    if n_matches > SHOW_LIMIT and not show_all:
        if st.button(f"Show all {n_matches} matches", key=f"toggle_{pid}"):
            st.session_state[show_all_key] = True
            st.rerun()
    elif show_all and n_matches > SHOW_LIMIT:
        if st.button("Collapse to top 20", key=f"toggle_{pid}_collapse"):
            st.session_state[show_all_key] = False
            st.rerun()

# Wage panel moved to the top-right slot, paired with Estimated Transfer Value
# under the KPI tile row (see "ETV + Wage paired panels" block above).
