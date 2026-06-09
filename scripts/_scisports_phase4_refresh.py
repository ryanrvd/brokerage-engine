"""Phase 4 — SciSports CA/PA refresh across the expanded universe.

Per-league pipeline (called once per league):
  1. Resolve SciSports league_id via /Leagues lookup (Name + nation.alpha3Code)
  2. Paginate /metrics/players/sciskill?LeagueIds={lid}&Limit=50 — every player
     in that SciSports league lands in one paginated sweep (~11 calls per ~500
     players). Far cheaper than per-player lookups.
  3. For each sciskill item, link to a tm_player_universe row via name + DOB
     (strict; no name-only fallback per Phase 3 audit). Update player_universe.
     scisports_player_id.
  4. UPSERT player_ratings(tm_player_id, current_ability=sciskill, …, source='scisports_api')

Cache:
  /Leagues   → 30 days (TTL_LEAGUES_TEAMS_META)
  /sciskill  → 7 days  (TTL_SCISKILL)

Pacing + rate limits enforced inside _scisports_client.py. This driver just
calls .get(...) and respects the EMERGENCY HALT exception.

Per Phase 4 directive: NO xlsx seeding. The xlsx is a downstream snapshot
written from player_ratings after the API refresh completes (handled in a
separate step, not here).
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from _scisports_cache import (
    TTL_LEAGUES_TEAMS_META,
    TTL_SCISKILL,
    get_or_fetch,
)
from _scisports_client import (
    ScisportsClient,
    ScisportsRateLimitedError,
    ScisportsRateLimitEmergency,
)

# (matcher league code) → (SciSports league name, ISO alpha3)
# Names verified empirically against /Leagues (some differ from intuitive forms):
#   ES1 = "LaLiga" (one word)
#   PO1 = "Liga Portugal" (not "Primeira")
#   BE1 = "Jupiler Pro League" (sponsor name)
LEAGUE_LOOKUP = {
    "GB1": ("Premier League",      "ENG"),
    "GB2": ("Championship",        "ENG"),
    "ES1": ("LaLiga",              "ESP"),
    "ES2": ("LaLiga 2",            "ESP"),
    "IT1": ("Serie A",             "ITA"),
    "IT2": ("Serie B",             "ITA"),
    "L1":  ("Bundesliga",          "DEU"),
    "L2":  ("2. Bundesliga",       "DEU"),
    "FR1": ("Ligue 1",             "FRA"),
    "FR2": ("Ligue 2",             "FRA"),
    "PO1": ("Liga Portugal",       "PRT"),
    "NL1": ("Eredivisie",          "NLD"),
    "BE1": ("Jupiler Pro League",  "BEL"),
}

PAGE = 50  # sciskill confirmed accepts up to 50 per page


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    cleaned = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_dob(v) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return None


def find_league(client: ScisportsClient, target_name: str, alpha3: str) -> dict | None:
    """Paginate /Leagues until Name + nation.alpha3Code match."""
    offset = 0
    while True:
        params = {"Offset": offset, "Limit": PAGE}
        data, _ = get_or_fetch(client, "/api/v2/Leagues", params, TTL_LEAGUES_TEAMS_META)
        items = data.get("items") or data.get("data") or []
        for L in items:
            name = (L.get("name") or "").strip()
            nation = L.get("nation") or {}
            if name == target_name and (nation.get("alpha3Code") or "").upper() == alpha3.upper():
                return L
        total = data.get("total") or 0
        if not items or offset + PAGE >= total:
            break
        offset += PAGE
    return None


def fetch_sciskill_for_league(client: ScisportsClient, sci_league_id: int) -> list[dict]:
    """Paginate /sciskill?LeagueIds={lid}&Limit=50 until exhausted."""
    items: list[dict] = []
    offset = 0
    while True:
        params = {"Offset": offset, "Limit": PAGE, "LeagueIds": sci_league_id}
        data, _ = get_or_fetch(
            client, "/api/v2/metrics/players/sciskill", params, TTL_SCISKILL,
        )
        page_items = data.get("items") or []
        items.extend(page_items)
        total = data.get("total") or 0
        if not page_items or offset + PAGE >= total:
            break
        offset += PAGE
    return items


def _build_tm_index(con: sqlite3.Connection, league_code: str | None = None) -> dict[str, list[dict]]:
    """tm players indexed by normalized name.

    If league_code is None, returns a GLOBAL index over all of player_universe
    — important because TM's post-override league assignment can diverge from
    SciSports' 25/26 view (e.g. Deportivo La Coruña was in ES2 in SciSports
    but is now flagged ES1 via the promotion override). Restricting the
    candidate pool to a single league causes those edge cases to go unmatched.

    DOB tie-breaking inside link_and_write distinguishes same-name collisions.

    Each indexed entry carries `position_bucket` so link_and_write can refuse
    Pass 2 token-subset candidates whose SciSports position1 disagrees with
    the TM bucket (e.g. TM ST_CF being matched to a SciSports DM was the
    Kalajdzic→Lukić bug class).
    """
    if league_code is None:
        rows = con.execute("""
            SELECT pu.player_id, pu.name, pu.date_of_birth, pu.parent_club,
                   pu.parent_club_id, pu.league_id, pu.position_bucket
            FROM player_universe pu
        """).fetchall()
    else:
        rows = con.execute("""
            SELECT pu.player_id, pu.name, pu.date_of_birth, pu.parent_club,
                   pu.parent_club_id, pu.league_id, pu.position_bucket
            FROM player_universe pu
            WHERE pu.league_id = ?
               OR pu.parent_club_id IN (
                   SELECT club_id FROM club_pressure WHERE league_id = ?
               )
        """, (league_code, league_code)).fetchall()

    index: dict[str, list[dict]] = {}
    for pid, name, dob, parent, pcid, lg, bucket in rows:
        norm = _normalize(name)
        if not norm:
            continue
        index.setdefault(norm, []).append({
            "player_id": int(pid),
            "name": name,
            "dob": dob,
            "parent_club": parent,
            "league_id": lg,
            "position_bucket": bucket,
        })
    return index


def link_and_write(
    con: sqlite3.Connection,
    sci_items: list[dict],
    tm_index: dict[str, list[dict]],
    today_iso: str,
) -> dict:
    """For each sciskill item, find a matching tm player and upsert ratings."""
    matched = 0
    ambiguous = 0
    unmatched = 0
    no_dob = 0

    sci_id_updates: list[tuple[int, int]] = []     # (sci_id, tm_pid)
    rating_upserts: list[tuple[int, float | None, float | None, str]] = []

    # SciSports position1 → project 10-bucket family. Used for the Pass 2
    # bucket-mismatch guard added 2026-06-04 after the Kalajdzic→Lukić bug.
    SCI_POS_TO_BUCKET = {
        "Goalkeeper": "GK", "CentreBack": "CB", "LeftBack": "LB", "RightBack": "RB",
        "DefensiveMidfield": "DM", "CentreMidfield": "CM", "AttackingMidfield": "AM",
        "LeftWing": "LW", "RightWing": "RW", "CentreForward": "ST_CF",
    }

    def sci_bucket_family(p) -> set[str]:
        positions = []
        for k in ("position1", "position2", "position3"):
            v = p.get(k)
            if v:
                positions.append(v)
        return {SCI_POS_TO_BUCKET[x] for x in positions if x in SCI_POS_TO_BUCKET}

    for item in sci_items:
        player = item.get("player") or {}
        sci_pid = player.get("id")
        sci_name = player.get("name") or ""
        sci_dob_raw = player.get("birthDate")
        sci_dob = _parse_dob(sci_dob_raw)
        sci_buckets = sci_bucket_family(player)
        ca = item.get("sciskill")
        pa = item.get("potential")

        if sci_pid is None:
            continue
        nm = _normalize(sci_name)
        candidates = tm_index.get(nm, [])

        # Pass 1: exact normalised-name match.
        # Hardening (2026-06-04): when both sides have DOB, require exact match;
        # one mismatch refuses the link rather than silently accepting.
        chosen = None
        if len(candidates) == 1:
            c = candidates[0]
            if sci_dob and c["dob"] and sci_dob != c["dob"]:
                # Same exact name but different DOB → different player; refuse.
                pass
            else:
                chosen = c
        elif len(candidates) > 1 and sci_dob:
            year = sci_dob[:4]
            year_hits = [c for c in candidates if c["dob"] and c["dob"].startswith(year)]
            if len(year_hits) == 1:
                chosen = year_hits[0]
            elif len(year_hits) > 1:
                # Try exact DOB match
                exact = [c for c in year_hits if c["dob"] == sci_dob]
                if len(exact) == 1:
                    chosen = exact[0]

        if not chosen:
            # Pass 2 (loose token-subset). Hardening 2026-06-04:
            #   • require ≥ 2 overlapping tokens (kills firstName-only collisions)
            #   • require DOB-year match if SciSports has DOB
            #   • require position-bucket family agreement when both sides have
            #     known positions; bucket-disagreement refuses the link.
            if nm:
                tokens = set(nm.split())
                subset_hits = []
                for tname, cands in tm_index.items():
                    ttok = set(tname.split())
                    if tokens.issubset(ttok) or ttok.issubset(tokens):
                        overlap = len(tokens & ttok)
                        if overlap >= 2:                         # guard 1
                            for c in cands:
                                # guard 2: DOB-year match if available
                                if sci_dob and c.get("dob"):
                                    if not c["dob"].startswith(sci_dob[:4]):
                                        continue
                                # guard 3: bucket compatibility
                                tm_b = c.get("position_bucket")
                                if tm_b and sci_buckets and tm_b not in sci_buckets:
                                    continue
                                subset_hits.append(c)
                if sci_dob:
                    year = sci_dob[:4]
                    year_subset = [c for c in subset_hits if c.get("dob") and c["dob"].startswith(year)]
                    if len(year_subset) == 1:
                        chosen = year_subset[0]
                elif len(subset_hits) == 1:
                    chosen = subset_hits[0]

        if chosen:
            matched += 1
            sci_id_updates.append((int(sci_pid), chosen["player_id"]))
            rating_upserts.append((
                chosen["player_id"],
                float(ca) if ca is not None else None,
                float(pa) if pa is not None else None,
                today_iso,
            ))
        elif len(candidates) > 1:
            ambiguous += 1
        else:
            unmatched += 1
            if sci_dob is None:
                no_dob += 1

    # Apply
    con.executemany(
        "UPDATE player_universe SET scisports_player_id = ? WHERE player_id = ?",
        sci_id_updates,
    )
    con.executemany(
        """INSERT INTO player_ratings (tm_player_id, current_ability, potential_ability,
                                       status, last_updated, source)
           VALUES (?, ?, ?, 'active', ?, 'scisports_api')
           ON CONFLICT(tm_player_id) DO UPDATE SET
             current_ability   = excluded.current_ability,
             potential_ability = excluded.potential_ability,
             status            = excluded.status,
             last_updated      = excluded.last_updated,
             source            = excluded.source
        """,
        [(p, ca, pa, ts) for (p, ca, pa, ts) in rating_upserts],
    )
    return {
        "matched": matched,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "no_dob": no_dob,
    }


def process_league(client: ScisportsClient, con: sqlite3.Connection,
                   league_code: str, today_iso: str) -> dict:
    """Full per-league flow. Returns a summary dict."""
    if league_code not in LEAGUE_LOOKUP:
        return {"league": league_code, "error": "no LEAGUE_LOOKUP entry"}
    sci_name, alpha3 = LEAGUE_LOOKUP[league_code]
    print(f"  → resolving league_id ({sci_name}, {alpha3})…")
    league = find_league(client, sci_name, alpha3)
    if league is None:
        return {"league": league_code, "error": "league not found in SciSports"}
    sci_lid = league.get("id")
    print(f"    sci_league_id={sci_lid}  ({league.get('name')})  "
          f"remaining={client.last_remaining}")

    print(f"  → paginating sciskill for league {sci_lid}…")
    sci_items = fetch_sciskill_for_league(client, sci_lid)
    print(f"    {len(sci_items)} sciskill records  remaining={client.last_remaining}")

    print(f"  → linking to player_universe (global tm_index)…")
    tm_index = _build_tm_index(con, None)
    n_tm_players = sum(len(v) for v in tm_index.values())
    print(f"    tm-candidates global: {n_tm_players}")

    result = link_and_write(con, sci_items, tm_index, today_iso)
    con.commit()

    print(f"    matched={result['matched']}  ambiguous={result['ambiguous']}  "
          f"unmatched={result['unmatched']}")
    return {
        "league": league_code,
        "sci_lid": sci_lid,
        "sci_items": len(sci_items),
        "tm_candidates": n_tm_players,
        **result,
    }


def run_phase(leagues: list[str], phase_label: str) -> dict:
    print("=" * 72)
    print(f"Phase {phase_label} — {len(leagues)} league(s): {', '.join(leagues)}")
    print("=" * 72)

    client = ScisportsClient()
    pf = client.preflight_baseline(halt_threshold=800)
    print(f"  Pre-flight remaining: {pf['remaining']}  fresh={pf['looks_fresh']}")
    if not pf["looks_fresh"]:
        print("  ⚠️ Halt — confirm no concurrent API consumer and re-run.")
        sys.exit(2)
    print()

    today_iso = dt.date.today().isoformat()
    summaries: list[dict] = []

    with sqlite3.connect(config.SQLITE_FILE) as con:
        # Ensure source column exists
        cols = [r[1] for r in con.execute("PRAGMA table_info(player_ratings)").fetchall()]
        if "source" not in cols:
            con.execute("ALTER TABLE player_ratings ADD COLUMN source TEXT")
            con.commit()

        for lc in leagues:
            print(f"--- {lc} ({LEAGUE_LOOKUP.get(lc, (lc,))[0]}) ---")
            try:
                s = process_league(client, con, lc, today_iso)
            except ScisportsRateLimitEmergency as e:
                print(f"\nEMERGENCY HALT: {e}")
                sys.exit(2)
            except ScisportsRateLimitedError as e:
                print(f"\n429 HALT: {e}")
                sys.exit(2)
            summaries.append(s)
            print()

    print("=" * 72)
    print(f"Phase {phase_label} summary")
    print("=" * 72)
    print(f"  {'lg':5s} {'sci_lid':>7s} {'sci_items':>9s} {'tm_cands':>8s} "
          f"{'matched':>7s} {'ambig':>6s} {'unmatched':>9s}")
    for s in summaries:
        if "error" in s:
            print(f"  {s['league']:5s}  ERROR: {s['error']}")
            continue
        print(f"  {s['league']:5s} {s['sci_lid']:>7d} {s['sci_items']:>9d} "
              f"{s['tm_candidates']:>8d} {s['matched']:>7d} {s['ambiguous']:>6d} "
              f"{s['unmatched']:>9d}")
    print()
    print(f"  Total API calls in this run:    {client.requests_made}")
    print(f"  Min X-RateLimit-Remaining seen: {client.min_remaining_seen}")
    print(f"  Final X-RateLimit-Remaining:    {client.last_remaining}")
    return {"summaries": summaries, "calls": client.requests_made,
            "min_remaining": client.min_remaining_seen}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--phase", choices=("A", "B"), required=True)
    args = p.parse_args()
    if args.phase == "A":
        leagues = ["ES1", "IT1", "L1", "FR1", "PO1", "NL1", "BE1"]
    else:
        leagues = ["ES2", "IT2", "L2", "FR2", "GB2"]
    run_phase(leagues, args.phase)
