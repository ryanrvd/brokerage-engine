"""Mandate Territory — Relegated cohort sells, promoted cohort buys.

Two focused sections:

  1. Priority Sellers (top): curated top ~20 players whose parent club is
     `recently_relegated = 1`, ranked by `market_match_score`. Each player
     name links to Player View.

  2. Promoted-Buyer Panel (bottom): one card per recently-promoted club
     showing their stated buyer requests (from map_club_requests), inferred
     demand (from script 21), and top relegated→promoted pathway candidates
     (matches where parent_club is relegated AND buyer is this promoted club).

The full match list across the wider mandate-relevant cohort stays on
Market Opportunities. Mandate Territory is the focused entry point.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import db
import labels
import components as ui

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
import config  # noqa: F401


st.set_page_config(
    page_title="Market View · Mandate Territory",
    page_icon="🚨",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# Sidebar
ui.render_page_accent()
ui.render_sidebar_engine_header()
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())


con = db.get_connection()

# ─── Header ─────────────────────────────────────────────────────────────────
n_relegated = con.execute(
    "SELECT COUNT(*) FROM club_pressure WHERE recently_relegated = 1"
).fetchone()[0]
n_promoted = con.execute(
    "SELECT COUNT(*) FROM club_pressure WHERE recently_promoted = 1"
).fetchone()[0]

st.title("Mandate Territory")
st.markdown(
    f"<div style='color:#374151; font-size:0.95rem; margin:-6px 0 18px 0;'>"
    f"<b>Relegated cohort sells, promoted cohort buys.</b>  "
    f"{n_relegated} recently-relegated parent clubs carry a ×1.3 sell-side mandate. "
    f"{n_promoted} recently-promoted clubs are strategic buyers with elevated "
    f"top-flight spend.</div>",
    unsafe_allow_html=True,
)

# ─── Section 1: Priority Sellers (relegated cohort) ─────────────────────────
st.markdown("### 🔻 Priority Sellers — relegated parent clubs (×1.3)")

# Tag strip
rel_clubs = con.execute("""
    SELECT cp.club_id, cp.name, cp.league_id
    FROM club_pressure cp WHERE cp.recently_relegated = 1
    ORDER BY cp.total_pressure_score DESC
""").fetchall()
if rel_clubs:
    pills = "".join(
        f'<span style="background:#FEE2E2; color:#991B1B; padding:3px 9px; '
        f'border-radius:6px; font-size:0.78rem; font-weight:600; margin:0 4px 4px 0; display:inline-block;">'
        f'{labels.club_display_name(c["club_id"], c["name"])} · {labels.league_name(c["league_id"])}'
        f'</span>'
        for c in rel_clubs
    )
    st.markdown(f'<div style="margin: 4px 0 14px 0;">{pills}</div>', unsafe_allow_html=True)


# Curated top-20 priority sellers — best (player, buyer) per player
PRIORITY_LIMIT = 20
prio_df = pd.read_sql_query("""
    WITH ranked AS (
        SELECT m.player_id, m.player_name, m.position_bucket,
               m.market_match_score, m.match_score, m.player_ca,
               m.buyer_club_id, m.buyer_club_name, m.buyer_league_id,
               pu.age, pu.parent_club, pu.parent_club_id, pu.league_id AS parent_league,
               pu.sellability_score,
               ROW_NUMBER() OVER (PARTITION BY m.player_id
                                  ORDER BY m.market_match_score DESC) AS rn
        FROM matches m
        JOIN player_universe pu ON pu.player_id = m.player_id
        JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
        WHERE cp.recently_relegated = 1
          AND m.market_match_score IS NOT NULL
          AND m.market_match_score >= 25
    )
    SELECT * FROM ranked WHERE rn = 1
    ORDER BY market_match_score DESC
    LIMIT ?
