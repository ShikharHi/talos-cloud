"""
Talos Cloud — Standalone Database Migration & Financial Configuration Seed Script.

Run this script to ensure all pricing and financial configuration tables are initialized in PostgreSQL.
"""

import asyncio
from sqlalchemy import select, func, text
from app.database import get_engine, get_session_factory, Base
import app.models
from app.models.pricing_configuration import PricingConfiguration

async def seed_financial_config():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pricing_configurations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                credit_reference_usd NUMERIC(18, 8) NOT NULL DEFAULT 0.10000000,
                version VARCHAR(50) NOT NULL DEFAULT 'v1',
                effective_from TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                effective_to TIMESTAMP WITH TIME ZONE,
                active BOOLEAN DEFAULT TRUE NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                created_by VARCHAR(255)
            );
        """))
    print("Database table pricing_configurations ready.")

    session_factory = get_session_factory()
    async with session_factory() as db:
        res = await db.execute(select(func.count(PricingConfiguration.id)))
        count = res.scalar() or 0
        if count == 0:
            config = PricingConfiguration(
                credit_reference_usd=0.1000,
                version="v1",
                active=True,
                created_by="system_init"
            )
            db.add(config)
            await db.commit()
            print("Seeded PricingConfiguration v1 ($0.1000).")
        else:
            print(f"PricingConfiguration already seeded ({count} records).")

if __name__ == "__main__":
    asyncio.run(seed_financial_config())
