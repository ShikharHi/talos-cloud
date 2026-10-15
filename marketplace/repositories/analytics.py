from typing import Optional, Dict, Any, List
from datetime import date
from marketplace.repositories.base import BaseRepository

class AnalyticsRepository(BaseRepository):
    async def record_event(
        self,
        kind: str,
        visitor_hash: str,
        plugin_id: Optional[str] = None,
        builder_id: Optional[str] = None,
        referrer: Optional[str] = None,
        utm_source: Optional[str] = None
    ):
        """Append an event into the page_events log."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO page_events (
                        kind, visitor_hash, plugin_id, builder_id, referrer, utm_source
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                """, (kind, visitor_hash, plugin_id, builder_id, referrer, utm_source))

                # Increment denormalized counter
                if kind == "plugin_view" and plugin_id:
                    cur.execute("UPDATE plugins SET page_views = page_views + 1 WHERE id = %s", (plugin_id,))
                elif kind == "builder_view" and builder_id:
                    cur.execute("UPDATE builders SET profile_views = profile_views + 1 WHERE id = %s", (builder_id,))

                conn.commit()

    async def get_builder_analytics(self, builder_id: str, days: int = 30) -> Dict[str, Any]:
        """Aggregate stats over the past N days for the Builder Studio."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COALESCE(SUM(views), 0) as total_views,
                        COALESCE(SUM(installs), 0) as total_installs,
                        COALESCE(SUM(ratings), 0) as total_ratings,
                        COALESCE(SUM(share_clicks), 0) as total_share_clicks
                    FROM daily_stats
                    WHERE builder_id = %s AND day >= CURRENT_DATE - (%s || ' days')::interval
                """, (builder_id, str(days)))
                totals = cur.fetchone()

                cur.execute("""
                    SELECT day, views, installs, ratings, share_clicks
                    FROM daily_stats
                    WHERE builder_id = %s AND day >= CURRENT_DATE - (%s || ' days')::interval
                    ORDER BY day ASC
                """, (builder_id, str(days)))
                series = cur.fetchall()

                return {
                    "totals": dict(totals) if totals else {"total_views": 0, "total_installs": 0, "total_ratings": 0, "total_share_clicks": 0},
                    "timeseries": [dict(s) for s in series]
                }
