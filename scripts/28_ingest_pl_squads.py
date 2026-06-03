"""
Step 28 — Ingest full Premier League squads into player_universe.

Uses a Safari-exported TM session cookie to bypass CloudFront bot detection.
Scrapes two pages per PL club:
  - /kader  (squad page)  → current first-team players
  - /leihspieleruebersicht (loans-out page) → players loaned out by the club

Cookie workflow:
  1. Open https://www.transfermarkt.com in Safari
  2. Solve the CAPTCHA if it appears
  3. Safari Web Inspector → Storage → Cookies → www.transfermarkt.com
  4. Right-click → Copy as Cookie String
  5. Paste into data/tm_cookie.txt
  6. Run: .venv/bin/python scripts/28_ingest_pl_squads.py --validate-only

CLI flags:
  --validate-only     Test the cookie with one request, then exit
  --single-club NAME  Scrape one club only (e.g. --single-club arsenal)

Pipeline position: after 07 + 19 (pre-08), before 08_compute_pressure.
"""

import argparse
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

import certifi
import duckdb
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from _position_buckets import bucket_for

PL_LEAGUE_ID = "GB1"
SEASON = "2025"
SLEEP_BETWEEN = 2.0
CACHE_MAX_AGE_HOURS = 24

CACHE_DIR = Path("data/tm_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_PATH = Path("data/tm_cookie.txt")
_ssl_context = ssl.create_default_context(cafile=certifi.where())

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "identity",
}

SLUG_OVERRIDES = {
    "990": "coventry-city",
}

_DATE_FORMATS = ("%b %d, %Y", "%d.%m.%Y", "%d/%m/%Y", "%d %b %Y")


def _parse_date(text: str) -> date | None:
    text = (text or "").strip()
    if not text or text == "-":
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(\d{4})", text)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%b %d, %Y"
            ).date()
        except ValueError:
            pass
    return None


def parse_market_value(text: str) -> int | None:
    text = (text or "").strip()
    m = re.match(r"€\s*([\d.,]+)\s*([mk])", text, re.IGNORECASE)
    if not m:
        return None
    amount = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    return int(amount * (1_000_000 if unit == "m" else 1_000))


# ─── Cookie management ───────────────────────────────────────────────────────

def load_cookie() -> str:
    if not COOKIE_PATH.exists():
        print("ERROR: Cookie file not found at data/tm_cookie.txt")
        print()
        _print_cookie_instructions()
        sys.exit(1)
    cookie = COOKIE_PATH.read_text(encoding="utf-8").strip()
    if not cookie:
        print("ERROR: data/tm_cookie.txt is empty")
        print()
        _print_cookie_instructions()
        sys.exit(1)
    return cookie


def _print_cookie_instructions():
    print("To export your TM session cookie:")
    print()
    print("  STEP 1: Open https://www.transfermarkt.com in Safari")
    print("  STEP 2: Solve the CAPTCHA if it appears")
    print("  STEP 3: Open Safari Web Inspector (Develop > Show Web Inspector)")
    print("  STEP 4: Go to the Storage tab > Cookies > www.transfermarkt.com")
    print("  STEP 5: Select all cookies, right-click > Copy as cURL")
    print("          Then extract the cookie string from the -H 'Cookie: ...' header")
    print("          OR: Go to the Network tab, click any request,")
    print("          find the Cookie header, and copy its value")
    print("  STEP 6: Paste into data/tm_cookie.txt and save")
    print("  STEP 7: Run: .venv/bin/python scripts/28_ingest_pl_squads.py --validate-only")


def _decode_response(raw: bytes, encoding: str) -> str:
    enc = (encoding or "").lower().strip()
    if enc == "gzip":
        import gzip
        return gzip.decompress(raw).decode("utf-8", errors="replace")
    if enc == "deflate":
        import zlib
        try:
            return zlib.decompress(raw).decode("utf-8", errors="replace")
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", errors="replace")
    if enc == "br":
        try:
            import brotli
            return brotli.decompress(raw).decode("utf-8", errors="replace")
        except ImportError:
            raise RuntimeError("Server returned br-compressed body but `brotli` is not installed.")
    return raw.decode("utf-8", errors="replace")


