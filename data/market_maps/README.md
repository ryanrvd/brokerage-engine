# `data/market_maps/` — DEPRECATED as of Phase 6

This directory was the **manual-copy zone**: 8 `.xlsx` workbooks hand-downloaded
from the Market Movement Map Google Sheets, loaded into the matcher's `map_*`
tables by the (now retired) `scripts/16_load_market_maps.py`.

**It is no longer read by any matcher script.** Phase 6 migrated the matcher to
consume the maps repo's versioned export interface instead:

    ~/market-movement-maps/exports/latest/      (override via MAPS_EXPORTS_PATH)

The new loader is `scripts/16b_load_maps_exports.py`. The maps repo is the single
source of truth — it resolves every club to a Transfermarkt id, applies league
overrides, and publishes `_manifest.json` + per-table JSON/xlsx. See the maps
repo's `exports/latest/_SCHEMA.md` for column-level docs.

The files here are kept for **archaeological reference only** (what the manual
workbooks looked like, and a fallback if the maps repo is ever unavailable).
They are stale and will not be refreshed. Safe to delete once the export
interface has been operating cleanly for a while — deferred for now.
