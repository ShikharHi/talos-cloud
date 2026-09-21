"""
Talos Cloud — Model Registry.

Agent-facing interface for dynamic model switching.
The agent calls registry.switch_model("reasoning_model") — NEVER a real model name.

INVARIANT:
  - The agent never depends on provider names (anthropic, groq, openai, etc.).
  - The agent never depends on model IDs (claude-3-5-sonnet, llama-3.3-70b, etc.).
  - All routing goes through this registry which reads from provider_mapping table.
  - Every model call dispatched via this registry goes through the Talos relay.

Flow:
  Agent → registry.switch_model("reasoning_model")
        → provider_mapping lookup (capability_id → provider + model_id)
        → returns ModelConfig (provider + model_id are internal; agent gets capability_id only)
        → subsequent relay calls use the new capability_id
        → every call independently metered + billed

This is used in:
  - talos-backend LangGraph agent (via the relay client)
  - Any future agent SDK that talks to the Talos relay
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_mapping import ProviderMapping

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    """
    Internal configuration returned when the agent switches models.
    provider and model_id are NEVER sent to the client — they are used
    only by the relay to dispatch to the correct provider.
    """
    capability_id: str
    # INTERNAL ONLY below this line
    provider: str
    model_id: str


# Default capability used when agent starts a task (configurable)
DEFAULT_CAPABILITY_ID = "fast_model"

# Valid capability IDs the agent can switch to
VALID_CAPABILITIES = frozenset([
    "fast_model",
    "code_model",
    "reasoning_model",
    "vision_model",
    "web_search",
    "browser_use",
    "image_gen",
])


class ModelRegistry:
    """
    Resolves abstract capability_id → (provider, model_id) via provider_mapping table.

    Usage (in agent):
        registry = ModelRegistry(db)
        config = await registry.switch_model("reasoning_model")
        # config.capability_id is what you pass to relay
        # provider and model_id are handled internally by relay
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_default_model(self) -> ModelConfig:
        """Returns the default ModelConfig for starting a new task."""
        return await self.switch_model(DEFAULT_CAPABILITY_ID)

    async def switch_model(self, capability_id: str) -> ModelConfig:
        """
        Resolves capability_id → ModelConfig via provider_mapping table.
        Returns the highest-priority active mapping for the given capability.

        The agent calls this when it decides to switch models.
        Raises ValueError for unknown capability_ids.
        """
        if capability_id not in VALID_CAPABILITIES:
            raise ValueError(
                f"Unknown capability_id '{capability_id}'. "
                f"Valid: {sorted(VALID_CAPABILITIES)}"
            )

        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(ProviderMapping)
            .where(
                ProviderMapping.capability_id == capability_id,
                ProviderMapping.active.is_(True),
                ProviderMapping.effective_from <= now,
            )
            .order_by(ProviderMapping.priority.asc())
        )
        mapping = result.scalars().first()

        if mapping is None:
            # Fallback: log warning and return placeholder config
            # This should not happen in production if seed data is applied
            logger.error(
                "No active provider_mapping for capability_id=%s. "
                "Check provider_mapping_seed.yaml is applied.",
                capability_id,
            )
            raise ValueError(
                f"No provider configured for capability '{capability_id}'. "
                "Admin must configure provider_mapping."
            )

        logger.debug(
            "Model switch: capability=%s → provider=%s model=%s",
            capability_id, mapping.provider, mapping.model_id,
        )

        return ModelConfig(
            capability_id=capability_id,
            provider=mapping.provider,
            model_id=mapping.model_id,
        )

    async def list_capabilities(self) -> list[str]:
        """Returns list of capability_ids that have active provider mappings."""
        result = await self.db.execute(
            select(ProviderMapping.capability_id)
            .where(ProviderMapping.active.is_(True))
            .distinct()
        )
        return [row[0] for row in result.fetchall()]
