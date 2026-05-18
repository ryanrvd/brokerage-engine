"""Entry point — explicit st.navigation() shell.

Streamlit 1.36+ recommends building multipage apps via st.navigation() rather
than the legacy pages/ auto-discovery. Nav widgets are guaranteed regardless
of custom CSS or theme.

Nav is placed at the TOP of the page (horizontal tab bar) for maximum
visibility — see position="top" below.
"""

import hashlib

import streamlit as st


def _auth_hash() -> str:
    """Truncated SHA-256 of the shared password — the token we carry in URLs."""
    return hashlib.sha256(
        str(st.secrets.get("password", "")).encode("utf-8")
    ).hexdigest()[:32]


def _check_password() -> bool:
    """Gate the app behind a shared password, persisted via ?_a=<hash> URL token.

    Cookie-based persistence is structurally broken on iOS Safari (ITP treats
    Streamlit's component iframes as third-party). The auth token rides in
    the URL instead — every in-app link helper (player_link, club_link, etc.
    in components.py) re-appends it so subsequent full-page navigations
    carry it through. Tradeoff: token visible in URL bar — acceptable for
    a demo gate (one-way hash, no PII).
    """
    expected = _auth_hash()

    # Primary check: URL token from a previous auth
    token = st.query_params.get("_a")
    if token == expected:
        st.session_state["_password_correct"] = True
        return True

    # Within-session fallback (just-authed user pre-rerun, or token lost
    # via a navigation that didn't preserve it). Re-inject the token so
    # the URL bar stays consistent.
    if st.session_state.get("_password_correct"):
        st.query_params["_a"] = expected
        return True

    st.markdown("### Brokerage Engine")
    st.caption("Private preview · enter access password")
    pw = st.text_input("Password", type="password", key="_password_input")

    if pw:
        if pw == st.secrets.get("password"):
            st.session_state["_password_correct"] = True
            st.query_params["_a"] = expected
            st.rerun()
        else:
            st.error("Incorrect password")

    st.stop()


_check_password()


# Pages — each is a Streamlit script file relative to this entry script.
# Explicit url_path ensures stable routing so inline anchor hrefs always resolve.
market_overview = st.Page("pages/0_Market_Overview.py", title="Market Overview", icon="📊", default=True, url_path="market_overview")
home          = st.Page("home.py",                   title="Targets",            icon="🎯", url_path="targets")
all_matches   = st.Page("pages/1_All_Matches.py",    title="All Matches",        icon="📋", url_path="all_matches")
player_view   = st.Page("pages/2_Player_View.py",    title="Player View",        icon="👤", url_path="player_view")
club_view     = st.Page("pages/3_Club_View.py",      title="Club View",          icon="🏟️", url_path="club_view")
position_view = st.Page("pages/4_Position_View.py",  title="Position View",      icon="📍", url_path="position_view")
league_view   = st.Page("pages/5_League_View.py",    title="League View",        icon="🌍", url_path="league_view")
excluded      = st.Page("pages/6_Excluded_Players.py", title="Kill List", icon="🚫", url_path="kill_list")

pages = [market_overview, home, all_matches, player_view, club_view, position_view, league_view, excluded]

# Place the nav at the TOP of the page (horizontal tabs).
# expanded=True forces the nav to be fully visible rather than collapsed
# behind a popover button on smaller screens.
pg = st.navigation(pages, position="top", expanded=True)
pg.run()
