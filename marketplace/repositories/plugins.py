import uuid
import json
import re
from typing import Optional, Dict, Any, List
from marketplace.repositories.base import BaseRepository

class PluginRepository(BaseRepository):
    async def get_plugin_by_category_and_slug(self, category: str, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch published plugin by category and slug, with builder profile and versions."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.*,
                           b.username as builder_username,
                           b.display_name as builder_display_name,
                           b.avatar_url as builder_avatar_url,
                           b.is_verified as builder_verified,
                           b.is_official as builder_official
                    FROM plugins p
                    JOIN builders b ON p.builder_id = b.id
                    WHERE p.category = %s AND p.slug = %s AND p.status = 'published'
                """, (category, slug))
                row = cur.fetchone()
                if not row:
                    # Check slug history for redirects
                    cur.execute("""
                        SELECT p.category, p.slug
                        FROM plugin_slug_history psh
                        JOIN plugins p ON psh.plugin_id = p.id
                        WHERE psh.category = %s AND psh.old_slug = %s AND p.status = 'published'
                    """, (category, slug))
                    history = cur.fetchone()
                    if history:
                        return {"redirect_to": f"/plugins/{history['category']}/{history['slug']}"}
                    return None

                data = dict(row)
                # Fetch versions
                cur.execute("""
                    SELECT version, changelog, created_at
                    FROM plugin_versions
                    WHERE plugin_id = %s
                    ORDER BY created_at DESC
                """, (data["id"],))
                data["versions"] = [dict(v) for v in cur.fetchall()]
                return data

    async def get_plugin_by_id(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM plugins WHERE id = %s", (plugin_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    async def record_download_verified(self, user_id: str, plugin_id: str, version: Optional[str] = None):
        """Mark an install as download-verified when package/artifact is retrieved."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                install_id = f"inst_{uuid.uuid4().hex[:12]}"
                cur.execute("""
                    INSERT INTO plugin_installs (
                        id, plugin_id, user_id, version, download_verified_at, installed_at
                    ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (plugin_id, user_id) DO UPDATE
                    SET download_verified_at = CURRENT_TIMESTAMP,
                        version = COALESCE(EXCLUDED.version, plugin_installs.version),
                        uninstalled_at = NULL
                """, (install_id, plugin_id, user_id, version))
                cur.execute("UPDATE plugins SET downloads_count = downloads_count + 1 WHERE id = %s", (plugin_id,))
                conn.commit()

    async def check_user_install_verified(self, user_id: str, plugin_id: str) -> bool:
        """Check if user has an authentic verified download for this plugin."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM plugin_installs
                    WHERE user_id = %s AND plugin_id = %s AND download_verified_at IS NOT NULL
                """, (user_id, plugin_id))
                return cur.fetchone() is not None

    async def record_user_uninstall(self, user_id: str, plugin_id: str):
        """Mark an installation as uninstalled."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE plugin_installs
                    SET uninstalled_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s AND plugin_id = %s
                """, (user_id, plugin_id))
                conn.commit()

    async def create_plugin_draft(
        self,
        builder_id: str,
        name: str,
        slug: str,
        category: str,
        short_desc: str,
        plugin_type: str = "agent"
    ) -> Dict[str, Any]:
        """Create a new draft plugin in Studio."""
        plugin_id = f"plg_{uuid.uuid4().hex[:12]}"
        clean_slug = re.sub(r'[^a-z0-9_-]', '', slug.lower().strip())
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO plugins (
                        id, builder_id, name, slug, category, short_desc, plugin_type,
                        status, publisher_id, description
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s)
                    RETURNING *
                """, (plugin_id, builder_id, name, clean_slug, category, short_desc, plugin_type, builder_id, short_desc))
                row = cur.fetchone()
                conn.commit()
                return dict(row)

    async def update_plugin_metadata(
        self,
        plugin_id: str,
        builder_id: str,
        name: Optional[str] = None,
        new_slug: Optional[str] = None,
        short_desc: Optional[str] = None,
        long_desc_md: Optional[str] = None,
        requirements: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        supported_models: Optional[List[str]] = None,
        screenshots: Optional[List[str]] = None,
        demo_url: Optional[str] = None,
        category: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Update draft or published plugin details, recording slug rename history if slug changes."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT slug, category FROM plugins WHERE id = %s AND builder_id = %s", (plugin_id, builder_id))
                existing = cur.fetchone()
                if not existing:
                    return None

                old_slug = existing["slug"]
                old_category = existing["category"]

                # Handle slug rename
                if new_slug and new_slug != old_slug:
                    cur.execute("""
                        INSERT INTO plugin_slug_history (plugin_id, old_slug, category)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (category, old_slug) DO NOTHING
                    """, (plugin_id, old_slug, old_category))

                cur.execute("""
                    UPDATE plugins
                    SET name = COALESCE(%s, name),
                        slug = COALESCE(%s, slug),
                        short_desc = COALESCE(%s, short_desc),
                        long_desc_md = COALESCE(%s, long_desc_md),
                        requirements = COALESCE(%s, requirements),
                        permissions = COALESCE(%s::jsonb, permissions),
                        supported_models = COALESCE(%s::jsonb, supported_models),
                        screenshots = COALESCE(%s::jsonb, screenshots),
                        demo_url = COALESCE(%s, demo_url),
                        category = COALESCE(%s, category),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND builder_id = %s
                    RETURNING *
                """, (
                    name,
                    new_slug,
                    short_desc,
                    long_desc_md,
                    requirements,
                    json.dumps(permissions) if permissions is not None else None,
                    json.dumps(supported_models) if supported_models is not None else None,
                    json.dumps(screenshots) if screenshots is not None else None,
                    demo_url,
                    category,
                    plugin_id,
                    builder_id
                ))
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None

    async def set_plugin_status(self, plugin_id: str, builder_id: str, status: str) -> bool:
        """Publish, unpublish, or unlist a plugin."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE plugins
                    SET status = %s,
                        published_at = CASE WHEN %s = 'published' AND published_at IS NULL THEN CURRENT_TIMESTAMP ELSE published_at END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND builder_id = %s
                """, (status, status, plugin_id, builder_id))
                conn.commit()
                return cur.rowcount > 0

    async def add_plugin_version(
        self,
        plugin_id: str,
        version: str,
        changelog: str,
        artifact_url: str = "",
        manifest: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Create a new version record for a plugin."""
        ver_id = f"pver_{uuid.uuid4().hex[:12]}"
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO plugin_versions (
                        id, plugin_id, version, changelog, manifest, status
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, 'published')
                    ON CONFLICT (plugin_id, version) DO UPDATE
                    SET changelog = EXCLUDED.changelog,
                        manifest = EXCLUDED.manifest
                    RETURNING *
                """, (ver_id, plugin_id, version, changelog, json.dumps(manifest or {})))
                cur.execute("UPDATE plugins SET latest_version = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (version, plugin_id))
                row = cur.fetchone()
                conn.commit()
                return dict(row)
