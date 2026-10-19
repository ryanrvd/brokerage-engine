"""Tests for scripts/22_match_engine.py demand weighting by source_origin.

Maps-side inference rows (source_origin='inference', loaded by 16b) arrive tagged
source='Intel'/validated='NO'. They must score on the inference tier of the
demand_intensity scale, not as explicit Intel.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("engine22", REPO / "scripts" / "22_match_engine.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)


def test_demand_intensity_explicit_scale_unchanged():
    assert engine.demand_intensity("Agent", "YES") == 1.00
    assert engine.demand_intensity("Agent", "NO") == 0.85
    assert engine.demand_intensity("Intel", "NO") == 0.60
    assert engine.demand_intensity("Inferred", "AUTO") == 0.40
    assert engine.demand_intensity(None, None) == 0.50
    assert engine.demand_intensity("Intel", "NO", "workbook") == 0.60


def test_demand_intensity_maps_inference_on_inference_tier():
    assert engine.demand_intensity("Intel", "NO", "inference") == 0.40
    assert engine.demand_intensity("Agent", "YES", "inference") == 0.40


def test_market_demand_term_maps_inference():
    assert engine.market_demand_term("Intel", "NO") == 0.75
    assert engine.market_demand_term("Intel", "NO", "inference") == 0.5


def test_score_pair_threads_source_origin():
    kw = dict(player_tm=20_000_000, sellability=80.0, buyer_max_fee=100_000_000,
              source="Intel", validated="NO")
    explicit = engine.score_pair(**kw)
    inferred = engine.score_pair(**kw, source_origin="inference")
    assert explicit["demand_intensity"] == 0.60
    assert inferred["demand_intensity"] == 0.40
    assert inferred["match_score_raw"] < explicit["match_score_raw"]
