"""
Talos Cloud — Pricing Engine.

Loads the active pricing schedule from the database (or config yaml at startup)
and translates: capability_id + units → credits to charge.

Key rules:
  - Only one `pricing_version` is active at a time (is_active = True).
  - Pricing changes are NEVER applied automatically. Only via explicit admin action.
  - In-flight tasks are billed at the pricing_version that was active when they
    started. PricingEvent rows stamp the version used.
  - No credit-to-currency peg is exposed anywhere. Internally we know the
    real_cost_usd per PricingEvent, but this is never surfaced in API responses.
"""

import yaml
from functools import lru_cache
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# Default in-process cache of the active pricing schedule.
# Refreshed from DB on startup and when pricing_version changes.
_active_schedule: dict | None = None
_active_version_str: str = "v1"


def _load_yaml_schedule(path: str | Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class PricingEngine:
    """
    Translates (capability_id, units) → credits.
    Backed by the active pricing schedule loaded from DB or config yaml.
    """

    def __init__(self, schedule: dict | None = None):
        # Allow injection for tests
        global _active_schedule
        if schedule is not None:
            self._schedule = schedule
        elif _active_schedule is not None:
            self._schedule = _active_schedule
        else:
            # Fall back to config file at startup (before DB is ready)
            default_path = Path(__file__).parent.parent.parent / "config" / "pricing_v1.yaml"
            self._schedule = _load_yaml_schedule(default_path)
            _active_schedule = self._schedule

    def units_to_credits(self, capability_id: str, units: int) -> int:
        """
        Convert abstract units (tokens, calls, minutes) to credits for a given capability.
        Raises ValueError for unknown capability_ids.
        """
        caps = self._schedule.get("capabilities", {})
        if capability_id not in caps:
            raise ValueError(
                f"Unknown capability_id '{capability_id}'. "
                f"Known capabilities: {list(caps.keys())}"
            )
        cap = caps[capability_id]
        unit_type = cap.get("unit", "per_call")

        if unit_type == "per_1k_tokens":
            return max(1, round(units * cap["credits"] / 1000))
        elif unit_type == "per_call":
            return cap["credits"] * max(1, units)
        elif unit_type == "per_minute":
            return cap["credits"] * max(1, units)
        else:
            raise ValueError(f"Unknown unit type '{unit_type}' for capability '{capability_id}'")

    async def get_active_version(self, db: AsyncSession) -> str:
        """Returns the currently active pricing version string."""
        result = await db.execute(
            text("SELECT version FROM pricing_versions WHERE is_active = true LIMIT 1")
        )
        row = result.fetchone()
        return row[0] if row else _active_version_str

    @classmethod
    async def reload_from_db(cls, db: AsyncSession) -> "PricingEngine":
        """Reloads the active pricing schedule from the database."""
        global _active_schedule, _active_version_str
        result = await db.execute(
            text("SELECT schedule_yaml, version FROM pricing_versions WHERE is_active = true LIMIT 1")
        )
        row = result.fetchone()
        if row:
            schedule = yaml.safe_load(row[0])
            _active_schedule = schedule
            _active_version_str = row[1]
            return cls(schedule=schedule)
        # No active version in DB yet — fall back to v1 yaml
        return cls()

    @staticmethod
    def compute_schedule_price(
        real_cost_per_unit: float,
        target_margin: float = 0.40,
        absolute_min_margin_per_unit: float = 0.01,
    ) -> float:
        """
        Pricing formula from the architecture spec.
        schedule_price = max(cost / (1 - target_margin), cost + absolute_min_margin)
        Used when setting prices in the admin CLI — NOT at charge time.
        """
        margin_based = real_cost_per_unit / max(1e-9, 1.0 - target_margin)
        floor_based = real_cost_per_unit + absolute_min_margin_per_unit
        return max(margin_based, floor_based)