def validate_cookie(cookie: str) -> bool:
    url = (
        f"https://www.transfermarkt.com/fc-arsenal/kader/verein/11"
        f"/saison_id/{SEASON}/plus/1"
    )
    req = urllib.request.Request(url)
    for k, v in HEADERS.items():
        req.add_header(k, v)
    req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, context=_ssl_context, timeout=20) as resp:
            raw = resp.read()
            enc = resp.headers.get("Content-Encoding", "")
            body = _decode_response(raw, enc)
            has_table = "hauptlink" in body and "profil/spieler" in body
            has_captcha = "Human Verification" in body
            if has_captcha:
                print("FAIL: Cookie returned 200 but page is still the CAPTCHA challenge.")
                print("      The cookie may be incomplete or expired. Re-export from Safari.")
                return False
            if not has_table:
                print(f"WARN: Got 200 but no squad table found ({len(body)} chars).")
                print("      Check if the page content looks right.")
                return False
            print(f"OK: Cookie valid. Arsenal kader page: {len(body)} chars, squad table present.")
            return True
    except urllib.error.HTTPError as e:
        print(f"FAIL: HTTP {e.code} ({e.reason})")
        if e.code == 405:
            print("      405 = CloudFront CAPTCHA challenge. Cookie is invalid or expired.")
        return False
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return False


# ─── HTTP fetch with cookie + cache ──────────────────────────────────────────

def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    return age_hours < CACHE_MAX_AGE_HOURS


def fetch_with_cookie(url: str, cache_key: str, cookie: str) -> str:
    cache_path = CACHE_DIR / f"{cache_key}.html"
    if _cache_is_fresh(cache_path):
        return cache_path.read_text(encoding="utf-8")

    time.sleep(SLEEP_BETWEEN)
    req = urllib.request.Request(url)
    for k, v in HEADERS.items():
        req.add_header(k, v)
    req.add_header("Cookie", cookie)

    try:
        with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
            raw = resp.read()
            body = _decode_response(raw, resp.headers.get("Content-Encoding", ""))
    except urllib.error.HTTPError as e:
        if e.code == 405:
            raise RuntimeError(
                f"HTTP 405 on {cache_key} — cookie expired or invalid. "
                "Re-export from Safari and paste into data/tm_cookie.txt."
            )
        if e.code in (429, 503):
            print(f"  ! HTTP {e.code} on {cache_key}, backing off 30s")
            time.sleep(30)
            with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
                raw = resp.read()
                body = _decode_response(raw, resp.headers.get("Content-Encoding", ""))
        else:
            raise

    if "Human Verification" in body:
        raise RuntimeError(
            f"CAPTCHA page returned for {cache_key} — cookie expired. "
            "Re-export from Safari and paste into data/tm_cookie.txt."
        )

    cache_path.write_text(body, encoding="utf-8")
    return body


# ─── Fetch pages ──────────────────────────────────────────────────────────────

def fetch_squad_page(club_id: str, slug: str, cookie: str) -> str:
    url = (
        f"https://www.transfermarkt.com/{slug}/kader/verein/{club_id}"
        f"/saison_id/{SEASON}/plus/1"
    )
    return fetch_with_cookie(url, f"pl_kader_{club_id}", cookie)


def fetch_loans_out_page(club_id: str, slug: str, cookie: str) -> str:
    url = (
        f"https://www.transfermarkt.com/{slug}/leihspieler"
        f"/verein/{club_id}/saison_id/{SEASON}/plus/1"
    )
    return fetch_with_cookie(url, f"pl_loans_{club_id}", cookie)


# ─── Parse squad (kader) page ────────────────────────────────────────────────

