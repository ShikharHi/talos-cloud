import uuid
import json
import re
from typing import Optional, Dict, Any, List
from marketplace.repositories.base import BaseRepository
from marketplace.services.sanitizer import is_username_reserved, validate_username_format

class BuilderRepository(BaseRepository):
    async def get_or_create_builder(self, user_id: str, username: str, display_name: str, bio: Optional[str] = None) -> Dict[str, Any]:
        """Fetch or create a builder record for the given user, automatically sourcing profile info from account."""
        clean_user = username.lower().strip()
        if is_username_reserved(clean_user):
            clean_user = f"{clean_user}_{uuid.uuid4().hex[:4]}"
        elif not validate_username_format(clean_user):
            clean_user = f"builder_{uuid.uuid4().hex[:6]}"

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT b.*
                    FROM builders b
                    WHERE b.user_id = %s
                """, (user_id,))
                row = cur.fetchone()
                if row:
                    return dict(row)

                # Ensure unique username
                cur.execute("SELECT id FROM builders WHERE username = %s", (clean_user,))
                if cur.fetchone():
                    clean_user = f"{clean_user}_{uuid.uuid4().hex[:4]}"

                cur.execute("""
                    INSERT INTO builders (
                        id, user_id, username, display_name, bio, status
                    ) VALUES (%s, %s, %s, %s, %s, 'active')
                    RETURNING *
                """, (user_id, user_id, clean_user, display_name, bio))
                new_row = dict(cur.fetchone())
                conn.commit()
                return new_row

    async def get_builder_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get public builder profile."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT b.*
                    FROM builders b
                    WHERE b.username = %s AND b.status = 'active'
                """, (username.lower().strip(),))
                row = cur.fetchone()
                return dict(row) if row else None

    async def get_builder_by_id(self, builder_id: str) -> Optional[Dict[str, Any]]:
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT b.*
                    FROM builders b
                    WHERE b.id = %s
                """, (builder_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    async def update_profile(
        self,
        builder_id: str,
        username: Optional[str] = None,
        display_name: Optional[str] = None,
        bio: Optional[str] = None,
        website_url: Optional[str] = None,
        social_links: Optional[Dict[str, Any]] = None,
        avatar_url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Update builder profile details."""
        clean_user = None
        if username:
            clean_user = username.lower().strip()
            if is_username_reserved(clean_user) or not validate_username_format(clean_user):
                clean_user = None

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if clean_user:
                    # Verify no one else has claimed this username
                    cur.execute("SELECT id FROM builders WHERE username = %s AND id != %s", (clean_user, builder_id))
                    if cur.fetchone():
                        clean_user = None

                cur.execute("""
                    UPDATE builders
                    SET username = COALESCE(%s::text, username),
                        display_name = COALESCE(%s::text, display_name),
                        bio = COALESCE(%s::text, bio),
                        website_url = CASE WHEN %s::text IS NULL THEN website_url ELSE NULLIF(%s::text, '') END,
                        social_links = COALESCE(%s::jsonb, social_links),
                        avatar_url = COALESCE(%s::text, avatar_url),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING *
                """, (
                    clean_user,
                    display_name,
                    bio[:500] if bio is not None else None,
                    website_url,
                    website_url,
                    json.dumps(social_links) if social_links is not None else None,
                    avatar_url,
                    builder_id
                ))
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None

    async def list_builders_directory(self, sort_by: str = "rating", limit: int = 24, offset: int = 0) -> List[Dict[str, Any]]:
        """List active builders with their aggregate statistics."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                order_clause = "ORDER BY b.rating_avg DESC NULLS LAST, b.install_count DESC"
                if sort_by == "installs":
                    order_clause = "ORDER BY b.install_count DESC"
                elif sort_by == "plugins":
                    order_clause = "ORDER BY b.plugin_count DESC"
                elif sort_by == "recent":
                    order_clause = "ORDER BY b.created_at DESC"

                cur.execute(f"""
                    SELECT b.*
                    FROM builders b
                    WHERE b.status = 'active'
                    {order_clause}
                    LIMIT %s OFFSET %s
                """, (limit, offset))
                rows = cur.fetchall()
                return [dict(r) for r in rows]
