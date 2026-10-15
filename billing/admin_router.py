from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from decimal import Decimal
import uuid
from datetime import datetime, timezone

from auth.service import get_current_user
from marketplace.routers.admin_router import require_admin_user
from billing.repository import billing_repo
from billing.security import encrypt_secret, mask_secret, validate_provider_base_url
import psycopg

router = APIRouter(prefix="/v1/admin", tags=["Admin Control Panel"])

# ---------------------------------------------------------------------------
# SUB-TAB 1: USERS DIRECTORY & DOSSIER
# ---------------------------------------------------------------------------

@router.get("/users")
async def list_users_directory(
    query: str = Query("", description="Search by email or ID"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: Dict[str, Any] = Depends(require_admin_user)
):
    """Search and list users with subscription status, wallet balance, and plugin count."""
    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            search_param = f"%{query.strip().lower()}%"
            cur.execute("""
                SELECT 
                    u.id, u.email, u.display_name, u.created_at,
                    COALESCE(w.settled_balance, 0.0) as settled_balance,
                    COALESCE(w.active_reserved, 0.0) as active_reserved,
                    COALESCE(s.plan_name, 'free') as plan_name,
                    COALESCE(s.status, 'unsubscribed') as subscription_status,
                    (SELECT COUNT(*) FROM user_installed_plugins uip WHERE uip.user_id = u.id) as installed_plugins_count
                FROM users u
                LEFT JOIN credit_wallets w ON u.id = w.user_id
                LEFT JOIN user_subscriptions s ON u.id = s.user_id
                WHERE LOWER(u.email) LIKE %s OR LOWER(u.id) LIKE %s
                ORDER BY u.created_at DESC
                LIMIT %s OFFSET %s
            """, (search_param, search_param, limit, offset))
            rows = cur.fetchall()

            cur.execute("SELECT COUNT(*) as total FROM users WHERE LOWER(email) LIKE %s OR LOWER(id) LIKE %s", (search_param, search_param))
            total_count = cur.fetchone()["total"]

            return {
                "status": "success",
                "total": total_count,
                "users": [dict(r) for r in rows]
            }

@router.get("/users/{user_id}/detail")
async def get_user_dossier(
    user_id: str,
    admin: Dict[str, Any] = Depends(require_admin_user)
):
    """Return comprehensive user dossier: profile, subscription, live wallet, installed plugins, published plugins, recent threads, and transactions."""
    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            # User profile
            cur.execute("SELECT id, email, display_name, created_at FROM users WHERE id = %s", (user_id,))
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            # Wallet
            cur.execute("SELECT * FROM credit_wallets WHERE user_id = %s", (user_id,))
            wallet = cur.fetchone() or {"settled_balance": 0.0, "active_reserved": 0.0}

            # Subscription
            cur.execute("SELECT * FROM user_subscriptions WHERE user_id = %s", (user_id,))
            subscription = cur.fetchone()

            # Installed plugins with rich details
            cur.execute("""
                SELECT 
                    p.id, p.name, p.slug, p.icon_url, p.plugin_type, p.latest_version, p.description,
                    uip.installed_at, uip.updated_at, uip.is_active, uip.install_status, uip.configuration_metadata
                FROM user_installed_plugins uip
                JOIN plugins p ON uip.plugin_id = p.id
                WHERE uip.user_id = %s
                ORDER BY uip.installed_at DESC
            """, (user_id,))
            installed_plugins = cur.fetchall()

            # Published plugins by this user
            cur.execute("""
                SELECT 
                    id, name, slug, plugin_type, latest_version, description,
                    downloads_count, stars_count, status, created_at, published_at
                FROM plugins
                WHERE publisher_id = %s OR builder_id = %s
                ORDER BY created_at DESC
            """, (user_id, user_id))
            published_plugins = cur.fetchall()

            # Subscription History / Grants / Expirations
            cur.execute("""
                SELECT * FROM credit_ledger 
                WHERE user_id = %s AND transaction_type IN ('subscription_grant', 'allowance_expiration', 'manual_admin_grant', 'refund')
                ORDER BY created_at DESC
            """, (user_id,))
            subscription_events = cur.fetchall()

            # Recent threads
            cur.execute("SELECT thread_id, title, created_at FROM thread_index WHERE user_id = %s ORDER BY created_at DESC LIMIT 10", (user_id,))
            threads = cur.fetchall()

            # Recent usage & credit ledger transactions
            cur.execute("SELECT * FROM credit_ledger WHERE user_id = %s ORDER BY created_at DESC LIMIT 25", (user_id,))
            transactions = cur.fetchall()

            return {
                "status": "success",
                "user": dict(user),
                "wallet": dict(wallet),
                "subscription": dict(subscription) if subscription else None,
                "installed_plugins": [dict(p) for p in installed_plugins],
                "published_plugins": [dict(p) for p in published_plugins],
                "subscription_history": [dict(e) for e in subscription_events],
                "threads": [dict(t) for t in threads],
                "recent_transactions": [dict(tx) for tx in transactions]
            }

class CreditGrantRequest(BaseModel):
    credits: float = Field(..., gt=0)
    reason: Optional[str] = "Admin manual grant"

@router.post("/users/{user_id}/credits/grant")
async def admin_grant_credits(
    user_id: str,
    req: CreditGrantRequest,
    admin: Dict[str, Any] = Depends(require_admin_user)
):
    """Manually grant credits to a user via an auditable ledger record."""
    now = datetime.now(timezone.utc)
    grant_amount = Decimal(str(req.credits))
    tx_id = f"tx_adm_{uuid.uuid4().hex[:12]}"

    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT settled_balance FROM credit_wallets WHERE user_id = %s FOR UPDATE", (user_id,))
            wallet = cur.fetchone()
            if not wallet:
                cur.execute("INSERT INTO credit_wallets (user_id, settled_balance, active_reserved, version) VALUES (%s, 0.0, 0.0, 1)", (user_id,))
                current_bal = Decimal("0")
            else:
                current_bal = Decimal(str(wallet["settled_balance"]))

            new_bal = current_bal + grant_amount
            cur.execute("UPDATE credit_wallets SET settled_balance = %s, version = version + 1, updated_at = %s WHERE user_id = %s", (new_bal, now, user_id))

            # Ledger entry
            cur.execute("""
                INSERT INTO credit_ledger 
                (id, user_id, transaction_type, credits_delta, raw_cost_usd, pricing_version, idempotency_key, meta, created_at)
                VALUES (%s, %s, 'manual_admin_grant', %s, 0.0, 'admin', %s, %s, %s)
            """, (tx_id, user_id, grant_amount, f"adm_{tx_id}", psycopg.types.json.Json({"reason": req.reason, "granted_by": admin.get("email")}), now))
            conn.commit()

            return {"status": "success", "new_balance": float(new_bal), "transaction_id": tx_id}

# ---------------------------------------------------------------------------
# SUB-TAB 2: PRICING EDITOR
# ---------------------------------------------------------------------------

@router.get("/pricing")
async def list_pricing_models(admin: Dict[str, Any] = Depends(require_admin_user)):
    """Fetch active versioned pricing models and tool rates."""
    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, bp.name as provider_name 
                FROM billing_pricing_models p
                LEFT JOIN billing_providers bp ON p.provider_id = bp.id
                WHERE p.effective_to IS NULL
                ORDER BY p.provider_type, p.resource_id
            """)
            rows = cur.fetchall()
            return {"status": "success", "pricing": [dict(r) for r in rows]}

class UpdatePricingRequest(BaseModel):
    input_rate_usd: float = Field(0.0, ge=0)
    output_rate_usd: float = Field(0.0, ge=0)
    cached_rate_usd: float = Field(0.0, ge=0)
    tool_call_rate_usd: float = Field(0.0, ge=0)
    credit_multiplier: float = Field(1.0, ge=0.1)

@router.put("/pricing/{resource_id:path}")
async def update_pricing_rate(
    resource_id: str,
    req: UpdatePricingRequest,
    admin: Dict[str, Any] = Depends(require_admin_user)
):
    """
    Update pricing rates immutably: close existing active record (effective_to = now) 
    and insert new version with effective_from = now.
    """
    now = datetime.now(timezone.utc)
    new_id = f"prc_{uuid.uuid4().hex[:12]}"
    new_version = f"v{int(now.timestamp())}"

    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM billing_pricing_models WHERE resource_id = %s AND effective_to IS NULL", (resource_id,))
            old = cur.fetchone()
            if not old:
                raise HTTPException(status_code=404, detail="Resource pricing record not found")

            # 1. Close old version
            cur.execute("UPDATE billing_pricing_models SET effective_to = %s WHERE id = %s", (now, old["id"]))

            # 2. Insert new version
            cur.execute("""
                INSERT INTO billing_pricing_models 
                (id, resource_id, provider_type, provider_id, pricing_version, input_rate_usd, output_rate_usd, cached_rate_usd, tool_call_rate_usd, credit_multiplier, effective_from, effective_to, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, TRUE)
            """, (new_id, resource_id, old["provider_type"], old["provider_id"], new_version, req.input_rate_usd, req.output_rate_usd, req.cached_rate_usd, req.tool_call_rate_usd, req.credit_multiplier, now))
            conn.commit()

            return {"status": "success", "new_version_id": new_id, "pricing_version": new_version}

# ---------------------------------------------------------------------------
# SUB-TAB 3: PROVIDERS MANAGEMENT
# ---------------------------------------------------------------------------

@router.get("/providers")
async def list_providers(admin: Dict[str, Any] = Depends(require_admin_user)):
    """List upstream providers with API keys safely masked."""
    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM billing_providers ORDER BY created_at ASC")
            rows = cur.fetchall()
            result = []
            for r in rows:
                p = dict(r)
                p["api_key_masked"] = mask_secret(p.pop("api_key_encrypted", ""))
                result.append(p)
            return {"status": "success", "providers": result}

class CreateProviderRequest(BaseModel):
    id: str = Field(..., min_length=2, max_length=32)
    name: str = Field(..., min_length=2)
    base_url: str = Field(..., min_length=8)
    api_key: str = Field(..., min_length=4)
    models: List[str] = Field(default_factory=list)

@router.post("/providers")
async def create_provider(
    req: CreateProviderRequest,
    admin: Dict[str, Any] = Depends(require_admin_user)
):
    """Create a new OpenAI-compatible provider with encrypted API key and SSRF URL validation."""
    clean_url = validate_provider_base_url(req.base_url)
    encrypted_key = encrypt_secret(req.api_key)
    now = datetime.now(timezone.utc)

    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM billing_providers WHERE id = %s", (req.id,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Provider ID already exists")

            cur.execute("""
                INSERT INTO billing_providers (id, name, base_url, api_key_encrypted, models, is_active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
            """, (req.id, req.name, clean_url, encrypted_key, psycopg.types.json.Json(req.models), now, now))

            # Auto-sync models to pricing registry
            for m in req.models:
                cur.execute("SELECT id FROM billing_pricing_models WHERE resource_id = %s AND effective_to IS NULL", (m,))
                if not cur.fetchone():
                    pid = f"prc_{uuid.uuid4().hex[:12]}"
                    cur.execute("""
                        INSERT INTO billing_pricing_models 
                        (id, resource_id, provider_type, provider_id, pricing_version, input_rate_usd, output_rate_usd, cached_rate_usd, tool_call_rate_usd, credit_multiplier, effective_from, effective_to, is_active)
                        VALUES (%s, %s, 'llm_provider', %s, 'initial_v1', 0.20, 0.80, 0.02, 0.0, 1.0, %s, NULL, TRUE)
                    """, (pid, m, req.id, now))

            conn.commit()
            return {"status": "success", "provider_id": req.id}

class UpdateProviderRequest(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[List[str]] = None

@router.put("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    req: UpdateProviderRequest,
    admin: Dict[str, Any] = Depends(require_admin_user)
):
    """Update an existing provider (name, base_url, api_key, and model list) and auto-sync any new models to pricing."""
    now = datetime.now(timezone.utc)
    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM billing_providers WHERE id = %s", (provider_id,))
            prov = cur.fetchone()
            if not prov:
                raise HTTPException(status_code=404, detail="Provider not found")

            updated_name = req.name.strip() if req.name else prov["name"]
            updated_url = validate_provider_base_url(req.base_url) if req.base_url else prov["base_url"]
            updated_key = encrypt_secret(req.api_key) if req.api_key else prov["api_key_encrypted"]
            updated_models = req.models if req.models is not None else prov["models"]

            cur.execute("""
                UPDATE billing_providers 
                SET name = %s, base_url = %s, api_key_encrypted = %s, models = %s, updated_at = %s
                WHERE id = %s
            """, (updated_name, updated_url, updated_key, psycopg.types.json.Json(updated_models), now, provider_id))

            # Auto-sync any new models from updated_models to billing_pricing_models
            for m in updated_models:
                cur.execute("SELECT id FROM billing_pricing_models WHERE resource_id = %s AND effective_to IS NULL", (m,))
                if not cur.fetchone():
                    pid = f"prc_{uuid.uuid4().hex[:12]}"
                    cur.execute("""
                        INSERT INTO billing_pricing_models 
                        (id, resource_id, provider_type, provider_id, pricing_version, input_rate_usd, output_rate_usd, cached_rate_usd, tool_call_rate_usd, credit_multiplier, effective_from, effective_to, is_active)
                        VALUES (%s, %s, 'llm_provider', %s, 'initial_v1', 0.20, 0.80, 0.02, 0.0, 1.0, %s, NULL, TRUE)
                    """, (pid, m, provider_id, now))

            conn.commit()
            return {"status": "success", "provider_id": provider_id}


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str,
    admin_user: Dict[str, Any] = Depends(require_admin_user)
):
    """Delete a provider and deactivate associated pricing models."""
    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM billing_providers WHERE id = %s", (provider_id,))
            prov = cur.fetchone()
            if not prov:
                raise HTTPException(status_code=404, detail="Provider not found")

            # Remove associated pricing models for models owned by this provider
            cur.execute("""
                DELETE FROM billing_pricing_models
                WHERE provider_id = %s
            """, (provider_id,))

            # Delete the provider
            cur.execute("DELETE FROM billing_providers WHERE id = %s", (provider_id,))
            conn.commit()
            return {"status": "success", "message": f"Provider {provider_id} deleted successfully"}


# ---------------------------------------------------------------------------
# SUB-TAB 4: LIVE NEON DATABASE & STORAGE INSPECTION
# ---------------------------------------------------------------------------

@router.get("/neon-status")
async def get_neon_storage_status(admin_user: Dict[str, Any] = Depends(require_admin_user)):
    """
    Query authoritative live stats directly from Neon Cloud PostgreSQL:
    - Connection metadata (host, database, role, server version)
    - Database storage size in bytes and human-readable MB
    - Exact table names, row counts, and table sizes on Neon
    - Summary counts across users, sessions, threads, plugins, and billing ledger
    """
    from config import config
    import urllib.parse
    parsed = urllib.parse.urlparse(config.POSTGRES_URL)
    host = parsed.hostname or "Neon Serverless"
    db_name = parsed.path.lstrip("/") or "neondb"
    user_name = parsed.username or "neondb_owner"

    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            # 1. PostgreSQL version
            cur.execute("SELECT version();")
            pg_version_raw = cur.fetchone()["version"]

            # 2. Database size
            cur.execute("SELECT pg_database_size(current_database()) as db_bytes, pg_size_pretty(pg_database_size(current_database())) as db_pretty;")
            size_row = cur.fetchone()
            db_size_pretty = size_row["db_pretty"]
            db_bytes = size_row["db_bytes"]

            # 3. All public tables with exact live disk size and row counts
            cur.execute("""
                SELECT 
                    table_name,
                    pg_total_relation_size(quote_ident(table_name)) as size_bytes,
                    pg_size_pretty(pg_total_relation_size(quote_ident(table_name))) as size_pretty
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                ORDER BY pg_total_relation_size(quote_ident(table_name)) DESC;
            """)
            table_rows = cur.fetchall()

            tables_info = []
            for t in table_rows:
                t_name = t["table_name"]
                try:
                    cur.execute(f"SELECT COUNT(*) as cnt FROM {t_name};")
                    count_row = cur.fetchone()
                    row_count = count_row["cnt"] if count_row else 0
                except Exception:
                    row_count = 0

                tables_info.append({
                    "name": t_name,
                    "rows": row_count,
                    "size_pretty": t["size_pretty"],
                    "size_bytes": t["size_bytes"]
                })

            # 4. Key Summary Counts & Published Artifacts
            cur.execute("SELECT count(*) as count FROM users;")
            users_count = cur.fetchone()["count"]

            cur.execute("SELECT count(*) as count FROM sessions WHERE revoked_at IS NULL;")
            active_sessions = cur.fetchone()["count"]

            cur.execute("SELECT count(*) as count FROM thread_index;")
            threads_count = cur.fetchone()["count"]

            cur.execute("SELECT count(*) as count FROM plugins;")
            plugins_count = cur.fetchone()["count"]

            cur.execute("SELECT count(*) as count FROM user_installed_plugins;")
            installed_plugins_count = cur.fetchone()["count"]

            cur.execute("SELECT count(*) as count FROM credit_ledger;")
            ledger_count = cur.fetchone()["count"]

            # Query all published marketplace items from Neon
            cur.execute("""
                SELECT 
                    p.id, p.slug, p.name, p.plugin_type, p.latest_version, p.category, 
                    p.zip_path, p.artifact_sha256, p.downloads_count, p.stars_count, p.created_at,
                    pub.handle as publisher_handle
                FROM plugins p
                LEFT JOIN publishers pub ON p.publisher_id = pub.id
                ORDER BY p.created_at DESC;
            """)
            published_items = [dict(r) for r in cur.fetchall()]

            # Query Tigris storage bucket telemetry
            from marketplace.tigris_service import tigris_service
            tigris_objects = tigris_service.list_objects()
            tigris_info = {
                "bucket": config.TIGRIS_BUCKET,
                "endpoint": config.TIGRIS_ENDPOINT,
                "region": "Global (Anycast)",
                "total_objects": len(tigris_objects),
                "total_bytes": sum(o.get("Size", 0) for o in tigris_objects),
                "objects": [
                    {
                        "key": o.get("Key"),
                        "size": o.get("Size"),
                        "last_modified": o.get("LastModified").isoformat() if o.get("LastModified") else None
                    }
                    for o in tigris_objects
                ]
            }

            # Query Upstash Redis status
            from redis_service import redis_cache
            redis_online = redis_cache.ping()
            redis_info = {
                "endpoint": "amazed-eft-176812.upstash.io",
                "region": "ap-southeast-1 (Singapore)",
                "status": "online" if redis_online else "offline",
                "latency_tier": "Sub-millisecond"
            }

            return {
                "status": "success",
                "neon": {
                    "host": host,
                    "database": db_name,
                    "user": user_name,
                    "ssl": "require",
                    "cloud_provider": "AWS ap-southeast-1 (Singapore)",
                    "server_version": pg_version_raw,
                    "database_size": db_size_pretty,
                    "database_bytes": db_bytes,
                },
                "tigris": tigris_info,
                "redis": redis_info,
                "summary": {
                    "total_tables": len(tables_info),
                    "total_users": users_count,
                    "active_sessions": active_sessions,
                    "saved_threads": threads_count,
                    "marketplace_plugins": plugins_count,
                    "active_installs": installed_plugins_count,
                    "audit_ledger_transactions": ledger_count,
                },
                "published_items": published_items,
                "tables": tables_info
            }