def parse_squad(html: str, club_id: str) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1.data-header__headline-wrapper") or soup.find("h1")
    club_name = h1.get_text(" ", strip=True) if h1 else f"club_{club_id}"

    rows_out: list[dict] = []
    table = soup.select_one("#yw1 table.items") or soup.select_one("table.items")
    if not table:
        return club_name, rows_out

    headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]

    def first_col(*needles: str) -> int | None:
        for i, h in enumerate(headers):
            for n in needles:
                if n in h:
                    return i
        return None

    age_col = first_col("age")
    contract_col = first_col("contract", "vertrag")
    dob_col = first_col("date of birth", "geburtsdatum", "born")
    mv_col = first_col("market value", "marktwert")
    foot_col = first_col("foot", "fuß")
    height_col = first_col("height", "größe")

    for r in table.select("tbody > tr.odd, tbody > tr.even"):
        cells = r.find_all("td", recursive=False)
        if not cells:
            continue
        plink = (
            r.select_one("td.hauptlink a[href*='/profil/spieler/']")
            or r.select_one("a[href*='/profil/spieler/']")
        )
        if not plink:
            continue
        m = re.search(r"/profil/spieler/(\d+)", plink["href"])
        if not m:
            continue
        player_id = int(m.group(1))
        name = plink.get_text(strip=True)

        sub_position = None
        inline = r.select_one("table.inline-table")
        if inline:
            trs = inline.select("tr")
            if len(trs) >= 2:
                sub_position = trs[1].get_text(strip=True) or None

        age = None
        dob = None
        for col in (age_col, dob_col):
            if col is not None and col < len(cells):
                txt = cells[col].get_text(" ", strip=True)
                am = re.search(r"\((\d{1,2})\)", txt)
                if am:
                    age = int(am.group(1))
                dob_m = re.search(r"(\w{3}\s+\d{1,2},?\s+\d{4})", txt)
                if dob_m:
                    dob = _parse_date(dob_m.group(1))
                if not dob:
                    dob_m2 = re.search(r"(\d{1,2}[./]\d{1,2}[./]\d{4})", txt)
                    if dob_m2:
                        dob = _parse_date(dob_m2.group(1))
                if age:
                    break
        if age is None:
            for c in cells:
                am = re.search(r"\((\d{1,2})\)", c.get_text(" ", strip=True))
                if am:
                    val = int(am.group(1))
                    if 14 <= val <= 50:
                        age = val
                        break

        contract_end = None
        if contract_col is not None and contract_col < len(cells):
            contract_end = _parse_date(cells[contract_col].get_text(" ", strip=True))

        tm_value = None
        if mv_col is not None and mv_col < len(cells):
            tm_value = parse_market_value(cells[mv_col].get_text(" ", strip=True))

        foot = None
        if foot_col is not None and foot_col < len(cells):
            ft = cells[foot_col].get_text(strip=True) or None
            if ft and ft != "-":
                foot = ft

        height_cm = None
        if height_col is not None and height_col < len(cells):
            ht = cells[height_col].get_text(strip=True)
            hm = re.match(r"(\d+)[,.](\d+)\s*m", ht)
            if hm:
                height_cm = int(hm.group(1)) * 100 + int(hm.group(2))

        rows_out.append({
            "player_id": player_id,
            "name": name,
            "sub_position": sub_position,
            "age": age,
            "date_of_birth": dob,
            "contract_end_date": contract_end,
            "current_tm_value_eur": tm_value,
            "foot": foot,
            "height_cm": height_cm,
            "on_loan": 0,
            "loan_end_date": None,
            "loan_club_id": None,
            "loan_club_name": None,
        })
    return club_name, rows_out


# ─── Parse loans-out page ────────────────────────────────────────────────────

