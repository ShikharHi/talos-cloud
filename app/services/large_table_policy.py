"""
Talos Cloud — Large Table Strategy & Retention Policy (Task 9).

Architectural Strategy for High-Volume Append-Only Tables:
1. usage_events:
   - Growth: Highest volume table in Talos Cloud (one row per LLM call/stream/step).
   - Hot Retention: 90 days in primary Postgres storage.
   - Cold Storage: Exported to Tigris S3 parquet/JSONL archives (`archives/usage_events/YYYY/MM/`)
     for analytical and compliance queries.
   - Partitioning Threshold: 5,000,000 rows.
   - Partition Strategy: Declarative Range Partitioning by `created_at` (Monthly: `usage_events_yYYYYmMM`).

2. credit_transactions:
   - Financial Ledger: Under zero financial leakage invariants, credit ledger entries are PERMANENT.
   - Hot Retention: Permanent. Never hard-deleted.
   - Partitioning Threshold: 5,000,000 rows.
   - Partition Strategy: Declarative Range Partitioning by `created_at` (Annual: `credit_transactions_yYYYY`).

3. pricing_events:
   - Metered Provider Record: Authoritative pricing snapshot.
   - Hot Retention: 180 days in primary Postgres storage.
   - Aggregation: Hourly/daily rollups in `task_summary` / `margin_snapshots`.

4. admin_audit_logs:
   - Security Audit Trail: Permanent security record.
   - Hot Retention: 365 days.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_audit_log import AdminAuditLog
from app.models.ledger import CreditTransaction, PricingEvent
from app.models.usage_event import UsageEvent

logger = logging.getLogger("talos.large_table_policy")


@dataclass(frozen=True)
class TableRetentionPolicy:
    table_name: str
    retention_days: int | None  # None = permanent retention
    partition_threshold_rows: int
    partition_interval: str  # "monthly" | "yearly" | "none"
    cold_storage_target: str | None
    is_financial_ledger: bool = False


RETENTION_POLICIES: dict[str, TableRetentionPolicy] = {
    "usage_events": TableRetentionPolicy(
        table_name="usage_events",
        retention_days=90,
        partition_threshold_rows=5_000_000,
        partition_interval="monthly",
        cold_storage_target="tigris://talos-archives/usage_events/",
        is_financial_ledger=False,
    ),
    "pricing_events": TableRetentionPolicy(
        table_name="pricing_events",
        retention_days=180,
        partition_threshold_rows=5_000_000,
        partition_interval="monthly",
        cold_storage_target="tigris://talos-archives/pricing_events/",
        is_financial_ledger=False,
    ),
    "credit_transactions": TableRetentionPolicy(
        table_name="credit_transactions",
        retention_days=None,  # Permanent audit trail: NEVER delete
        partition_threshold_rows=5_000_000,
        partition_interval="yearly",
        cold_storage_target=None,
        is_financial_ledger=True,
    ),
    "admin_audit_logs": TableRetentionPolicy(
        table_name="admin_audit_logs",
        retention_days=365,
        partition_threshold_rows=2_000_000,
        partition_interval="yearly",
        cold_storage_target="tigris://talos-archives/admin_audit_logs/",
        is_financial_ledger=False,
    ),
}


async def inspect_table_growth(db: AsyncSession) -> dict[str, Any]:
    """
    Inspects row counts and table sizes to verify health against partition thresholds.
    """
    stats: dict[str, Any] = {}

    for table_name, policy in RETENTION_POLICIES.items():
        try:
            query = text(f"SELECT COUNT(*) FROM {table_name}")
            result = await db.execute(query)
            row_count = result.scalar() or 0
            
            exceeds_threshold = row_count >= policy.partition_threshold_rows
            stats[table_name] = {
                "row_count": row_count,
                "retention_days": policy.retention_days,
                "partition_threshold_rows": policy.partition_threshold_rows,
                "partition_interval": policy.partition_interval,
                "partitioning_recommended": exceeds_threshold,
                "is_financial_ledger": policy.is_financial_ledger,
            }
        except Exception as e:
            stats[table_name] = {
                "error": str(e),
                "policy": {
                    "retention_days": policy.retention_days,
                    "partition_threshold_rows": policy.partition_threshold_rows,
                },
            }

    return stats


def generate_partition_ddl(table_name: str, year: int, month: int | None = None) -> str:
    """
    Generates declarative PostgreSQL partition DDL statements for usage when
    tables exceed threshold in production environments.
    """
    if month is not None:
        # Monthly partition
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1
        part_name = f"{table_name}_y{year:04d}m{month:02d}"
        from_date = f"{year:04d}-{month:02d}-01"
        to_date = f"{next_year:04d}-{next_month:02d}-01"
        return (
            f"CREATE TABLE IF NOT EXISTS {part_name} PARTITION OF {table_name} "
            f"FOR VALUES FROM ('{from_date}') TO ('{to_date}');"
        )
    else:
        # Yearly partition
        part_name = f"{table_name}_y{year:04d}"
        from_date = f"{year:04d}-01-01"
        to_date = f"{year+1:04d}-01-01"
        return (
            f"CREATE TABLE IF NOT EXISTS {part_name} PARTITION OF {table_name} "
            f"FOR VALUES FROM ('{from_date}') TO ('{to_date}');"
        )
