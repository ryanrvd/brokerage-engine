# Yatin Matcher — project briefing

A brokerage matcher commissioned by Yatin Patel, executed by RV Corp. Surfaces 17-24 year-old finished-product footballers in the €20-40m transfer band where selling clubs face structural pressure and buying clubs have validated demand. Output: Brokerage Opportunities workbook + Replenishment Leads workbook (by-product) + Streamlit multi-view app for interrogation.

This file briefs a fresh session. Assume the reader has the build spec PDF available but no conversation history.

---

## Project structure

```
yatin-matcher/
├── config.py                              # All tunables (age band, value band, leagues, snapshot date)
├── BrokerageWorkbook.xlsx                 # User-facing deliverable (sheet-by-sheet over Days 2-9)
├── db/yatin.db                            # SQLite — canonical project storage
├── data/
│   ├── transfermarkt-datasets.duckdb      # dcaribou's weekly snapshot (read-only)
│   ├── tm_cache/                          # Scraped HTML + JSON (transfer history, profiles, squads)
│   ├── market_maps/                       # 8 manual .xlsx downloads of the Google Sheets demand maps
│   ├── manual_flags.xlsx                  # Day 4: structured checklist (was manual_flags.csv pre-Day 4)
│   ├── manual_flags.csv                   # Legacy CSV — replaced by xlsx but kept for migration safety
│   ├── manual_league_overrides.csv        # Day 4: hand-curated league corrections (promotion/relegation lag)
│   └── scisports_ratings.xlsx             # Manual CA/PA ratings layer (Sci Sports source); maintained by reconcile_scisports.py
├── scripts/
│   ├── 01_download.py                     # Pull dcaribou DuckDB from R2
│   ├── 02_init_schema.py                  # Reset SQLite tables (player_universe, club_pressure, senior_roster)
│   ├── 03_build_universe.py               # Filter dcaribou → top-tier player_universe
│   ├── 04_summary.py                      # Diagnostic prints
│   ├── 05_excel_export.py                 # Sheet 2 "Player Universe"
│   ├── 06_scrape_second_tiers.py          # Scrape 5 second-tier leagues for player_universe
│   ├── 07_extend_roster.py                # Build senior_roster (dcaribou + per-club squad scrape)
│   ├── 08_compute_pressure.py             # 5-component must-sell score per club; reads manual_flags.xlsx
│   ├── 09_compute_sellability.py          # Sellability per player + top_3_likely_to_move
│   ├── 10_export_pressure_sheets.py       # Sheets 3 ("Seller Pressure") and 4 ("Sellable Assets")
│   ├── 11_patch_loans.py                  # Detect "On loan from X" via TM profile HTML
│   ├── 13_scrape_fees.py                  # Backfill last_fee_paid_eur via TM transferHistory JSON
│   ├── 15_define_demand_schema.py         # Day 4: schema for 4 map_* demand-side tables
│   ├── 16_load_market_maps.py             # Day 4: load 8 manual workbooks → map_* tables (with cross-league fallback matcher)
│   ├── 17_export_demand_sheets.py         # Day 4: Sheets 5/6/7 (Live Demand, Live Supply, Demand Map Mirror)
│   ├── 18_manual_flags_excel.py           # Day 4: migrate manual_flags.csv → structured manual_flags.xlsx
│   ├── 19_apply_league_overrides.py       # Day 4: cascade hand-curated league corrections across all tables
│   ├── reconcile_scisports.py             # Sci Sports: maintain data/scisports_ratings.xlsx vs current cohort
│   ├── load_scisports_ratings.py          # Sci Sports: push CA/PA from xlsx → SQLite player_ratings table
│   └── _position_buckets.py               # Shared TM sub_position → 10-bucket mapping
└── requirements.txt                       # duckdb, certifi, openpyxl, bs4, lxml
```

