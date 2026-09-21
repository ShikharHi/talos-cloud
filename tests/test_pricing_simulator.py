"""
Talos Cloud — PricingSimulator Unit & Persistence Tests.

Tests:
  - test_simulator_calculates_margin
  - test_simulator_persists_to_db
  - test_margin_status_levels
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.margin_simulation import MarginSimulation
from app.services.pricing_simulator import PricingSimulator


@pytest.mark.asyncio
async def test_simulator_calculates_and_persists_margin(db_session: AsyncSession):
    simulator = PricingSimulator(db_session)

    usage_mix = {
        "fast_model": 0.40,
        "code_model": 0.15,
        "reasoning_model": 0.25,
        "web_search": 0.10,
        "image_gen": 0.07,
        "browser_use": 0.03,
    }

    sim = await simulator.simulate(
        name="Reasoning-heavy test mix",
        usage_mix=usage_mix,
        description="Testing simulation calculation and persistence",
    )
    await db_session.flush()

    assert sim.id is not None
    assert sim.projected_margin > 0.0
    assert "capability_results" in sim.simulation_output

    # Query DB to verify persistence
    result = await db_session.execute(
        select(MarginSimulation).where(MarginSimulation.id == sim.id)
    )
    saved_sim = result.scalar_one_or_none()
    assert saved_sim is not None
    assert saved_sim.name == "Reasoning-heavy test mix"


def test_margin_status_levels():
    assert PricingSimulator._margin_status(0.80) == "healthy"
    assert PricingSimulator._margin_status(0.72) == "acceptable"
    assert PricingSimulator._margin_status(0.67) == "warning"
    assert PricingSimulator._margin_status(0.55) == "critical"
