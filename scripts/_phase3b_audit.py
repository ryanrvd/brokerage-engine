"""Phase 3b audit — broader linker validation after DOB backfill.

After Phase 2 fills in date_of_birth for the previously-NULL cohort, this
script validates the existing scisports_player_id → CA/PA link by comparing
SciSports' birthDate against TM's now-backfilled date_of_birth.

Method:
  1. Collect every player_universe row that (a) has scisports_player_id IS NOT NULL
     and (b) now has date_of_birth populated (came in via Phase 2 backfill).
  2. Batch-fetch SciSports player records via /api/v2/Players?PlayerIds=a,b,c...
     in chunks of 50. Cache aggressively.
  3. For each row, compare TM date_of_birth to SciSports birthDate.
       • DOB year mismatch → flag as mis-link.
       • DOB month/day mismatch but same year → log as warn (could be data error).
       • Match → pass.
  4. For confirmed mis-links, do a per-surname /Players search (cached), filter
     by TM age + position bucket + first-name overlap, and resolve to the correct
     sci_id where possible. NULL the link otherwise.
  5. Apply repairs in a single transaction. Report.

Read-only by default; pass `apply` arg to commit changes.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unicodedata
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import config
from _scisports_client import ScisportsClient
from _scisports_cache import get_or_fetch, read_cached, write_cached, TTL_SQUAD_ROSTERS, TTL_SCISKILL


SCI_POS_TO_BUCKET = {
    "Goalkeeper": "GK", "CentreBack": "CB", "LeftBack": "LB", "RightBack": "RB",
    "DefensiveMidfield": "DM", "CentreMidfield": "CM", "AttackingMidfield": "AM",
    "LeftWing": "LW", "RightWing": "RW", "CentreForward": "ST_CF",
}


def _norm(s: str | None) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def _sci_buckets(info: dict) -> set[str]:
    positions = info.get("positions") or []
    for k in ("position1", "position2"):
        v = info.get(k)
        if v and v not in positions:
            positions.append(v)
    return {SCI_POS_TO_BUCKET[p] for p in positions if p in SCI_POS_TO_BUCKET}


def batch_fetch_sci_players(client: ScisportsClient, sci_ids: list[int]) -> dict[int, dict]:
    """Batch-fetch SciSports player records in chunks of 50. Cached per chunk.
    Returns {sci_id: info_dict}."""
    out: dict[int, dict] = {}
    chunk_size = 50
    for i in range(0, len(sci_ids), chunk_size):
        chunk = sorted(sci_ids[i:i + chunk_size])
        params = {"Offset": 0, "Limit": chunk_size, "PlayerIds": ",".join(str(x) for x in chunk)}
        hit = read_cached("/api/v2/Players", params)
        if hit is not None:
            data, _ = hit
        else:
            data = client.get_players(**{
                "offset": 0, "limit": chunk_size,
                "PlayerIds": ",".join(str(x) for x in chunk),
            })
            write_cached("/api/v2/Players", params, data, TTL_SQUAD_ROSTERS, source="live")
        items = data.get("items") or []
        for it in items:
            info = it.get("info") or {}
            sid = info.get("id")
            if sid:
                # Merge in top-level team info
                out[sid] = {
                    **info,
                    "team": (it.get("team") or {}).get("name"),
                    "team_id": (it.get("team") or {}).get("id"),
                    "league": (it.get("league") or {}).get("name"),
                }
    return out


def main(apply: bool = False) -> None:
    con = sqlite3.connect(config.SQLITE_FILE)
    con.row_factory = sqlite3.Row

    # Audit cohort: linked + DOB now known
    rows = con.execute("""
        SELECT pu.player_id, pu.name, pu.date_of_birth, pu.age,
               pu.position_bucket, pu.scisports_player_id,
               pu.parent_club, pu.current_club, pu.league_id,
               pr.current_ability AS ca, pr.potential_ability AS pa
        FROM player_universe pu
        LEFT JOIN player_ratings pr ON pr.tm_player_id = pu.player_id
        WHERE pu.scisports_player_id IS NOT NULL
          AND pu.date_of_birth IS NOT NULL
    """).fetchall()
    print(f"Linked rows with DOB present: {len(rows):,}")
    if not rows:
        print("No audit cohort — nothing to do.")
        return

    sci_ids = sorted({r["scisports_player_id"] for r in rows})
    print(f"Distinct sci_ids to batch-fetch: {len(sci_ids):,}")
    print(f"Chunks of 50 → expected API calls: {(len(sci_ids) + 49) // 50}")

    client = ScisportsClient()
    client.authenticate()
    pre = client.preflight_baseline(halt_threshold=800)
    print(f"PREFLIGHT: remaining={pre['remaining']}  looks_fresh={pre['looks_fresh']}")
    if not pre.get("looks_fresh"):
        print("ABORT — quota too low.")
        return

    sci_data = batch_fetch_sci_players(client, sci_ids)
    print(f"Fetched SciSports records for {len(sci_data):,}/{len(sci_ids):,} sci_ids")
    print(f"Quota: remaining={client.last_remaining}  min_seen={client.min_remaining_seen}")
    print()

    # Audit comparisons
    dob_year_mismatch: list[dict] = []
    dob_full_mismatch: list[dict] = []
    bucket_mismatch:   list[dict] = []
    sci_not_found:     list[dict] = []  # sci_id we have linked is unknown to SciSports
    audit_ok = 0

    for r in rows:
        sci_id = r["scisports_player_id"]
        info = sci_data.get(sci_id)
        if info is None:
            sci_not_found.append(dict(r))
            continue
        sci_dob = (info.get("birthDate") or "")[:10] or None
        tm_dob = r["date_of_birth"]
        bucket_ok = True
        sb = _sci_buckets(info)
        if r["position_bucket"] and sb and r["position_bucket"] not in sb:
            bucket_ok = False
            bucket_mismatch.append({"row": dict(r), "sci_info": {
                "sci_id": sci_id, "sci_name": info.get("name"),
                "sci_buckets": sorted(sb), "sci_dob": sci_dob, "team": info.get("team"),
            }})
        # DOB checks
        if sci_dob and tm_dob:
            if sci_dob[:4] != str(tm_dob)[:4]:
                dob_year_mismatch.append({"row": dict(r), "sci_info": {
                    "sci_id": sci_id, "sci_name": info.get("name"),
                    "sci_dob": sci_dob, "team": info.get("team"),
                }})
            elif sci_dob != str(tm_dob)[:10]:
                dob_full_mismatch.append({"row": dict(r), "sci_info": {
                    "sci_id": sci_id, "sci_name": info.get("name"),
                    "sci_dob": sci_dob, "team": info.get("team"),
                }})
            else:
                if bucket_ok:
                    audit_ok += 1

    print(f"audit OK:            {audit_ok}")
    print(f"DOB year mismatch:   {len(dob_year_mismatch)}  ← definite mis-links")
    print(f"DOB full mismatch:   {len(dob_full_mismatch)}  (same year, different day — soft)")
    print(f"Bucket mismatch:     {len(bucket_mismatch)}    ← also definite mis-links")
    print(f"sci_id unknown to SciSports: {len(sci_not_found)}")
    print()

    # Union of definite mis-links (dedup by pid)
    definite_pids: set[int] = set()
    definite: list[dict] = []
    for d in dob_year_mismatch + bucket_mismatch:
        pid = d["row"]["player_id"]
        if pid not in definite_pids:
            definite_pids.add(pid)
            definite.append(d)
    print(f"=== Definite mis-links (union, deduped): {len(definite)} ===")
    for d in definite[:30]:
        r, si = d["row"], d["sci_info"]
        print(f"  pid={r['player_id']:>9}  {r['name']!r:30s} TM_DOB={r['date_of_birth']}  "
              f"sci_id={si['sci_id']:>6} {si['sci_name']!r:25s} sci_dob={si['sci_dob']}  team={si.get('team')}")
    if len(definite) > 30:
        print(f"  ... and {len(definite) - 30} more")
    print()

    # Persist audit report
    audit_path = Path("logs/phase3b_audit.json")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps({
        "audit_ok": audit_ok,
        "dob_year_mismatch": dob_year_mismatch,
        "dob_full_mismatch": dob_full_mismatch,
        "bucket_mismatch":   bucket_mismatch,
        "sci_not_found":     sci_not_found,
    }, indent=2, ensure_ascii=False, default=str))
    print(f"Audit report written: {audit_path}")
    print()

    if not apply:
        print("Read-only mode. Pass 'apply' to repair definite mis-links.")
        return

    # APPLY: for each definite mis-link, NULL the link + DELETE the ratings row.
    # Recovery (finding the CORRECT sci_id) is left to a follow-up targeted-API
    # script, since per-surname searches can run hundreds of calls.
    print("=== APPLYING (NULL definite mis-links) ===")
    applied = 0
    for d in definite:
        pid = d["row"]["player_id"]
        con.execute("UPDATE player_universe SET scisports_player_id = NULL WHERE player_id = ?", (pid,))
        con.execute("DELETE FROM player_ratings WHERE tm_player_id = ?", (pid,))
        applied += 1
    con.commit()
    print(f"NULLed {applied} mis-links.")


if __name__ == "__main__":
    apply = "apply" in sys.argv
    main(apply=apply)
