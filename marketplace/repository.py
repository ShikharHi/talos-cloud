import os
import psycopg
from psycopg.rows import dict_row
import json
import uuid
import re
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from config import config

from neon_pool import neon_pool

class MarketplaceRepository:
    def __init__(self, dsn: str = config.POSTGRES_URL):
        self.dsn = dsn

    def _get_conn(self):
        return neon_pool.connection()

    async def init_db(self):
        """Create skills registry, versions, publisher, stars, and user installations tables."""
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                # 1. Publishers
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS publishers (
                        id TEXT PRIMARY KEY,
                        handle TEXT UNIQUE NOT NULL,
                        display_name TEXT,
                        verified BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # 2. Skills
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS skills (
                        id TEXT PRIMARY KEY,
                        publisher_id TEXT NOT NULL REFERENCES publishers(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        namespace TEXT UNIQUE NOT NULL,
                        description TEXT NOT NULL,
                        tags JSONB DEFAULT '[]'::jsonb,
                        icon TEXT DEFAULT 'bot',
                        downloads_count INTEGER DEFAULT 0,
                        stars_count INTEGER DEFAULT 0,
                        is_featured BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # 3. Skill Versions
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS skill_versions (
                        id TEXT PRIMARY KEY,
                        skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
                        version TEXT NOT NULL,
                        manifest_md TEXT NOT NULL,
                        frontmatter_json JSONB NOT NULL,
                        changelog TEXT DEFAULT '',
                        bundle_hash TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(skill_id, version)
                    );
                """)
                # 4. User Installed Skills
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_installed_skills (
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
                        is_active BOOLEAN DEFAULT TRUE,
                        installed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(user_id, skill_id)
                    );
                """)
                # 5. Skill Stars
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS skill_stars (
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
                        starred_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(user_id, skill_id)
                    );
                """)
                # 6. Ensure zip_path exists for package persistence
                cur.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS zip_path TEXT;")
                cur.execute("ALTER TABLE skill_versions ADD COLUMN IF NOT EXISTS zip_path TEXT;")

                # 7. Plugins (unified: skill/agent/mcp)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS plugins (
                        id TEXT PRIMARY KEY,
                        slug TEXT NOT NULL,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL,
                        plugin_type TEXT NOT NULL CHECK(plugin_type IN ('skill','agent','mcp')),
                        publisher_id TEXT NOT NULL REFERENCES publishers(id) ON DELETE CASCADE,
                        latest_version TEXT DEFAULT '1.0.0',
                        icon_url TEXT,
                        metadata JSONB DEFAULT '{}'::jsonb,
                        configuration_schema JSONB DEFAULT '[]'::jsonb,
                        capabilities_snapshot JSONB DEFAULT '{}'::jsonb,
                        endpoint_url TEXT,
                        artifact_id TEXT,
                        artifact_sha256 TEXT,
                        zip_path TEXT,
                        downloads_count INTEGER DEFAULT 0,
                        stars_count INTEGER DEFAULT 0,
                        is_featured BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 8. Plugin Versions
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS plugin_versions (
                        id TEXT PRIMARY KEY,
                        plugin_id TEXT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
                        version TEXT NOT NULL,
                        changelog TEXT DEFAULT '',
                        artifact_id TEXT,
                        artifact_sha256 TEXT,
                        manifest JSONB DEFAULT '{}'::jsonb,
                        capabilities_snapshot JSONB DEFAULT '{}'::jsonb,
                        zip_path TEXT,
                        status TEXT DEFAULT 'published',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(plugin_id, version)
                    );
                """)
                cur.execute("ALTER TABLE plugin_versions ADD COLUMN IF NOT EXISTS changelog TEXT DEFAULT '';")

                # 9. User Installed Plugins
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_installed_plugins (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        plugin_id TEXT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
                        plugin_version_id TEXT REFERENCES plugin_versions(id),
                        is_active BOOLEAN DEFAULT TRUE,
                        local_install_id TEXT,
                        configuration_metadata JSONB DEFAULT '{}'::jsonb,
                        install_status TEXT DEFAULT 'ready',
                        install_logs TEXT DEFAULT '',
                        install_error TEXT,
                        installed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, plugin_id)
                    );
                """)

                # 10. Runtime Tokens: scoped tokens for sub-agent relay auth
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS runtime_tokens (
                        id TEXT PRIMARY KEY,
                        token_hash TEXT UNIQUE NOT NULL,
                        user_id TEXT NOT NULL,
                        plugin_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        revoked BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 11. Social Layer: Builders
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS builders (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                        username TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        avatar_url TEXT,
                        bio TEXT,
                        website_url TEXT,
                        social_links JSONB NOT NULL DEFAULT '{}'::jsonb,
                        is_verified BOOLEAN NOT NULL DEFAULT FALSE,
                        is_official BOOLEAN NOT NULL DEFAULT FALSE,
                        status TEXT NOT NULL DEFAULT 'active',
                        plugin_count INT NOT NULL DEFAULT 0,
                        install_count BIGINT NOT NULL DEFAULT 0,
                        rating_count INT NOT NULL DEFAULT 0,
                        review_count INT NOT NULL DEFAULT 0,
                        rating_avg NUMERIC(3,2),
                        profile_views BIGINT NOT NULL DEFAULT 0,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 12. Extend plugins with social & discovery columns
                cur.execute("""
                    ALTER TABLE plugins
                        ADD COLUMN IF NOT EXISTS builder_id TEXT REFERENCES builders(id),
                        ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'agents',
                        ADD COLUMN IF NOT EXISTS short_desc TEXT,
                        ADD COLUMN IF NOT EXISTS long_desc_md TEXT,
                        ADD COLUMN IF NOT EXISTS requirements TEXT,
                        ADD COLUMN IF NOT EXISTS permissions JSONB DEFAULT '[]'::jsonb,
                        ADD COLUMN IF NOT EXISTS supported_models JSONB DEFAULT '[]'::jsonb,
                        ADD COLUMN IF NOT EXISTS screenshots JSONB DEFAULT '[]'::jsonb,
                        ADD COLUMN IF NOT EXISTS demo_url TEXT,
                        ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'published',
                        ADD COLUMN IF NOT EXISTS published_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        ADD COLUMN IF NOT EXISTS rating_count INT DEFAULT 0,
                        ADD COLUMN IF NOT EXISTS rating_sum INT DEFAULT 0,
                        ADD COLUMN IF NOT EXISTS rating_avg NUMERIC(3,2),
                        ADD COLUMN IF NOT EXISTS rating_weighted NUMERIC(3,2),
                        ADD COLUMN IF NOT EXISTS rating_hist INT[] DEFAULT '{0,0,0,0,0}',
                        ADD COLUMN IF NOT EXISTS page_views BIGINT DEFAULT 0;
                """)
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_plugins_cat_slug ON plugins (category, slug);")

                # 13. Plugin Slug History for 301 redirects
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS plugin_slug_history (
                        id TEXT PRIMARY KEY,
                        plugin_id TEXT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
                        old_slug TEXT NOT NULL,
                        category TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(category, old_slug)
                    );
                """)

                # 14. Verified Plugin Installs (Eligibility Gate)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS plugin_installs (
                        id TEXT PRIMARY KEY,
                        plugin_id TEXT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        version TEXT,
                        download_verified_at TIMESTAMP WITH TIME ZONE,
                        installed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        uninstalled_at TIMESTAMP WITH TIME ZONE,
                        UNIQUE(plugin_id, user_id)
                    );
                """)

                # 15. Plugin Ratings (1-5 stars)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS plugin_ratings (
                        id TEXT PRIMARY KEY,
                        plugin_id TEXT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        stars SMALLINT NOT NULL CHECK (stars BETWEEN 1 AND 5),
                        version_used TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(plugin_id, user_id)
                    );
                """)

                # 16. Plugin Reviews & Replies
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS plugin_reviews (
                        id TEXT PRIMARY KEY,
                        plugin_id TEXT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        rating_id TEXT REFERENCES plugin_ratings(id) ON DELETE SET NULL,
                        parent_id TEXT REFERENCES plugin_reviews(id) ON DELETE CASCADE,
                        body TEXT NOT NULL,
                        helpful_count INT DEFAULT 0,
                        status TEXT DEFAULT 'visible',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_plugin ON plugin_reviews (plugin_id, created_at DESC);")

                # 17. Review Votes & Reports
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS review_votes (
                        review_id TEXT NOT NULL REFERENCES plugin_reviews(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(review_id, user_id)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS review_reports (
                        id TEXT PRIMARY KEY,
                        review_id TEXT NOT NULL REFERENCES plugin_reviews(id) ON DELETE CASCADE,
                        reporter_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        reason TEXT NOT NULL,
                        status TEXT DEFAULT 'open',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)



                # 19. Analytics Events & Daily Stats
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS page_events (
                        id BIGSERIAL PRIMARY KEY,
                        kind TEXT NOT NULL,
                        plugin_id TEXT,
                        builder_id TEXT,
                        referrer TEXT,
                        utm_source TEXT,
                        visitor_hash TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS daily_stats (
                        day DATE NOT NULL,
                        plugin_id TEXT NOT NULL,
                        builder_id TEXT,
                        views INT DEFAULT 0,
                        installs INT DEFAULT 0,
                        ratings INT DEFAULT 0,
                        share_clicks INT DEFAULT 0,
                        PRIMARY KEY(day, plugin_id)
                    );
                """)

                # 20. Backfill: Map existing publishers and plugins to builders
                cur.execute("""
                    INSERT INTO builders (id, user_id, username, display_name, is_verified)
                    SELECT p.id, p.id, LOWER(p.handle), COALESCE(p.display_name, p.handle), p.verified
                    FROM publishers p
                    WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = p.id)
                    ON CONFLICT (id) DO NOTHING;
                """)
                cur.execute("""
                    UPDATE plugins p
                    SET builder_id = p.publisher_id
                    WHERE p.builder_id IS NULL AND EXISTS (SELECT 1 FROM builders b WHERE b.id = p.publisher_id);
                """)
                # Backfill user_installed_plugins to plugin_installs
                cur.execute("""
                    INSERT INTO plugin_installs (id, plugin_id, user_id, download_verified_at, installed_at)
                    SELECT 'inst_' || SUBSTRING(MD5(RANDOM()::TEXT), 1, 12), uip.plugin_id, uip.user_id, uip.installed_at, uip.installed_at
                    FROM user_installed_plugins uip
                    ON CONFLICT (plugin_id, user_id) DO NOTHING;
                """)

                conn.commit()

    async def _seed_default_skills(self):
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # Create official publisher if not exists
                pub_id = "pub_talos_official"
                cur.execute(
                    "INSERT INTO publishers (id, handle, display_name, verified) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (pub_id, "talos", "Talos Core Team", True)
                )
                conn.commit()

    async def list_skills(
        self,
        query: Optional[str] = None,
        tag: Optional[str] = None,
        sort_by: str = "popular", # popular, stars, recent
        current_user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List skills from the registry with publisher info and user installation status."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                where_clauses = ["1=1"]
                params: List[Any] = []

                if query:
                    where_clauses.append("(s.name ILIKE %s OR s.description ILIKE %s OR s.namespace ILIKE %s)")
                    wildcard = f"%{query.strip()}%"
                    params.extend([wildcard, wildcard, wildcard])

                if tag and tag != "all":
                    where_clauses.append("s.tags @> %s::jsonb")
                    params.append(json.dumps([tag.lower()]))

                order_clause = "ORDER BY s.downloads_count DESC"
                if sort_by == "stars":
                    order_clause = "ORDER BY s.stars_count DESC"
                elif sort_by == "recent":
                    order_clause = "ORDER BY s.created_at DESC"
                elif sort_by == "featured":
                    order_clause = "ORDER BY s.is_featured DESC, s.downloads_count DESC"

                where_sql = " AND ".join(where_clauses)

                sql = f"""
                    SELECT 
                        s.id, s.publisher_id, s.name, s.namespace, s.description, s.tags, s.icon, s.zip_path,
                        s.downloads_count, s.stars_count, s.is_featured, s.created_at,
                        p.handle as publisher_handle, p.display_name as publisher_name, p.verified as publisher_verified,
                        COALESCE(v.version, '1.0.0') as latest_version,
                        EXISTS(
                            SELECT 1 FROM user_installed_skills uis 
                            WHERE uis.skill_id = s.id AND uis.user_id = %s AND uis.is_active = TRUE
                        ) as is_installed,
                        EXISTS(
                            SELECT 1 FROM skill_stars ss
                            WHERE ss.skill_id = s.id AND ss.user_id = %s
                        ) as is_starred,
                        (s.publisher_id = %s) as is_owner
                    FROM skills s
                    JOIN publishers p ON s.publisher_id = p.id
                    LEFT JOIN LATERAL (
                        SELECT version FROM skill_versions sv
                        WHERE sv.skill_id = s.id
                        ORDER BY sv.created_at DESC LIMIT 1
                    ) v ON true
                    WHERE {where_sql}
                    {order_clause}
                """
                cur.execute(sql, [current_user_id or "", current_user_id or "", current_user_id or ""] + params)
                rows = cur.fetchall()
                valid_skills = []
                for r in rows:
                    s_dict = dict(r)
                    zp = s_dict.get("zip_path")
                    if zp and not os.path.exists(zp):
                        continue
                    valid_skills.append(s_dict)
                return valid_skills

    async def get_skill(self, namespace_or_id: str, current_user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get complete skill details including latest manifest and version history."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        s.id, s.name, s.namespace, s.description, s.tags, s.icon, s.zip_path,
                        s.downloads_count, s.stars_count, s.is_featured, s.created_at,
                        p.handle as publisher_handle, p.display_name as publisher_name, p.verified as publisher_verified,
                        EXISTS(
                            SELECT 1 FROM user_installed_skills uis 
                            WHERE uis.skill_id = s.id AND uis.user_id = %s AND uis.is_active = TRUE
                        ) as is_installed,
                        EXISTS(
                            SELECT 1 FROM skill_stars ss
                            WHERE ss.skill_id = s.id AND ss.user_id = %s
                        ) as is_starred
                    FROM skills s
                    JOIN publishers p ON s.publisher_id = p.id
                    WHERE s.id = %s OR s.namespace = %s
                """, (current_user_id or "", current_user_id or "", namespace_or_id, namespace_or_id))
                skill = cur.fetchone()
                if not skill:
                    return None

                skill_data = dict(skill)

                # Fetch all versions
                cur.execute("""
                    SELECT version, manifest_md, frontmatter_json, changelog, zip_path, created_at
                    FROM skill_versions
                    WHERE skill_id = %s
                    ORDER BY created_at DESC
                """, (skill_data["id"],))
                versions = cur.fetchall()
                skill_data["versions"] = [dict(v) for v in versions]
                skill_data["latest_manifest"] = versions[0]["manifest_md"] if versions else ""
                skill_data["latest_frontmatter"] = versions[0]["frontmatter_json"] if versions else {}
                return skill_data

    async def get_or_create_publisher(self, user_id: str, handle: str, display_name: str) -> str:
        """Find or create publisher profile for a user."""
        clean_handle = re.sub(r"[^a-zA-Z0-9_\-]", "", handle.lower().strip())
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM publishers WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if row:
                    return row["id"]

                # Ensure handle uniqueness
                cur.execute("SELECT id FROM publishers WHERE handle = %s", (clean_handle,))
                existing_handle = cur.fetchone()
                if existing_handle:
                    clean_handle = f"{clean_handle}_{uuid.uuid4().hex[:4]}"

                cur.execute("""
                    INSERT INTO publishers (id, handle, display_name, verified)
                    VALUES (%s, %s, %s, FALSE)
                    RETURNING id
                """, (user_id, clean_handle, display_name))
                new_row = cur.fetchone()
                conn.commit()
                return new_row["id"]

    async def publish_skill(
        self,
        publisher_id: str,
        name: str,
        namespace: str,
        description: str,
        tags: List[str],
        icon: str,
        version: str,
        manifest_md: str,
        frontmatter_json: Dict[str, Any],
        zip_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create or update a skill and insert a new semantic version."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, publisher_id FROM skills WHERE namespace = %s", (namespace,))
                existing = cur.fetchone()

                if existing:
                    if existing["publisher_id"] != publisher_id:
                        raise ValueError("You do not own this namespace")
                    skill_id = existing["id"]
                    cur.execute("""
                        UPDATE skills 
                        SET name = %s, description = %s, tags = %s, icon = %s, zip_path = COALESCE(%s, zip_path), updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (name, description, json.dumps(tags), icon, zip_path, skill_id))
                else:
                    skill_id = f"skl_{uuid.uuid4().hex[:12]}"
                    cur.execute("""
                        INSERT INTO skills (id, publisher_id, name, namespace, description, tags, icon, zip_path)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (skill_id, publisher_id, name, namespace, description, json.dumps(tags), icon, zip_path))

                # Insert Version
                version_id = f"ver_{uuid.uuid4().hex[:12]}"
                bundle_hash = uuid.uuid4().hex
                cur.execute("""
                    INSERT INTO skill_versions (id, skill_id, version, manifest_md, frontmatter_json, bundle_hash, zip_path)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (skill_id, version) DO UPDATE 
                    SET manifest_md = EXCLUDED.manifest_md, frontmatter_json = EXCLUDED.frontmatter_json, zip_path = COALESCE(EXCLUDED.zip_path, skill_versions.zip_path)
                """, (version_id, skill_id, version, manifest_md, json.dumps(frontmatter_json), bundle_hash, zip_path))

                conn.commit()
                return {"id": skill_id, "namespace": namespace, "version": version, "zip_path": zip_path}

    async def install_skill(self, user_id: str, skill_id: str) -> bool:
        """Install a skill into user's active environment and increment downloads counter."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_installed_skills (user_id, skill_id, is_active, installed_at)
                    VALUES (%s, %s, TRUE, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id, skill_id) DO UPDATE SET is_active = TRUE
                """, (user_id, skill_id))
                cur.execute("UPDATE skills SET downloads_count = downloads_count + 1 WHERE id = %s", (skill_id,))
                conn.commit()
                return True

    async def uninstall_skill(self, user_id: str, skill_id: str) -> bool:
        """Uninstall/deactivate a skill for user."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM user_installed_skills WHERE user_id = %s AND skill_id = %s
                """, (user_id, skill_id))
                conn.commit()
                return True

    async def toggle_star(self, user_id: str, skill_id: str) -> Dict[str, Any]:
        """Toggle star for a skill and update stars_count."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM skill_stars WHERE user_id = %s AND skill_id = %s", (user_id, skill_id))
                already_starred = cur.fetchone() is not None

                if already_starred:
                    cur.execute("DELETE FROM skill_stars WHERE user_id = %s AND skill_id = %s", (user_id, skill_id))
                    cur.execute("UPDATE skills SET stars_count = GREATEST(0, stars_count - 1) WHERE id = %s", (skill_id,))
                    starred = False
                else:
                    cur.execute("INSERT INTO skill_stars (user_id, skill_id) VALUES (%s, %s)", (user_id, skill_id))
                    cur.execute("UPDATE skills SET stars_count = stars_count + 1 WHERE id = %s", (skill_id,))
                    starred = True

                cur.execute("SELECT stars_count FROM skills WHERE id = %s", (skill_id,))
                count_row = cur.fetchone()
                conn.commit()
                return {"starred": starred, "stars_count": count_row["stars_count"] if count_row else 0}

    async def get_user_installed_skills(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all active installed skills for a user, including full instructions for agent progressive disclosure."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        s.id, s.name, s.namespace, s.description, s.tags, s.icon,
                        uis.installed_at,
                        v.version, v.manifest_md, v.frontmatter_json
                    FROM user_installed_skills uis
                    JOIN skills s ON uis.skill_id = s.id
                    LEFT JOIN LATERAL (
                        SELECT version, manifest_md, frontmatter_json
                        FROM skill_versions sv
                        WHERE sv.skill_id = s.id
                    ) v ON true
                    WHERE uis.user_id = %s AND uis.is_active = TRUE
                    ORDER BY uis.installed_at DESC
                """, (user_id,))
                rows = cur.fetchall()
                return [dict(r) for r in rows]

    async def get_user_published_skills(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all skills authored/published by this user with download counts and stats."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        s.id, s.name, s.namespace, s.description, s.tags, s.icon,
                        s.downloads_count, s.stars_count, s.is_featured, s.created_at,
                        p.handle as publisher_handle, p.display_name as publisher_name,
                        v.version as latest_version, v.manifest_md
                    FROM skills s
                    JOIN publishers p ON s.publisher_id = p.id
                    LEFT JOIN LATERAL (
                        SELECT version, manifest_md
                        FROM skill_versions sv
                        WHERE sv.skill_id = s.id
                        ORDER BY sv.created_at DESC LIMIT 1
                    ) v ON true
                    WHERE p.id = %s OR s.publisher_id = %s
                    ORDER BY s.created_at DESC
                """, (user_id, user_id))
    async def get_skill_by_id(self, skill_id: str) -> Optional[Dict[str, Any]]:
        """Get skill record by primary ID."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.*, sv.version, sv.manifest_md, sv.zip_path as version_zip_path
                    FROM skills s
                    LEFT JOIN LATERAL (
                        SELECT version, manifest_md, zip_path
                        FROM skill_versions sv
                        WHERE sv.skill_id = s.id
                        ORDER BY sv.created_at DESC LIMIT 1
                    ) sv ON true
                    WHERE s.id = %s
                """, (skill_id,))
                row = cur.fetchone()
    async def delete_skill(self, skill_id: str, publisher_id: str) -> Optional[Dict[str, Any]]:
        """
        Unpublish/delete a skill from the registry.
        Enforces that only the publisher/owner can delete the skill.
        Returns the deleted skill info (including zip_path) so storage files can be purged.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # Verify ownership
                cur.execute("SELECT id, publisher_id, name, namespace, zip_path FROM skills WHERE id = %s", (skill_id,))
                skill = cur.fetchone()
                if not skill:
                    return None

                if skill["publisher_id"] != publisher_id:
                    raise ValueError("Only the publisher of this skill can unpublish or delete it.")

                # Cascade delete from user installations, versions, and skills table
                cur.execute("DELETE FROM user_installed_skills WHERE skill_id = %s", (skill_id,))
                cur.execute("DELETE FROM skill_stars WHERE skill_id = %s", (skill_id,))
                cur.execute("DELETE FROM skill_versions WHERE skill_id = %s", (skill_id,))
                cur.execute("DELETE FROM skills WHERE id = %s", (skill_id,))
                conn.commit()
                return dict(skill)

    # -----------------------------------------------------------------------
    # Plugin Marketplace Methods (plugins / plugin_versions / user_installed_plugins)
    # -----------------------------------------------------------------------

    async def list_plugins(
        self,
        plugin_type: Optional[str] = None,
        query: Optional[str] = None,
        sort_by: str = "popular",
        current_user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List marketplace plugins with optional type filter, search, and user install status."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                where_clauses = ["1=1"]
                params: List[Any] = []

                if plugin_type and plugin_type != "all":
                    where_clauses.append("p.plugin_type = %s")
                    params.append(plugin_type)

                if query:
                    where_clauses.append("(p.name ILIKE %s OR p.description ILIKE %s OR p.slug ILIKE %s)")
                    wildcard = f"%{query.strip()}%"
                    params.extend([wildcard, wildcard, wildcard])

                order_clause = "ORDER BY p.downloads_count DESC"
                if sort_by == "recent":
                    order_clause = "ORDER BY p.created_at DESC"
                elif sort_by in ("stars", "top_rated"):
                    order_clause = "ORDER BY COALESCE(p.rating_avg, 0) DESC, p.stars_count DESC, p.downloads_count DESC"
                elif sort_by == "trending":
                    order_clause = "ORDER BY p.downloads_count DESC, COALESCE(p.rating_avg, 0) DESC, p.created_at DESC"

                where_sql = " AND ".join(where_clauses)

                sql = f"""
                    SELECT
                        p.id, p.slug, p.name, p.description, p.plugin_type,
                        p.latest_version, p.icon_url, p.metadata, p.configuration_schema,
                        p.capabilities_snapshot, p.endpoint_url, p.zip_path, p.downloads_count,
                        p.stars_count, p.is_featured, p.created_at,
                        p.rating_avg, p.rating_count, p.rating_weighted, p.rating_hist,
                        pub.handle as publisher_handle, pub.display_name as publisher_name,
                        pub.verified as publisher_verified,
                        b.username as builder_username, b.display_name as builder_display_name,
                        b.is_verified as builder_verified, b.is_official as builder_official,
                        EXISTS(
                            SELECT 1 FROM user_installed_plugins uip
                            WHERE uip.plugin_id = p.id AND uip.user_id = %s AND uip.is_active = TRUE
                        ) as is_installed,
                        (
                            SELECT pv.version FROM user_installed_plugins uip
                            LEFT JOIN plugin_versions pv ON uip.plugin_version_id = pv.id
                            WHERE uip.plugin_id = p.id AND uip.user_id = %s LIMIT 1
                        ) as installed_version,
                        (
                            SELECT uip.configuration_metadata FROM user_installed_plugins uip
                            WHERE uip.plugin_id = p.id AND uip.user_id = %s LIMIT 1
                        ) as configuration_metadata,
                        (
                            SELECT uip.install_status FROM user_installed_plugins uip
                            WHERE uip.plugin_id = p.id AND uip.user_id = %s LIMIT 1
                        ) as install_status,
                        (
                            SELECT uip.install_logs FROM user_installed_plugins uip
                            WHERE uip.plugin_id = p.id AND uip.user_id = %s LIMIT 1
                        ) as install_logs,
                        (
                            SELECT uip.install_error FROM user_installed_plugins uip
                            WHERE uip.plugin_id = p.id AND uip.user_id = %s LIMIT 1
                        ) as install_error,
                        (p.publisher_id = %s) as is_owner
                    FROM plugins p
                    JOIN publishers pub ON p.publisher_id = pub.id
                    LEFT JOIN builders b ON p.builder_id = b.id
                    WHERE {where_sql}
                    {order_clause}
                """
                uid = current_user_id or ""
                cur.execute(sql, [uid, uid, uid, uid, uid, uid, uid] + params)
                rows = cur.fetchall()
                valid_plugins = []
                for r in rows:
                    p_dict = dict(r)
                    ptype = p_dict.get("plugin_type")
                    zp = p_dict.get("zip_path")

                    # For skills and packaged agents: verify the physical zip artifact exists on disk or in Tigris object storage
                    if ptype in ("skill", "agent"):
                        if not zp:
                            continue
                        if not zp.startswith("tigris://") and not os.path.exists(zp):
                            continue

                    # For MCP servers:
                    # - If hosted/local package with zip_path, zip must exist (locally or in Tigris)
                    # - If remote MCP server, endpoint_url must be present
                    elif ptype == "mcp":
                        endpoint = p_dict.get("endpoint_url")
                        if zp and not zp.startswith("tigris://") and not os.path.exists(zp):
                            continue
                        if not zp and not endpoint:
                            continue

                    valid_plugins.append(p_dict)
                return valid_plugins

    async def publish_plugin(
        self,
        publisher_id: str,
        slug: str,
        name: str,
        description: str,
        plugin_type: str,
        version: str = "1.0.0",
        icon_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        configuration_schema: Optional[List[Dict[str, Any]]] = None,
        capabilities_snapshot: Optional[Dict[str, Any]] = None,
        endpoint_url: Optional[str] = None,
        zip_path: Optional[str] = None,
        artifact_sha256: Optional[str] = None,
        manifest: Optional[Dict[str, Any]] = None,
        changelog: Optional[str] = None,
        category: Optional[str] = None,
        short_desc: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create or update a plugin in the marketplace registry."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # Check if slug already exists for this publisher
                cur.execute(
                    "SELECT id, publisher_id FROM plugins WHERE slug = %s AND plugin_type = %s",
                    (slug, plugin_type)
                )
                existing = cur.fetchone()

                meta_dict = dict(metadata or {})
                if tags:
                    meta_dict["tags"] = tags

                if existing:
                    if existing["publisher_id"] != publisher_id:
                        raise ValueError("You do not own this plugin slug")
                    plugin_id = existing["id"]
                    cur.execute("""
                        UPDATE plugins SET name=%s, description=%s, latest_version=%s,
                            icon_url=COALESCE(%s, icon_url),
                            metadata=COALESCE(%s::jsonb, metadata),
                            configuration_schema=COALESCE(%s::jsonb, configuration_schema),
                            capabilities_snapshot=COALESCE(%s::jsonb, capabilities_snapshot),
                            endpoint_url=COALESCE(%s, endpoint_url),
                            zip_path=COALESCE(%s, zip_path),
                            category=COALESCE(%s, category),
                            short_desc=COALESCE(%s, short_desc),
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s
                    """, (
                        name, description, version, icon_url,
                        json.dumps(meta_dict),
                        json.dumps(configuration_schema or []),
                        json.dumps(capabilities_snapshot or {}),
                        endpoint_url, zip_path,
                        category, short_desc,
                        plugin_id
                    ))
                else:
                    plugin_id = f"plg_{uuid.uuid4().hex[:12]}"
                    cur.execute("""
                        INSERT INTO plugins (
                            id, slug, name, description, plugin_type, publisher_id,
                            latest_version, icon_url, metadata, configuration_schema,
                            capabilities_snapshot, endpoint_url, zip_path, category, short_desc
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
                    """, (
                        plugin_id, slug, name, description, plugin_type, publisher_id,
                        version, icon_url,
                        json.dumps(meta_dict),
                        json.dumps(configuration_schema or []),
                        json.dumps(capabilities_snapshot or {}),
                        endpoint_url, zip_path,
                        category or "agents", short_desc or description[:160]
                    ))

                # Insert version record
                version_id = f"pver_{uuid.uuid4().hex[:12]}"
                cur.execute("""
                    INSERT INTO plugin_versions (
                        id, plugin_id, version, changelog, artifact_sha256, zip_path, manifest,
                        capabilities_snapshot, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,'published')
                    ON CONFLICT (plugin_id, version) DO UPDATE
                    SET changelog=COALESCE(EXCLUDED.changelog, plugin_versions.changelog),
                        artifact_sha256=EXCLUDED.artifact_sha256,
                        zip_path=COALESCE(EXCLUDED.zip_path, plugin_versions.zip_path),
                        manifest=EXCLUDED.manifest,
                        capabilities_snapshot=EXCLUDED.capabilities_snapshot
                """, (
                    version_id, plugin_id, version, changelog or "Initial release", artifact_sha256, zip_path,
                    json.dumps(manifest or {}),
                    json.dumps(capabilities_snapshot or {})
                ))

                conn.commit()
                return {
                    "id": plugin_id, "slug": slug, "name": name,
                    "plugin_type": plugin_type, "version": version
                }

    async def install_plugin(self, user_id: str, plugin_id: str, plugin_version_id: Optional[str] = None) -> bool:
        """Register a plugin as installed for the user."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                install_id = f"inst_{uuid.uuid4().hex[:12]}"
                cur.execute("""
                    INSERT INTO user_installed_plugins
                        (id, user_id, plugin_id, plugin_version_id, is_active, installed_at)
                    VALUES (%s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id, plugin_id) DO UPDATE
                    SET is_active=TRUE, 
                        plugin_version_id=COALESCE(EXCLUDED.plugin_version_id, user_installed_plugins.plugin_version_id),
                        updated_at=CURRENT_TIMESTAMP
                """, (install_id, user_id, plugin_id, plugin_version_id))
                cur.execute("UPDATE plugins SET downloads_count=downloads_count+1 WHERE id=%s", (plugin_id,))
                conn.commit()
                return True

    async def uninstall_plugin(self, user_id: str, plugin_id: str) -> bool:
        """Remove a plugin installation for the user."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_installed_plugins WHERE user_id=%s AND plugin_id=%s",
                    (user_id, plugin_id)
                )
                conn.commit()
                return True

    async def get_user_installed_plugins(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all plugins installed by the user."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        p.id, p.slug, p.name, p.description, p.plugin_type,
                        p.latest_version, p.icon_url, p.configuration_schema,
                        p.capabilities_snapshot, p.endpoint_url,
                        uip.is_active, uip.configuration_metadata, uip.installed_at,
                        uip.install_status, uip.install_logs, uip.install_error,
                        pub.handle as publisher_handle, pub.display_name as publisher_name
                    FROM user_installed_plugins uip
                    JOIN plugins p ON uip.plugin_id = p.id
                    JOIN publishers pub ON p.publisher_id = pub.id
                    WHERE uip.user_id = %s
                    ORDER BY uip.installed_at DESC
                """, (user_id,))
                rows = cur.fetchall()
                return [dict(r) for r in rows]

    async def update_install_status(
        self,
        user_id: str,
        plugin_id: str,
        status: str,
        logs: Optional[str] = None,
        error: Optional[str] = None
    ) -> bool:
        """Update installation status, build/runtime logs, and error for a user installed plugin."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE user_installed_plugins
                    SET install_status = %s,
                        install_logs = COALESCE(%s, install_logs),
                        install_error = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s AND plugin_id = %s
                """, (status, logs, error, user_id, plugin_id))
                conn.commit()
                return True

    async def toggle_plugin_active(self, user_id: str, plugin_id: str, is_active: bool) -> bool:
        """Toggle whether a plugin is active (accessible by the agent)."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE user_installed_plugins SET is_active=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE user_id=%s AND plugin_id=%s
                """, (is_active, user_id, plugin_id))
                conn.commit()
                return True

    async def update_plugin_config(self, user_id: str, plugin_id: str, config_metadata: Dict[str, Any]) -> bool:
        """Update non-secret configuration metadata for a user's plugin install."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE user_installed_plugins
                    SET configuration_metadata=%s::jsonb, updated_at=CURRENT_TIMESTAMP
                    WHERE user_id=%s AND plugin_id=%s
                """, (json.dumps(config_metadata), user_id, plugin_id))
                conn.commit()
                return True

    async def create_runtime_token(self, user_id: str, plugin_id: str, run_id: str, token_hash: str, expires_at: Any) -> str:
        """Record a scoped runtime relay token."""
        token_id = f"rtk_{uuid.uuid4().hex[:12]}"
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO runtime_tokens (id, token_hash, user_id, plugin_id, run_id, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (token_id, token_hash, user_id, plugin_id, run_id, expires_at))
                conn.commit()
        return token_id

    async def validate_runtime_token(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """Validate a scoped runtime token. Returns claims dict or None if invalid."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id, plugin_id, run_id, expires_at, revoked
                    FROM runtime_tokens
                    WHERE token_hash=%s
                """, (token_hash,))
                row = cur.fetchone()
                if not row:
                    return None
                r = dict(row)
                from datetime import datetime, timezone
                if r['revoked']:
                    return None
                exp = r['expires_at']
                if hasattr(exp, 'tzinfo') and exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp < datetime.now(timezone.utc):
                    return None
                return {"user_id": r["user_id"], "plugin_id": r["plugin_id"], "run_id": r["run_id"]}

    async def revoke_runtime_token(self, token_hash: str) -> bool:
        """Revoke a scoped runtime token."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE runtime_tokens SET revoked=TRUE WHERE token_hash=%s", (token_hash,))
                conn.commit()
        return True

    async def get_plugin_by_id(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        """Get a plugin record by ID."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.*, pub.handle as publisher_handle, pub.display_name as publisher_name
                    FROM plugins p
                    JOIN publishers pub ON p.publisher_id = pub.id
                    WHERE p.id=%s
                """, (plugin_id,))
                row = cur.fetchone()
                return dict(row) if row else None
    async def delete_plugin(self, plugin_id: str, publisher_id: str) -> Optional[Dict[str, Any]]:
        """
        Unpublish/delete a plugin from the registry.
        Enforces that only the publisher/owner can delete the plugin.
        Returns the deleted plugin info (including zip_path) so storage files can be purged.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # Verify ownership
                cur.execute("SELECT id, publisher_id, name, slug, plugin_type, zip_path FROM plugins WHERE id = %s", (plugin_id,))
                plugin = cur.fetchone()
                if not plugin:
                    return None

                if plugin["publisher_id"] != publisher_id:
                    raise ValueError("Only the publisher of this plugin can unpublish or delete it.")

                cur.execute("DELETE FROM user_installed_plugins WHERE plugin_id = %s", (plugin_id,))
                cur.execute("DELETE FROM plugin_versions WHERE plugin_id = %s", (plugin_id,))
                cur.execute("DELETE FROM plugins WHERE id = %s", (plugin_id,))
                conn.commit()
                return dict(plugin)

    async def list_plugin_versions(self, plugin_id: str) -> List[Dict[str, Any]]:
        """Get all published versions for a plugin ordered by created_at DESC."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, plugin_id, version, changelog, artifact_sha256,
                           manifest, capabilities_snapshot, status, created_at
                    FROM plugin_versions
                    WHERE plugin_id = %s
                    ORDER BY created_at DESC
                """, (plugin_id,))
                rows = cur.fetchall()
                return [dict(r) for r in rows]

    async def get_plugin_version(self, plugin_id: str, version: str) -> Optional[Dict[str, Any]]:
        """Get specific version of a plugin."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, plugin_id, version, changelog, artifact_sha256,
                           manifest, capabilities_snapshot, zip_path, status, created_at
                    FROM plugin_versions
                    WHERE plugin_id = %s AND version = %s
                """, (plugin_id, version))
                row = cur.fetchone()
                return dict(row) if row else None


marketplace_repo = MarketplaceRepository()
