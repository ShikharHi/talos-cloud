import uuid
from typing import Optional, Dict, Any, List
from marketplace.repositories.base import BaseRepository

class RatingRepository(BaseRepository):
    async def get_user_rating(self, user_id: str, plugin_id: str) -> Optional[Dict[str, Any]]:
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM plugin_ratings WHERE user_id = %s AND plugin_id = %s", (user_id, plugin_id))
                row = cur.fetchone()
                return dict(row) if row else None

    async def upsert_rating(self, user_id: str, plugin_id: str, stars: int, version_used: Optional[str] = None) -> Dict[str, Any]:
        """Atomically upsert a user rating, and update plugin rating histogram and aggregates."""
        stars = max(1, min(5, stars))
        rating_id = f"rat_{uuid.uuid4().hex[:12]}"

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Fetch old rating if exists
                cur.execute("SELECT stars FROM plugin_ratings WHERE user_id = %s AND plugin_id = %s", (user_id, plugin_id))
                old_row = cur.fetchone()
                old_stars = old_row["stars"] if old_row else None

                # 2. Upsert rating record
                cur.execute("""
                    INSERT INTO plugin_ratings (id, plugin_id, user_id, stars, version_used, updated_at)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (plugin_id, user_id) DO UPDATE
                    SET stars = EXCLUDED.stars,
                        version_used = COALESCE(EXCLUDED.version_used, plugin_ratings.version_used),
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING *
                """, (rating_id, plugin_id, user_id, stars, version_used))
                saved_rating = dict(cur.fetchone())

                # 3. Read current plugin stats
                cur.execute("SELECT rating_count, rating_sum, rating_hist FROM plugins WHERE id = %s FOR UPDATE", (plugin_id,))
                p_stat = cur.fetchone()
                if not p_stat:
                    conn.commit()
                    return saved_rating

                r_count = p_stat["rating_count"] or 0
                r_sum = p_stat["rating_sum"] or 0
                hist = list(p_stat["rating_hist"] or [0, 0, 0, 0, 0])
                while len(hist) < 5:
                    hist.append(0)

                # 4. Adjust histogram and sums
                if old_stars is not None:
                    # Update existing
                    hist[old_stars - 1] = max(0, hist[old_stars - 1] - 1)
                    r_sum -= old_stars
                else:
                    # Brand new rating
                    r_count += 1

                hist[stars - 1] += 1
                r_sum += stars
                raw_avg = round(r_sum / r_count, 2) if r_count > 0 else 0.0

                cur.execute("""
                    UPDATE plugins
                    SET rating_count = %s,
                        rating_sum = %s,
                        rating_avg = %s,
                        rating_hist = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (r_count, r_sum, raw_avg, hist, plugin_id))

                conn.commit()
                return saved_rating

    async def delete_rating(self, user_id: str, plugin_id: str) -> bool:
        """Remove a rating and adjust histograms atomically."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT stars FROM plugin_ratings WHERE user_id = %s AND plugin_id = %s", (user_id, plugin_id))
                row = cur.fetchone()
                if not row:
                    return False
                old_stars = row["stars"]

                cur.execute("DELETE FROM plugin_ratings WHERE user_id = %s AND plugin_id = %s", (user_id, plugin_id))

                cur.execute("SELECT rating_count, rating_sum, rating_hist FROM plugins WHERE id = %s FOR UPDATE", (plugin_id,))
                p_stat = cur.fetchone()
                if p_stat:
                    r_count = max(0, (p_stat["rating_count"] or 0) - 1)
                    r_sum = max(0, (p_stat["rating_sum"] or 0) - old_stars)
                    hist = list(p_stat["rating_hist"] or [0, 0, 0, 0, 0])
                    hist[old_stars - 1] = max(0, hist[old_stars - 1] - 1)
                    raw_avg = round(r_sum / r_count, 2) if r_count > 0 else 0.0

                    cur.execute("""
                        UPDATE plugins
                        SET rating_count = %s,
                            rating_sum = %s,
                            rating_avg = %s,
                            rating_hist = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (r_count, r_sum, raw_avg, hist, plugin_id))

                conn.commit()
                return True
