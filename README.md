# Talos Cloud Control Plane (`talos-cloud`)

The **Talos Cloud Control Plane** (running on Port 8001) manages authentication, multi-gateway billing, dedicated credit wallets, subscriptions, LLM provider relay metering, marketplace registry, and administrative business telemetry for the Talos platform.

---

## 🚀 Key Features

1. **Authentication & Identity**:
   - Google OAuth 2.0 / OIDC login and callback.
   - Signed HS256 JWT Web Sessions (`type: "web_session"`).
   - Bcrypt-hashed runtime device tokens (`type: "device_token"`).
   - Anti-account hijacking verification.

2. **Dedicated Dual-Balance Wallets**:
   - `monthly_balance` (consumed first, resets every 30 days).
   - `topup_balance` (consumed second, non-expiring).
   - Atomic pre-check holds (`reserve()`), usage commits (`commit()`), and failure refunds (`release()`).

3. **Dedicated Subscriptions & Background Workers**:
   - `subscriptions` table tracking active plans, billing periods, and next reset dates.
   - APScheduler workers: 15-minute subscription resets (idempotent), 5-minute reservation cleanup, and daily 02:00 UTC margin checks.

4. **Truth-First Financial Subsystem**:
   - **Customer Revenue**: Derived 100% from recorded `billing_transactions` (Stripe USD & Razorpay INR).
   - **Provider COGS**: Calculated via `usage_events` using high-precision `NUMERIC(18, 8)` and Python `Decimal` math.
   - **Credit Ledger**: Double-entry ledger (`credit_transactions` and `wallets`).
   - **Zero-Revenue Margin**: Returns `null` (`N/A`) when Revenue is $0.00.

5. **Dynamic PricingEngine & Simulator**:
   - Database-backed `PricingConfiguration` storing `credit_reference_usd`.
   - Admin Pricing Simulator with persisted scenario modeling in `margin_simulations`.

6. **11-Tab Admin Control Center**:
   - Overview KPIs, User directory & balance editor, Plan tiers, Dynamic Pricing & Simulator, Global Economics, Run traces, Immutable Ledger, Audit Logs, and Provider Analytics.

7. **Marketplace Registry**:
   - Centralized package discovery, zip downloads, user installation counts, and moderation.

---

## 🛠️ Local Execution

```bash
# 1. Initialize PostgreSQL Database & Seed PricingConfiguration
python scripts/seed_financial_config.py

# 2. Run Test Suite
pytest

# 3. Start Server
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```
