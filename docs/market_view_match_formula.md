# Market View match score — formula specification

**Version:** 0.1 (initial)
**Status:** Approved design. Implementation pending data prerequisites for position tension, scarcity, and valuation components.
**Scope:** Per (player × buyer) match score in the Market View lens of the engine. Sister documents pending: `brokerage_engine_match_formula.md` (the original YP arbitrage view) and `seller_pressure_formula.md` (club-level pressure, already implemented in `08_compute_pressure.py`).

---

## What this score answers

> *"Of all the theoretically-matched buyers for this player, how likely is THIS specific match to be the move that actually happens this window?"*

A per-(player × buyer) probability-of-movement score. Aggregates up per player ("where is this player most likely to end up") and per club ("what's their best pack of likely-to-move assets" — Yatin's relegated-clubs framing).

Market View is **comprehensive** — it scores every player in the mandate-relevant universe regardless of age, value band, or sellability profile. The Brokerage Engine (sister score) is the targeted, filtered, in-form cohort. Both views look for edge; they use different signal sets to surface it.

---

## Cohort definition

The "available pool" used as the benchmark cohort for scarcity, valuation, and median calculations:

```
cohort_member = (player.sellability_score > 50
                 OR player.sellability_status = 'sellable_now')
              AND NOT player.is_imminent_free_agent
```

Scales naturally as the squad universe expands across the 10 demand-mapped leagues. The Brokerage Engine's strict `sellable_now` cohort (~104 players today) is **not** this cohort — Market View's pool is intentionally broader.

**Imminent Free Agent exclusion (2026-06-04):** any player whose registered contract ends within 180 days of `config.SNAPSHOT_DATE` carries `is_imminent_free_agent = 1` (set by `scripts/09_compute_sellability.py`) and is excluded from the cohort entirely. These are Bosman pre-contract candidates — not fee-bearing brokerage opportunities. Surfaced in Club View's "Imminent Free Agents" panel as possible player-side mandates.

---

## Score formula

```
market_match = sellability
             × age_multiplier
             × demand_term
             × level_fit
             × financial_fit
             × pathway_plausibility
             × position_tension_multiplier
             × scarcity_term
             × valuation_term
```

Multiplicative product. Theoretical range: 0 to ~2.0+ (scarcity and tension can lift good matches above the 1.0 baseline ceiling). Default sort across the app uses this score descending when the toggle is on Market View.

---

## Component definitions

### 1. Sellability — range 0–1

`player.sellability_score / 100`. The player-level pillar. Includes parent club pressure (relegation as dominant term per `seller_pressure_formula.md`), contract leverage, public must-sell flag, finished product, right-priced. See `09_compute_sellability.py` for composition.

### 2. Age multiplier — range 0.35–1.0

| Age | Multiplier | Reasoning |
|---|---|---|
| ≤25 | 1.0 | Peak market value, prospect/peak demand |
| 26–29 | 0.85 | Still strong market, more buyer-specific |
| 30–32 | 0.6 | Limited market — wage / role-specific moves |
| 33+ | 0.35 | Thin market — mostly free transfers or wage-driven specific moves |

Age is a first-order predictor of market viability. A 35-year-old at a relegated club has sellability boosted (parent pressure is real), but the market doesn't exist at his profile — the age multiplier dampens the match score accordingly. Stops blanket relegation signals from inflating mathematically-eligible-but-realistically-immovable players.

### 3. Demand term — range 0.5–1.0

Tiered by the buyer-side signal from market movement maps. **Positional + level signal only — named player interest is not weighted.**

| Tier | Multiplier | Description |
|---|---|---|
| Agent-validated positional+level | 1.0 | Position and level requirement confirmed via private agent network |
| Intel-only positional+level | 0.75 | Position+level from intel signals, not directly validated |
| Inferred from squad gap | 0.5 | Buyer has a thin position; demand assumed, no explicit signal |

**Named player interest lists are not used as a scoring tier.** Named interests in market maps reflect journalist/rumour-mill noise; the structural value of the maps is the positional + level + budget triplet. Named interests display as informational context on Player View / matched-opportunities surfaces but do not weight the match score.

### 4. Level fit — range 0.35–1.0

Player CA vs buyer's required threshold for the level (squad / first team / key player) at the requested position.

| Level fit | Multiplier | Definition |
|---|---|---|
| ON LEVEL | 1.0 | Player CA ≥ buyer's threshold for requested level |
| UPSIDE | 0.7 | Player CA < threshold but PA ≥ threshold (development play) |
| BELOW | 0.35 | Both CA and PA below threshold |
| UNRATED | **excluded — no match row created** | See UNRATED hard rule below |

**UNRATED hard rule.** UNRATED matches do not enter the matches table. UNRATED players surface on a dedicated **review worklist** (Streamlit page or Excluded Players panel) filtered from `scisports_ratings.xlsx` where `status = pending` AND player ∈ mandate-relevant cohort. Operational signal to investigate the data gap, not scoring noise to absorb. Every match in Market View has a real level signal by construction.

### 5. Financial fit — range 0–1

Product of two sub-components, both implemented in `22_match_engine.py`:
- **budget_fit:** 0 if buyer's max_fee < `config.MIN_BROKERAGE_FEE` (€15m). Linear ramp from 0 to 0.80 between €15m and 2× indicative fee. Plateau at 0.80 above.
- **wage_feasibility:** 1.0 if confirmed feasible against `manual_wages.xlsx`, 0.7 if unknown, 0.0 if infeasible.

`indicative_fee = player.tm_market_value_eur × config.tm_to_fee_multiplier(tm_value)` (tiered 2.0× / 1.5× / 1.2× per `config.py`).

### 6. Pathway plausibility — range 0.4–1.0

League-tier transition score. Reflects how plausible the move is in football-career terms, independent of buyer-specific signals.

**Tier mapping** (covers our 19 leagues):

| Tier | Leagues |
|---|---|
| S | Premier League (GB1) |
| A | La Liga (ES1), Serie A (IT1), Bundesliga (L1), Ligue 1 (FR1) |
| B | Eredivisie (NL1), Primeira Liga (PO1), Belgian Pro League (BE1), Süper Lig (TR1) |
| C | Championship (GB2), 2. Bundesliga (L2), Ligue 2 (FR2), Serie B (IT2), LaLiga 2 (ES2) |
| D | Super League Greece (GR1), Superliga Denmark (DK1), Scottish Premiership (SC1) |
| X | MLS (MLS1), Saudi Pro League (SA1) — off-pyramid markets |

**Transition score per (from_tier → to_tier):**

| Move type | Example | Score |
|---|---|---|
| Upward in pyramid (one or more tiers) | B → A (Eredivisie → La Liga), C → S (Championship → PL) | **1.0** |
| Lateral within S | PL → PL | **0.95** |
| Within same tier (A or B or C or D) | A → A, B → B | **0.85** |
| Mild downward (1 tier) | A → B (Serie A → Eredivisie) | **0.6** |
| Steep downward (2+ tiers) | S → C (PL → Championship) | **0.45** |
| Into X tier | Any → MLS / Saudi | **0.5** |
| Out of X tier | MLS / Saudi → Any | **0.4** |

### 7. Position tension multiplier — range 0.7–1.4

**Status: disabled (locked at 1.0) until data prerequisite met.**

**Data prerequisite:** full squad ingestion across all 10 demand-mapped leagues. Currently only PL is fully loaded; supply count for other leagues uses the filtered `sellable_now` cohort while demand count uses all buyer requests across the maps. Apples-to-oranges — every position reads "demand massively exceeds supply" as artefact, not signal.

**Once enabled:**
```
tension_ratio = demand_count / sellability_weighted_supply_count   (per position)

tight market (ratio > 1.3)   → multiplier 1.4
balanced (0.7 ≤ ratio ≤ 1.3) → multiplier 1.0
loose market (ratio < 0.7)   → multiplier 0.7
```

Multiplier (not additive) so that within-position differentiation is preserved while the position group as a whole is lifted or dampened. Capped 0.7–1.4 so no single tight position can dominate overall ranking.

### 8. Scarcity term — range 0.5–1.5

Per (player × position) signal — is this player above or below the median CA of the available pool at their position?

```
z_score        = (player.CA - cohort_median_CA_at_position) / cohort_std_CA_at_position
scarcity_term  = exp(z_score), clipped to [0.5, 1.5]
```

Behaviour:
- Player CA at position median → 1.0× (neutral)
- 1σ above median → ~1.0× boost (factor ~1.3, e.g. 1.28)
- 2σ above → clipped to 1.5 ceiling
- 1σ below → factor ~0.7
- 2σ below → clipped to 0.5 floor

Surfaces scarce quality at a position as a market-making arbitrage signal — under-recognised quality climbs the rankings.

### 9. Valuation term — range 0.6–1.4

Per-player signal — is the player's predicted transfer fee in line with benchmark for their position × CA band?

```
predicted_fee   = player.tm_market_value_eur × config.tm_to_fee_multiplier(tm_value)
benchmark_fee   = median(predicted_fee) over comparable cohort members
                  (same position bucket, similar CA band ±10)
valuation_term  = exp(-(predicted_fee - benchmark_fee) / benchmark_fee), clipped to [0.6, 1.4]
```

Behaviour:
- Predicted fee at benchmark → 1.0× (neutral)
- 30% below benchmark → ~1.30× boost (under-priced asset / arbitrage)
- ≥40% below → clipped to 1.4 ceiling
- 30% above → ~0.74× dampen
- ≥50% above → clipped to 0.6 floor

Captures mispricing arbitrage within the cohort — under-priced players relative to comparable benchmarks surface higher; over-priced dampen.

---

## Worked examples

All three use the formula at full strength (position tension at 1.0 since disabled in v0.1).

### Example 1 — Strong obvious Market View match

Wolves first-team CB (hypothetical Toti Gomes-tier) → Newcastle, first-team CB:

| Component | Value | Reasoning |
|---|---|---|
| Sellability | 0.90 | Relegated parent (Wolves pressure 81.6), contract leverage, must-sell flag |
| Age multiplier | 0.85 | Age 26, band 26–29 |
| Demand term | 0.85 | Agent-validated positional+level (intel tier-adjacent) |
| Level fit | 1.0 | ON LEVEL — CA matches Newcastle's first-team CB threshold |
| Financial fit | 0.80 | Newcastle €75m budget, indicative fee ~€30m — plateau |
| Pathway | 0.95 | S → S lateral (PL → PL) |
| Position tension | 1.0 | Disabled |
| Scarcity | 1.3 | CA ~1σ above CB cohort median |
| Valuation | 1.0 | Predicted fee in line with benchmark |

**Score = 0.90 × 0.85 × 0.85 × 1.0 × 0.80 × 0.95 × 1.0 × 1.3 × 1.0 = 0.643**

### Example 2 — Hidden arbitrage match (the killer signature)

Eredivisie midfielder (high CA, below benchmark fee) → PL midtable buyer with intel-tier positional need:

| Component | Value | Reasoning |
|---|---|---|
| Sellability | 0.65 | No relegation but contract leverage + intel-grade pressure |
| Age multiplier | 1.0 | Age 23 |
| Demand term | 0.75 | Intel-only positional+level |
| Level fit | 1.0 | ON LEVEL |
| Financial fit | 0.80 | Comfortable |
| Pathway | 1.0 | B → S upward (canonical feeder pathway) |
| Position tension | 1.0 | Disabled |
| Scarcity | 1.45 | ~1.5σ above Eredivisie CM cohort median |
| Valuation | 1.30 | ~30% below benchmark |

**Score = 0.65 × 1.0 × 0.75 × 1.0 × 0.80 × 1.0 × 1.0 × 1.45 × 1.30 = 0.735**

**Higher than Example 1 despite lower sellability and demand.** Scarcity + valuation arbitrage signals compensate. This is the intended Market View signature: structural arbitrage surfaces above obvious mandate moves when the signals point to mispriced quality.

### Example 3 — Weak match (floors)

Coventry (Championship) midtable CM → Greek Super League club:

| Component | Value | Reasoning |
|---|---|---|
| Sellability | 0.40 | Stable parent, no pressure flags |
| Age multiplier | 0.85 | Age 28, band 26–29 |
| Demand term | 0.50 | Inferred from squad gap (no explicit signal) |
| Level fit | 0.35 | BELOW (CA below Greek club's threshold for this role) |
| Financial fit | 0.50 | Thin budget, marginal fit |
| Pathway | 0.6 | C → D mild downward |
| Position tension | 1.0 | Disabled |
| Scarcity | 0.85 | ~0.5σ below cohort median |
| Valuation | 0.9 | Slightly above benchmark |

**Score = 0.40 × 0.85 × 0.50 × 0.35 × 0.50 × 0.6 × 1.0 × 0.85 × 0.9 = 0.0146**

Effectively floors near zero. Correct outcome — does not surface in any meaningful sort.

---

## UNRATED review worklist

UNRATED players are excluded from matches but must be visible operationally. Implementation requirement:

- Streamlit page or panel section listing every player where:
  - `player_universe.player_id IN (mandate_relevant_cohort)`
  - AND `scisports_ratings.xlsx[status] = 'pending'` (or row missing from xlsx entirely)
- Columns: Player, Parent club, Position, Age, Reason for unrated (missing from xlsx / status=pending / etc.), Days since added to worklist
- Sorted by days descending — oldest gaps surface first as the longest-standing data debt
- Click-through to the SciSports manual entry workflow

This is operational signal: the worklist size measures the data debt; closing the worklist closes the data debt.

---

## Data prerequisites before full activation

For the formula to operate at full strength, the following must land in order:

| # | Prerequisite | Status |
|---|---|---|
| 1 | TM squad expansion to 9 non-PL demand-mapped leagues (Championship, ESP-1, ITA-1, FRA-1, FRA-2, GER-1, POR-1, NLD-1, BEL-1) | **pending** |
| 2 | Sellability computed across the full expanded universe | pending |
| 3 | SciSports CA/PA refresh across the full universe (incl. Championship bridge extension, etc.) | partial — PL only |
| 4 | Cohort medians per position (median CA, std CA) computed and cached | pending |
| 5 | Valuation benchmarks per (position × CA band) computed and cached | pending |
| 6 | Position tension multiplier enabled (supply/demand on like-for-like basis) | pending |

Until prerequisites land, Market View runs the formula with **position tension locked at 1.0**, **scarcity computed over the partial cohort (PL only initially) with a "partial coverage" caveat surfaced in the UI**, and **UNRATED worklist scaling as squads expand**.

---

## Implementation notes

### Database layer

- `matches` table gains a new column: `market_match_score` (REAL). The existing `match_score` is retained for backward compatibility and becomes the *Brokerage Engine* default (or eventually the *legacy* score once the dedicated brokerage formula doc is written).
- `scripts/22_match_engine.py` extended to compute both scores per match row in a single pass.
- New cohort statistics computed and cached pre-match-engine (likely as a new script `scripts/_market_view_cohort_stats.py` invoked from 22): per-position median CA, std CA, benchmark fee per (position × CA band).

### UI toggle

- Sidebar control (persistent across pages): **View: ⚪ Brokerage Engine · ⚫ Market View**
- Toggle switches the active match-score column used for ranking across All Matches, Targets, Player View match list, Club View matches-as-buyer, Position View matches, League View matched opportunities, Market Overview top-5.
- Player View match list shows both scores side-by-side as columns so users can see how a match reads in each view.
- Default on page load: Market View (since it's the comprehensive view; Brokerage is the targeted slice).

### UNRATED worklist surface

- New page (or panel on existing Kill List / Excluded Players page): **"Needs Sci Sports rating"**
- Populated from the intersection of mandate-relevant cohort and `scisports_ratings.xlsx[status = pending OR missing]`.

---

## Versioning

| Version | Date | Change |
|---|---|---|
| 0.1 | (today) | Initial — design locked, awaiting data prerequisites for full activation. |
