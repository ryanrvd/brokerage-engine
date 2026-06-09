"""Engine selection — the post-auth fork.

Shown after password gates clear and before any engine has been chosen.
Setting st.session_state["engine_selected"] to "brokerage" or "market_view"
causes streamlit_app.py to load the corresponding navigation on rerun.

Visual: two side-by-side click cards (Brokerage Engine in brick-red,
Market View in brand-blue) on desktop, stacked on narrow viewports.
Cohort counts are pulled live from SQLite — no hardcoded numbers, so the
weekly pipeline run propagates automatically.
"""

from __future__ import annotations

import streamlit as st

import db
import labels


BROKERAGE_RED = "#b91c1c"
MARKET_BLUE   = "#1d4ed8"


def _leagues_block_html(label_colour: str) -> str:
    """Render the 'Leagues covered (19)' block as monospace-style HTML."""
    rows: list[str] = []
    for country, leagues in labels.LEAGUE_DISPLAY_BY_COUNTRY:
        names = " · ".join(name for _code, name in leagues)
        rows.append(
            f'<div class="rv-fork-league-row">'
            f'  <span class="rv-fork-league-country">{country}</span>'
            f'  <span class="rv-fork-league-names">{names}</span>'
            f'</div>'
        )
    return (
        f'<div class="rv-fork-leagues" style="border-left:3px solid {label_colour};">'
        + "".join(rows)
        + '</div>'
    )


_FORK_CSS = """
<style>
.rv-fork-page { max-width: 1280px; margin: 0 auto; padding: 8px 4px 28px 4px; }
.rv-fork-master {
    text-align:center; margin-bottom: 8px;
    font-size: 1.25rem; font-weight: 800; letter-spacing: 0.12em;
    color: #111827;
}
.rv-fork-subline {
    text-align:center; color:#6B7280;
    font-size: 0.95rem; margin-bottom: 28px;
}
.rv-fork-card {
    border: 2px solid; border-radius: 14px;
    padding: 22px 24px 24px 24px;
    background: #FFFFFF;
    transition: box-shadow 0.18s ease, transform 0.18s ease, border-color 0.18s ease;
    display:flex; flex-direction:column; height: 100%;
}
.rv-fork-card:hover {
    box-shadow: 0 14px 38px rgba(0,0,0,0.08);
    transform: translateY(-2px);
}
.rv-fork-card-title {
    font-size: 1.55rem; font-weight: 800; margin: 0 0 4px 0;
    display:flex; align-items:center; gap:10px;
}
.rv-fork-card-body {
    color:#374151; font-size: 0.92rem; line-height: 1.5; margin: 6px 0 14px 0;
}
.rv-fork-criteria {
    background: #F9FAFB; border-radius: 8px; padding: 10px 12px;
    font-size: 0.82rem; color:#374151; margin: 6px 0 14px 0;
    line-height: 1.55;
}
.rv-fork-criteria b { color:#111827; }
.rv-fork-coverage {
    font-size: 0.92rem; font-weight: 600; margin: 4px 0 10px 0;
    display:flex; align-items:baseline; gap:8px;
}
.rv-fork-coverage-value { font-size: 1.35rem; font-weight: 800; }
.rv-fork-leagues-label {
    font-size: 0.78rem; color:#6B7280; text-transform: uppercase;
    letter-spacing: 0.08em; margin: 12px 0 6px 0;
}
.rv-fork-leagues {
    background: #F9FAFB; border-radius: 8px; padding: 10px 14px 6px 14px;
    margin: 0 0 16px 0; font-size: 0.81rem; line-height: 1.55;
}
.rv-fork-league-row { display:flex; gap: 14px; margin-bottom: 3px; }
.rv-fork-league-country {
    flex: 0 0 110px; font-weight: 700; color:#374151; font-variant: tabular-nums;
}
.rv-fork-league-names { color:#374151; }
</style>
"""


def render() -> None:
    """Render the fork page. Returns nothing; sets state on button click."""
    st.markdown(_FORK_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="rv-fork-page">'
        '<div class="rv-fork-master">RV CORP</div>'
        '<div class="rv-fork-subline">Choose your view</div>',
        unsafe_allow_html=True,
    )

    cohort_brokerage = db.cohort_size_brokerage()
    cohort_market    = db.cohort_size_market_view()

    col_left, col_right = st.columns(2, gap="large")

    # ─── LEFT: Brokerage Engine ──────────────────────────────────────────────
    with col_left:
        st.markdown(
            f'<div class="rv-fork-card" style="border-color:{BROKERAGE_RED};">'
            f'  <div class="rv-fork-card-title" style="color:{BROKERAGE_RED};">'
            f'    🎯 Brokerage Engine'
            f'  </div>'
            f'  <div class="rv-fork-card-body">'
            f'    Targeted brokerage matches across the strict matcher cohort. '
            f'    Conservative scoring, action-ready picks.'
            f'  </div>'
            f'  <div class="rv-fork-criteria">'
            f'    <b>Preselected criteria:</b><br/>'
            f'    • Age 17–24<br/>'
            f'    • TM value €8m–€45m<br/>'
            f'    • Finished product (50%+ first-team minutes in last 18 months)<br/>'
            f'    • Contract leverage (within 3 years of expiry)<br/>'
            f'    • Parent club under selling pressure'
            f'  </div>'
            f'  <div class="rv-fork-coverage">Player Coverage:'
            f'    <span class="rv-fork-coverage-value" style="color:{BROKERAGE_RED};">'
            f'      {cohort_brokerage:,}'
            f'    </span>'
            f'    <span style="color:#6B7280; font-weight:500;">players</span>'
            f'  </div>'
            f'  <div class="rv-fork-leagues-label">Leagues covered (19)</div>'
            f'  {_leagues_block_html(BROKERAGE_RED)}'
            f'</div>',
            unsafe_allow_html=True,
        )
        if st.button("Enter Brokerage Engine →", key="enter_brokerage",
                     use_container_width=True, type="primary"):
            st.session_state["engine_selected"] = db.ENGINE_BROKERAGE
            st.rerun()

    # ─── RIGHT: Market View ──────────────────────────────────────────────────
    with col_right:
        st.markdown(
            f'<div class="rv-fork-card" style="border-color:{MARKET_BLUE};">'
            f'  <div class="rv-fork-card-title" style="color:{MARKET_BLUE};">'
            f'    🌐 Market View'
            f'  </div>'
            f'  <div class="rv-fork-card-body">'
            f'    Macro market intelligence across every squad player at every '
            f'    covered club. Track market movements, identify mandate '
            f'    opportunities, see predicted destinations and arbitrage signals.'
            f'  </div>'
            f'  <div class="rv-fork-coverage">Player Coverage:'
            f'    <span class="rv-fork-coverage-value" style="color:{MARKET_BLUE};">'
            f'      {cohort_market:,}'
            f'    </span>'
            f'    <span style="color:#6B7280; font-weight:500;">players</span>'
            f'  </div>'
            f'  <div class="rv-fork-leagues-label">Leagues covered (19)</div>'
            f'  {_leagues_block_html(MARKET_BLUE)}'
            f'</div>',
            unsafe_allow_html=True,
        )
        if st.button("Enter Market View →", key="enter_market",
                     use_container_width=True, type="primary"):
            st.session_state["engine_selected"] = db.ENGINE_MARKET
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)
