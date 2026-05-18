"""Entry point — explicit st.navigation() shell.

Streamlit 1.36+ recommends building multipage apps via st.navigation() rather
than the legacy pages/ auto-discovery. Nav widgets are guaranteed regardless
of custom CSS or theme.

Nav is placed at the TOP of the page (horizontal tab bar) for maximum
visibility — see position="top" below.
"""

import hashlib

import streamlit as st


def _check_password() -> bool:
    """Gate the app behind a shared password.

    Mobile Safari (and other mobile browsers) close + recreate the WebSocket
    on every in-app navigation, so a session_state-only gate re-prompts on
    every hyperlink click. We persist a SHA-256 hash of the password in a
    cookie that survives navigation. Session_state is kept as a within-session
    fallback while the cookie write round-trips through the browser.
    """
    from streamlit_cookies_controller import CookieController

    cookie_name = "brokerage_engine_auth"
    expected_hash = hashlib.sha256(
        str(st.secrets.get("password", "")).encode("utf-8")
    ).hexdigest()

    # Belt: cookie survives full-page navigation between Streamlit pages.
    cookies = CookieController(key="auth_cookies")
    try:
        auth_cookie = cookies.get(cookie_name)
    except TypeError:
        # CookieController's internal store is briefly None before the
        # component mounts; treat as "no cookie yet" and fall through.
        auth_cookie = None
    if auth_cookie == expected_hash:
        return True

    # Braces: in-session memory while the cookie write round-trips client-side
    # (CookieController's getAll is async on cold sessions and returns {} on
    # the first script run).
    if st.session_state.get("_password_correct"):
        return True

    st.markdown("### Brokerage Engine")
    st.caption("Private preview · enter access password")
    pw = st.text_input("Password", type="password", key="_password_input")

    if pw:
        if pw == st.secrets.get("password"):
            st.session_state["_password_correct"] = True
            # Cookie write is queued for the response flush; the
            # subsequent st.rerun() lets the new render pick up the
            # session_state flag immediately.
            cookies.set(
                cookie_name,
                expected_hash,
                max_age=86400,        # 24 hours
                same_site="lax",       # required for in-app link clicks
            )
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
