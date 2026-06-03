"""Phase 2 — Resolve PL teams + capture rosters via SciSports API.

Three sub-steps, all paced through scripts/_scisports_client.py and cached
via scripts/_scisports_cache.py:

  1) Find league id where Name='Premier League' AND nation.alpha3Code='ENG'.
     Maps repo hint: id=50. Verify, don't assume.
  2) List the 20 teams where currentLeague.id == league_id.
     Save to data/scisports_team_ids.json.
  3) For each team, paginate /Players?CurrentTeamIds=[team_id] capturing every
     player and full identifying fields (name, dob, foot, positions, contract,
     loan, market value, scisports_player_id).
     Save to data/scisports_pl_player_roster.json — the bridge file for
     Phase 3's TM-to-SciSports matching.

The pre-flight check runs first; if X-RateLimit-Remaining < 800 the script
halts and asks the user to confirm no concurrent operation in the maps repo.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scisports_cache import (
    TTL_LEAGUES_TEAMS_META,
    TTL_SQUAD_ROSTERS,
    get_or_fetch,
    read_cached,
    stats as cache_stats,
)
from _scisports_client import (
    ScisportsClient,
    ScisportsRateLimitedError,
    ScisportsRateLimitEmergency,
)

PAGE_SIZE = 10  # API silently returns empty when limit > 10
OUT_TEAMS = PROJECT_ROOT / "data" / "scisports_team_ids.json"
OUT_ROSTER = PROJECT_ROOT / "data" / "scisports_pl_player_roster.json"


def find_premier_league(client: ScisportsClient) -> dict:
    """Paginate /Leagues until the English Premier League is found."""
    offset = 0
    while True:
        params = {"Offset": offset, "Limit": PAGE_SIZE}
        data, src = get_or_fetch(
            client, "/api/v2/Leagues", params, TTL_LEAGUES_TEAMS_META,
        )
        items = data.get("items") or data.get("data") or []
        total = data.get("total") or data.get("totalCount") or 0
        for league in items:
            name = (league.get("name") or "").strip()
            nation = league.get("nation") or {}
            alpha3 = (nation.get("alpha3Code") or "").upper()
            if name == "Premier League" and alpha3 == "ENG":
                return league
        if offset + PAGE_SIZE >= total or not items:
            break
        offset += PAGE_SIZE
    raise RuntimeError("Premier League (Name='Premier League', nation.alpha3Code='ENG') not found")


def list_teams_in_league(client: ScisportsClient, league_id: int) -> list[dict]:
    """Paginate /Teams?CurrentLeagueIds=[league_id]."""
    teams: list[dict] = []
    offset = 0
    while True:
        params = {
            "Offset": offset,
            "Limit": PAGE_SIZE,
            "CurrentLeagueIds": league_id,
        }
        data, src = get_or_fetch(
            client, "/api/v2/Teams", params, TTL_LEAGUES_TEAMS_META,
        )
        items = data.get("items") or data.get("data") or []
        total = data.get("total") or data.get("totalCount") or 0
        teams.extend(items)
        if offset + PAGE_SIZE >= total or not items:
            break
        offset += PAGE_SIZE
    return teams


def roster_for_team(client: ScisportsClient, team_id: int) -> list[dict]:
    """Paginate /Players?CurrentTeamIds=[team_id]."""
    players: list[dict] = []
    offset = 0
    while True:
        params = {
            "Offset": offset,
            "Limit": PAGE_SIZE,
            "CurrentTeamIds": team_id,
        }
        data, src = get_or_fetch(
            client, "/api/v2/Players", params, TTL_SQUAD_ROSTERS,
        )
        items = data.get("items") or data.get("data") or []
        total = data.get("total") or data.get("totalCount") or 0
        players.extend(items)
        if offset + PAGE_SIZE >= total or not items:
            break
        offset += PAGE_SIZE
    return players


def _extract_bridge_fields(p: dict, team_id: int, team_name: str) -> dict:
    """Shape we save per player to data/scisports_pl_player_roster.json.

    Intentionally fat — captures everything potentially useful for TM matching.
    """
    info = p.get("info") or p
    contract = p.get("contract") or {}
    loan_team = contract.get("loanTeam") or {}
    team = p.get("team") or {}
    positions = p.get("positions") or info.get("positions") or []

    return {
        "scisports_player_id": info.get("id") or p.get("id"),
        "name":          info.get("name") or p.get("name"),
        "firstName":     info.get("firstName") or p.get("firstName"),
        "lastName":      info.get("lastName") or p.get("lastName"),
        "footballName":  info.get("footballName") or p.get("footballName"),
        "birthDate":     info.get("birthDate") or p.get("birthDate"),
        "age":           info.get("age") or p.get("age"),
        "preferredFoot": info.get("preferredFoot") or p.get("preferredFoot"),
        "positions":     positions,
        "team": {
            "id":   team.get("id") or team_id,
            "name": team.get("name") or team_name,
        },
        "contract": {
            "contractEnd": contract.get("contractEnd"),
            "marketValue": contract.get("marketValue"),
            "loanTeam":   {"id": loan_team.get("id"), "name": loan_team.get("name")}
                          if loan_team else None,
        },
        # Raw kept for forensic value but small (typical 1–3KB per player)
        "raw": p,
    }


def main() -> None:
    client = ScisportsClient()

    print("=" * 72)
    print("SciSports Phase 2 — Resolve PL teams + capture rosters")
    print("=" * 72)

    # Pre-flight
    pf = client.preflight_baseline(halt_threshold=800)
    print(f"  Pre-flight remaining: {pf['remaining']}  fresh={pf['looks_fresh']}  "
          f"sample_league={pf['sample_league_name']}")
    if not pf["looks_fresh"]:
        print()
        print("  ⚠️  Remaining quota < 800 — possible concurrent maps-repo activity.")
        print("  Halting. Confirm safety and re-run.")
        sys.exit(2)
    print()

    # Step 1: find PL
    print("Step 1: finding Premier League (ENG) in /api/v2/Leagues …")
    pl = find_premier_league(client)
    pl_id = pl["id"]
    print(f"  → league_id={pl_id}  name={pl.get('name')!r}  "
          f"nation={pl.get('nation', {}).get('alpha3Code')}")
    if pl_id != 50:
        print(f"  (NOTE: maps-repo hint was id=50; got {pl_id}. Hint was approximate.)")
    print()

    # Step 2: list teams
    print(f"Step 2: listing teams in PL (league_id={pl_id}) …")
    teams = list_teams_in_league(client, pl_id)
    print(f"  → {len(teams)} teams returned")
    if len(teams) != 20:
        print(f"  (Note: expected 20, got {len(teams)} — verify before proceeding)")

    # Write team_ids.json
    teams_simple = []
    for t in teams:
        info = t.get("info") or t
        teams_simple.append({
            "id":   info.get("id") or t.get("id"),
            "name": info.get("name") or t.get("name"),
        })
    OUT_TEAMS.write_text(json.dumps({
        "league": {"id": pl_id, "name": pl.get("name")},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "teams": teams_simple,
    }, indent=2), encoding="utf-8")
    print(f"  → wrote {OUT_TEAMS}")
    print()

    # Step 3: per-team rosters
    print(f"Step 3: capturing rosters for {len(teams_simple)} teams …")
    roster: list[dict] = []
    for i, t in enumerate(teams_simple, 1):
        team_id = t["id"]
        team_name = t["name"]
        players = roster_for_team(client, team_id)
        bridge_rows = [_extract_bridge_fields(p, team_id, team_name) for p in players]
        roster.extend(bridge_rows)
        rem = client.last_remaining
        print(f"  [{i:>2d}/20] {team_name[:30]:30s}  team_id={team_id:>6}  "
              f"players={len(players):>3}  remaining={rem}")
    print()

    OUT_ROSTER.write_text(json.dumps({
        "league_id": pl_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "team_count": len(teams_simple),
        "player_count": len(roster),
        "players": roster,
    }, indent=2, default=str), encoding="utf-8")
    print(f"  → wrote {OUT_ROSTER} ({len(roster)} players)")
    print()

    # Final summary
    print("=" * 72)
    print("Phase 2 summary")
    print("=" * 72)
    print(f"  API calls made:           {client.requests_made}")
    print(f"  Min X-RateLimit-Remaining seen: {client.min_remaining_seen}")
    print(f"  Final X-RateLimit-Remaining:    {client.last_remaining}")
    cs = cache_stats()
    print(f"  Cache files on disk:      {cs['files']} ({cs['total_kb']} KB)")
    print(f"  Cache by source:          {cs['by_source']}")


if __name__ == "__main__":
    try:
        main()
    except (ScisportsRateLimitEmergency, ScisportsRateLimitedError) as e:
        print(f"\nRATE-LIMIT EMERGENCY: {e}")
        print("Inspect logs/scisports_api.log and confirm safety before re-running.")
        sys.exit(2)
