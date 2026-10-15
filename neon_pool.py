import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from config import config

class GlobalNeonPool:
    _instance = None
    _pool = None

    @classmethod
    def get_pool(cls) -> ConnectionPool:
        if cls._pool is None:
            # Persistent connection pool to avoid TLS/SSL handshake latency on every request
            cls._pool = ConnectionPool(
                config.POSTGRES_URL,
                min_size=2,
                max_size=15,
                timeout=10.0,
                max_idle=300.0,
                check=ConnectionPool.check_connection,
                kwargs={"row_factory": dict_row, "autocommit": False}
            )
            cls._pool.open()
        return cls._pool

    @classmethod
    def connection(cls):
        pool = cls.get_pool()
        return pool.connection()

neon_pool = GlobalNeonPool