def parse_loans_out(html: str, parent_club_id: str, parent_club_name: str) -> list[dict]:
    """The leihspieler page has TWO tables:
      - 'Players loaned from other clubs' (loans IN)  ← skip
      - 'Out on loan' (loans OUT)                      ← parse this
    Distinguish by the 'Loaned to' column header (loans-out) vs
    'On loan from' (loans-in).
    """
    soup = BeautifulSoup(html, "lxml")
    rows_out: list[dict] = []

    # Find the table whose thead contains "Loaned to"
    table = None
    loan_dest_col = None
    for t in soup.select("table.items"):
        headers = [th.get_text(' ', strip=True).lower() for th in t.select("thead th")]
        for i, h in enumerate(headers):
            if "loaned to" in h:
                table = t
                loan_dest_col = i
                break
        if table:
            break
    if not table:
        return rows_out

    # Also locate other useful columns by header
    headers_lower = [th.get_text(' ', strip=True).lower() for th in table.select("thead th")]

    def find_col(*needles: str) -> int | None:
        for i, h in enumerate(headers_lower):
            for n in needles:
                if n in h:
                    return i
        return None

    age_col = find_col("age")
    loan_end_col = find_col("loan ends")
    mv_col = find_col("market value", "marktwert")
    contract_col = find_col("contract expires")

    for r in table.select("tbody > tr.odd, tbody > tr.even"):
        cells = r.find_all("td", recursive=False)
        if not cells:
            continue
        plink = (
            r.select_one("td.hauptlink a[href*='/profil/spieler/']")
            or r.select_one("a[href*='/profil/spieler/']")
        )
        if not plink:
            continue
        m = re.search(r"/profil/spieler/(\d+)", plink["href"])
        if not m:
            continue
        player_id = int(m.group(1))
        name = plink.get_text(strip=True)

        sub_position = None
        inline = r.select_one("table.inline-table")
        if inline:
            trs = inline.select("tr")
            if len(trs) >= 2:
                sub_position = trs[1].get_text(strip=True) or None

        age = None
        if age_col is not None and age_col < len(cells):
            try:
                age = int(cells[age_col].get_text(strip=True))
            except ValueError:
                pass

        # Loan destination from the "Loaned to" cell
        loan_club_id = None
        loan_club_name = None
        if loan_dest_col is not None and loan_dest_col < len(cells):
            for a_tag in cells[loan_dest_col].find_all("a", href=True):
                href = a_tag.get("href", "")
                cid_m = re.search(r"/verein/(\d+)", href)
                if cid_m and cid_m.group(1) != parent_club_id:
                    loan_club_id = cid_m.group(1)
                    loan_club_name = a_tag.get("title") or a_tag.get_text(strip=True)
                    break

        loan_end = None
        if loan_end_col is not None and loan_end_col < len(cells):
            loan_end = _parse_date(cells[loan_end_col].get_text(" ", strip=True))

        contract_end = None
        if contract_col is not None and contract_col < len(cells):
            contract_end = _parse_date(cells[contract_col].get_text(" ", strip=True))

        tm_value = None
        if mv_col is not None and mv_col < len(cells):
            tm_value = parse_market_value(cells[mv_col].get_text(" ", strip=True))

        rows_out.append({
            "player_id": player_id,
            "name": name,
            "sub_position": sub_position,
            "age": age,
            "date_of_birth": None,
            "contract_end_date": contract_end,
            "current_tm_value_eur": tm_value,
            "foot": None,
            "height_cm": None,
            "on_loan": 1,
            "loan_end_date": loan_end,
            "loan_club_id": loan_club_id,
            "loan_club_name": loan_club_name,
        })
    return rows_out


# ─── PL club discovery ───────────────────────────────────────────────────────

