# Backlog — parked items for after the Yatin demo

Items deliberately deferred. Each one was discussed, scoped enough to be implementable, then parked because it would (a) shift the v1 deliverable's behaviour without time to validate, (b) belong to Stage 2 of the project, or (c) require infrastructure beyond the 9-day sprint.

---

## Maps Auto-Sync Infrastructure (Decision Required First)

**Status:** parked. **Priority: HIGH — infrastructure-blocking for the Market Movement Maps build.**

**Current state (Day 4):** Demand-side market maps are manually downloaded from Google Sheets as `.xlsx` files and placed in `data/market_maps/`. `scripts/16_load_market_maps.py` reads those snapshots into the four `map_*` tables in `db/yatin.db`.

**Why this is unacceptable as a long-term solution:** the source Google Sheets are live documents updated continuously. Any download is stale on arrival. Re-downloading 8 files manually every refresh cycle is not viable beyond the Yatin demo. Stage 1 accepts the staleness as a temporary constraint; the proper Market Movement Maps build (post-Yatin) cannot.

**When the proper Market Movement Maps build begins, the FIRST decision must be how to auto-sync the maps into the pipeline.** Options to evaluate:

- **(a) gspread + Google Cloud Console with personal Google account.** Requires a service account or OAuth setup on the operator's Google account. Verify the free tier covers our read volume (~10 sheets × a few refreshes/day).
- **(b) OAuth Desktop App flow.** User-account OAuth (no service account). Token refresh handled locally. Slightly more friction at first run but no Cloud Console project to maintain.
- **(c) Apps Script auto-export from within each sheet.** Each sheet hosts a small script that exports CSV/XLSX to Drive or a webhook on edit. Most "Google-native" option; no external infra. Downside: 8 scripts to maintain.
- **(d) A third-party sync service** (Coupler, Stitch, etc.). Paid. Lowest engineering effort, ongoing cost.

This is **infrastructure-blocking** for the maps build, not a polish item. Until this is resolved, the maps build cannot meaningfully advance beyond Stage 1's manual-download workflow.

**Dependencies before unparking:** Yatin demo done; decision on which option (a–d) to commit to. The decision shape: (i) where the auth lives, (ii) refresh cadence, (iii) failure mode when a sheet is unavailable.

---

## Auto-Relegation Cascade for Weekly Refresh

**Status:** parked. **Priority: HIGH — required before sustained weekly-refresh operation post-Yatin demo.**

**Why this matters:** the build is intended to refresh weekly going forward, including across the end-of-season transition window when promotions and relegations resolve across every league we cover. Currently, league transitions are handled by **two hand-curated files** that must be edited each season:

- `data/manual_league_overrides.csv` — corrects `league_id` on clubs that have changed division since the source data was last refreshed (4 entries seeded today: Coventry/Ipswich GB2→GB1, Wolves/Burnley GB1→GB2).
- `data/parachute_payments.xlsx` — declares which clubs sit in years 1/2/3 of post-relegation parachute payments, driving `public_must_sell_flag` via `parachute_yrN` basis.

Each season transition currently requires Ryan (or a future operator) to edit both files by hand. That is not viable as a sustained weekly process. The signal these files encode is **derivable** from dcaribou's `games` table — final-table standings, season-over-season league membership — so this should be automated.

### Scope

A new script (provisional `21_derive_relegations.py`) that, after `01_download.py`, inspects dcaribou's `games` table and:

