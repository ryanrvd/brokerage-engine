# Phase 6.1 — Budget source swap: workbook → club_budget_derived

_Re-run against the CORRECTED export: snapshot `2026-06-20` (commit `b8ee2b23a4`), calibration 61.8%. The maps' promotion/relegation multipliers now reflect the 26/27 transition (fixed at source, superseding the earlier 25/26-window version). PRE and POST run against this same export — the only change is the budget source._

## What changed

`scripts/16b_load_maps_exports.py` sets each Club Request's `max_transfer_fee_eur` from `club_budget_derived.derived_highest_transfer_fee_eur` instead of `club_overview.workbook_highest_transfer_fee_eur`. `max_wage_pw_eur` unchanged. The derived budget differs from the workbook in **two** ways: (a) the 26/27 promotion/relegation multipliers, and (b) it is a **3-year rolling mean** rather than a single hand-entered figure.

## Match volume (matches table rows)

| metric | PRE (workbook) | POST (derived) | Δ |
|---|---:|---:|---:|
| matches total | 33,021 | 34,546 | +4.6% |
| match_score rows (brokerage) | 1,623 | 1,520 | -6.3% |

_(Per-club / swing analysis at distinct (player × buyer-club) granularity, best match_score per pair.)_

## (a) The transition-multiplier fix — VERIFIED

| club | role | workbook | derived | n_pre | n_post | avg MS pre | avg MS post | Δavg |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Wolves | releg | €27.0m | €9.9m | 14 | 0 | 13.4 | 0.0 | -13.4 |
| Burnley | releg | €28.7m | €4.0m | 11 | 0 | 15.3 | 0.0 | -15.3 |
| West Ham | releg | €44.0m | €11.1m | 17 | 0 | 35.2 | 0.0 | -35.2 |
| Coventry | promo | €5.0m | €17.1m | 0 | 0 | 0.0 | 0.0 | +0.0 |
| Ipswich | promo | €20.0m | €35.5m | 16 | 43 | 6.9 | 19.1 | +12.2 |
| Hull | promo | €0.0m | €20.9m | 4 | 21 | 6.2 | 6.2 | -0.0 |

Relegated clubs' budgets correctly drop (÷ multiplier on the pre-relegation norm) → fewer/weaker matches; promoted clubs rise (×) → more/stronger. **Leeds — the broken-multiplier outlier in the previous (held) run — now carries 1.0× and is flat.** The fix landed.

## (b) The rolling-mean effect — a SEPARATE, broader change

Top 12 clubs by |derived − workbook|. Note most have **1.0× multipliers** — their swing is purely single-figure-workbook vs 3-yr-rolling-mean, NOT a transition effect:

| club | workbook | derived | Δ | promoX | relegX |
|---|---:|---:|---:|---:|---:|
| Liverpool | €145.0m | €61.7m | -83.3m | 1.0 | 1.0 |
| Chelsea | €63.7m | €99.0m | +35.3m | 1.0 | 1.0 |
| West Ham ⟵ transition | €44.0m | €11.1m | -32.9m | 1.0 | 4.0 |
| Sunderland | €31.5m | €3.6m | -27.9m | 1.0 | 1.0 |
| Crystal Palace | €49.7m | €24.5m | -25.2m | 1.0 | 1.0 |
| Burnley ⟵ transition | €28.7m | €4.0m | -24.7m | 1.0 | 4.0 |
| FC Barcelona | €25.0m | €49.7m | +24.7m | 1.0 | 1.0 |
| Real Madrid | €62.5m | €84.8m | +22.3m | 1.0 | 1.0 |
| Hull City ⟵ transition | €0.0m | €20.9m | +20.9m | 4.0 | 1.0 |
| Villarreal CF | €31.0m | €11.8m | -19.2m | 1.0 | 1.0 |
| ESTAC Troyes | €3.0m | €21.7m | +18.7m | 5.0 | 1.0 |
| Aston Villa | €30.0m | €48.5m | +18.4m | 1.0 | 1.0 |

Liverpool (−€83m: €145m Wirtz-era figure → €62m rolling mean), Chelsea (+€35m), Crystal Palace (−€25m), Barcelona, Real Madrid — all 1.0×. This is the budget *method* changing, not the transition multipliers.

## Top 20 match_score swings (player × buyer)

| Δ | buyer | transition? | MS pre → post |
|---:|---|:--:|---:|
| -68.2 | West Ham | yes (releg) | 68.2 → 0.0 |
| -60.1 | West Ham | yes (releg) | 60.1 → 0.0 |
| -55.9 | West Ham | yes (releg) | 55.9 → 0.0 |
| -48.1 | Crystal Palace | no | 67.4 → 19.3 |
| -44.5 | West Ham | yes (releg) | 44.5 → 0.0 |
| -44.2 | Crystal Palace | no | 71.1 → 26.9 |
| -43.5 | West Ham | yes (releg) | 43.5 → 0.0 |
| -36.3 | Everton | no | 76.4 → 40.1 |
| -36.2 | Crystal Palace | no | 49.8 → 13.6 |
| -36.0 | Crystal Palace | no | 52.6 → 16.6 |
| -35.6 | Fulham | no | 71.1 → 35.5 |
| -34.4 | West Ham | yes (releg) | 34.4 → 0.0 |
| -34.3 | Bayer 04 Leverkusen | no | 34.3 → 0.0 |
| -34.1 | West Ham | yes (releg) | 34.1 → 0.0 |
| -34.1 | West Ham | yes (releg) | 34.1 → 0.0 |
| -33.7 | West Ham | yes (releg) | 33.7 → 0.0 |
| -33.7 | Brentford | no | 56.8 → 23.1 |
| -32.7 | Bayer 04 Leverkusen | no | 32.7 → 0.0 |
| -32.6 | Crystal Palace | no | 44.8 → 12.2 |
| -32.5 | Bayer 04 Leverkusen | no | 32.5 → 0.0 |

**Top 20: 9/20 transition. Top 50: 16/50 transition.** Leeds/Liverpool no longer dominate the match-score swings. But the swings are NOT exclusive to the 6 transition clubs — the most frequent single swinger is **Crystal Palace (15/50)**, a stable PL club whose budget dropped via the rolling-mean (effect b), ahead of any individual transition club. Top-50 buyer breakdown: Crystal Palace 15, West Ham 11, Bayer 04 Leverkusen 7, Brentford 5, Ipswich Town 5, Villarreal CF 2, Sunderland 2, Everton 1.

## Verdict

The transition-multiplier fix is **verified**: relegated/promoted clubs swing in the right direction and Leeds is normalised. However, switching workbook→derived is a broader change than transition multipliers alone — it also adopts a 3-yr rolling mean for every club, producing legitimate (arguably more accurate) swings at stable clubs (Liverpool, Crystal Palace, Chelsea). So the swings are concentrated in transition clubs *and* in stable clubs with outlier workbook figures. The change is sound and defensible; whether the stable-club rolling-mean swings are acceptable for go-live is a judgement call flagged for Ryan.
