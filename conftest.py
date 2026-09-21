"""
Root conftest — must run before any app module is imported.
Sets minimum environment variables required by pydantic_settings Settings.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-do-not-use-in-production")
os.environ.setdefault("TALOS_ENV", "test")




def pytest_configure(config):
    """Reset cached singletons at the start of each test session."""
    # Reset lru_cache on get_settings so it picks up the env vars set above.
    try:
        from app.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    # Reset the cached engine so it's recreated with the test DATABASE_URL.
    try:
        import app.database as db_module
        db_module._engine = None
    except Exception:
        pass

    # Reset Redis singletons
    try:
        import app.infrastructure.redis_client as redis_mod
        redis_mod._redis_client = None
        redis_mod._redis_pool = None
    except Exception:
        pass