1. **Detects league transitions** by comparing club membership in each league across consecutive seasons. For each `(competition_id, club_id)` pair, derive the season-of-last-appearance. Clubs in `GB1` in season N but not N+1 are relegations; clubs not in N but in N+1 are promotions. Same logic for every league we cover.
2. **Writes / updates `data/manual_league_overrides.csv`** with the most recent transition for any club whose dcaribou league differs from where they actually start the upcoming season. Preserves any hand-curated rows (flagged via a `source` column: `'derived'` vs `'manual'`).
3. **Maintains `data/parachute_payments.xlsx`** with the rolling 3-year window for PL→Championship relegations:
   - Bottom-3 of GB1 in the most recently completed season → write as `parachute_yr1` (overwriting any prior yr1 cohort).
   - Last year's yr1 cohort → shift to yr2 *unless* the club was immediately re-promoted (then drop entirely — see Ipswich Town pattern).
   - Last year's yr2 cohort → shift to yr3 (same re-promotion guard).
   - Last year's yr3 cohort → drop (parachute window expired).
   - Preserves hand-curated `notes` / `needs_review` cells.

### Design considerations

- **End-of-season detection is the hard part.** dcaribou updates weekly. Mid-season, the games table won't yet have all matchdays played for season N. The script must detect when a season is "complete" before shifting cohorts. Proxy: when `MAX(games.date)` for the league has passed and the final-table game count equals the expected matchday total (e.g. 380 for a 20-team round-robin), the season is complete. Defer cohort shifts until then.
- **Idempotency across weekly runs.** If the season isn't complete yet, the script should be a no-op (touch nothing). If it is complete and the cohort has already been shifted, re-running should be a no-op.
- **Promotion-back guard.** A club relegated in 23/24 (entering yr1 of parachutes) and immediately promoted at the end of 24/25 exits the parachute window (Bosman-style: while back in PL they're earning PL revenue, not parachutes). The shift logic must drop these clubs rather than continue the year-counter. (Ipswich Town today is exactly this case — derived as yr2 from games data, excluded from `parachute_payments.xlsx` because `manual_league_overrides.csv` shows them back in GB1 for 26/27.)
- **Equivalent-payment leagues.** Today only PL→Championship has the declining-parachute structure. La Liga, Bundesliga, Serie A have no equivalent. Limit cohort derivation to GB1→GB2; treat other relegations as `manual_league_overrides` candidates only (no parachute side-effect).
- **Hand-curation preservation.** The script must NEVER overwrite a row marked `source='manual'`. The two files should remain ergonomic for hand-overrides (the Luton Town outside-coverage case is a hand override that the auto layer must respect).
- **Cascade dependency on script 19.** After the override CSV is updated, `19_apply_league_overrides.py` must run before `08_compute_pressure.py` (existing pipeline order). Confirm the cascade still works when overrides become numerous (e.g. all 6 PL/Championship swaps each season, plus equivalents across 10+ leagues).

### Dependencies before unparking

- Yatin demo complete (current build is frozen for that purpose; this changes data semantics).
- Confirmation that weekly refresh is the operating mode going forward (vs the current "manual one-off per season").
- Decision on whether to cover equivalent leagues for transition correction (Bundesliga, La Liga, Serie A all have promotion/relegation that affects `league_id` accuracy week-to-week even though they have no parachute equivalent).

### What "done" looks like

Re-running the full pipeline at any point in the season produces correct league assignments and parachute cohort flags without any manual file edits. The two hand-curation files become *opt-in* overrides for edge cases (loans from outside-coverage clubs, mid-season administrative relegations, etc.) rather than the primary mechanism.

---

## Post-demo Stage 2

Three high-priority items scheduled for after the Yatin demo. Each is operational / Stage 2 infrastructure rather than a v1 formula refinement.

### Weekly refresh + diff report

**Status:** parked. **Priority: HIGH — required before sustained weekly-refresh operation post-demo.**

Schedule a job (cron, GitHub Action, or local launchd) that runs every Monday morning and:

- Refreshes the dcaribou TM dataset via `01_download.py`.
- Re-runs the full pipeline (scripts `01` → end of build).
- Generates a "What changed this week" diff report covering:
  - New players entering the universe (passed all filters this week, didn't pass last week)
  - Players dropping out of the universe (passed last week, didn't pass this week)
  - Match scores that moved by more than 10 points between snapshots
  - New manager change flags raised since last refresh
  - New parachute clubs added (e.g. fresh Premier League relegation cohort)
  - New Kill List additions (manual + agency-rule-derived)
- Outputs the diff to `data/weekly_diffs/<date>.md` and surfaces the same data as a new tab in `BrokerageWorkbook.xlsx` (provisional name: **"What's Changed"**).

The manual layers (Kill List, `manual_flags.xlsx`, `parachute_payments.xlsx`) maintain themselves as Ryan edits entries — the diff job just needs to record their state at snapshot time. The **missing infrastructure piece is Maps auto-sync** — covered separately by the "Maps Auto-Sync Infrastructure" entry above. Until Maps auto-sync resolves, the weekly job either accepts stale map snapshots or pauses the demand-map portion of the refresh.

This entry overlaps with **Auto-Relegation Cascade** above and the **"Cross-sessional refresh / what changed since last week"** item in Other Deferred Decisions. Treat those as components of this job rather than separate workstreams.

**Dependencies before unparking:** Yatin demo done; confirmation that weekly refresh is the operating mode going forward; Maps auto-sync decision made; agreement on whether the diff report ships as Markdown only, Excel tab only, or both.

### Notion operational layer (complementary, not mirror)

**Status:** parked. **Priority: HIGH — operational gap; Stage 2 build.**

Architecture: **SQLite remains the source of truth for signal data.** Notion holds *operational* data — notes, status tags, contact logs, file attachments — per active target. The two systems are complementary, not a mirror.

Notion stores per-target:
- `status_tag` — one of: `Cold`, `Discovering`, `In Discussion`, `Negotiating`, `Closed`, `Dead`
- `notes` — free text, history-preserving
- `contact_log` — dated entries (call, email, meeting, etc.)
- `last_touched` — auto-updated whenever any operational field changes
- File attachments (scout reports, videos, agent comms)

#### Build

- **Notion database schema** "Brokerage Targets" with the operational columns above, plus DB-derived read-only fields: player name, sellability score, current club, buyer-matches summary.
- **Weekly sync script.** For each player in the top 50 by `match_score`, ensure a Notion page exists with current DB-derived fields. *Never overwrite* user-edited operational fields. New players entering the top 50 get a new page; players dropping out of the top 50 keep their existing pages (no deletion — operational history is permanent).
- **Streamlit Player View link.** Player View adds a **"View in Notion →"** anchor next to the header. Clicking opens that player's Notion page in a new tab. (New-tab is correct here — Notion is a different app context, not part of the matcher's drill-through chain.)
- **No bidirectional sync.** Notion never writes back to the DB. The matcher's signal data is computed; operational state lives in Notion.

#### Demo guidance

Stage 2 work. For the demo: skip entirely. Do not surface placeholders or hooks that aren't wired up. If Yatin asks how operational state will be tracked, the answer is *"Notion layer, Stage 2 — architecture decided, build to follow."*

**Dependencies before unparking:** Yatin demo done; Notion workspace provisioned with API access; sync cadence decided (weekly vs daily); decision on what happens to operational state when a player drops below the top-50 threshold.

### Player quality banding (validates matches against buyer-tier fit)

**Status:** parked. **Priority: HIGH — known gap in the matching logic; likely demo question from Yatin.**

The matcher today answers *"is this player sellable AND does the position bucket match the buyer's request"* but **not** *"is the player actually good enough for that buyer's level."* A worked example:

- Brentford's €25m left-back matches Liverpool's open LB slot on position + budget filters.
- Liverpool wouldn't actually sign that player — quality tier is wrong.
- The match passes filters and survives to the top of the Targets list, despite being commercially implausible.

#### Three implementation paths (in order of cleanness)

1. **Sci Sports banding** — if subscription is accessible.
   - Wire `player_quality_tier` column on `player_universe` + `min_quality_tier` per buying club (derived from the buying squad's average Sci Sports tier).
   - Match requires `player_quality_tier ≥ min_quality_tier`. Drop or down-weight matches that fail.
   - Cleanest because Sci Sports' tiering is already a recognised industry signal.

2. **TM value as proxy** — free, less precise.
   - Map clubs to quality tiers by their squad-average TM value (top quartile = top-tier buyer, bottom quartile = budget buyer). Players inherit a tier from their parent club's tier (or own value relative to peers).
   - Match requires the same tier-floor logic as path (1).
   - Risk: TM values lag transfer fees and underweight contract-leveraged players; quality and value are correlated but not identical.

3. **Custom composite** — most work, most defensible.
   - Build a quality score from: rolling TM value (last 18m trend), minutes share, age trajectory, national-team caps. Weight per component, normalise, band into 5 tiers.
   - Defensible because every component is auditable; controllable because we own the weights.
   - Costliest in build time.

#### Demo guidance

Acknowledge as a **known gap in the narrative** before Yatin raises it. Phrasing: *"v2 will add player-quality banding so a Brentford left-back doesn't surface as a Liverpool match. Today's filters cover position and budget; quality-tier validation is the next layer."*

Yatin is likely to surface this concern himself — being ahead of it is the strongest demo posture.

**Dependencies before unparking:** Yatin demo done; decision on which implementation path (1/2/3); Sci Sports subscription status confirmed if path (1) is chosen; agreement on how `min_quality_tier` is derived for unmapped buying clubs (Tier B / Tier C / Tier D leagues).

### Per-request level requirement on map_club_requests

**Status:** parked. **Priority: MEDIUM — refinement of the SciSports level-fit logic now live in the match engine.**

The SciSports talent layer (player CA/PA → level-fit multiplier on match_score) shipped with a **uniform first-team-level assumption** for every buyer request — the match engine looks up `map_club_overview.sci_first_team_level` per buyer and compares to player CA. This is defensible (the sellable cohort IS the first-team prospect band by design) but loses signal: a Bayern request for a U21 prospect should compare to `sci_rotation_level`, and a Real Madrid Galactico request should compare to `sci_key_player_level`.

**Fix:** add a `level_required` column to the Club Requests sheet of each market-map workbook with three accepted values:

| Value | Maps to threshold |
|---|---|
| `rotation` | `map_club_overview.sci_rotation_level` |
| `first_team` (default) | `map_club_overview.sci_first_team_level` |
| `key_player` | `map_club_overview.sci_key_player_level` |

#### Implementation path

1. **Schema:** add a `level_required TEXT` column to `map_club_requests`. Default `'first_team'` for backwards compatibility.
2. **Loader (`scripts/16_load_market_maps.py`):** parse a new `Level Required` column on the Club Requests sheet. Empty cells → `'first_team'`. Validate against the three accepted values; warn on typos.
3. **Match engine (`scripts/22_match_engine.py:compute_level_fit`):** switch the threshold lookup from hardcoded `sci_first_team_level` to a per-request branch on `level_required`.
4. **Workbook seeding:** for the first refresh after this lands, the Google Sheets template needs the new column added by hand. Subsequent edits flow through automatically.

#### Demo guidance

The Stage 1 default (first-team for all) gives ~30% of matches an `ON_LEVEL` rating, which is realistic — sellable-cohort players are mostly at-or-near first-team CA. Adding the per-request column shifts a small minority of requests but doesn't materially change the headline match list. Defer until post-demo unless the user wants finer control sooner.

**Dependencies before unparking:** Yatin demo done; agreement on the three-value taxonomy (`rotation` / `first_team` / `key_player`) vs richer schemes (numeric CA target, position-specific levels, etc.); workbook template update.

### Second-tier minutes scrape (TM client-side migration)

**Status:** parked. **Priority: HIGH — closes the last NULL hole in the `finished_product` flag for ~44 tm_scrape-sourced players.**

Day 7 attempt to extend the second-tier scraper to capture minutes/appearances data failed when we discovered Transfermarkt has migrated the per-competition stats table from server-rendered HTML to a JavaScript-rendered web component (`tm-player-performance-table-new`). The page HTML now contains zero `<table>` elements; the data is fetched client-side via JS after page load. Every plausible `/ceapi/` JSON endpoint returns 404.

**Affected cohort:** 44 players in `player_universe.minutes_share_pct IS NULL`:
- 39 second-tier players (GB2: 28, L2: 4, FR2: 3, ES2: 3, IT2: 1)
- 5 GB1 tm_scrape players (Coventry/Ipswich post-promotion, still scrape-sourced)

**Implementation path — Playwright headless browser.**

- Add `playwright` (~70 MB) to `requirements.txt`. Single `pip install` + `playwright install chromium` post-clone.
- Replace the `urllib.request.urlopen` path in `scripts/20_patch_minutes_2tier.py` with `playwright.sync_api.sync_playwright()` — render each profile page, wait for `tm-player-performance-table-new` to populate, then `page.content()` to grab the post-render HTML.
- Existing BeautifulSoup parser (`parse_current_season_stats()` in the same file) should work as-is on the post-render DOM.
- Wall time: ~5s per page × 44 players = ~3-4 minutes. Cache the rendered HTML at `data/tm_cache/stats_{player_id}.html` (44 stub files already exist from the failed urllib attempts — Playwright run will overwrite with usable HTML).

**Demo guidance:** Aaron Ramsey + the 43 other tm_scrape players continue to show *"Minutes data unavailable (second-tier scrape or relaxed-minutes league)"* on the Established flag card. The matcher gives them `finished_product_value = 0.5` (NULL fallback) so they're not unduly penalised — only slightly muted vs confirmed first-team regulars. If Yatin asks: *"v2 will fill in Championship-and-below minutes data — TM moved this to client-side rendering last month, blocking our current scraper; a headless-browser path resolves it."*

**Dependencies before unparking:** Yatin demo done; comfort with adding Playwright as a dependency (CI footprint considerations); confirmation that the Stage 1 NULL fallback is acceptable for the demo cohort.

### Per-league TM-to-fee multiplier calibration

**Status:** parked. **Priority: MEDIUM — refinement of an already-defensible static multiplier.**

Today the TM-to-fee multiplier is a **three-tier static ramp** (config.py: 2.0× / 1.5× / 1.2× by TM band) applied uniformly across all 19 leagues. This is a Stage 1 simplification — empirically the multiplier should vary by league because transfer-market dynamics differ:

- Portuguese / Belgian sellers historically clear 2-3× TM for prospects (Benfica's resale machine)
- Bundesliga sellers clear closer to 1.2-1.4× (more disciplined valuations)
- Championship players who get promoted often see fees substantially above TM

**Stage 2 work:** derive per-league multipliers empirically.

```sql
-- Build calibration pairs from dcaribou
WITH calibration AS (
  SELECT
    t.player_id,
    t.from_club_id,
    c.competition_id AS from_league,
    t.transfer_fee,
    -- TM value at time of transfer: nearest player_valuation BEFORE the transfer date
    (SELECT market_value_in_eur FROM player_valuations pv
      WHERE pv.player_id = t.player_id AND pv.date <= t.transfer_date
      ORDER BY pv.date DESC LIMIT 1) AS tm_at_transfer
  FROM transfers t
  JOIN clubs c ON c.club_id = t.from_club_id
  WHERE t.transfer_fee IS NOT NULL AND t.transfer_fee > 1000000
    AND t.transfer_date >= DATE '2020-07-01'
)
SELECT from_league,
       COUNT(*) AS n,
       MEDIAN(transfer_fee / tm_at_transfer) AS median_multiplier,
       PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY transfer_fee / tm_at_transfer) AS p25,
       PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY transfer_fee / tm_at_transfer) AS p75
FROM calibration
WHERE tm_at_transfer IS NOT NULL AND tm_at_transfer >= 5000000
GROUP BY from_league
HAVING COUNT(*) >= 20
ORDER BY median_multiplier DESC;
```

Replace `TM_TO_FEE_BANDS` in `config.py` with a per-league dict (or per-league × value-band 2D table if the per-league sample is too small). The function `tm_to_fee_multiplier(tm_eur, league_id)` gains a league parameter; callsites in `scripts/22_match_engine.py` and `scripts/25_zero_match_diagnostic.py` already pass the player row through so adding `r['parent_league']` is trivial.

**Dependencies before unparking:** Yatin demo done; sample-size audit per league (the SQL above) — leagues with n < 20 calibration pairs may still need the static fallback; agreement on cutoff date (5 years feels right — older transfers may not reflect current market dynamics).

---

## Refinement 1 — Continuous minutes-share weighting

**Status:** parked, not implemented.

**Current state:** `finished_product_value` in the sellability formula is binary-ish:

```
finished_product_value = 1.0 (minutes_share ≥ 50%)
                       | 0.5 (NULL — second-tier scraped, no minutes data)
                       | 0.0 (minutes_share < 50%)
```

**Proposed refinement:** make `finished_product_value` continuous, sliding linearly with `minutes_share_pct`:

```
finished_product_value = clamp(minutes_share_pct / 100, 0.0, 1.0)  for top-tier
                       = 0.5                                        for second-tier (still NULL)
```

A player at 65% minutes scores 0.65 instead of 1.0. A player at 45% scores 0.45 instead of 0 (and would now pass the inclusion filter — note: this proposal pairs with lowering MIN_MINUTES_SHARE to e.g. 30% so the sellability score does the fine-grained discrimination instead of the binary inclusion gate).

**Why parked:** changes player ranking and the inclusion gate together. The current binary form is what's been validated against the brief and the demo set. Reintroducing as a refinement after the brokerage match works end-to-end.

**Dependencies before unparking:** demo done; agreement that more rotation players should be included; confirmation that sellability ranking should reflect minutes-share granularity (vs binary "started enough").

---

## Refinement 2 — Tiered contract leverage by year

**Status:** parked, not implemented.

**Current state:** `contract_leveraged` is binary, set in `03_build_universe.py`:

```
contract_leveraged = 1 if contract_end ≤ snapshot + 2 years (end-of-season-aware) else 0
```

**Proposed refinement:** tiered by contract years remaining:

```
years_remaining = (contract_end - snapshot) / 365.25

contract_leveraged_value =
    1.0  if years_remaining < 1   (Bosman within 12 months — maximum leverage)
    0.7  if 1 ≤ years_remaining < 2
    0.4  if 2 ≤ years_remaining < 3
    0.0  if years_remaining ≥ 3
```

A player 11 months from free agency scores 1.0; a player 28 months out scores 0.4. The sellability formula's `(rp + cl + fpv) / 3` becomes weighted by actual contract urgency rather than a single cutoff.

**Why parked:** identical reason to Refinement 1 — alters ranking under the brief without time to validate.

**Dependencies before unparking:** demo done; brokerage match working; user agreement on the tier boundaries.

---

## Market Movement Maps — full standalone build (Stage 2)

**Status:** scoped at concept level, parked entirely. Distinct deliverable, post-Yatin demo.

A continuously-refreshed visual artefact showing the European transfer market as a state-coloured map: clubs and players plotted by position/value/pressure, with colour-coded states (settling, in motion, imminent, resolved), refreshing weekly and surfacing journalist-sourced rumours as a parallel signal layer.

### Known design notes

**State-driven colour architecture**
- Each player and club carries a state. States: `dormant`, `whispers`, `confirmed-interest`, `bid-rejected`, `agreed`, `transferred`. Colour palette assigns each state a hue + opacity. Transitions are events, not just state changes — the visual emphasises *movement* not status.
- States are computed from a combination of structured signals (price changes, contract proximity, manager-change flags) and unstructured signals (the journalist layer below). Each signal has a confidence and a decay.

**Journalist signal layer**
- Parallel ingest pipeline scrapes / fetches reporting from named tier-1 journalists (Romano, Ornstein, Plettenberg, Schira, others TBD) plus club-specific reliables.
- Each piece of reporting tagged with: player(s), club(s), state-transition signal, journalist confidence. Journalists weighted by historical hit-rate.
- Confidence-decayed: a Romano tweet from 3 days ago weighs more than the same wording from 3 weeks ago.

**Weekly refresh**
- Scheduled run pulls fresh dcaribou snapshot, re-scrapes affected TM endpoints, re-ingests journalist signals from the past week.
- Diff against prior week: surface the state changes ("X moved from whispers → confirmed-interest"). This is the actual deliverable — the *new* movement, not the current state.

**Visual rendering**
- Not Excel. Web-based (likely D3 or Observable Plot), interactive, hosted somewhere Ryan can share a URL.
- Two views: club-pressure heatmap (geographic / league grouping) and player-state stream (timeline). Filterable by position, value band, league.

### Why parked

This is Stage 2 — a separate artefact from the Yatin Matcher v1. It depends on:
- Journalist-signal pipeline (not built)
- State-machine engine (not built)
- Web rendering layer (out of scope for the 9-day brief)
- Scheduled refresh infrastructure (not built)

### Dependencies before unparking

- Yatin demo done
- Clarity on whether Stage 2 is a separate project or an extension
- Budget for hosting / scheduled jobs
- Pipeline for journalist-signal ingest (which sources, what cadence, what tagging)

---

## Other deferred decisions

### Lower MIN_MINUTES_SHARE to 40% (revisited)

Was attempted mid-Day 3 to catch Sporting/Benfica rotation pieces (Debast, Eduardo Quaresma, Schjelderup). Universe went 163 → 197. Reverted to 50% at Ryan's instruction to keep the agreed state pre-demo. Tied to Refinement 1 — if continuous minutes weighting goes in, the inclusion threshold drop comes with it.

### Brief-band tightening (€20m–€45m or €20m–€40m)

Current band is €8m–€45m. The widening is documented in `CLAUDE.md`. **120 of 163** players (74%) sit below the brief's €20m floor. If Yatin wants a stricter view, the query is one-liner: `SELECT * FROM player_universe WHERE current_tm_value_eur BETWEEN 20000000 AND 45000000`. Decision to tighten the filter itself is parked until the demo conversation.

### Wage estimate column on Sheet 4

`wage_estimate (€/wk)` is a placeholder NULL column. Source would be Capology or equivalent — paid API or scrape. Originally scoped for Day 6; will revisit only if wage signal is needed for matching logic on Day 5.

### Wage feasibility constant at 0.7 in match scoring (Day 5)

`scripts/22_match_engine.py` includes a `wage_feasibility` term in the match-score formula (`sellability × demand_intensity × budget_fit × wage_feasibility`). With no player wage column populated today, every match lands on `wage_feasibility = 0.7` ('unknown'). The term is a uniform scaling factor — it doesn't affect ranks, only absolute scores (each match is 30% lower than it will be once data lands). Lifts to dynamic `1.0` / `0.7` / `0` per match when Capology wage data integrates on Day 6+. The formula is wired through unchanged so the upgrade is data-only.

### Buyer-side data deeper than dcaribou

Day 4 builds Buyer Need from dcaribou + senior_roster. If buyer-side scoring needs FFP/PSR detail beyond `clubs.net_transfer_record`, there's a deeper scrape available at TM club financial pages — parked unless Day 4 surfaces a clear gap.

### Cross-sessional refresh / "what changed since last week"

Currently `02_init_schema.py` drops and recreates tables on every run. There's no diff against a previous state. Implementing diff would let us answer "which players entered the universe since last week" — useful for Stage 2's weekly-refresh model, parked for now.

---

## When to revisit this backlog

- After the Yatin demo (any feedback that changes the formula shape → check Refinements 1 & 2)
- Before scoping Stage 2 (Market Movement Maps)
- Whenever a parked item is mentioned in the brief and a decision is needed mid-build
