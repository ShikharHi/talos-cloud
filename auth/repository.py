import psycopg
from psycopg.rows import dict_row
import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta
from config import config

from neon_pool import neon_pool

class PostgresAuthRepository:
    def __init__(self, dsn: str = config.POSTGRES_URL):
        self.dsn = dsn

    def _get_conn(self):
        return neon_pool.connection()

    async def init_db(self):
        """Initialize PostgreSQL authentication, sessions, and thread index tables."""
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        hashed_password TEXT NOT NULL,
                        display_name TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        revoked_at TIMESTAMP WITH TIME ZONE NULL
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS thread_index (
                        thread_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        title TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.commit()

    async def create_user(self, email: str, hashed_pw: str, display_name: str) -> Dict[str, Any]:
        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO users (id, email, hashed_password, display_name, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id, email, display_name""",
                    (user_id, email.lower().strip(), hashed_pw, display_name.strip(), now, now)
                )
                row = cur.fetchone()
                conn.commit()
                return dict(row)

    async def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE email = %s", (email.lower().strip(),))
                row = cur.fetchone()
                return dict(row) if row else None

    async def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, email, display_name, created_at FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    async def create_session(self, user_id: str, duration_days: int = 7) -> Dict[str, Any]:
        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=duration_days)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
                    (session_id, user_id, now, expires_at)
                )
                conn.commit()

        # Cache session in Upstash Redis
        try:
            from redis_service import redis_cache
            ttl_seconds = int((expires_at - now).total_seconds())
            redis_cache.set_json(f"session:{session_id}", {
                "id": session_id,
                "user_id": user_id,
                "expires_at": expires_at.isoformat(),
                "created_at": now.isoformat()
            }, ex=ttl_seconds)
        except Exception:
            pass

        return {"id": session_id, "user_id": user_id, "expires_at": expires_at.isoformat()}

    async def get_valid_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        # 1. Try Redis cache first
        try:
            from redis_service import redis_cache
            cached = redis_cache.get_json(f"session:{session_id}")
            if cached and isinstance(cached, dict):
                exp = datetime.fromisoformat(cached["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) <= exp:
                    return cached
        except Exception:
            pass

        # 2. Fallback to Neon PostgreSQL
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM sessions WHERE id = %s AND revoked_at IS NULL", 
                    (session_id,)
                )
                row = cur.fetchone()
                if not row:
                    return None
                sess = dict(row)
                expires_at = sess["expires_at"]
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires_at:
                    return None

                # Populate Redis cache on cache miss
                try:
                    from redis_service import redis_cache
                    remaining_ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
                    if remaining_ttl > 0:
                        redis_cache.set_json(f"session:{session_id}", {
                            "id": sess["id"],
                            "user_id": sess["user_id"],
                            "expires_at": expires_at.isoformat(),
                            "created_at": sess["created_at"].isoformat() if sess.get("created_at") else None
                        }, ex=remaining_ttl)
                except Exception:
                    pass

                return sess

    async def revoke_session(self, session_id: str):
        now = datetime.now(timezone.utc)
        # Evict from Redis
        try:
            from redis_service import redis_cache
            redis_cache.delete(f"session:{session_id}")
        except Exception:
            pass

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE sessions SET revoked_at = %s WHERE id = %s", (now, session_id))
                conn.commit()

    # Thread Index Operations
    async def index_thread(self, thread_id: str, user_id: str, title: str):
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO thread_index (thread_id, user_id, title, created_at, updated_at) 
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT(thread_id) DO UPDATE SET title = EXCLUDED.title, updated_at = EXCLUDED.updated_at""",
                    (thread_id, user_id, title, now, now)
                )
                conn.commit()

    async def get_user_threads(self, user_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT thread_id, title, created_at, updated_at FROM thread_index WHERE user_id = %s ORDER BY updated_at DESC",
                    (user_id,)
                )
                rows = cur.fetchall()
                # Format datetimes
                result = []
                for r in rows:
                    item = dict(r)
                    if isinstance(item.get("created_at"), datetime):
                        item["created_at"] = item["created_at"].isoformat()
                    if isinstance(item.get("updated_at"), datetime):
                        item["updated_at"] = item["updated_at"].isoformat()
                    result.append(item)
                return result

    async def get_thread_owner(self, thread_id: str) -> Optional[str]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM thread_index WHERE thread_id = %s", (thread_id,))
                row = cur.fetchone()
                return row["user_id"] if row else None

    async def delete_thread_index(self, thread_id: str, user_id: str) -> bool:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM thread_index WHERE thread_id = %s AND user_id = %s",
                    (thread_id, user_id)
                )
                conn.commit()
                return cur.rowcount > 0

auth_repo = PostgresAuthRepository()
