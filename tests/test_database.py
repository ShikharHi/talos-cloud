from app.database import _normalize_database_url


def test_normalize_asyncpg_postgres_url_removes_libpq_options():
    url = _normalize_database_url(
        "postgresql+asyncpg://user:pass@host/db?sslmode=require&channel_binding=require"
    )

    assert "channel_binding" not in url
    assert "sslmode" not in url
    assert url == "postgresql+asyncpg://user:pass@host/db"


def test_normalize_non_asyncpg_url_is_unchanged():
    url = "postgresql://user:pass@host/db?sslmode=require&channel_binding=require"

    assert _normalize_database_url(url) == url