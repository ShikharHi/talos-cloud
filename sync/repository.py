import psycopg
from psycopg.rows import dict_row
from typing import Optional, Dict, Any, List, Set
from datetime import datetime, timezone
from config import config
from neon_pool import neon_pool

class SyncRepository:
    def __init__(self, dsn: str = config.POSTGRES_URL):
        self.dsn = dsn

    def _get_conn(self):
        return neon_pool.connection()

    async def init_db(self):
        """Create sync tables in Neon PostgreSQL."""
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                # 1. Zero-Knowledge Key Envelope Store
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_sync_keys (
                        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                        wrapped_dek TEXT NOT NULL,
                        salt TEXT NOT NULL,
                        recovery_wrapped_dek TEXT,
                        recovery_salt TEXT,
                        key_version INT NOT NULL DEFAULT 1,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 2. Content-Addressed Chunks Registry
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sync_chunks (
                        chunk_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        storage_key TEXT NOT NULL,
                        size_bytes INT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_sync_chunks_user ON sync_chunks(user_id);
                """)

                # 3. Versioned Immutable Manifests with Vector Clock/Parents
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sync_manifests (
                        manifest_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        device_id TEXT NOT NULL,
                        sequence_number INT NOT NULL,
                        parent_manifest_id TEXT,
                        encrypted_manifest TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_sync_manifests_thread ON sync_manifests(thread_id, sequence_number DESC);
                    CREATE INDEX IF NOT EXISTS idx_sync_manifests_user ON sync_manifests(user_id);
                """)
                conn.commit()

    async def set_user_keys(
        self, user_id: str, wrapped_dek: str, salt: str,
        recovery_wrapped_dek: Optional[str] = None, recovery_salt: Optional[str] = None
    ):
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_sync_keys (user_id, wrapped_dek, salt, recovery_wrapped_dek, recovery_salt, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                        wrapped_dek = EXCLUDED.wrapped_dek,
                        salt = EXCLUDED.salt,
                        recovery_wrapped_dek = EXCLUDED.recovery_wrapped_dek,
                        recovery_salt = EXCLUDED.recovery_salt,
                        key_version = user_sync_keys.key_version + 1,
                        updated_at = EXCLUDED.updated_at
                """, (user_id, wrapped_dek, salt, recovery_wrapped_dek, recovery_salt, now))
                conn.commit()

    async def get_user_keys(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM user_sync_keys WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    async def find_existing_chunks(self, user_id: str, chunk_ids: List[str]) -> Set[str]:
        if not chunk_ids:
            return set()
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT chunk_id FROM sync_chunks WHERE user_id = %s AND chunk_id = ANY(%s)",
                    (user_id, chunk_ids)
                )
                rows = cur.fetchall()
                return {r["chunk_id"] for r in rows}

    async def record_chunks(self, user_id: str, chunk_records: List[Dict[str, Any]]):
        if not chunk_records:
            return
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for c in chunk_records:
                    cur.execute("""
                        INSERT INTO sync_chunks (chunk_id, user_id, storage_key, size_bytes)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT(chunk_id) DO NOTHING
                    """, (c["chunk_id"], user_id, c["storage_key"], c["size_bytes"]))
                conn.commit()

    async def commit_manifest(
        self, manifest_id: str, thread_id: str, user_id: str,
        device_id: str, sequence_number: int, parent_manifest_id: Optional[str],
        encrypted_manifest: str
    ):
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sync_manifests (
                        manifest_id, thread_id, user_id, device_id, sequence_number,
                        parent_manifest_id, encrypted_manifest
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    manifest_id, thread_id, user_id, device_id, sequence_number,
                    parent_manifest_id, encrypted_manifest
                ))
                conn.commit()

    async def get_latest_thread_manifest(self, user_id: str, thread_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM sync_manifests
                    WHERE user_id = %s AND thread_id = %s
                    ORDER BY sequence_number DESC, created_at DESC
                    LIMIT 1
                """, (user_id, thread_id))
                row = cur.fetchone()
                return dict(row) if row else None

    async def get_all_user_manifests(self, user_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (thread_id) *
                    FROM sync_manifests
                    WHERE user_id = %s
                    ORDER BY thread_id, sequence_number DESC, created_at DESC
                """, (user_id,))
                rows = cur.fetchall()
                return [dict(r) for r in rows]

sync_repo = SyncRepository()
