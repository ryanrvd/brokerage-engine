"""Step 29 — Link SciSports squad data onto tm_squad_scrape.

ZERO API calls. Reads the bridge file produced by Phase 2's resolver
(data/scisports_pl_player_roster.json) and the tm_squad_scrape rows seeded by
script 28 (TM scrape), then fuzzy-matches SciSports → TM by name + DOB and
UPDATEs each TM row with the matched scisports_player_id.

Why local-only: Phase 2 already paid the API cost to capture the 572 PL
players. Re-fetching here would burn the shared client_id budget for no
new data. Phase 3's CA/PA refresh (script 30) is the only API-hitting step.

Two side-effects on player_universe (data_source='tm_squad_scrape'):
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
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

ROSTER_PATH = PROJECT_ROOT / "data" / "scisports_pl_player_roster.json"
AUDIT_LOG = PROJECT_ROOT / "logs" / "linker_pass3_audit.log"

# SciSports position1 → project bucket family (10-bucket model).
# Used by the Pass 3 position-bucket sanity check.
SCI_POS_TO_BUCKET: dict[str, str] = {
    "Goalkeeper":         "GK",
    "CentreBack":         "CB",
    "LeftBack":           "LB",
    "RightBack":          "RB",
    "DefensiveMidfield":  "DM",
    "CentreMidfield":     "CM",
    "AttackingMidfield":  "AM",
    "LeftWing":           "LW",
    "RightWing":          "RW",
    "CentreForward":      "ST_CF",
}


def _audit_log(line: str) -> None:
    """Append a line to logs/linker_pass3_audit.log with timestamp prefix."""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts}\t{line}\n")


def _sci_bucket_family(positions: list[str] | None, position1: str | None) -> set[str]:
    """Map a SciSports candidate's position list to the set of project buckets it could plausibly fill.

    Returns an empty set if no positions resolve — callers should treat that as 'unknown',
    not 'mismatch'.
    """
    raw: list[str] = []
    if positions:
        raw.extend(positions)
    if position1:
        raw.append(position1)
    out: set[str] = set()
    for p in raw:
        b = SCI_POS_TO_BUCKET.get(p)
        if b:
            out.add(b)
    return out


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
    """Load the SciSports bridge file.

    Bugfix (2026-06-04): previously `names_norm` included standalone `firstName`
    and `lastName` values. That allowed Pass 3 to match on a single-token overlap
    (e.g. TM "Sasa Kalajdzic" matched SciSports Saša Lukić because Lukić's
    firstName-only entry "sasa" was a subset of the TM tokens). We now only keep
    multi-token full-name forms — `name`, `footballName`, and the explicit
    firstName + " " + lastName concatenation.
    """
    raw = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    out: list[dict] = []
    for p in raw["players"]:
        names: list[str] = []
        for k in ("name", "footballName"):
            if p.get(k):
                names.append(p[k])
        if p.get("firstName") and p.get("lastName"):
            names.append(f"{p['firstName']} {p['lastName']}")
        # Deduplicate after normalisation; keep only multi-token entries.
        norm_set: set[str] = set()
        for n in names:
            nn = _normalize(n)
            if nn and len(nn.split()) >= 2:
                norm_set.add(nn)

        # SciSports positions for Pass 3 sanity check
        positions: list[str] = []
        for k in ("position1", "position2"):
            v = p.get(k)
            if v:
                positions.append(v)
        for v in (p.get("positions") or []):
            if v and v not in positions:
                positions.append(v)

        out.append({
            "scisports_player_id": p.get("scisports_player_id"),
            "name":      p.get("name"),
            "names_norm": sorted(norm_set),
            "birthDate": _parse_dob(p.get("birthDate")),
            "team_name": (p.get("team") or {}).get("name"),
            "team_id":   (p.get("team") or {}).get("id"),
            "positions": positions,
            "position1": (p.get("position1") or
                          (positions[0] if positions else None)),
        })
    return out


def match_tm_to_sci(tm_name: str, tm_dob: str | None,
                    tm_bucket: str | None,
                    bridge: list[dict],
                    tm_player_id: int | None = None
                    ) -> tuple[int | None, str, int | None]:
    """Match a TM player to a SciSports bridge entry.

    Three passes, in increasing looseness:

    Pass 1: exact normalised full-name match + exact DOB.
    Pass 2: token-subset overlap + DOB year match (single hit).
    Pass 3 (loose fallback): token-subset overlap, with STRICT guards added in
            the 2026-06-04 hardening:
              • require ≥ 2 overlapping tokens (kills single-firstName matches)
              • require DOB year match if TM DOB is available
              • require position-bucket family agreement if both sides have
                known positions; a clear bucket disagreement refuses the link
                and logs a WARN entry to logs/linker_pass3_audit.log.

    Every Pass 3 candidate (accepted or refused) is appended to the audit log
    so the looser-match decisions are reviewable.

    Returns (accepted_sci_id, kind, refused_sci_id):
      • accepted_sci_id  — sci_id to write, or None if no accept
      • kind             — "name+dob" / "name+year" / "name_unique" / refusal reason
      • refused_sci_id   — when a candidate was explicitly refused by the Pass 3
                           guards, this is its sci_id (so the caller can clear
                           ONLY stale links that point at the refused candidate).
                           None for plain unmatched ("no candidate in bridge").
    """
    nm = _normalize(tm_name)
    if not nm:
        return None, "", None
    tokens = set(nm.split())
    if not tokens:
        return None, "", None
    tm_year = tm_dob[:4] if tm_dob and len(tm_dob) >= 4 else None

    # Pass 1: exact name + exact DOB
    if tm_dob:
        for b in bridge:
            if b["birthDate"] == tm_dob and any(nm == n for n in b["names_norm"]):
                return b["scisports_player_id"], "name+dob", None

    # Pass 2 build-candidates: token-subset overlap (any direction).
    # Track per-candidate the maximum token-overlap count for Pass 3.
    candidates: list[tuple[dict, int]] = []  # (bridge_entry, max_overlap_count)
    for b in bridge:
        best_overlap = 0
        for n_norm in b["names_norm"]:
            sci_tokens = set(n_norm.split())
            if tokens.issubset(sci_tokens) or sci_tokens.issubset(tokens):
                overlap = len(tokens & sci_tokens)
                if overlap > best_overlap:
                    best_overlap = overlap
        if best_overlap > 0:
            candidates.append((b, best_overlap))

    if tm_year:
        year_hits = [(b, o) for (b, o) in candidates
                     if b["birthDate"] and b["birthDate"].startswith(tm_year)]
        if len(year_hits) == 1:
            return year_hits[0][0]["scisports_player_id"], "name+year", None
        if len(year_hits) > 1:
            return None, "ambiguous", None

    # Pass 3: tightened loose fallback (2026-06-04).
    if len(candidates) == 0:
        return None, "", None
    if len(candidates) > 1:
        return None, "ambiguous", None

    # Single candidate — apply the stricter Pass 3 guards.
    cand, overlap_count = candidates[0]
    cand_id = cand["scisports_player_id"]
    cand_name = cand["name"]
    cand_bucket_set = _sci_bucket_family(cand.get("positions"), cand.get("position1"))

    # Guard 1: token overlap must be ≥ 2.
    if overlap_count < 2:
        _audit_log(
            f"REFUSE single-token tm_pid={tm_player_id} tm_name={tm_name!r} "
            f"sci_id={cand_id} sci_name={cand_name!r} overlap={overlap_count}"
        )
        return None, "pass3_single_token", cand_id

    # Guard 2: if TM has DOB, require year match (already short-circuited above,
    # but the explicit check makes the contract clear when tm_year falsy.)
    if tm_year:
        if not (cand["birthDate"] and cand["birthDate"].startswith(tm_year)):
            _audit_log(
                f"REFUSE dob-year-mismatch tm_pid={tm_player_id} tm_name={tm_name!r} "
                f"tm_year={tm_year} sci_id={cand_id} sci_name={cand_name!r} "
                f"sci_dob={cand['birthDate']}"
            )
            return None, "pass3_dob_year_mismatch", cand_id

    # Guard 3: if both have a bucket family, require overlap.
    if tm_bucket and cand_bucket_set:
        if tm_bucket not in cand_bucket_set:
            _audit_log(
                f"REFUSE bucket-mismatch tm_pid={tm_player_id} tm_name={tm_name!r} "
                f"tm_bucket={tm_bucket} sci_id={cand_id} sci_name={cand_name!r} "
                f"sci_buckets={sorted(cand_bucket_set)}"
            )
            return None, "pass3_bucket_mismatch", cand_id

    bucket_status = (
        "match" if tm_bucket and tm_bucket in cand_bucket_set
        else ("unknown" if not (tm_bucket and cand_bucket_set) else "?")
    )
    _audit_log(
        f"ACCEPT pass3 tm_pid={tm_player_id} tm_name={tm_name!r} "
        f"sci_id={cand_id} sci_name={cand_name!r} overlap={overlap_count} "
        f"bucket={bucket_status} tm_bucket={tm_bucket} "
        f"sci_buckets={sorted(cand_bucket_set) if cand_bucket_set else 'none'}"
    )
    return cand_id, "name_unique", None


def main() -> None:
    bridge = load_bridge()
    print(f"Bridge file: {len(bridge)} SciSports PL players")

    con = sqlite3.connect(config.SQLITE_FILE)
    tm_rows = con.execute("""
        SELECT player_id, name, date_of_birth, parent_club, parent_club_id,
               on_loan, current_club, league_id, data_source,
               parent_club_recently_relegated, mandate_priority_multiplier,
               position_bucket, scisports_player_id
        FROM player_universe
        WHERE data_source = 'tm_squad_scrape'
    """).fetchall()
    print(f"tm_squad_scrape rows in DB: {len(tm_rows)}")

    # Stamp a separator on the audit log so the latest run is easy to find.
    _audit_log(f"=== RUN START tm_rows={len(tm_rows)} bridge_rows={len(bridge)} ===")

    matched = 0
    by_kind: dict[str, int] = {}
    unmatched: list[tuple[int, str, str | None, str]] = []
    set_updates: list[tuple[int, int]] = []           # (sci_id, tm_player_id) — apply
    clear_updates: list[tuple[int, int]] = []         # (pid, refused_sci_id) — clear ONLY if current link == refused
    sci_ids_used: set[int] = set()
    refused = {
        "pass3_single_token":       0,
        "pass3_dob_year_mismatch":  0,
        "pass3_bucket_mismatch":    0,
    }
    cleared_count = 0

    for (pid, tm_name, tm_dob, parent_club, parent_cid,
         on_loan, current_club, league_id, ds,
         relegated, multiplier, position_bucket,
         current_sci_id) in tm_rows:
        sci_id, kind, refused_sci_id = match_tm_to_sci(
            tm_name, tm_dob, position_bucket, bridge, tm_player_id=pid
        )
        if sci_id is None:
            unmatched.append((pid, tm_name, tm_dob, parent_club or ""))
            if kind in refused:
                refused[kind] += 1
            # Targeted stale-link cleanup: clear ONLY if the row's current link
            # IS the refused candidate. Other rows (linked by a different source
            # — e.g. scripts/_scisports_phase4_refresh.py — keep their links).
            if refused_sci_id is not None and current_sci_id == refused_sci_id:
                clear_updates.append((pid, refused_sci_id))
            continue
        set_updates.append((sci_id, pid))
        sci_ids_used.add(sci_id)
        matched += 1
        by_kind[kind] = by_kind.get(kind, 0) + 1

    # Apply set first, then targeted clears (set wins if both happen on same pid).
    con.executemany(
        "UPDATE player_universe SET scisports_player_id = ? WHERE player_id = ?",
        set_updates,
    )
    for pid, refused_sci_id in clear_updates:
        cur = con.execute(
            "UPDATE player_universe SET scisports_player_id = NULL "
            "WHERE player_id = ? AND scisports_player_id = ?",
            (pid, refused_sci_id),
        )
        cleared_count += cur.rowcount
    con.commit()

    print(f"Matched: {matched} / {len(tm_rows)}  ({matched / len(tm_rows):.1%})")
    print()
    print("Match-kind breakdown:")
    for k, n in sorted(by_kind.items()):
        print(f"  {k:20s} {n}")
    print()
    print("Pass 3 refusals (candidate refused under new guards):")
    for k, n in sorted(refused.items()):
        print(f"  {k:30s} {n}")
    print()
    print(f"Stale links actually cleared (current link == refused candidate): {cleared_count}")
    print()
    _audit_log(
        f"=== RUN END matched={matched} refused={refused} cleared={cleared_count} ==="
    )

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
