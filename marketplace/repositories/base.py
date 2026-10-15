import psycopg
from psycopg.rows import dict_row
from typing import Any
from config import config

from neon_pool import neon_pool

class BaseRepository:
    def __init__(self, dsn: str = config.POSTGRES_URL):
        self.dsn = dsn

    def get_conn(self):
        return neon_pool.connection()
