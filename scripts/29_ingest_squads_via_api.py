"""Step 29 — Link SciSports squad data onto pl_squad_full.

ZERO API calls. Reads the bridge file produced by Phase 2's resolver
(data/scisports_pl_player_roster.json) and the pl_squad_full rows seeded by
script 28 (TM scrape), then fuzzy-matches SciSports → TM by name + DOB and
UPDATEs each TM row with the matched scisports_player_id.

Why local-only: Phase 2 already paid the API cost to capture the 572 PL
players. Re-fetching here would burn the shared client_id budget for no
new data. Phase 3's CA/PA refresh (script 30) is the only API-hitting step.

Two side-effects on player_universe (data_source='pl_squad_full'):
  • scisports_player_id      — populated where match succeeded
  • parent_club_recently_relegated, mandate_priority_multiplier — already
    set by 19_apply_league_overrides; this script doesn't touch them, but
    prints them for the run summary.

Unmatched players (TM-only or SciSports-only) are logged so we can audit the
delta between the two sources. Common reasons:
  • Academy/youth in TM kader but not in SciSports
  • Loaned-out TM rows (their parent is PL but they're physically elsewhere;
    SciSports indexes them under their loan club)
  • Spelling/diacritic mismatches the fuzzy matcher misses
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import unicodedata
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

ROSTER_PATH = PROJECT_ROOT / "data" / "scisports_pl_player_roster.json"


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^\w\s]", " ", no_accents.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_dob(v) -> str | None:
    if not v:
        return None
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return None


def load_bridge() -> list[dict]:
    raw = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    out: list[dict] = []
    for p in raw["players"]:
        names = []
        for k in ("name", "firstName", "lastName", "footballName"):
            if p.get(k):
                names.append(p[k])
        if p.get("firstName") and p.get("lastName"):
            names.append(f"{p['firstName']} {p['lastName']}")
        out.append({
            "scisports_player_id": p.get("scisports_player_id"),
            "name":      p.get("name"),
            "names_norm": [_normalize(n) for n in names if n],
            "birthDate": _parse_dob(p.get("birthDate")),
            "team_name": (p.get("team") or {}).get("name"),
            "team_id":   (p.get("team") or {}).get("id"),
        })
    return out


def match_tm_to_sci(tm_name: str, tm_dob: str | None,
                    bridge: list[dict]) -> tuple[int | None, str]:
    nm = _normalize(tm_name)
    if not nm:
        return None, ""
    tokens = set(nm.split())
    if not tokens:
        return None, ""
    tm_year = tm_dob[:4] if tm_dob and len(tm_dob) >= 4 else None

    # Pass 1: exact name + exact DOB
    if tm_dob:
        for b in bridge:
            if b["birthDate"] == tm_dob and any(nm == n for n in b["names_norm"]):
                return b["scisports_player_id"], "name+dob"

    # Pass 2: token-subset match + DOB year
    candidates = []
    for b in bridge:
        for n_norm in b["names_norm"]:
            sci_tokens = set(n_norm.split())
            if tokens.issubset(sci_tokens) or sci_tokens.issubset(tokens):
                candidates.append(b)
                break
    if tm_year:
        year_hits = [b for b in candidates
                     if b["birthDate"] and b["birthDate"].startswith(tm_year)]
        if len(year_hits) == 1:
            return year_hits[0]["scisports_player_id"], "name+year"
        if len(year_hits) > 1:
            return None, "ambiguous"

    # Pass 3: single name-match across full bridge
    if len(candidates) == 1:
        return candidates[0]["scisports_player_id"], "name_unique"
    if len(candidates) > 1:
        return None, "ambiguous"
    return None, ""


def main() -> None:
    bridge = load_bridge()
    print(f"Bridge file: {len(bridge)} SciSports PL players")

    con = sqlite3.connect(config.SQLITE_FILE)
    tm_rows = con.execute("""
        SELECT player_id, name, date_of_birth, parent_club, parent_club_id,
               on_loan, current_club, league_id, data_source,
               parent_club_recently_relegated, mandate_priority_multiplier
        FROM player_universe
        WHERE data_source = 'pl_squad_full'
    """).fetchall()
    print(f"pl_squad_full rows in DB: {len(tm_rows)}")

    matched = 0
    by_kind: dict[str, int] = {}
    unmatched: list[tuple[int, str, str | None, str]] = []
    updates: list[tuple[int, int]] = []  # (sci_id, tm_player_id)
    sci_ids_used: set[int] = set()

    for (pid, tm_name, tm_dob, parent_club, parent_cid,
         on_loan, current_club, league_id, ds,
         relegated, multiplier) in tm_rows:
        sci_id, kind = match_tm_to_sci(tm_name, tm_dob, bridge)
        if sci_id is None:
            unmatched.append((pid, tm_name, tm_dob, parent_club or ""))
            continue
        updates.append((sci_id, pid))
        sci_ids_used.add(sci_id)
        matched += 1
        by_kind[kind] = by_kind.get(kind, 0) + 1

    con.executemany(
        "UPDATE player_universe SET scisports_player_id = ? WHERE player_id = ?",
        updates,
    )
    con.commit()

    print(f"Matched: {matched} / {len(tm_rows)}  ({matched / len(tm_rows):.1%})")
    print()
    print("Match-kind breakdown:")
    for k, n in sorted(by_kind.items()):
        print(f"  {k:20s} {n}")
    print()

    # SciSports players in the bridge that DIDN'T match any TM row
    matched_sci_ids = sci_ids_used
    sci_unmatched = [b for b in bridge if b["scisports_player_id"] not in matched_sci_ids]
    print(f"SciSports bridge players NOT linked to a TM row: {len(sci_unmatched)}")
    if sci_unmatched:
        print("  Sample (first 10):")
        for b in sci_unmatched[:10]:
            print(f"    sci_id={b['scisports_player_id']:>6}  {(b['name'] or '')[:30]:30s}  "
                  f"dob={b['birthDate']}  team={b['team_name']}")
    print()

    # Mandate-priority cohort summary
    print("Mandate-priority cohort (recently_relegated parents):")
    rows = con.execute("""
        SELECT pu.parent_club, COUNT(*) as n,
               SUM(CASE WHEN pu.scisports_player_id IS NOT NULL THEN 1 ELSE 0 END) as linked,
               MAX(pu.mandate_priority_multiplier) as mult
        FROM player_universe pu
        WHERE pu.parent_club_recently_relegated = 1
        GROUP BY pu.parent_club
        ORDER BY n DESC
    """).fetchall()
    if not rows:
        print("  (none — check 19_apply_league_overrides ran)")
    else:
        print(f"  {'parent_club':35s} {'players':>8s} {'linked_sci':>11s} {'mult':>5s}")
        for parent, n, linked, mult in rows:
            print(f"  {(parent or '?')[:35]:35s} {n:>8} {linked:>11} {mult:>5.1f}")
    print()

    # Promoted-cohort summary (multiplier 1.1)
    print("Mandate-priority cohort (recently_promoted parents):")
    rows = con.execute("""
        SELECT pu.parent_club, COUNT(*) as n, MAX(pu.mandate_priority_multiplier)
        FROM player_universe pu
        WHERE pu.parent_club_id IN (SELECT club_id FROM club_pressure WHERE recently_promoted = 1)
        GROUP BY pu.parent_club
        ORDER BY n DESC
    """).fetchall()
    if not rows:
        print("  (none)")
    else:
        print(f"  {'parent_club':35s} {'players':>8s} {'mult':>5s}")
        for parent, n, mult in rows:
            print(f"  {(parent or '?')[:35]:35s} {n:>8} {mult or 1.0:>5.1f}")

    con.close()


if __name__ == "__main__":
    main()