""", con, params=(PRIORITY_LIMIT,))

if prio_df.empty:
    st.info("No matches surfaced from relegated clubs yet.")
else:
    rows = []
    for _, r in prio_df.iterrows():
        pid = int(r["player_id"])
        bid = int(r["buyer_club_id"]) if pd.notna(r["buyer_club_id"]) else None
        pcid = int(r["parent_club_id"]) if pd.notna(r["parent_club_id"]) else None
        p_disp = labels.player_display_name(pid, r["player_name"])
        parent_disp = labels.club_display_name(pcid, r["parent_club"])
        buyer_disp = labels.club_display_name(bid, r["buyer_club_name"]) if bid else "—"
        p_href = ui.with_auth(f"/player_view?player_id={pid}")
        b_href = ui.with_auth(f"/club_view?club_id={bid}") if bid else "#"
        pos_html = labels.display_bucket(r["position_bucket"])
        rows.append(
            f"<tr>"
            f"<td><a href='{p_href}' target='_self' style='color:#1F3864; font-weight:700; text-decoration:none;'>{p_disp}</a></td>"
            f"<td style='text-align:center;'>{int(r['age']) if pd.notna(r['age']) else '—'}</td>"
            f"<td style='text-align:center;'>{pos_html}</td>"
            f"<td>{parent_disp} <span style='color:#9CA3AF; font-size:0.78rem;'>{labels.league_name(r['parent_league'])}</span></td>"
            f"<td style='text-align:right;'>{ui.fmt_score_capped(r['sellability_score'])}</td>"
            f"<td><a href='{b_href}' target='_self' style='color:#1F3864; text-decoration:none;'>{buyer_disp}</a> "
            f"<span style='color:#9CA3AF; font-size:0.78rem;'>{labels.league_name(r['buyer_league_id'])}</span></td>"
            f"<td style='text-align:right;'>{ui.fmt_score_capped(r['player_ca'])}</td>"
            f"<td style='text-align:right; font-weight:700; color:#991B1B;'>{ui.fmt_score_capped(r['market_match_score'])}</td>"
            f"</tr>"
        )
    table_html = (
        "<table class='rvc-table' style='width:100%; border-collapse:collapse; font-size:0.86rem;'>"
        "<thead style='background:#F9FAFB; border-bottom:2px solid #E5E7EB;'>"
        "<tr>"
        "<th style='text-align:left; padding:8px;'>Player</th>"
        "<th>Age</th><th>Position</th>"
        "<th style='text-align:left;'>Parent Club</th>"
        "<th>Sellability</th>"
        "<th style='text-align:left;'>Top Buyer</th>"
        "<th>CA</th><th>Market Score</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)
    st.caption(
        f"Top {len(prio_df)} of {prio_df.shape[0]} relegated-cohort players "
        f"by `market_match_score`. Click any player to open their Player View."
    )


# ─── Section 2: Promoted-Buyer Panel ────────────────────────────────────────
st.markdown("---")
st.markdown("### 🔺 Promoted-Buyer Panel — strategic buyers entering top-flight budgets")
st.caption(
    "Promoted clubs are *strengthening*, not selling. They appear here on the "
    "buyer side: stated requests, inferred squad gaps, and the highest-scoring "
    "candidates whose parent club is recently-relegated (the Burnley → Coventry "
    "style pathway)."
)

# Per-promoted-club aggregates
prom_clubs = con.execute("""
    SELECT cp.club_id, cp.name, cp.league_id
    FROM club_pressure cp
    WHERE cp.recently_promoted = 1
    ORDER BY cp.league_id, cp.name
