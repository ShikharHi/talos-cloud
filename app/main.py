"""
Talos Cloud — FastAPI application entrypoint (Engine v3).
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, func, select, text
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.database import Base, get_engine, get_session_factory
from app.models.accounts import Account
from app.models.wallet import Wallet
from app.models.subscription_plans import SubscriptionPlan
from app.models.capability_pricing import CapabilityPricing
from app.models.provider_pricing import ProviderPricing
from app.models.provider_mapping import ProviderMapping
from app.services.background_worker import schedule_background_jobs

from app.routers import (
    admin,
    auth,
    billing,
    credits,
    dashboard,
    llm as llm_router,
    marketplace as marketplace_router,
    pricing as pricing_router,
    relay,
    tasks,
)

import asyncio
logger = logging.getLogger(__name__)


class CancelledErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and isinstance(record.exc_info[1], (asyncio.CancelledError, GeneratorExit)):
            return False
        msg = record.getMessage()
        if "CancelledError" in msg or "Exception terminating connection" in msg:
            return False
        return True


logging.getLogger("sqlalchemy.pool").addFilter(CancelledErrorFilter())
logging.getLogger("sqlalchemy.dialects.postgresql").addFilter(CancelledErrorFilter())


async def _seed_v3_tables() -> None:
    """
    Seeds subscription_plans, capability_pricing, provider_pricing, and provider_mapping
    from YAML seed files if they are empty.
    """
    config_dir = Path(__file__).parent.parent / "config"
    session_factory = get_session_factory()

    async with session_factory() as db:
        # 1. Seed Subscription Plans
        res = await db.execute(select(func.count()).select_from(SubscriptionPlan))
        if res.scalar() == 0:
            plan_file = config_dir / "subscription_plans_seed.yaml"
            if plan_file.exists():
                data = yaml.safe_load(plan_file.read_text()) or {}
                for p in data.get("plans", []):
                    db.add(SubscriptionPlan(
                        name=p["name"],
                        price_usd=p["price_usd"],
                        monthly_credits=p["monthly_credits"],
                        reset_period_days=p.get("reset_period_days", 30),
                        topup_allowed=p.get("topup_allowed", True),
                        active=p.get("active", True),
                        version=p.get("version", "v1"),
                    ))
                await db.commit()
                logger.info("Seeded subscription_plans")

        # 2. Seed Capability Pricing
        res = await db.execute(select(func.count()).select_from(CapabilityPricing))
        if res.scalar() == 0:
            cap_file = config_dir / "capability_pricing_seed.yaml"
            if cap_file.exists():
                data = yaml.safe_load(cap_file.read_text()) or {}
                for c in data.get("capability_pricing", []):
                    db.add(CapabilityPricing(
                        capability_id=c["capability_id"],
                        unit=c["unit"],
                        credit_cost=c["credit_cost"],
                        target_margin=c.get("target_margin", 0.75),
                        pricing_version=c.get("pricing_version", "v1"),
                        active=c.get("active", True),
                    ))
                await db.commit()
                logger.info("Seeded capability_pricing")

        # 3. Seed Provider Pricing
        res = await db.execute(select(func.count()).select_from(ProviderPricing))
        if res.scalar() == 0:
            prov_file = config_dir / "provider_pricing_seed.yaml"
            if prov_file.exists():
                data = yaml.safe_load(prov_file.read_text()) or {}
                for p in data.get("provider_pricing", []):
                    db.add(ProviderPricing(
                        provider=p["provider"],
                        model_id=p["model_id"],
                        pricing_type=p.get("pricing_type", "token"),
                        input_cost_usd_per_1m=p.get("input_cost_usd_per_1m"),
                        output_cost_usd_per_1m=p.get("output_cost_usd_per_1m"),
                        cached_input_cost_usd_per_1m=p.get("cached_input_cost_usd_per_1m"),
                        tool_cost_usd=p.get("tool_cost_usd"),
                        image_cost_usd_low=p.get("image_cost_usd_low"),
                        image_cost_usd_medium=p.get("image_cost_usd_medium"),
                        image_cost_usd_high=p.get("image_cost_usd_high"),
                        source_url=p.get("source_url"),
                        version=p.get("version", "v1"),
                        active=p.get("active", True),
                    ))
                await db.commit()
                logger.info("Seeded provider_pricing")

        # 4. Seed Provider Mapping
        res = await db.execute(select(func.count()).select_from(ProviderMapping))
        if res.scalar() == 0:
            map_file = config_dir / "provider_mapping_seed.yaml"
            if map_file.exists():
                data = yaml.safe_load(map_file.read_text()) or {}
                for m in data.get("provider_mapping", []):
                    db.add(ProviderMapping(
                        capability_id=m["capability_id"],
                        provider=m["provider"],
                        model_id=m["model_id"],
                        priority=m.get("priority", 1),
                        active=m.get("active", True),
                    ))
                await db.commit()
                logger.info("Seeded provider_mapping")


async def _seed_admin_accounts() -> None:
    settings = get_settings()
    admin_emails = settings.admin_email_list
    if not admin_emails:
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        for email in admin_emails:
            result = await db.execute(select(Account).where(Account.email == email))
            account = result.scalar_one_or_none()
            if account:
                account.role = "admin"
                account.subscription_tier = "admin"
            else:
                account = Account(
                    email=email,
                    role="admin",
                    subscription_tier="admin",
                )
                db.add(account)
                await db.flush()

            res_w = await db.execute(select(Wallet).where(Wallet.account_id == account.account_id))
            wallet = res_w.scalar_one_or_none()
            if wallet is None:
                wallet = Wallet(
                    account_id=account.account_id,
                    monthly_balance=0,
                    topup_balance=1_000_000_000,
                )
                db.add(wallet)
            elif wallet.topup_balance < 1_000_000_000:
                wallet.topup_balance = 1_000_000_000
        await db.commit()
        logger.info("Admin accounts and wallets seeded successfully")

async def _clean_mock_skills() -> None:
    from app.models.marketplace import MarketplaceListing
    session_factory = get_session_factory()
    async with session_factory() as db:
        stmt = delete(MarketplaceListing).where(
            (MarketplaceListing.is_builtin == True) | 
            (MarketplaceListing.slug.in_(["web_search", "git_workflow", "pdf_analyzer", "xlsx"]))
        )
        await db.execute(stmt)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(f"Talos Cloud starting in {settings.talos_env} mode (Engine v3)")

    _engine = get_engine()
    try:
        if settings.talos_env in ("development", "test"):
            async with _engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        await _seed_v3_tables()
        await _seed_admin_accounts()
        await _clean_mock_skills()
    except Exception as exc:
        logger.error("Error during startup database seeding: %s", exc, exc_info=True)

    scheduler = None
    if os.environ.get("ENABLE_LEGACY_APSCHEDULER", "false").lower() in ("true", "1"):
        scheduler = AsyncIOScheduler()
        schedule_background_jobs(scheduler, get_session_factory())
        scheduler.start()
        logger.info("Legacy APScheduler enabled and started")
    else:
        logger.info("Background jobs managed durably via Inngest (/api/inngest)")

    try:
        from app.infrastructure.redis_client import init_redis_pool, close_redis_pool
        await init_redis_pool()
    except Exception as exc:
        logger.error("Error initializing Redis pool on startup: %s", exc, exc_info=True)

    yield

    if scheduler is not None:
        scheduler.shutdown()
    try:
        await close_redis_pool()
    except Exception:
        pass
    try:
        await _engine.dispose()
    except Exception:
        pass
    logger.info("Talos Cloud shut down")


app = FastAPI(
    title="Talos Cloud",
    description="Credit & billing control plane for Talos autonomous agent runtime (Engine v3).",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["set-cookie"],
)

app.include_router(auth.router)
app.include_router(auth.router_devices)
app.include_router(relay.router)
app.include_router(relay.router_v1)
app.include_router(credits.router)
app.include_router(pricing_router.router)
app.include_router(dashboard.router)
app.include_router(billing.router)
app.include_router(admin.router)
app.include_router(tasks.router)
app.include_router(marketplace_router.router)
app.include_router(marketplace_router.router_v1)
app.include_router(llm_router.router)
app.include_router(llm_router.router_v1)

# Inngest FastAPI Integration — mounts at /api/inngest
import inngest.fast_api
from app.inngest import inngest_client, all_inngest_functions

inngest.fast_api.serve(
    app,
    inngest_client,
    all_inngest_functions,
    serve_path="/api/inngest",
)


from fastapi.responses import JSONResponse
from app.database import check_db_health
from app.infrastructure.redis_client import check_redis_health


@app.get("/health")
async def health():
    return {"status": "ok", "service": "talos-cloud", "version": "3.0.0"}


@app.get("/")
async def root():
    return {"status": "ok", "service": "talos-cloud", "health": "/health", "ready": "/ready"}


@app.get("/health/db")
async def health_db():
    result = await check_db_health()
    status_code = 200 if result.get("status") == "healthy" else 503
    return JSONResponse(status_code=status_code, content=result)


@app.get("/health/redis")
async def health_redis():
    result = await check_redis_health()
    status_code = 200 if result.get("status") == "healthy" else 503
    return JSONResponse(status_code=status_code, content=result)


@app.get("/ready")
async def readiness():
    db_res = await check_db_health()
    redis_res = await check_redis_health()
    is_ready = db_res.get("status") == "healthy" and redis_res.get("status") == "healthy"
    status_code = 200 if is_ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if is_ready else "not_ready",
            "components": {
                "database": db_res,
                "redis": redis_res,
            },
        },
    )



@app.get("/api/connections/{provider}/callback")
async def connection_callback_relay(provider: str, request: Request):
    """
    Relays OAuth callbacks received on port 8001 to the local backend on port 8000.
    """
    from fastapi.responses import RedirectResponse
    query = request.url.query
    target_url = f"http://localhost:8000/api/connections/{provider}/callback"
    if query:
        target_url += f"?{query}"
    return RedirectResponse(url=target_url)
