"""
Tests for the Pricing Engine.
Pure unit tests — no DB required.
"""
import pytest
from app.services.pricing_engine import PricingEngine


SAMPLE_SCHEDULE = {
    "version": "v1",
    "capabilities": {
        "reasoning_model": {"unit": "per_1k_tokens", "credits": 10, "target_margin": 0.40},
        "fast_model": {"unit": "per_1k_tokens", "credits": 2, "target_margin": 0.65},
        "web_search": {"unit": "per_call", "credits": 3, "target_margin": 0.50},
        "image_gen": {"unit": "per_call", "credits": 20, "target_margin": 0.50},
    },
}


class TestPricingEngine:
    @pytest.fixture
    def engine(self):
        return PricingEngine(schedule=SAMPLE_SCHEDULE)

    def test_reasoning_model_per_1k_tokens(self, engine):
        assert engine.units_to_credits("reasoning_model", 1000) == 10
        assert engine.units_to_credits("reasoning_model", 2000) == 20
        assert engine.units_to_credits("reasoning_model", 500) == 5

    def test_fast_model_per_1k_tokens(self, engine):
        assert engine.units_to_credits("fast_model", 1000) == 2
        assert engine.units_to_credits("fast_model", 5000) == 10

    def test_web_search_per_call(self, engine):
        # per_call: 1 call = 3 credits
        assert engine.units_to_credits("web_search", 1) == 3

    def test_image_gen_per_call(self, engine):
        assert engine.units_to_credits("image_gen", 1) == 20

    def test_unknown_capability_raises(self, engine):
        with pytest.raises(ValueError, match="Unknown capability_id"):
            engine.units_to_credits("nonexistent_capability", 100)

    def test_minimum_1_credit(self, engine):
        # Even tiny usage should charge at least 1 credit
        assert engine.units_to_credits("reasoning_model", 1) >= 1

    def test_compute_schedule_price_margin(self):
        """Formula: max(cost / (1 - margin), cost + floor)"""
        price = PricingEngine.compute_schedule_price(
            real_cost_per_unit=1.0,
            target_margin=0.40,
            absolute_min_margin_per_unit=0.10,
        )
        # margin_based = 1.0 / 0.60 = 1.666...
        # floor_based = 1.0 + 0.10 = 1.10
        assert abs(price - 1.0 / 0.60) < 0.001

    def test_compute_schedule_price_floor(self):
        """When floor exceeds margin-based price, floor wins."""
        price = PricingEngine.compute_schedule_price(
            real_cost_per_unit=0.01,
            target_margin=0.01,  # tiny margin → margin_based ≈ 0.0101
            absolute_min_margin_per_unit=0.50,  # large floor → 0.51
        )
        assert price == pytest.approx(0.51, rel=0.01)
