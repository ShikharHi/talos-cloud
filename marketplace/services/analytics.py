import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from marketplace.repositories.base import BaseRepository

DAILY_SALT_SECRET = "talos_daily_telemetry_salt_secret_2026"

def generate_visitor_hash(ip: str, user_agent: str, date_str: Optional[str] = None) -> str:
    """Generate salted rotating daily hash without storing raw IP address."""
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = f"{DAILY_SALT_SECRET}:{date_str}:{ip}:{user_agent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

class AnalyticsService:
    def __init__(self):
        self.base_repo = BaseRepository()

    async def rollup_daily_events(self):
        """Aggregate raw page_events into daily_stats and clean up old records."""
        with self.base_repo.get_conn() as conn:
            with conn.cursor() as cur:
                # Rollup events from yesterday or today
                cur.execute("""
                    INSERT INTO daily_stats (day, plugin_id, builder_id, views, installs, share_clicks)
                    SELECT
                        created_at::date as day,
                        plugin_id,
                        builder_id,
                        COUNT(*) FILTER (WHERE kind = 'plugin_view') as views,
                        COUNT(*) FILTER (WHERE kind = 'install_click') as installs,
                        COUNT(*) FILTER (WHERE kind = 'share_click') as share_clicks
                    FROM page_events
                    WHERE plugin_id IS NOT NULL
                    GROUP BY created_at::date, plugin_id, builder_id
                    ON CONFLICT (day, plugin_id) DO UPDATE
                    SET views = EXCLUDED.views,
                        installs = EXCLUDED.installs,
                        share_clicks = EXCLUDED.share_clicks
                """)
                # Purge raw page_events older than 90 days for privacy compliance
                cur.execute("DELETE FROM page_events WHERE created_at < NOW() - INTERVAL '90 days'")
                conn.commit()

analytics_service = AnalyticsService()
