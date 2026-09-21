"""
Unit tests for Database Scalability & Resilience (Tasks 7, 8, 9).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import Base, check_db_health, get_engine, is_transient_db_error
from app.main import app
from app.services.large_table_policy import (
    RETENTION_POLICIES,
    generate_partition_ddl,
    inspect_table_growth,
)


def test_task_7_composite_indexes_declared_in_metadata():
    """Verify that all performance composite indexes from Task 7 are present on tables."""
    tables = Base.metadata.tables
    
    # usage_events
    usage_indices = {idx.name for idx in tables["usage_events"].indexes}
    assert "ix_usage_events_account_created" in usage_indices
    assert "ix_usage_events_status_created" in usage_indices
    assert "ix_usage_events_account_status" in usage_indices

    # credit_transactions
    credit_indices = {idx.name for idx in tables["credit_transactions"].indexes}
    assert "ix_credit_transactions_account_created" in credit_indices

    # pricing_events
    pricing_indices = {idx.name for idx in tables["pricing_events"].indexes}
    assert "ix_pricing_events_account_created" in pricing_indices
    assert "ix_pricing_events_cap_created" in pricing_indices

    # billing_transactions
    billing_indices = {idx.name for idx in tables["billing_transactions"].indexes}
    assert "ix_billing_transactions_account_created" in billing_indices
    assert "ix_billing_transactions_gateway_status" in billing_indices

    # admin_audit_logs
    admin_indices = {idx.name for idx in tables["admin_audit_logs"].indexes}
    assert "ix_admin_audit_logs_admin_created" in admin_indices
    assert "ix_admin_audit_logs_resource_created" in admin_indices

    # marketplace_listings
    mkt_indices = {idx.name for idx in tables["marketplace_listings"].indexes}
    assert "ix_marketplace_listings_status_kind" in mkt_indices
    assert "ix_marketplace_listings_status_created" in mkt_indices
    assert "ix_marketplace_listings_install_count" in mkt_indices

    # user_installs
    install_indices = {idx.name for idx in tables["user_installs"].indexes}
    assert "ix_user_installs_listing_id" in install_indices

    # device_tokens
    device_indices = {idx.name for idx in tables["device_tokens"].indexes}
    assert "ix_device_tokens_lookup" in device_indices


@pytest.mark.asyncio
async def test_task_8_database_health_checks():
    """Verify /health and /health/db endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. /health
        res = await client.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"
        
        # 2. /health/db
        res_db = await client.get("/health/db")
        assert res_db.status_code == 200
        data = res_db.json()
        assert data["status"] == "healthy"
        assert "latency_ms" in data


def test_task_8_transient_error_detection():
    """Verify classification of transient serverless connection errors."""
    assert is_transient_db_error(Exception("CannotConnectNowError: server is starting"))
    assert is_transient_db_error(Exception("ConnectionResetError: server closed connection"))
    assert not is_transient_db_error(ValueError("syntax error at or near 'SELECT'"))


def test_task_9_retention_policy_and_partitioning():
    """Verify large table retention policies and DDL generation."""
    # Check retention policies definition
    assert "usage_events" in RETENTION_POLICIES
    assert RETENTION_POLICIES["usage_events"].retention_days == 90
    assert RETENTION_POLICIES["credit_transactions"].is_financial_ledger is True
    assert RETENTION_POLICIES["credit_transactions"].retention_days is None  # Permanent

    # Test partition DDL generator
    ddl_monthly = generate_partition_ddl("usage_events", year=2026, month=9)
    assert "CREATE TABLE IF NOT EXISTS usage_events_y2026m09 PARTITION OF usage_events" in ddl_monthly
    assert "FROM ('2026-09-01') TO ('2026-10-01')" in ddl_monthly

    ddl_yearly = generate_partition_ddl("credit_transactions", year=2026)
    assert "CREATE TABLE IF NOT EXISTS credit_transactions_y2026 PARTITION OF credit_transactions" in ddl_yearly
    assert "FROM ('2026-01-01') TO ('2027-01-01')" in ddl_yearly
