import uuid
from typing import Optional, Dict, Any, List
from marketplace.repositories.base import BaseRepository

class ReviewRepository(BaseRepository):
    async def create_review(
        self,
        user_id: str,
        plugin_id: str,
        body: str,
        rating_id: Optional[str] = None,
        parent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Post a top-level review or a builder reply."""
        review_id = f"rev_{uuid.uuid4().hex[:12]}"
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                # If replying, ensure only the plugin's builder can reply to reviews of their plugin
                if parent_id:
                    cur.execute("""
                        SELECT p.builder_id
                        FROM plugin_reviews pr
                        JOIN plugins p ON pr.plugin_id = p.id
                        WHERE pr.id = %s
                    """, (parent_id,))
                    p_row = cur.fetchone()
                    if not p_row or p_row["builder_id"] != user_id:
                        raise PermissionError("Only the plugin's author can reply to reviews.")

                cur.execute("""
                    INSERT INTO plugin_reviews (
                        id, plugin_id, user_id, rating_id, parent_id, body, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'visible')
                    RETURNING *
                """, (review_id, plugin_id, user_id, rating_id, parent_id, body.strip()))
                row = cur.fetchone()

                # Increment review count on builder
                cur.execute("""
                    UPDATE builders b
                    SET review_count = review_count + 1
                    FROM plugins p
                    WHERE p.id = %s AND b.id = p.builder_id
                """, (plugin_id,))

                conn.commit()
                return dict(row)

    async def list_reviews(
        self,
        plugin_id: str,
        sort_by: str = "helpful", # helpful, newest, highest, lowest
        limit: int = 20,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """List top-level reviews along with nested builder replies."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                order_clause = "ORDER BY pr.helpful_count DESC, pr.created_at DESC"
                if sort_by == "newest":
                    order_clause = "ORDER BY pr.created_at DESC"
                elif sort_by == "highest":
                    order_clause = "ORDER BY prate.stars DESC NULLS LAST, pr.created_at DESC"
                elif sort_by == "lowest":
                    order_clause = "ORDER BY prate.stars ASC NULLS LAST, pr.created_at DESC"

                cur.execute(f"""
                    SELECT pr.id, pr.plugin_id, pr.user_id, pr.body, pr.helpful_count,
                           pr.created_at, pr.updated_at,
                           u.display_name as reviewer_name,
                           prate.stars as rating_stars,
                           prate.version_used,
                           (
                               SELECT json_agg(json_build_object(
                                   'id', rep.id,
                                   'user_id', rep.user_id,
                                   'body', rep.body,
                                   'created_at', rep.created_at,
                                   'author_name', b.display_name
                               ))
                               FROM plugin_reviews rep
                               JOIN builders b ON rep.user_id = b.id
                               WHERE rep.parent_id = pr.id AND rep.status = 'visible'
                           ) as replies
                    FROM plugin_reviews pr
                    JOIN users u ON pr.user_id = u.id
                    LEFT JOIN plugin_ratings prate ON pr.rating_id = prate.id
                    WHERE pr.plugin_id = %s AND pr.parent_id IS NULL AND pr.status = 'visible'
                    {order_clause}
                    LIMIT %s OFFSET %s
                """, (plugin_id, limit, offset))
                rows = cur.fetchall()
                return [dict(r) for r in rows]

    async def vote_helpful(self, user_id: str, review_id: str) -> Dict[str, Any]:
        """Cast or toggle a helpful vote for a review."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM review_votes WHERE review_id = %s AND user_id = %s", (review_id, user_id))
                exists = cur.fetchone() is not None
                if exists:
                    cur.execute("DELETE FROM review_votes WHERE review_id = %s AND user_id = %s", (review_id, user_id))
                    cur.execute("UPDATE plugin_reviews SET helpful_count = GREATEST(0, helpful_count - 1) WHERE id = %s", (review_id,))
                    voted = False
                else:
                    cur.execute("INSERT INTO review_votes (review_id, user_id) VALUES (%s, %s)", (review_id, user_id))
                    cur.execute("UPDATE plugin_reviews SET helpful_count = helpful_count + 1 WHERE id = %s", (review_id,))
                    voted = True

                cur.execute("SELECT helpful_count FROM plugin_reviews WHERE id = %s", (review_id,))
                row = cur.fetchone()
                conn.commit()
                return {"voted": voted, "helpful_count": row["helpful_count"] if row else 0}

    async def report_review(self, reporter_id: str, review_id: str, reason: str) -> Dict[str, Any]:
        """Report a review for administrative moderation."""
        rep_id = f"rpt_{uuid.uuid4().hex[:12]}"
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO review_reports (id, review_id, reporter_id, reason, status)
                    VALUES (%s, %s, %s, %s, 'open')
                    RETURNING *
                """, (rep_id, review_id, reporter_id, reason.strip()))
                row = cur.fetchone()
                conn.commit()
                return dict(row)

    async def list_reports(self, status: str = "open", limit: int = 50) -> List[Dict[str, Any]]:
        """List reports for the admin moderation queue."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT rr.*, pr.body as review_body, pr.user_id as review_author_id,
                           u.display_name as reporter_name
                    FROM review_reports rr
                    JOIN plugin_reviews pr ON rr.review_id = pr.id
                    JOIN users u ON rr.reporter_id = u.id
                    WHERE rr.status = %s
                    ORDER BY rr.created_at DESC
                    LIMIT %s
                """, (status, limit))
                rows = cur.fetchall()
                return [dict(r) for r in rows]

    async def set_review_status(self, review_id: str, status: str) -> bool:
        """Hide or remove a review (admin action)."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE plugin_reviews SET status = %s WHERE id = %s", (status, review_id))
                conn.commit()
                return cur.rowcount > 0