Numbered scripts run in order, idempotently. Skip number = no longer needed (12 was reserved for a state-audit script that's now ad-hoc SQL; 14 reserved/unused).

---

## Leagues covered (19)

| Tier | League ID | Country | Source |
|---|---|---|---|
| 1 | GB1 | England (Premier League) | dcaribou |
| 1 | ES1 | Spain (La Liga) | dcaribou |
| 1 | IT1 | Italy (Serie A) | dcaribou |
| 1 | L1 | Germany (Bundesliga) | dcaribou |
| 1 | FR1 | France (Ligue 1) | dcaribou |
| 1 | PO1 | Portugal (Primeira Liga) | dcaribou |
| 1 | NL1 | Netherlands (Eredivisie) | dcaribou |
| 1 | BE1 | Belgium (Pro League) | dcaribou |
| 1 | TR1 | Türkiye (Süper Lig) | dcaribou |
| 1 | DK1 | Denmark (Superliga) | dcaribou |
| 1 | SC1 | Scotland (Premiership) | dcaribou |
| 1 | GR1 | Greece (Super League) | dcaribou |
| 1 | SA1 | Saudi Arabia (Pro League) | dcaribou — **relaxed_minutes** (no appearances) |
| 1 | MLS1 | USA (MLS) | dcaribou — **relaxed_minutes** |
| 2 | GB2 | England (Championship) | TM scrape |
| 2 | FR2 | France (Ligue 2) | TM scrape |
| 2 | ES2 | Spain (LaLiga 2) | TM scrape |
| 2 | IT2 | Italy (Serie B) | TM scrape |
| 2 | L2 | Germany (2. Bundesliga) | TM scrape |

---

## Key decisions

### Value band: €8m–€45m (not €20m–€40m as in the brief)

**Brief**: "Focus on 20m plus transfers, but nothing over 40m."

**Why we widened the floor to €8m**: TM market values lag actual transfer fees by roughly 1.5-2×, and contract-leveraged players see their TM value depressed further. An €8m TM-valued player frequently transfers for €20m+. The floor catches players whose *transfer fee* lands in the brief band even when their *TM value* is below it.

**Why €45m ceiling**: 5m buffer above the brief's €40m to catch players sitting right at the boundary whose transfer fee would land just above.

This is a deliberate deviation from the brief's prose, owned by Ryan. The brief-compliant subset is queryable any time: `SELECT * FROM player_universe WHERE current_tm_value_eur BETWEEN 20000000 AND 40000000`.

### Filter thresholds (current state — Day 3.6)

| Tunable | Value | Notes |
|---|---|---|
| AGE_MIN / AGE_MAX | 17 / 24 | Brief: "17-24 year olds" |
| TM_VALUE_MIN_EUR / MAX | €8M / €45M | See widening rationale above |
| CONTRACT_MAX_YEARS_AHEAD | 3 | Inclusion filter: contract ≤ end of season + 3y |
| CONTRACT_LEVERAGED_YEARS | 2 | Flag: contract ≤ end of season + 2y → contract_leveraged=1 |
| MINUTES_LOOKBACK_MONTHS | 18 | 18-month window for minutes share |
| MIN_MINUTES_SHARE | 0.50 | 50% share of available club minutes (relaxed for SA1/MLS1) |

The contract cutoff is **end-of-season-aware** (via `config.end_of_season_plus`). For snapshot 2026-05-12 with CONTRACT_MAX_YEARS_AHEAD=3 → cutoff = **2029-06-30** (end of 28/29 season), not 2029-05-12. Fix went in mid-Day 3 after a 49-day off-by-one bug missed every June-30 Portuguese contract.

### Data sources

- **dcaribou/transfermarkt-datasets** (weekly DuckDB snapshot): top tiers only, ~93% NULL fees in transfers table, no loan field
- **Custom TM scraper** (`scripts/06`, `07`, `11`, `13`): polite (1.5s sleep, 30s backoff on 429/503), file-cached at `data/tm_cache/`, User-Agent set to a real browser string
- **TM transferHistory JSON endpoint** (`/ceapi/transferHistory/list/{player_id}`): undocumented but stable — fee strings, dates, loan vs permanent distinction
- **TM kader (squad) pages** for second-tier clubs: per-club roster scrape
- **TM profile HTML** for loan detection: `<a title="On loan from {Club} until {date}">` is the reliable marker

### Loan attribution (Day 3.5)

Sellability joins on **parent_club_id** (legal owner), not current_club_id. dcaribou's player rows show loan destination as current_club with no parent reference. We scrape TM profiles to detect loans and store:
- `parent_club`, `parent_club_id` — the legal owner (= current_club for non-loaned)
- `on_loan` — 1 if `parent_club_id != current_club_id`

Loan bonus: **+15 sellability** when `on_loan=1 AND finished_product=1`. Captures the "loan and showcase" structural sell pathway (option-to-buy / first-refusal mechanics). Cap at 100.

Parents outside the 19-league coverage (e.g. Shakhtar Donetsk, Bayern Munich II) show their owning-club pressure as "—" on Sheet 4 with `parent-outside-coverage` in Scoring Notes. Floor + loan bonus still apply; multiplicative branch yields 0.

### Position bucketing (10 buckets)

`scripts/_position_buckets.py`. TM sub_position → bucket:

| TM sub_position | Bucket | Notes |
|---|---|---|
| Goalkeeper | GK | direct |
| Centre-Back | CB | direct |
| Left-Back / Right-Back | LB / RB | direct |
| Defensive Midfield | DM | direct |
| Central Midfield | CM | direct |
| Attacking Midfield | AM | direct |
| Left Winger / Right Winger | LW / RW | direct |
| Centre-Forward | ST_CF | direct |
| Second Striker | ST_CF | judgment — bundled with strikers |
| Left Midfield / Right Midfield | LW / RW | judgment — modern usage |

Oversupply thresholds (used in Seller Pressure component 2): GK ≥4, CB ≥5, LB ≥3, RB ≥3, AM ≥3, LW ≥3, RW ≥3, ST_CF ≥4. **DM/CM combined rule**: both fire only when both ≥3 (a club with 3 DMs but no CMs isn't oversupplied — a 4-2-3-1 needs both pools deep).

### Seller Pressure formula (Sheet 3)

Per-club score 0-100, five weighted components:

| # | Component | Weight | Source |
|---|---|---|---|
| 1 | Contract leverage | 25% | senior_roster minutes-weighted share of leveraged contracts (headcount-weighted for second tiers) |
| 2 | Squad oversupply | 20% | count of buckets over threshold / 10 × 100 |
| 3 | Net spend | 20% | `clubs.net_transfer_record` parsed from dcaribou; negative = net buyer, normalised within league |
| 4 | Manager change flag | 15% | manual, from `data/manual_flags.csv` |
| 5 | Public must-sell flag | 20% | manual, from `data/manual_flags.csv` (FFP/PSR/parachute/parent stress) |

### Sellability formula (Day 5 — additive rebalance)

```
finished_product_value = 1.0 (true) | 0.5 (NULL / unknown) | 0.0 (false)
player_quality = (right_priced + contract_leveraged + finished_product_value) / 3

additive   = player_quality × 50 + (parent_club.total_pressure_score / 100) × 50
floor      = contract_leveraged × finished_product_value × 50    ← Bosman safety net
loan_bonus = 15 if (on_loan=1 AND finished_product=1) else 0

sellability = min(100, max(additive, floor) + loan_bonus)
```

**Why additive (Day 5 change from Day 3.6's multiplicative form):** the earlier formula `quality × (pressure/100) × 100` made pressure a multiplier on quality, collapsing scores for high-quality players at moderate-pressure clubs. A diagnostic of the Day 5 zero-match cohort surfaced 27 named sale-this-window candidates (Tzolis, Akliouche, Verbruggen, Kubo, Lukeba, Schade, Beier, Svensson, Pavlovic, etc.) all stuck at sellability 13-30 — below the score floor — because their parent clubs aren't in crisis even though the players are obvious moves. Football's transfer market does not gate on contract-leveraged status; a good bid moves a player whose owner needs cash or wants a sale. The additive form gives quality and pressure each up to 50 points, honestly reflecting independent contribution.

**Why equal-third on the three flags inside `quality`**: maps cleanly to the brief's three player criteria (right price, not over-locked-in, finished product). All three are first-order brief requirements — bespoke weighting would be editorialising.

**Why right_priced is excluded from the floor**: the floor is the Bosman-risk safety net. A leveraged + playing-well player is sellable even at a low-pressure club because their contract is running out. That logic is independent of whether the owning club is stuck above water on the original fee.

**Why the floor scales to 50 (was 20)**: the floor was previously calibrated against multiplicative scores that could reach 100; in the additive form the headline shape produces 30-80 for typical brokerage candidates, so the Bosman safety net needs to match.

**Why the loan bonus is independent of the floor**: a player loaned out and performing is signalling sell-readiness via behaviour — orthogonal to contract Bosman risk.

### Match scoring (Day 5)

Day 5's match engine (`scripts/22_match_engine.py`) scores `(player, buyer_request)` pairs that survive position-bucket / budget / league-tier / side filters. **Sheet 1 surfaces only the move; commercial pricing judgement is applied by Yatin on review** — no commission/sell-on/lifecycle columns are exposed anywhere.

```
sellability_term  = player.sellability_score / 100
demand_intensity  = 1.00 (Agent/YES) | 0.85 (Agent/other-validated_by) |
                    0.60 (Intel/NO) | 0.50 (NULL/NULL)
budget_fit        = max(0, 1 - (player_tm × TM_TO_FEE_MULTIPLIER / buyer_max_fee))
wage_feasibility  = 1.0 confirmed | 0.7 unknown | 0 infeasible    # constant 0.7 today

match_score (0..1) = sellability_term × demand_intensity × budget_fit × wage_feasibility
```

Top 3 matches per player kept; Sheet 1 sorted by `match_score` desc.

**`config.tm_to_fee_multiplier()`** is an internal scoring constant, also surfaced on the Player View detail page as the **Estimated Transfer Value** panel (with the per-player band + a "methodology" expander linking back to this section). The lift is **tiered** because the TM-vs-realised-fee gap is non-uniform across the value band:

| TM band | Multiplier | Rationale |
|---|---|---|
| < €15m | 2.0× | Low-TM contract-leveraged prospects can sell at 2-3× TM (the original value-band widening rationale) |
| €15-25m | 1.5× | Mid-band, moderate lift |
| ≥ €25m | 1.2× | Established players sell close to TM (recent real-world transfers: Wirtz 0.9×, Olise 1.1×, Mbeumo 1.5×) |

### Calibration constants — single source of truth

All transfer-fee / brokerage-economics tunables are centralised in `config.py`. Editing the constants there propagates everywhere (matcher, zero-match diagnostic, Player View Estimated Transfer Value panel). Inline duplication is forbidden — every consumer must `import config` and call the named constant or function.

| Constant | Location | Value (Stage 1) | Stage 2 calibration path |
|---|---|---|---|
| `TM_TO_FEE_BANDS` / `tm_to_fee_multiplier(tm_eur)` | `config.py:107-120` | Tiered 2.0× / 1.5× / 1.2× as table above | Empirical per-league multipliers from dcaribou `transfers` × `player_valuations`. See BACKLOG. |
| `MIN_BROKERAGE_FEE` | `config.py` | €15M | Static threshold; revisit only if buyer-cohort behaviour changes meaningfully |
| `budget_fit` plateau ceiling | `scripts/22_match_engine.py:172` | `2.0 × indicative_fee` | Static — defensibility checked against Wirtz / Olise / Mbeumo benchmarks |

**Why the multiplier is flat-by-band today** (Stage 1 simplification): we don't yet have calibrated per-league pairs of (TM at time of sale, realised fee) within our 19-league coverage. The tiered band approach was a defensible compromise — empirically the gap shrinks as TM rises (low-TM prospects can 2-3× their valuation in deals; established players sell close to TM). Stage 2 will replace the three-tier static structure with a per-league multiplier derived from historical dcaribou transfers — see BACKLOG entry "Per-league TM-to-fee multiplier calibration".

Empirically defensible: a flat 2.0× was too aggressive for high-TM players and silently killed legitimate matches (e.g. El Khannouss at TM €30m × 2.0 = €60m indicative would only have ~3 affordable buyers; with 1.2× → €36m indicative → 8+ affordable buyers).

The **budget filter is a `MIN_BROKERAGE_FEE = €15M` floor** — any club spending ≥€15m on a single signing is a credible brokerage candidate regardless of player TM. This deliberately allows "stretchers": buyers whose max_fee is below the player's indicative fee but above €15m enter scoring and earn partial budget_fit via the ramp curve below. Clubs with max_fee under €15m never surface, regardless of position need.

**`budget_fit` curve (two-stage):**

```
max_fee < €15m                          → 0     (not a credible buyer)
€15m ≤ max_fee < 2 × indicative_fee     → linear ramp 0 → 0.80
max_fee ≥ 2 × indicative_fee            → 0.80  (plateau — clearly affordable)
```

The €15m floor came from Ryan's Day 5 framing: clubs that can stretch budgets vs last year start showing real intent at €15m+. The plateau at 0.80 prevents PL big-budget dominance (Liverpool €145m and Newcastle €75m both score 0.80 on a €12m TM player — both can clearly afford the €24m indicative fee).

**League-tier hierarchy** (allow/exclude filter — not a scoring term). Lateral or upward moves only; Tier D self-contained.

| Tier | Leagues |
|---|---|
| A | GB1, ES1, IT1, L1, FR1 |
| B | PO1, NL1, BE1, TR1, GB2, FR2, ES2, IT2, L2 |
| C | DK1, SC1, GR1 |
| D | SA1, MLS1 |

**Demand-side input (UNION of two sources):**

| Source | Built by | Demand-intensity weight | Rationale |
|---|---|---|---|
| `map_club_requests` (explicit) | `scripts/16_load_market_maps.py` from 8 manual Google Sheets | 1.00 (Agent/YES), 0.85 (Agent/other), 0.60 (Intel/NO), 0.50 (NULL/NULL) | High-signal — agent or intel verified |
| `inferred_club_requests` (synthetic) | `scripts/21_infer_demand.py` from `senior_roster` thinness | 0.40 | Lower-signal — derived from squad-gap heuristic |

Inferred demand fires for a (club, bucket) pair when active headcount ≤ a "thin" threshold (GK ≤1, CB ≤2, LB ≤1, RB ≤1, CM ≤2, LW ≤1, RW ≤1, ST_CF ≤2 — DM/AM skipped because they're formation-dependent). Budget proxy uses `map_club_overview.highest_transfer_fee_2526_eur` for mapped clubs (so PSV/Ajax/Benfica/Sporting/Porto/Braga get their real spending power) and a conservative tier default for unmapped clubs (Tier A €25m, Tier B €5m, Tier C €3m, Tier D €15m). Inferred is suppressed where explicit demand for the same (club, bucket) already exists.

This lifts buyer-side coverage from 10 demand-mapped leagues to all 19, and from 196 buyer clubs to 370. Clubs without genuine spending power (most of NL/POR/BEL below the top 3-4) self-filter via budget_fit → 0 → match_score → 0 → cut by the score floor.

**Score floor:** `match_score >= 10` is required for a (player, buyer) pair to enter the `matches` table. Suppresses the long tail of "clears the four filters but tactically marginal" candidates (e.g. Kalimuendo→Arsenal at 5.5 before the multiplier re-cal — now around 13-17, which is acceptable mid-tier).

### Manual flag pattern (Day 4 — migrated CSV → xlsx)

`data/manual_flags.xlsx` is the structured checklist Ryan edits to set `manager_change_flag` and `public_must_sell_flag` per club. Generated by `18_manual_flags_excel.py`; read by `08_compute_pressure.py` (falls back to legacy `manual_flags.csv` if xlsx missing). 354 clubs, sorted by `current_base_pressure_score` descending so the highest-impact reviews surface first. Columns: club_id, club, league, current_base_pressure_score (= 0.25·CL + 0.20·SO + 0.20·NS, before manual flags), current_manager_name (from `map_club_overview`), manager_tenure_months (NULL today), `manager_change_check` prompt, `manager_change_flag` (user input 0/1, data-validated), `public_must_sell_check` prompt, `public_must_sell_flag` (user input 0/1), notes_source, last_reviewed (auto-stamped to today's date when a flag value changes between refreshes).

Flag-editable cells are pale yellow; read-only cells are pale grey. Workbook is the *output* — flags don't write back to it.

### Manual league overrides (Day 4)

`data/manual_league_overrides.csv` is a small hand-curated table that fixes clubs whose league has changed since the underlying data sources (dcaribou snapshot, TM second-tier scrape, and the manual workbooks) were last refreshed — typically post-promotion/relegation. Columns: `club_id, club_name, override_league_id, override_league_display, reason`.

Seeded on first `19_apply_league_overrides.py` run with 4 known cases:

| club_id | club | from → to | reason |
|---|---|---|---|
| 990 | Coventry City | GB2 → **GB1** | promoted to Premier League for 26/27 |
| 677 | Ipswich Town | GB2 → **GB1** | promoted to Premier League for 26/27 |
| 543 | Wolverhampton Wanderers | GB1 → **GB2** | relegated to Championship for 26/27 |
| 1132 | Burnley FC | GB1 → **GB2** | relegated to Championship for 26/27 |

`19_apply_league_overrides.py` cascades the corrections across every table that carries a league_id: `player_universe`, `senior_roster`, `club_pressure`, `map_club_overview`, `map_club_tracker`, `map_club_requests` — and re-derives `map_demand_signal`. Idempotent. To add/remove an override: edit the CSV and re-run 19.

**Pipeline position**: 19 runs twice — once before `08_compute_pressure.py` (so 08 computes pressure with corrected leagues and applies the second-tier "external" net_spend rule to the right cohort), then again after `16_load_market_maps.py` (to override the workbook's league tags on the map_* tables). See "Re-running the pipeline" below for the canonical order.

### Loader matching (Day 4)

`16_load_market_maps.py` resolves each workbook club to a TM `club_id` via length-weighted token-overlap matching after unicode-normalisation and stopword stripping (see `STOPWORDS` and `MANUAL_NAME_OVERRIDES` in that file). Two-stage matcher:

1. **In-league match** (preferred): score every DB club in the workbook's tagged league; accept the unique top scorer.
2. **Cross-league fallback**: if no in-league match (or in-league candidates tie), search every other league with a stricter threshold — every workbook token must be a full-token match (no substring fallback). This catches point-in-time disagreements where the workbook tags a club to one league but the DB has it in another (e.g. Schalke at workbook L1 but DB L2).

196/197 workbook clubs match end-to-end. The single residual miss is ADO Den Haag (NL1 in workbook Club Tracker) — no ADO row exists anywhere in our DB, so cross-league fallback can't help. Its 3 Club Tracker rows stay `club_id=NULL`; sheets render fine using workbook names. Add ADO via a fresh TM scrape post-Yatin if it becomes load-bearing.

### Club Display Names (Day 7)

The data layer keeps the official names from dcaribou as-is (`club_pressure.name` = "Sport Lisboa e Benfica", "Verein für Leibesübungen Wolfsburg", etc.). All **user-facing surfaces** render the short form via a display layer. Single source of truth: `BrokerageWorkbook.xlsx` → tab **"Club Display Names"** (sheet 7, between Demand Map Mirror and Zero-Match Diagnostic).

**Schema** (6 columns):

| Column | Editable | Purpose |
|---|---|---|
| `club_id` | RO | join key |
| `official_name` | RO | from `club_pressure.name` |
| `league` | RO | full league name |
| `display_name` | **yes** | the short form everything renders |
| `auto_or_manual` | **yes** | `auto` (regen each run) / `manual` (preserved verbatim) |
| `notes` | RO | "low confidence — review" flag for the ~38 cases where auto-stripping is uncertain |

**Logic** lives in `kill_list.py`'s sibling at project root: `club_display.py`. Two layers:
1. **EXPLICIT_NAMES** — hand-curated dict of ~200 mappings encoding football-domain knowledge:
   - "Real" stays (Real Madrid, Real Sociedad, Real Betis, Real Oviedo, Real Valladolid) — except Mallorca (English media universally drops "Real")
   - "1. FC" stays (1. FC Union Berlin, 1. FC Köln)
   - "VfB Stuttgart" / "VfL Wolfsburg" / "TSG Hoffenheim" / "Hamburger SV" — German football-media canonical forms
   - "Inter Milan" / "AC Milan" / "AS Roma" / "Napoli" — Italian football usage
   - "PSG" / "Marseille" / "Nice" / "AS Monaco" — French
   - "Benfica" / "Sporting CP" / "Braga" — Portuguese
   - "Wolves" / "Tottenham" / "Brighton" / "Bournemouth" — English (AFC drops on Bournemouth + Ajax)
   - "AEK Athens" / "PAOK" / "Olympiakos" — Greek (transliterations)
2. **`_auto_simplify`** — conservative suffix/prefix stripping for the rest. Drops `S.A.D.`, `S.p.A.`, `Football Club`, `Voetbalvereniging`, `Spor Kulübü`, etc. when they appear at name boundaries. Protects "1. FC", "Real", "Atlético", "Athletic" prefixes from stripping.

**Read-preserve-write** (same as Kill List): script 23 reads the tab, regenerates `auto` rows (so future explicit-mapping additions propagate), preserves `manual` rows verbatim, writes back. Other workbook scripts (05, 10, 17, 24, 25) load the display map at startup and use `club_display.display_for(club_id, fallback)` for each cell write.

**Streamlit** reads live via `app/labels.py`'s `club_display_name(club_id)` helper, which calls `club_display.load_display_map()` (cached). Edits to the tab propagate to the app within the cache TTL (60s) without re-running the pipeline. Search box matches **both** official name and display name so "Stuttgart" finds "VfB Stuttgart" and "Verein für Bewegungsspiele Stuttgart 1893" alike.

**League codes** rendered as full names everywhere via `app/labels.LEAGUE_DISPLAY_NAMES` (alias of `LEAGUE_NAMES`): GB1 → "Premier League", L1 → "Bundesliga", etc.

### Player Display Names (Day 7)

Same architecture as Club Display Names. Logic lives in `player_display.py` at project root. Tab **"Player Display Names"** sits at sheet 8, between Club Display Names and Zero-Match Diagnostic. Schema mirrors Club Display Names with a `current_club` context column (rendered through Club Display Names so the spreadsheet reads naturally).

**Football-domain rules:**
- Anglo middle-name strip (3+ tokens → first + last): Harrison James Burrows → Harrison Burrows, Cameron Desmond Archer → Cameron Archer
- **Nicknames for young British players** where media universally uses the short form: Joseph Paul Gelhardt → Joe Gelhardt; Thomas Glyn Doyle → Tommy Doyle; Thomas Christopher Cannon → Tommy Cannon; Tommy Daniel John Conway → Tommy Conway
- **"Known by middle name"** edge case: Memeh Caleb Okoli → Caleb Okoli (Leicester signed him as Caleb)
- **Compound first names preserved** (José Ángel, Juan Carlos, João Pedro etc.): José Ángel Carmona stays intact
- **Hyphenated names preserved** (first or last): Jan-Carlo Simić, Jaden Philogene-Bidace
- **Surname-particle names preserved** (Dutch van/den/de, Portuguese da/de/dos, Spanish de): Zeno Van Den Bosch, Sepp van den Berg, Lucas Da Cunha all stay intact
- **Spanish dual-surname drop** (mother's surname): Sergio Arribas Calvo → Sergio Arribas
- **Greek first names stay full** per spec: Konstantinos Tzolakis, Giannis Konstantelias
- **Asian names intact**: Takefusa Kubo
- **Ordering quirks** handled per case: Issahaku Abdul Fatawu → Abdul Fatawu

**Wiring**: rendered everywhere a player name appears — Sheet 1 (script 23), Player Universe / Sheet 2 (script 05), Top 3 Likely to Move text on Seller Pressure / Sheet 3 + Sellable Assets / Sheet 4 (script 10), Zero-Match Diagnostic (script 25), and the Kill List tab (so "Caleb Okoli" not "Memeh Caleb Okoli"). Streamlit reads live via `app/labels.player_display_name(player_id)` and the search box matches both official and display forms.

### Kill List — single source of truth (Day 6)

The Kill List is the canonical exclusion mechanism for both Sheet 1 (workbook) and the Streamlit Targets view. Two ways to add a player to it:

**(a) Manual entries** — type a `player` name + `reason why` into the **Kill List** tab of `BrokerageWorkbook.xlsx`. Use for exclusions the matcher can't see (free transfers, exclusive mandates with other agencies, off-market intel, etc.). Manual rows are preserved verbatim across pipeline runs.

**(b) Agency rules** — list of blocked agencies in `data/blocked_agencies.csv` (one agency per row). Any player whose `agency` field contains a listed string (case-insensitive substring) is auto-killed. The Kill List tab gets a corresponding row with `source = agency:<name>`. Auto rows are regenerated every pipeline run, so agency changes propagate automatically. Default seeds: CAA Base, Gestifute, Gol International, Unique Sports Group.

**Logic lives in `kill_list.py` at the project root** — shared by both `scripts/23_export_brokerage_sheet.py` (writes Sheet 1 and the tab) and `app/db.py` (reads live for the Streamlit Targets / Excluded Players pages). Single function: `compute_kill_list_state(con)`.

**Kill List tab schema** (three columns):

| player | reason why | source |
|---|---|---|
| Zeno Van Den Bosch | Going free — Bosman | manual |
| Arouna Sangante | Contract end June 2026 | manual |
| José Ángel Carmona | Signed exclusive mandate with USG | manual |
| (auto-derived players) | Agency: Unique Sports Group (does not collaborate) | agency:Unique Sports Group |

**Manual + auto dedup**: when an agency rule fires for a player who's already listed manually (e.g. Carmona = USG), the manual entry wins and no duplicate row is written. So if you want a different reason text on a USG-tagged player, just list them manually.

**Fuzzy matching for manual entries**:
1. Exact normalised match (lowercase, diacritics stripped, punctuation removed)
2. Token-subset (every kill-list token appears in the player's name — handles surname-only entries like "Tzolis")
3. SequenceMatcher ratio ≥ 0.85 fallback

Zero-match or ambiguous (multi-hit) manual entries are **not applied** and surface as warnings at the end of the run (and in an expander on the Excluded Players page). Under-drop bias by design — never silently kill a legitimate candidate.

**Legacy `data/player_overrides.xlsx`** — superseded by the Kill List. The file may still exist on disk but is no longer consulted by anything. Safe to delete.

---

## Confirmed structural signals (project intent affirmed)

The system is designed to surface market inflection points — moments where supply and demand at the current price level don't clear. Each finding below is a **signal worth acting on**, not a calibration problem to fix. When the filters expose a gap of this kind, the gap *is* the deliverable.

### Striker (ST_CF) supply gap — ~10× demand-to-supply imbalance (Day 4)

| | |
|---|---|
| Demand | 148 clubs requesting an ST_CF across the 10 demand-mapped leagues |
| Supply | 18 ST_CF in the player universe (19 leagues, post-filter) |
| Ratio | ~8:1 club-demand to player-supply; sellability-eligible supply tighter still |

**Filter cascade for ST_CF in top-tier dcaribou (14 leagues)**:

```
Raw strikers in top tiers                  4,260
  After age 17–24                            942   (−78%)
  After value €8m–€45m                        50   (−95%)
  After contract ≤ 2029-06-30                 30
  After minutes ≥ 50%                         13
  After right_priced (mv ≥ last_fee)          13
  + 5 from second-tier scrape (GB2/FR2)       18  total in player_universe

Strikers excluded SOLELY by €45m ceiling:     0
Strikers above €45m (age-only filter):        4 (Ekitiké, Woltemade, João Pedro, Sesko)
                                                — ALL fail contract (2030–2033)
```

**Why this is project-intent-affirming**: raising the €45m ceiling would yield **zero** additional sellable assets. The 4 young strikers above the ceiling are all contract-locked at top clubs through 2030–2033. The shortage is genuine market scarcity of contract-leveraged, finished-product strikers in the €8–45m band — exactly the kind of inflection point the brokerage layer is built to spot. ST_CF candidates that *do* surface (e.g. Højlund on loan from Manchester United, Kalimuendo on loan from Forest, Abline at Nantes) are disproportionately high-leverage outputs of the system precisely because of this imbalance.

**Operationally**: treat the ST_CF gap as a feature flag for Day 5 matching — strikers that pass should be ranked at the top of the seller-pressure × buyer-fit output, since the supply scarcity makes any single match unusually high-value.

---

## Known data limitations carried forward

- **22% of universe (36/163) has NULL fee data** after the Day 3.6 backfill. These are true academy graduates (TM has no transfer record) and pass `right_priced=1` by the academy-graduate rule. Correct treatment — academy products have no acquisition cost to recoup.
- **44 of 163 (27%)** have `finished_product=NULL` — the second-tier scraped cohort. No minutes data was scraped on Day 2; oversupply scoring for those clubs uses headcount, not minutes-weighting (flagged in `scoring_basis`).
- **2 leagues with relaxed minutes filter** (SA1, MLS1): dcaribou has no appearances data → finished_product defaults to 0, minutes share defaults to 0%. They pass the inclusion filter on the relaxed-minutes rule but score low on finished_product.
- **2 loan parents outside coverage**: Shakhtar Donetsk (UA1, not in our 19) and FC Bayern Munich II (3. Liga). Their players (Sudakov, Nkili) show `—` pressure on Sheet 4 with `parent-outside-coverage` notes.
- **TM market values can be stale** for very recent transfers — Victor Froholdt shows €400k on dcaribou despite a 2025 Copenhagen→Porto move. Dataset refreshes weekly; affected players cycle out quickly.
- **dcaribou top-tier rosters include loanees/youth/academy** (Crystal Palace had 105 senior_roster rows before filtering). Squad oversupply specifically filters to `minutes_last_18m > 0` to get the active first-team pool.
- **Extra-time matches** inflate minutes share above 100% (denominator counts 90 min per game). Minor — doesn't affect the 50% threshold.

---

## Known issues

### `manual_flags.xlsx` ↔ `club_pressure` divergence (Day 4.5)

**`club_pressure` is the canonical source of truth for manual flag values.** Not the xlsx. If the two ever diverge, trust the DB and restore the xlsx from it.

A bug was observed once during Day 4.5: after the user manually flipped 44 `public_must_sell_flag` cells in `manual_flags.xlsx`, a subsequent `scripts/18_manual_flags_excel.py` run silently failed to fire `manual_override` detection on those rows and overwrote the xlsx with auto-only values. `club_pressure` (populated earlier in the same pipeline run by `08_compute_pressure.py`) had captured the correct 49 flagged clubs and was the recovery source. The bug was **not reproducible** from the post-restore state; root cause not isolated.

**Defence (Day 4.5+):** `scripts/18_manual_flags_excel.py` now writes to a staging file (`data/manual_flags.staging.xlsx`), re-reads it, and reconciles every `manager_change_flag` and `public_must_sell_flag` value against `club_pressure`. On divergence the script:

1. Refuses to overwrite `data/manual_flags.xlsx` (the prior version stays as fallback).
2. Writes a timestamped log to `logs/script_18_reconciliation_<ts>.log`.
3. Keeps the staging file at `data/manual_flags.staging.xlsx` for inspection.
4. Exits with code 2.

This doesn't fix the underlying bug — it guarantees you'll see the next occurrence immediately, with the divergence captured for diagnosis.

**Day 5 hardening (additive):** the reconciliation now layers four count-based checks on top of the per-row diff (xlsx vs `club_pressure` for both flag-1 counts, and xlsx vs in-memory `build_rows` firings for both `manual_override` basis counts). And a rolling `.bak` of the live xlsx is captured at the start of every run at `data/manual_flags.xlsx.bak`, refreshed on each successful run — independent of the staging-file mechanism. Together: the live xlsx is never touched on failure, the previous live state is preserved as `.bak`, and the reconciliation surfaces both row-level *and* top-line divergences. Re-stating the original incident: a one-time Day 4.5 bug stripped 44 manual flag overrides from the xlsx. Could not reproduce. If it recurs, the script will refuse to overwrite and surface the diff.

**Restoration path** if divergence is detected at runtime:

1. Inspect the log (`logs/script_18_reconciliation_<ts>.log`) to understand which clubs and flags diverged, and which top-line counts failed.
2. The pre-run `data/manual_flags.xlsx` is your fallback — 18 left it untouched. A second copy is at `data/manual_flags.xlsx.bak` (start-of-run snapshot).
3. **Recommended (trust the DB):** the canonical source of truth is `club_pressure` in `db/yatin.db`. Re-run `08 → 18` cleanly. Script 18 preserves `manual_override` rows correctly when its inputs are consistent. (If you're recovering from a prior corruption, you may also need to one-off rewrite the xlsx from `club_pressure` to re-stamp `manual_override` on rows where the user's edit was lost — see the helper pattern used during the Day 4.5 incident.)
4. **Alternative (trust the staging file):** only if you're confident `club_pressure` is stale (e.g. 18 was run without first running 08), commit the staging file manually: `mv data/manual_flags.staging.xlsx data/manual_flags.xlsx`, then re-run `08 → 09 → 10` to propagate.

---

## Build preferences (Ryan's)

- **Explain everything in plain English.** Treat me as a smart non-developer colleague — I'm a football agent, not a coder. Don't assume technical context, but don't dumb things down either.
- **Confirm before money or external service requests.** Anything that hits TM at scale (≥10 requests) flag first. Same for any paid service.
- **Default to small reversible steps.** Single-file scripts, idempotent re-runs, snapshot tables before destructive changes.
- **Propose plans before code; wait for sign-off.** For non-trivial work, surface the plan (data path, key decisions, weighting choices, scope), get me to say go, then build.
- **Verify → propose → sign-off → build rhythm.** Don't skip the verify step — pulling actual data into the proposal beats guessing.
- **When I ask "why", go deeper, not shorter.** Show your working.
- **Numbered scripts** in `scripts/` (`01_…`, `02_…`, …). Each is a single-purpose, idempotent step. Skip numbers when a step is retired rather than renumber the world.
- **Push back when I drift.** If I ask for something that contradicts the brief or earlier decisions, surface the conflict explicitly — don't silently comply.
- **Memory is canonical for cross-session context.** Updates to `MEMORY.md` matter; do them when something durable is decided.

---

## Where we are now (end of Day 4)

### Done

- **Day 1**: Player universe v1 (8 top-tier leagues, filters, Sheet 2)
- **Day 2**: Expanded to 19 leagues; second-tier scrape; agency backfill; Sheet 2 polished
- **Day 3**: Senior rosters (all 19 leagues); Sheet 3 Seller Pressure (354 clubs, 5 components); Sheet 4 Sellable Asset Ledger; manual flags CSV; position-bucket mapping
- **Day 3.5** (loan patch): TM profile scrape for loan detection; parent_club / parent_club_id / on_loan columns; sellability re-routed to parent; +15 loan bonus
- **Day 3.6**: contract-cutoff bug fix (end-of-season-aware); minutes threshold restored to 50%; right-priced fee backfill via TM transferHistory JSON; right_priced re-integrated into sellability formula as equal-third with contract_leveraged and finished_product_value
- **Day 4** (this session): demand-side layer
  - 4 new tables in db/yatin.db: `map_club_overview`, `map_club_tracker`, `map_club_requests`, `map_demand_signal` — fed by 8 manual workbook downloads from Google Sheets (covers 10 of 19 leagues)
  - Sheets 5 (Live Demand Signal), 6 (Live Supply Signal), 7 (Demand Map Mirror)
  - Manual flags migrated CSV → structured xlsx (`18_manual_flags_excel.py`); pipeline reads xlsx
  - **Bug fix**: `08_compute_pressure.py` previously kept stale `league_id` on `club_pressure` rows when a club changed leagues (`INSERT OR IGNORE` skipped the update). Patched to UPDATE on every run — 55 rows relabelled in the first corrected pass. Side-benefit: per-league net_spend normalisation in component 3 now pools the correct cohort.
  - **Manual league overrides**: 4 promotion/relegation cases (Coventry, Ipswich, Wolves, Burnley) where data sources haven't caught up yet — `19_apply_league_overrides.py` cascades the corrections across all 6 league-bearing tables
  - **Loader matching**: in-league + cross-league fallback in `16_load_market_maps.py` resolves 196/197 workbook clubs to TM `club_id` (lone miss: ADO Den Haag — no DB row exists)
  - **BACKLOG**: "Maps Auto-Sync Infrastructure (Decision Required First)" added as high-priority blocker for the post-Yatin Maps build

### State today

- **player_universe**: 163 rows across 18 of 19 leagues (MLS1 empty after filters)
- **club_pressure**: 354 clubs scored; league counts align with real-world sizes (GB1=20, GB2=24, FR1=18, FR2=18, ES1=20, ES2=22, L1=18, L2=18, IT1=20, IT2=20, NL1=18, PO1=18, BE1=16, …)
- **map_club_overview**: 192 club rows (10 demand-mapped leagues)
- **map_club_requests**: 1,074 rows (Either-expanded; 1,016 distinct workbook rows mirrored on Sheet 7)
- **map_demand_signal**: 105 league×bucket aggregations
- **BrokerageWorkbook.xlsx**: 7 sheets — placeholder, Player Universe (163), Seller Pressure (354), Sellable Assets (157), Live Demand Signal (10 buckets), Live Supply Signal (10 buckets), Demand Map Mirror (1,016)
- **manual_flags.xlsx**: 354 clubs, sorted by base pressure desc; legacy `manual_flags.csv` retained for migration safety
- **manual_league_overrides.csv**: 4 entries (Coventry/Ipswich → GB1; Wolves/Burnley → GB2)
- **27 loans** detected; 2 parents outside coverage
- **127/163** with fee data after backfill; **126/163** pass right_priced

### Day 5 — matching engine (next)

Day 5 builds the seller↔buyer matching layer on top of Day 4's supply/demand tables:

1. Per-player, find clubs whose Club Requests target that player's position_bucket + side
2. Filter by budget (workbook's `max_transfer_fee_eur` ≥ player's `current_tm_value_eur` or `last_fee_paid_eur`)
3. Rank candidate matches by combined seller-pressure + buyer-fit score
4. Output: Sheet 8 "Match Candidates" — one row per (player, buying-club) pair worth surfacing

Day 6+ (TBD) follow.

---

## Re-running the pipeline

After a code change or fresh dcaribou snapshot:

```bash
.venv/bin/python scripts/01_download.py          # fresh dcaribou snapshot (only if needed)
.venv/bin/python scripts/02_init_schema.py
.venv/bin/python scripts/03_build_universe.py
.venv/bin/python scripts/06_scrape_second_tiers.py
.venv/bin/python scripts/07_extend_roster.py
.venv/bin/python scripts/19_apply_league_overrides.py    # pre-08: corrects senior_roster + player_universe before pressure is scored
.venv/bin/python scripts/08_compute_pressure.py
.venv/bin/python scripts/11_patch_loans.py
.venv/bin/python scripts/13_scrape_fees.py
.venv/bin/python scripts/09_compute_sellability.py
.venv/bin/python scripts/05_excel_export.py
.venv/bin/python scripts/10_export_pressure_sheets.py
# ── Day 4 demand-side layer ────────────────────────────────────────────────
.venv/bin/python scripts/15_define_demand_schema.py
.venv/bin/python scripts/16_load_market_maps.py
.venv/bin/python scripts/19_apply_league_overrides.py    # post-16: corrects map_* tables created by 16
.venv/bin/python scripts/17_export_demand_sheets.py
.venv/bin/python scripts/18_manual_flags_excel.py        # refreshes manual_flags.xlsx from updated club_pressure
.venv/bin/python scripts/reconcile_scisports.py          # refreshes data/scisports_ratings.xlsx against new cohort
.venv/bin/python scripts/load_scisports_ratings.py       # pulls user-entered CA/PA values into player_ratings
.venv/bin/python scripts/22_match_engine.py              # match scoring (runs AFTER Sci Sports load so future versions can incorporate CA/PA)
.venv/bin/python scripts/23_export_brokerage_sheet.py    # Sheet 1
```

All HTML/JSON cached in `data/tm_cache/`, so post-first-run reruns are instant. The full cold rerun (with fresh scrapes) takes ~7 minutes wall time. Script 19 runs twice — it's idempotent and fast; the second pass just propagates the same overrides into the freshly-loaded map_* tables.

---

## Sci Sports talent layer

User-curated CA (current ability) and PA (potential ability) ratings, manually entered and preserved across weekly TM refreshes.

- **Source of truth:** `data/scisports_ratings.xlsx`
- **Reconciliation:** `python scripts/reconcile_scisports.py` (auto-runs in the refresh pipeline above, between `18_manual_flags_excel.py` and `22_match_engine.py`)
- **SQLite loader:** `python scripts/load_scisports_ratings.py` (runs right after reconciliation)
- **SQLite table:** `player_ratings` — columns `tm_player_id` (PK, INTEGER), `current_ability` (REAL, NULL when not yet rated), `potential_ability` (REAL, NULL when not yet rated), `status` (TEXT: `pending` / `active` / `departed` / `killed`), `last_updated` (TEXT, ISO date)
- **User workflow:** after the weekly pipeline runs, open `data/scisports_ratings.xlsx`, filter `status = pending` (those are new players in the cohort awaiting a rating), fill in CA / PA from the Sci Sports dashboard, save, and re-run `load_scisports_ratings.py` (or wait for the next pipeline run, which picks up the change automatically).
- **Reconciliation rules:** new players in cohort → `pending` with blank CA/PA; existing rated players still in cohort → `active`, CA/PA preserved; rated players who dropped out of the cohort → `departed`, CA/PA preserved so a transient drop-out doesn't lose the rating; Kill List players → `killed` (still in file, flagged); `last_updated` auto-stamps only on rows whose status changed.
- **File ordering:** workbook sorted `pending → active → departed → killed`, within each band by player name, so the worklist surfaces first.
- **Scale TBD:** CA/PA stored as `REAL` with no validation today. Confirm the Sci Sports scale (1-100? 1-10? Letter grades?) when the first batch of ratings is entered; add bounds-checking to the loader once the convention is locked.
- **File-lock safety:** if the workbook is open in Excel when `reconcile_scisports.py` runs, openpyxl raises `PermissionError` and the script exits with a clear message rather than corrupting the file.
- **Full UI + match-logic integration:** deferred to a separate prompt. Today's build is schema + plumbing only — Sheet 1 / Player View / matcher do NOT consume `player_ratings` yet.
