import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def selector_loop_factory(use_subprocess: bool = False):
    """Custom loop factory to ensure Windows uses SelectorEventLoop for psycopg async compatibility."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from config import config
from relay import router as relay_router

from contextlib import asynccontextmanager
from auth.repository import auth_repo
from auth.router import router as auth_router
from auth.gateway_router import router as gateway_router
from billing.repository import billing_repo
from billing.router import router as billing_router
from billing.admin_router import router as admin_billing_router
from marketplace.repository import marketplace_repo
from marketplace.router import router as marketplace_router
from marketplace.plugin_router import router as plugin_marketplace_router
from marketplace.web.ssr_router import router as ssr_router
from marketplace.routers.events_router import router as events_router
from marketplace.routers.user_router import router as user_router
from marketplace.routers.studio_router import router as studio_router
from marketplace.routers.admin_router import router as admin_router
from marketplace.jobs.scheduler import setup_marketplace_scheduler
from sync.repository import sync_repo
from sync.router import router as snapshot_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize PostgreSQL tables on startup
    await auth_repo.init_db()
    await billing_repo.init_db()
    await marketplace_repo.init_db()
    await sync_repo.init_db()
    # Start background scheduler
    sched = setup_marketplace_scheduler()
    yield
    sched.shutdown()

app = FastAPI(
    title="Talos Cloud - LLM Gated Relay & Auth Authority",
    version="6.0.0",
    description="Gated LLM proxy and user authentication authority for Talos v6",
    lifespan=lifespan
)

# CORS Policy for web client with credentials
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security Headers Middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Server"] = "TalosCloud-AuthRelay"
    return response

app.include_router(relay_router)
app.include_router(billing_router)
app.include_router(admin_billing_router)
app.include_router(auth_router)
app.include_router(gateway_router)
app.include_router(marketplace_router)
app.include_router(plugin_marketplace_router)
app.include_router(ssr_router)
app.include_router(events_router)
app.include_router(user_router)
app.include_router(studio_router)
app.include_router(admin_router)
app.include_router(snapshot_router)

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "talos-cloud",
        "version": "6.0.0",
        "mock_mode": config.MOCK_MODE
    }

if __name__ == "__main__":
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
