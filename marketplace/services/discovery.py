from typing import Optional, Dict, Any, List
from marketplace.repositories.base import BaseRepository

class DiscoveryService:
    def __init__(self):
        self.base_repo = BaseRepository()

    async def get_discovery_plugins(
        self,
        mode: str = "featured", # featured, top_rated, trending, new_and_rising, category
        category: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Query plugins using Bayesian ranking, decay trending, or full text search.
        """
        with self.base_repo.get_conn() as conn:
            with conn.cursor() as cur:
                where_clauses = ["p.status = 'published'"]
                params: List[Any] = []

                if category and category != "all":
                    where_clauses.append("p.category = %s")
                    params.append(category)

                if query:
                    where_clauses.append("""
                        (to_tsvector('english', p.name || ' ' || COALESCE(p.short_desc, '') || ' ' || COALESCE(p.description, '')) @@ plainto_tsquery('english', %s)
                         OR p.name ILIKE %s
                         OR b.username ILIKE %s)
                    """)
                    params.extend([query, f"%{query}%", f"%{query}%"])

                # Determine sort order
                if mode == "top_rated":
                    # Bayesian weighted rating with minimum 5 ratings
                    where_clauses.append("p.rating_count >= 5")
                    order_clause = "ORDER BY p.rating_weighted DESC, p.rating_count DESC"
                elif mode == "trending":
                    # Time-decayed velocity formula
                    order_clause = "ORDER BY (p.downloads_count * 0.7 + COALESCE(p.rating_count, 0) * 2.0) DESC"
                elif mode == "new_and_rising":
                    where_clauses.append("p.published_at >= NOW() - INTERVAL '30 days'")
                    order_clause = "ORDER BY p.downloads_count DESC, p.published_at DESC"
                else: # featured / popular
                    order_clause = "ORDER BY p.is_featured DESC, p.downloads_count DESC"

                where_sql = " AND ".join(where_clauses)
                sql = f"""
                    SELECT p.*,
                           b.username as builder_username,
                           b.display_name as builder_display_name,
                           b.is_verified as builder_verified,
                           b.is_official as builder_official
                    FROM plugins p
                    JOIN builders b ON p.builder_id = b.id
                    WHERE {where_sql}
                    {order_clause}
                    LIMIT %s OFFSET %s
                """
                params.extend([limit, offset])
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

discovery_service = DiscoveryService()
