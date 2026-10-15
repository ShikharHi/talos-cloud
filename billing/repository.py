import psycopg
from psycopg.rows import dict_row
from decimal import Decimal
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from config import config
from billing.security import encrypt_secret, decrypt_secret, mask_secret

from neon_pool import neon_pool

class BillingRepository:
    def __init__(self, dsn: str = config.POSTGRES_URL):
        self.dsn = dsn

    def _get_conn(self):
        return neon_pool.connection()

    async def init_db(self):
        """Initialize all PostgreSQL tables for subscriptions, wallets, providers, pricing, reservations, and ledger."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # 1. Subscriptions Table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_subscriptions (
                        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                        plan_name TEXT NOT NULL DEFAULT 'pro_20',
                        status TEXT NOT NULL DEFAULT 'active',
                        monthly_credit_allowance NUMERIC(20, 8) NOT NULL DEFAULT 5000.00000000,
                        period_cogs_ceiling_usd NUMERIC(20, 12) NOT NULL DEFAULT 5.000000000000,
                        period_actual_cogs_usd NUMERIC(20, 12) NOT NULL DEFAULT 0.000000000000,
                        period_reserved_cogs_usd NUMERIC(20, 12) NOT NULL DEFAULT 0.000000000000,
                        period_start TIMESTAMP WITH TIME ZONE NOT NULL,
                        period_end TIMESTAMP WITH TIME ZONE NOT NULL,
                        external_subscription_id TEXT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT chk_cogs_ceiling CHECK ((period_actual_cogs_usd + period_reserved_cogs_usd) <= period_cogs_ceiling_usd)
                    );
                    CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON user_subscriptions(status);
                """)

                # 2. Wallets Table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS credit_wallets (
                        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                        settled_balance NUMERIC(20, 8) NOT NULL DEFAULT 0.00000000,
                        active_reserved NUMERIC(20, 8) NOT NULL DEFAULT 0.00000000,
                        version INTEGER NOT NULL DEFAULT 1,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT chk_non_negative_spendable CHECK ((settled_balance - active_reserved) >= 0)
                    );
                """)

                # 3. Dynamic Upstream Providers Table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS billing_providers (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        base_url TEXT NOT NULL,
                        api_key_encrypted TEXT NOT NULL,
                        models JSONB NOT NULL DEFAULT '[]'::jsonb,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 4. Versioned Pricing Models Table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS billing_pricing_models (
                        id TEXT PRIMARY KEY,
                        resource_id TEXT NOT NULL,
                        provider_type TEXT NOT NULL,
                        provider_id TEXT NULL REFERENCES billing_providers(id) ON DELETE SET NULL,
                        pricing_version TEXT NOT NULL,
                        input_rate_usd NUMERIC(20, 12) NOT NULL DEFAULT 0,
                        output_rate_usd NUMERIC(20, 12) NOT NULL DEFAULT 0,
                        cached_rate_usd NUMERIC(20, 12) NOT NULL DEFAULT 0,
                        tool_call_rate_usd NUMERIC(20, 12) NOT NULL DEFAULT 0,
                        credit_multiplier NUMERIC(10, 4) NOT NULL DEFAULT 1.0,
                        effective_from TIMESTAMP WITH TIME ZONE NOT NULL,
                        effective_to TIMESTAMP WITH TIME ZONE NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_pricing_active ON billing_pricing_models(resource_id) WHERE effective_to IS NULL AND is_active = TRUE;
                """)

                # 5. Reservations Table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS credit_reservations (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        thread_id TEXT NULL,
                        operation_type TEXT NOT NULL,
                        reserved_credits NUMERIC(20, 8) NOT NULL,
                        reserved_cogs_usd NUMERIC(20, 12) NOT NULL DEFAULT 0.000000000000,
                        settled_credits NUMERIC(20, 8) NOT NULL DEFAULT 0.00000000,
                        status TEXT NOT NULL DEFAULT 'RESERVED',
                        idempotency_key TEXT UNIQUE NOT NULL,
                        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        settled_at TIMESTAMP WITH TIME ZONE NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_reservations_user_status ON credit_reservations(user_id, status);
                """)

                # 6. Immutable Credit Ledger Table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS credit_ledger (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        thread_id TEXT NULL,
                        reservation_id TEXT NULL REFERENCES credit_reservations(id) ON DELETE SET NULL,
                        transaction_type TEXT NOT NULL,
                        credits_delta NUMERIC(20, 8) NOT NULL,
                        raw_cost_usd NUMERIC(20, 12) NOT NULL DEFAULT 0.000000000000,
                        currency TEXT NOT NULL DEFAULT 'USD',
                        pricing_version TEXT NOT NULL,
                        idempotency_key TEXT UNIQUE NOT NULL,
                        meta JSONB DEFAULT '{}'::jsonb,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON credit_ledger(user_id, created_at DESC);
                """)

                # Seed default providers and pricing if empty
                self._seed_default_data_if_empty(cur)
                conn.commit()

    def _seed_default_data_if_empty(self, cur):
        cur.execute("SELECT COUNT(*) as cnt FROM billing_providers")
        row = cur.fetchone()
        cnt = row["cnt"] if isinstance(row, dict) else row[0]
        if cnt == 0:
            # Seed Mistral & OpenAI
            now = datetime.now(timezone.utc)
            m_key_enc = encrypt_secret(config.MISTRAL_API_KEY)
            o_key_enc = encrypt_secret(config.OPENAI_API_KEY)
            cur.execute("""
                INSERT INTO billing_providers (id, name, base_url, api_key_encrypted, models, is_active, created_at, updated_at)
                VALUES 
                ('mistral', 'Mistral AI', %s, %s, '["codestral-2508", "mistral-medium-latest"]'::jsonb, TRUE, %s, %s),
                ('openai', 'OpenAI', %s, %s, '["gpt-4o-mini"]'::jsonb, TRUE, %s, %s)
            """, (config.MISTRAL_BASE_URL, m_key_enc, now, now, config.OPENAI_BASE_URL, o_key_enc, now, now))

            # Seed pricing
            cur.execute("""
                INSERT INTO billing_pricing_models 
                (id, resource_id, provider_type, provider_id, pricing_version, input_rate_usd, output_rate_usd, cached_rate_usd, tool_call_rate_usd, credit_multiplier, effective_from, effective_to, is_active)
                VALUES 
                ('prc_codestral_v1', 'codestral-2508', 'llm_provider', 'mistral', 'v2026.10.1', 0.30, 0.90, 0.03, 0.0, 1.0, %s, NULL, TRUE),
                ('prc_gpt4omini_v1', 'gpt-4o-mini', 'llm_provider', 'openai', 'v2026.10.1', 0.15, 0.60, 0.075, 0.0, 1.0, %s, NULL, TRUE),
                ('prc_tavily_basic_v1', 'tavily/search-basic', 'system_tool', NULL, 'v2026.10.1', 0.0, 0.0, 0.0, 0.0075, 1.0, %s, NULL, TRUE),
                ('prc_tavily_adv_v1', 'tavily/search-advanced', 'system_tool', NULL, 'v2026.10.1', 0.0, 0.0, 0.0, 0.0150, 1.0, %s, NULL, TRUE)
            """, (now, now, now, now))

    async def get_active_pricing(self, resource_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the current effective pricing row for a resource."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM billing_pricing_models
                    WHERE resource_id = %s AND effective_to IS NULL AND is_active = TRUE
                    LIMIT 1
                """, (resource_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    async def get_wallet(self, user_id: str) -> Dict[str, Any]:
        """Fetch or initialize wallet row for a user."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM credit_wallets WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if not row:
                    cur.execute("""
                        INSERT INTO credit_wallets (user_id, settled_balance, active_reserved, version)
                        VALUES (%s, 0.0, 0.0, 1)
                        RETURNING *
                    """, (user_id,))
                    row = cur.fetchone()
                    conn.commit()
                return dict(row)

    async def get_subscription(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM user_subscriptions WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    async def reserve_credits(
        self,
        user_id: str,
        thread_id: Optional[str],
        operation_type: str,
        amount_credits: Decimal,
        estimated_cogs_usd: Decimal,
        idempotency_key: str,
        ttl_seconds: int = 120
    ) -> Dict[str, Any]:
        """
        Atomically reserve credits and COGS limit using SELECT ... FOR UPDATE.
        Fails if spendable balance < amount_credits OR (actual_cogs + reserved_cogs + estimated_cogs) > ceiling.
        """
        res_id = f"res_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc)

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # Idempotency check
                cur.execute("SELECT * FROM credit_reservations WHERE idempotency_key = %s", (idempotency_key,))
                existing = cur.fetchone()
                if existing:
                    return dict(existing)

                # 1. Lock wallet
                cur.execute("SELECT settled_balance, active_reserved FROM credit_wallets WHERE user_id = %s FOR UPDATE", (user_id,))
                wallet = cur.fetchone()
                if not wallet:
                    raise ValueError("User wallet does not exist")

                spendable = Decimal(str(wallet["settled_balance"])) - Decimal(str(wallet["active_reserved"]))
                if spendable < amount_credits:
                    raise ValueError(f"Insufficient spendable credits. Spendable: {spendable}, Requested: {amount_credits}")

                # 2. Lock subscription and check COGS ceiling
                cur.execute("SELECT period_actual_cogs_usd, period_reserved_cogs_usd, period_cogs_ceiling_usd FROM user_subscriptions WHERE user_id = %s FOR UPDATE", (user_id,))
                sub = cur.fetchone()
                if sub:
                    act_cogs = Decimal(str(sub["period_actual_cogs_usd"]))
                    res_cogs = Decimal(str(sub["period_reserved_cogs_usd"]))
                    ceil_cogs = Decimal(str(sub["period_cogs_ceiling_usd"]))
                    if (act_cogs + res_cogs + estimated_cogs_usd) > ceil_cogs:
                        raise ValueError(f"Provider expense cap ($5.00) reached. Current: {act_cogs + res_cogs}, Additional: {estimated_cogs_usd}")

                    cur.execute("""
                        UPDATE user_subscriptions 
                        SET period_reserved_cogs_usd = period_reserved_cogs_usd + %s, updated_at = %s
                        WHERE user_id = %s
                    """, (estimated_cogs_usd, now, user_id))

                # 3. Increment active_reserved in wallet
                cur.execute("""
                    UPDATE credit_wallets 
                    SET active_reserved = active_reserved + %s, version = version + 1, updated_at = %s
                    WHERE user_id = %s
                """, (amount_credits, now, user_id))

                # 4. Insert reservation
                cur.execute("""
                    INSERT INTO credit_reservations 
                    (id, user_id, thread_id, operation_type, reserved_credits, reserved_cogs_usd, status, idempotency_key, expires_at, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'RESERVED', %s, %s, %s)
                    RETURNING *
                """, (res_id, user_id, thread_id, operation_type, amount_credits, estimated_cogs_usd, idempotency_key, expires_at, now))
                created = cur.fetchone()
                conn.commit()
                return dict(created)

    async def settle_reservation(
        self,
        reservation_id: str,
        actual_credits: Decimal,
        raw_cost_usd: Decimal,
        pricing_version: str,
        idempotency_key: str,
        meta: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Settle a reservation idempotently: debit settled balance, release unused hold, commit ledger row.
        """
        now = datetime.now(timezone.utc)
        meta = meta or {}
        tx_id = f"tx_{uuid.uuid4().hex[:12]}"

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Check reservation
                cur.execute("SELECT * FROM credit_reservations WHERE id = %s FOR UPDATE", (reservation_id,))
                res = cur.fetchone()
                if not res:
                    raise ValueError("Reservation not found")
                if res["status"] == "SETTLED":
                    return {"status": "SETTLED", "already_settled": True}

                user_id = res["user_id"]
                reserved_cr = Decimal(str(res["reserved_credits"]))
                reserved_cogs = Decimal(str(res["reserved_cogs_usd"]))

                # 2. Lock wallet
                cur.execute("SELECT settled_balance, active_reserved FROM credit_wallets WHERE user_id = %s FOR UPDATE", (user_id,))
                wallet = cur.fetchone()

                # Deduct actual from settled_balance, and decrease active_reserved by reserved_cr
                new_settled = max(Decimal("0"), Decimal(str(wallet["settled_balance"])) - actual_credits)
                new_reserved = max(Decimal("0"), Decimal(str(wallet["active_reserved"])) - reserved_cr)

                cur.execute("""
                    UPDATE credit_wallets 
                    SET settled_balance = %s, active_reserved = %s, version = version + 1, updated_at = %s
                    WHERE user_id = %s
                """, (new_settled, new_reserved, now, user_id))

                # 3. Update subscription COGS
                cur.execute("""
                    UPDATE user_subscriptions 
                    SET period_actual_cogs_usd = period_actual_cogs_usd + %s,
                        period_reserved_cogs_usd = GREATEST(0.0, period_reserved_cogs_usd - %s),
                        updated_at = %s
                    WHERE user_id = %s
                """, (raw_cost_usd, reserved_cogs, now, user_id))

                # 4. Mark reservation settled
                cur.execute("""
                    UPDATE credit_reservations 
                    SET settled_credits = %s, status = 'SETTLED', settled_at = %s
                    WHERE id = %s
                """, (actual_credits, now, reservation_id))

                # 5. Commit immutable ledger entry
                cur.execute("""
                    INSERT INTO credit_ledger 
                    (id, user_id, thread_id, reservation_id, transaction_type, credits_delta, raw_cost_usd, pricing_version, idempotency_key, meta, created_at)
                    VALUES (%s, %s, %s, %s, 'usage_debit', %s, %s, %s, %s, %s, %s)
                """, (tx_id, user_id, res["thread_id"], reservation_id, -actual_credits, raw_cost_usd, pricing_version, idempotency_key, psycopg.types.json.Json(meta), now))

                conn.commit()
                return {
                    "status": "SETTLED",
                    "transaction_id": tx_id,
                    "settled_credits": float(actual_credits),
                    "released_credits": float(reserved_cr - actual_credits),
                    "new_settled_balance": float(new_settled)
                }

    async def release_reservation(self, reservation_id: str, reason: str = "cancelled") -> bool:
        """Release unused reservation hold and refund reserved COGS."""
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM credit_reservations WHERE id = %s FOR UPDATE", (reservation_id,))
                res = cur.fetchone()
                if not res or res["status"] != "RESERVED":
                    return False

                user_id = res["user_id"]
                reserved_cr = Decimal(str(res["reserved_credits"]))
                reserved_cogs = Decimal(str(res["reserved_cogs_usd"]))

                cur.execute("""
                    UPDATE credit_wallets 
                    SET active_reserved = GREATEST(0.0, active_reserved - %s), version = version + 1, updated_at = %s
                    WHERE user_id = %s
                """, (reserved_cr, now, user_id))

                cur.execute("""
                    UPDATE user_subscriptions 
                    SET period_reserved_cogs_usd = GREATEST(0.0, period_reserved_cogs_usd - %s), updated_at = %s
                    WHERE user_id = %s
                """, (reserved_cogs, now, user_id))

                cur.execute("UPDATE credit_reservations SET status = 'RELEASED', settled_at = %s WHERE id = %s", (now, reservation_id))
                conn.commit()
                return True

billing_repo = BillingRepository()
