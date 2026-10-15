import math
from typing import Optional, Dict, Any, List
from marketplace.repositories.base import BaseRepository

class RatingsService:
    def __init__(self, dsn: Optional[str] = None):
        self.base_repo = BaseRepository()

    def calculate_bayesian_score(self, rating_avg: float, rating_count: int, global_mean: float, prior_m: float = 10.0) -> float:
        """
        Bayesian weighted score:
        weighted = (v / (v + m)) * R + (m / (v + m)) * C
        """
        if rating_count == 0:
            return round(global_mean, 2)
        v = float(rating_count)
        m = float(prior_m)
        r = float(rating_avg)
        c = float(global_mean)
        score = ((v / (v + m)) * r) + ((m / (v + m)) * c)
        return round(score, 2)

    def calculate_builder_composite(self, plugin_stats: List[Dict[str, Any]], global_mean: float, prior_m: float = 10.0) -> Optional[float]:
        """
        Builder rating:
        rating_count-weighted mean over published plugins, then smoothed with Bayesian prior.
        """
        total_sum = 0
        total_count = 0
        for p in plugin_stats:
            c = p.get("rating_count", 0)
            s = p.get("rating_sum", 0)
            total_count += c
            total_sum += s

        if total_count < 5:
            return None # Show 'New Builder' if under 5 total ratings

        raw_avg = total_sum / total_count
        return self.calculate_bayesian_score(raw_avg, total_count, global_mean, prior_m)

    async def recompute_plugin_and_builder(self, plugin_id: str):
        """Update single plugin's weighted score and builder's aggregate rating."""
        with self.base_repo.get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Get global mean C
                cur.execute("SELECT COALESCE(AVG(rating_avg), 4.2) as global_mean FROM plugins WHERE status = 'published' AND rating_count > 0")
                row = cur.fetchone()
                c = float(row["global_mean"] or 4.2)

                # 2. Update plugin weighted rating
                cur.execute("SELECT id, builder_id, rating_avg, rating_count FROM plugins WHERE id = %s", (plugin_id,))
                p = cur.fetchone()
                if not p:
                    return

                weighted = self.calculate_bayesian_score(float(p["rating_avg"] or 0.0), int(p["rating_count"] or 0), c)
                cur.execute("UPDATE plugins SET rating_weighted = %s WHERE id = %s", (weighted, plugin_id))

                # 3. Update builder aggregate stats
                b_id = p["builder_id"]
                if b_id:
                    cur.execute("""
                        SELECT rating_count, rating_sum
                        FROM plugins
                        WHERE builder_id = %s AND status = 'published'
                    """, (b_id,))
                    plugins_list = [dict(r) for r in cur.fetchall()]
                    b_score = self.calculate_builder_composite(plugins_list, c)

                    cur.execute("""
                        SELECT COUNT(*) as p_count,
                               COALESCE(SUM(downloads_count), 0) as total_installs,
                               COALESCE(SUM(rating_count), 0) as total_ratings
                        FROM plugins
                        WHERE builder_id = %s AND status = 'published'
                    """, (b_id,))
                    b_totals = cur.fetchone()

                    cur.execute("""
                        UPDATE builders
                        SET rating_avg = %s,
                            plugin_count = %s,
                            install_count = %s,
                            rating_count = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (b_score, b_totals["p_count"], b_totals["total_installs"], b_totals["total_ratings"], b_id))

                conn.commit()

    async def reconcile_aggregates(self):
        """Nightly reconciliation verifying denormalized counters match raw truth."""
        with self.base_repo.get_conn() as conn:
            with conn.cursor() as cur:
                # Recompute each plugin's rating_count, rating_sum, and hist from plugin_ratings
                cur.execute("""
                    SELECT pr.plugin_id,
                           COUNT(*) as true_count,
                           SUM(pr.stars) as true_sum,
                           ARRAY[
                               COUNT(*) FILTER (WHERE pr.stars = 1),
                               COUNT(*) FILTER (WHERE pr.stars = 2),
                               COUNT(*) FILTER (WHERE pr.stars = 3),
                               COUNT(*) FILTER (WHERE pr.stars = 4),
                               COUNT(*) FILTER (WHERE pr.stars = 5)
                           ] as true_hist
                    FROM plugin_ratings pr
                    GROUP BY pr.plugin_id
                """)
                for row in cur.fetchall():
                    pid = row["plugin_id"]
                    t_cnt = row["true_count"]
                    t_sum = row["true_sum"]
                    t_hist = row["true_hist"]
                    t_avg = round(t_sum / t_cnt, 2) if t_cnt > 0 else 0.0

                    cur.execute("""
                        UPDATE plugins
                        SET rating_count = %s, rating_sum = %s, rating_hist = %s, rating_avg = %s
                        WHERE id = %s
                    """, (t_cnt, t_sum, t_hist, t_avg, pid))

                conn.commit()

ratings_service = RatingsService()