""").fetchall()

if not prom_clubs:
    st.info("No clubs flagged recently_promoted.")
else:
    # Pull stated requests, inferred demand, and pathway candidates per club
    for c in prom_clubs:
        cid = c["club_id"]
        c_display = labels.club_display_name(cid, c["name"])
        c_href = ui.with_auth(f"/club_view?club_id={cid}")

        # Stated requests
        reqs = pd.read_sql_query("""
            SELECT position_bucket, preferred_side,
                   max_transfer_fee_eur, source, validated
            FROM map_club_requests
            WHERE cast(club_id AS TEXT) = cast(? AS TEXT)
            ORDER BY max_transfer_fee_eur DESC NULLS LAST
        """, con, params=(cid,))

        # Inferred demand
        inferred = pd.read_sql_query("""
            SELECT position_bucket, max_transfer_fee_eur
            FROM inferred_club_requests
            WHERE club_id = ?
        """, con, params=(cid,))

        # Relegated → this promoted club pathway candidates
        pathway = pd.read_sql_query("""
            SELECT m.player_id, m.player_name, m.position_bucket, m.market_match_score,
                   m.match_score, m.player_ca, pu.age, pu.parent_club, pu.parent_club_id,
                   pu.league_id AS parent_league
            FROM matches m
            JOIN player_universe pu ON pu.player_id = m.player_id
            JOIN club_pressure pcp ON pcp.club_id = pu.parent_club_id
            WHERE pcp.recently_relegated = 1
              AND m.buyer_club_id = ?
              AND m.market_match_score IS NOT NULL
              AND m.market_match_score >= 25
            ORDER BY m.market_match_score DESC LIMIT 5
        """, con, params=(cid,))

        n_req = len(reqs)
        n_inf = len(inferred)
        top_budget = float(reqs["max_transfer_fee_eur"].max()) if n_req and reqs["max_transfer_fee_eur"].notna().any() else None
        budget_str = ui.fmt_money(top_budget) if top_budget is not None else "—"
        positions_needed = sorted(set(reqs["position_bucket"].dropna().tolist()
                                       + inferred["position_bucket"].dropna().tolist()))
        pos_pills = " ".join(
            f'<span style="background:#E0E7FF; color:#1E3A8A; padding:2px 8px; '
            f'border-radius:5px; font-size:0.78rem; margin-right:4px;">{labels.display_bucket(p)}</span>'
            for p in positions_needed
        ) or "<span style='color:#9CA3AF; font-size:0.85rem;'>—</span>"

        # Card header
        st.markdown(
            f"<div style='border:1px solid #E5E7EB; border-radius:10px; padding:14px 16px; "
            f"margin-bottom:14px; background:#FFFFFF;'>"
            f"  <div style='display:flex; justify-content:space-between; align-items:baseline;'>"
            f"    <div>"
            f"      <a href='{c_href}' target='_self' style='color:#1d4ed8; font-weight:700; "
            f"text-decoration:none; font-size:1.05rem;'>{c_display}</a>  "
            f"      <span style='color:#6B7280; font-size:0.82rem;'>{labels.league_name(c['league_id'])}</span>"
            f"    </div>"
            f"    <div style='font-size:0.82rem; color:#374151;'>"
            f"      <b>{n_req}</b> stated · <b>{n_inf}</b> inferred · top budget <b>{budget_str}</b>"
            f"    </div>"
            f"  </div>"
            f"  <div style='margin-top:8px; font-size:0.86rem; color:#374151;'>"
            f"    <b>Positions:</b> {pos_pills}"
            f"  </div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Pathway candidates table (if any)
        if not pathway.empty:
            pw_rows = []
            for _, r in pathway.iterrows():
                pid = int(r["player_id"])
                pcid = int(r["parent_club_id"]) if pd.notna(r["parent_club_id"]) else None
                p_disp = labels.player_display_name(pid, r["player_name"])
                parent_disp = labels.club_display_name(pcid, r["parent_club"])
                p_href = ui.with_auth(f"/player_view?player_id={pid}")
                pw_rows.append(
                    f"<tr>"
                    f"<td><a href='{p_href}' target='_self' style='color:#1F3864; font-weight:600; text-decoration:none;'>{p_disp}</a></td>"
                    f"<td style='text-align:center;'>{int(r['age']) if pd.notna(r['age']) else '—'}</td>"
                    f"<td style='text-align:center;'>{labels.display_bucket(r['position_bucket'])}</td>"
                    f"<td>{parent_disp} <span style='color:#9CA3AF; font-size:0.76rem;'>{labels.league_name(r['parent_league'])}</span></td>"
                    f"<td style='text-align:right;'>{ui.fmt_score_capped(r['player_ca'])}</td>"
                    f"<td style='text-align:right; color:#991B1B; font-weight:700;'>{ui.fmt_score_capped(r['market_match_score'])}</td>"
                    f"</tr>"
                )
            st.markdown(
                "<div style='margin: -8px 0 18px 12px; padding-left:8px; border-left:3px solid #E5E7EB;'>"
                "<div style='font-size:0.82rem; color:#6B7280; margin:8px 0 4px 0;'>"
                "Top relegated → promoted pathway candidates"
                "</div>"
                "<table class='rvc-table' style='width:100%; border-collapse:collapse; font-size:0.82rem;'>"
                "<thead style='background:#F9FAFB;'>"
                "<tr>"
                "<th style='text-align:left; padding:4px 8px;'>Player</th>"
                "<th>Age</th><th>Pos</th>"
                "<th style='text-align:left;'>From (relegated)</th>"
                "<th>CA</th><th>Market</th>"
                "</tr></thead>"
                f"<tbody>{''.join(pw_rows)}</tbody></table>"
                "</div>",
                unsafe_allow_html=True,
            )
