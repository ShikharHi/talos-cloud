import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from config import config
from neon_pool import neon_pool

# Maximum snapshot size allowed: 2 GB
MAX_SNAPSHOT_SIZE_BYTES = 2 * 1024 * 1024 * 1024


class SyncRepository:
    def __init__(self):
        pass

    def _get_conn(self):
        return neon_pool.connection()

    async def init_db(self):
        """Create snapshot tables in Neon PostgreSQL."""
        import psycopg
        with psycopg.connect(config.POSTGRES_URL) as conn:
            with conn.cursor() as cur:
                # 1. Snapshot metadata table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_snapshots (
                        snapshot_id     TEXT PRIMARY KEY,
                        user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        object_key      TEXT NOT NULL,
                        generation      BIGINT NOT NULL DEFAULT 1,
                        size_bytes      BIGINT NOT NULL,
                        checksum_sha256 TEXT NOT NULL,
                        format_version  INT NOT NULL DEFAULT 1,
                        created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        committed_at    TIMESTAMP WITH TIME ZONE
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_snapshots_user
                        ON user_snapshots(user_id);
                    CREATE INDEX IF NOT EXISTS idx_user_snapshots_gen
                        ON user_snapshots(user_id, generation DESC);
                """)

                # 2. Durable tombstones for thread deletions
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS snapshot_pending_deletes (
                        user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        thread_id    TEXT NOT NULL,
                        deleted_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, thread_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_pending_deletes_user
                        ON snapshot_pending_deletes(user_id);
                """)
            conn.commit()

    # ── Snapshot metadata ──────────────────────────────────────────────────────

    def get_user_snapshot(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM user_snapshots WHERE user_id = %s",
                    (user_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def upsert_snapshot(
        self,
        user_id: str,
        object_key: str,
        generation: int,
        size_bytes: int,
        checksum_sha256: str,
        format_version: int = 1,
    ) -> str:
        """Insert or replace the user's snapshot pointer.
        Uses conditional update: only replaces when the incoming generation
        is strictly greater than what's already committed.
        Returns the snapshot_id.
        """
        snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_snapshots (
                        snapshot_id, user_id, object_key, generation,
                        size_bytes, checksum_sha256, format_version,
                        created_at, committed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE
                        SET snapshot_id     = EXCLUDED.snapshot_id,
                            object_key      = EXCLUDED.object_key,
                            generation      = EXCLUDED.generation,
                            size_bytes      = EXCLUDED.size_bytes,
                            checksum_sha256 = EXCLUDED.checksum_sha256,
                            format_version  = EXCLUDED.format_version,
                            committed_at    = EXCLUDED.committed_at
                        WHERE user_snapshots.generation < EXCLUDED.generation
                """, (
                    snapshot_id, user_id, object_key, generation,
                    size_bytes, checksum_sha256, format_version,
                    now, now,
                ))
            conn.commit()
        return snapshot_id

    def delete_snapshot_record(self, user_id: str) -> bool:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_snapshots WHERE user_id = %s RETURNING snapshot_id",
                    (user_id,)
                )
                deleted = cur.fetchone()
            conn.commit()
        return deleted is not None

    # ── Pending deletes (tombstones) ───────────────────────────────────────────

    def add_pending_delete(self, user_id: str, thread_id: str):
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO snapshot_pending_deletes (user_id, thread_id)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, thread_id) DO NOTHING
                """, (user_id, thread_id))
            conn.commit()

    def get_pending_deletes(self, user_id: str) -> List[str]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT thread_id FROM snapshot_pending_deletes WHERE user_id = %s",
                    (user_id,)
                )
                return [row["thread_id"] for row in cur.fetchall()]

    def resolve_pending_deletes(self, user_id: str, thread_ids: List[str]):
        """Remove only the specified tombstones (those confirmed in the committed snapshot)."""
        if not thread_ids:
            return
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM snapshot_pending_deletes
                    WHERE user_id = %s AND thread_id = ANY(%s)
                """, (user_id, thread_ids))
            conn.commit()


sync_repo = SyncRepository()