def discover_pl_clubs() -> list[dict]:
    with sqlite3.connect(config.SQLITE_FILE) as con:
        real_pl = con.execute("""
            SELECT club_id, club_name FROM senior_roster
            WHERE league_id = ?
            GROUP BY club_id, club_name HAVING COUNT(*) >= 20
            ORDER BY club_name
        """, (PL_LEAGUE_ID,)).fetchall()

    slug_map: dict[str, str] = {}
    try:
        src = duckdb.connect(config.DUCKDB_FILE, read_only=True)
        for cid, url in src.execute(
            "SELECT CAST(club_id AS VARCHAR), url FROM clubs WHERE url IS NOT NULL"
        ).fetchall():
            m = re.search(r"/([^/]+)/startseite/verein/", url or "")
            if m:
                slug_map[str(cid)] = m.group(1)
        src.close()
    except Exception:
        pass

    clubs: list[dict] = []
    for cid, name in real_pl:
        slug = SLUG_OVERRIDES.get(str(cid)) or slug_map.get(str(cid))
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        clubs.append({"club_id": str(cid), "slug": slug, "name": name})
    return clubs


# ─── Write to SQLite ─────────────────────────────────────────────────────────

def insert_player(con: sqlite3.Connection, p: dict, parent_club_id: str,
                  parent_club_name: str, snapshot: date,
                  leverage_cutoff: date) -> bool:
    contract_end = p["contract_end_date"]
    contract_leveraged = (
        1 if contract_end and contract_end <= leverage_cutoff
        else (0 if contract_end else None)
    )
    if p["on_loan"]:
        current_club = p.get("loan_club_name") or parent_club_name
        current_club_id = p.get("loan_club_id") or parent_club_id
    else:
        current_club = parent_club_name
        current_club_id = parent_club_id

    league_display = config.LEAGUE_DISPLAY.get(PL_LEAGUE_ID, PL_LEAGUE_ID)

    loan_end_str = (
        str(p["loan_end_date"]) if p.get("loan_end_date") else None
    )
    try:
        con.execute(
            "INSERT OR IGNORE INTO player_universe VALUES ("
            + ",".join(["?"] * 37) + ")",
            (
                p["player_id"],
                p["name"],
                current_club,
                str(current_club_id) if current_club_id else None,
                league_display,
                PL_LEAGUE_ID,
                None,                   # primary_position
                p["sub_position"],
                p["age"],
                str(p["date_of_birth"]) if p.get("date_of_birth") else None,
                p["current_tm_value_eur"],
                str(contract_end) if contract_end else None,
                None,                   # last_fee_paid_eur
                None,                   # last_fee_paid_date
                None,                   # minutes_last_18m
                None,                   # appearances_last_18m
                None,                   # minutes_available_18m
                None,                   # minutes_share_pct
                None,                   # agency
                p.get("foot"),
                p.get("height_cm"),
                None,                   # nationality
                1,                      # right_priced (NULL fee = academy passes)
                None,                   # finished_product (unknown)
                contract_leveraged,
                bucket_for(p["sub_position"]),
                None,                   # sellability_score — set by 09
                parent_club_name,
                str(parent_club_id),
                p["on_loan"],
                "pl_squad_full",
                str(snapshot),
                None,                   # sellability_status — set by 09
                loan_end_str,
                None,                   # scisports_player_id — set by 29
                0,                      # parent_club_recently_relegated — set by 19
                1.0,                    # mandate_priority_multiplier — set by 19
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest full PL squads from TM")
    parser.add_argument("--validate-only", action="store_true",
                        help="Test the cookie and exit")
    parser.add_argument("--single-club", type=str, default=None,
                        help="Scrape one club only (substring match on name)")
    args = parser.parse_args()

    cookie = load_cookie()

    if args.validate_only:
        ok = validate_cookie(cookie)
        sys.exit(0 if ok else 1)

    print("Validating cookie...")
    if not validate_cookie(cookie):
        sys.exit(1)
    print()

    snapshot = config.SNAPSHOT_DATE
    leverage_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_LEVERAGED_YEARS)
    pl_clubs = discover_pl_clubs()

    if args.single_club:
        needle = args.single_club.lower()
        pl_clubs = [c for c in pl_clubs if needle in c["name"].lower() or needle in c["slug"]]
        if not pl_clubs:
            sys.exit(f"No PL club matches '{args.single_club}'")

    print(f"Scraping {len(pl_clubs)} PL club(s)...")
    print()

    with sqlite3.connect(config.SQLITE_FILE) as con:
        existing_pids = set(
            r[0] for r in con.execute("SELECT player_id FROM player_universe").fetchall()
        )

    total_squad = 0
    total_loans = 0
    total_inserted = 0
    total_skipped = 0
    per_club: list[tuple[str, int, int, int]] = []
    all_players: list[dict] = []

    with sqlite3.connect(config.SQLITE_FILE) as con:
        for club in pl_clubs:
            cid, cslug, cname = club["club_id"], club["slug"], club["name"]
            print(f"--- {cname} (id={cid}, slug={cslug}) ---")

            try:
                squad_html = fetch_squad_page(cid, cslug, cookie)
                resolved_name, squad_rows = parse_squad(squad_html, cid)
            except RuntimeError as e:
                print(f"    ! FATAL: {e}")
                sys.exit(1)
            except Exception as e:
                print(f"    ! squad page error: {type(e).__name__}: {e}")
                resolved_name, squad_rows = cname, []
            print(f"    squad: {len(squad_rows)} players")
            total_squad += len(squad_rows)

            try:
                loans_html = fetch_loans_out_page(cid, cslug, cookie)
                loan_rows = parse_loans_out(loans_html, cid, resolved_name)
            except RuntimeError as e:
                print(f"    ! FATAL: {e}")
                sys.exit(1)
            except Exception as e:
                print(f"    ! loans page error: {type(e).__name__}: {e}")
                loan_rows = []
            print(f"    loans-out: {len(loan_rows)} players")
            total_loans += len(loan_rows)

            seen: set[int] = set()
            club_inserted = 0
            for p in squad_rows + loan_rows:
                pid = p["player_id"]
                if pid in seen:
                    continue
                seen.add(pid)
                all_players.append({**p, "parent_club": resolved_name, "parent_club_id": cid})
                if pid in existing_pids:
                    total_skipped += 1
                    continue
                ok = insert_player(con, p, cid, resolved_name, snapshot, leverage_cutoff)
                if ok:
                    club_inserted += 1
                    existing_pids.add(pid)
                else:
                    total_skipped += 1
            total_inserted += club_inserted
            per_club.append((cname, len(squad_rows), len(loan_rows), club_inserted))
            print(f"    → inserted {club_inserted}, skipped {len(seen) - club_inserted}")

        con.commit()

    bar = "─" * 80
    print()
    print(bar)
    print("PL Full Squad Ingestion — Summary")
    print(bar)
    print(f"  Clubs scraped:          {len(per_club)}")
    print(f"  Squad-page players:     {total_squad}")
    print(f"  Loans-out players:      {total_loans}")
    print(f"  New rows inserted:      {total_inserted}")
    print(f"  Skipped (existing):     {total_skipped}")
    print()
    print(f"  {'club':40s} {'squad':>6s} {'loans':>6s} {'new':>5s}")
    for name, sq, lo, ins in per_club:
        print(f"  {name[:40]:40s} {sq:>6d} {lo:>6d} {ins:>5d}")

    if args.single_club:
        print()
        print("Players found:")
        print(f"  {'#':>3s}  {'name':30s} {'position':18s} {'age':>4s} {'MV':>8s} {'contract':>12s} {'loan':>5s}")
        for i, p in enumerate(all_players, 1):
            mv_str = f"€{p['current_tm_value_eur']/1e6:.1f}m" if p.get("current_tm_value_eur") else "—"
            ce_str = str(p["contract_end_date"]) if p.get("contract_end_date") else "—"
            loan_str = p.get("loan_club_name", "")[:20] if p["on_loan"] else ""
            print(f"  {i:>3d}  {p['name'][:30]:30s} {(p['sub_position'] or ''):18s} "
                  f"{p['age'] or '':>4} {mv_str:>8s} {ce_str:>12s} {loan_str}")


if __name__ == "__main__":
    main()
