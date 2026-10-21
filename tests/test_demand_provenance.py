"""W / M / Y demand-provenance codes and the derived demand-coverage set."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import demand_provenance as dp  # noqa: E402


def test_source_code_maps_origin_before_source_tag():
    # Maps-inference rows arrive tagged Intel/NO; origin must win.
    assert dp.source_code("inference", "Intel") == dp.MAPS_INFERENCE
    assert dp.source_code("workbook", "Agent") == dp.WORKBOOK
    assert dp.source_code(None, "Inferred") == dp.YATIN_INFERENCE
    assert dp.source_code(None, "Intel") == dp.UNKNOWN


def test_demand_label_is_badge_prefixed():
    assert dp.demand_label("Intel", "NO", "inference") == "M · Maps inference"
    assert dp.demand_label("Inferred", None, None) == "Y · Matcher inference (squad gap)"
    assert dp.demand_label("Agent", "yes", "workbook") == "W · Validated agent"
    assert dp.demand_label("Agent", "NO", "workbook") == "W · Agent (partial)"
    assert dp.demand_label("Intel", "NO", "workbook") == "W · Intel-derived"


def test_count_line_lists_all_three_in_trust_order():
    counts = dp.count_codes(["M", "Y", "M", "Y"])
    assert dp.count_line(counts) == "0 W · 2 M · 2 Y"


def test_derive_demand_mapped_leagues_follows_loaded_requests():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE map_club_requests (league TEXT)")
    lgs = list(config.LEAGUE_IDS)
    con.executemany("INSERT INTO map_club_requests VALUES (?)",
                    [(lgs[2],), (lgs[0],), (lgs[0],), (None,), ("ZZ9",)])
    # config.LEAGUE_IDS order, de-duplicated, unknown codes and NULLs dropped.
    assert dp.derive_demand_mapped_leagues(con) == (lgs[0], lgs[2])


def test_squad_scrape_leagues_replaces_demand_mapped_constant():
    assert not hasattr(config, "DEMAND_MAPPED_LEAGUES")
    assert len(config.SQUAD_SCRAPE_LEAGUES) == 10
    assert set(config.SQUAD_SCRAPE_LEAGUES) <= set(config.LEAGUE_IDS)
