"""
Talos Cloud — settings loader.
All configuration comes from environment variables or a .env file.
No hardcoded secrets anywhere in this codebase.
"""
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_storage_fallbacks(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("tigris_access_key_id") and not data.get("TIGRIS_ACCESS_KEY_ID"):
                aws_key = data.get("aws_access_key_id") or data.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("TIGRIS_ACCESS_KEY_ID")
                if aws_key:
                    data["tigris_access_key_id"] = aws_key
            if not data.get("tigris_secret_access_key") and not data.get("TIGRIS_SECRET_ACCESS_KEY"):
                aws_secret = data.get("aws_secret_access_key") or data.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("TIGRIS_SECRET_ACCESS_KEY")
                if aws_secret:
                    data["tigris_secret_access_key"] = aws_secret
            if not data.get("tigris_endpoint_url") and not data.get("TIGRIS_ENDPOINT_URL"):
                aws_ep = (
                    data.get("aws_endpoint_url_s3")
                    or data.get("AWS_ENDPOINT_URL_S3")
                    or data.get("aws_endpoint_url")
                    or data.get("AWS_ENDPOINT_URL")
                    or os.environ.get("AWS_ENDPOINT_URL_S3")
                    or os.environ.get("TIGRIS_ENDPOINT_URL")
                )
                if aws_ep:
                    data["tigris_endpoint_url"] = aws_ep
            if not data.get("tigris_region") and not data.get("TIGRIS_REGION"):
                aws_reg = data.get("aws_region") or data.get("AWS_REGION") or os.environ.get("AWS_REGION") or os.environ.get("TIGRIS_REGION")
                if aws_reg:
                    data["tigris_region"] = aws_reg
            if not data.get("tigris_bucket_name") and not data.get("TIGRIS_BUCKET_NAME"):
                bucket = os.environ.get("TIGRIS_BUCKET_NAME")
                if bucket:
                    data["tigris_bucket_name"] = bucket
        return data

    database_url: str
    jwt_secret: str = "talos-default-secret-key-change-in-production"
    jwt_algorithm: str = "RS256"
    jwt_private_key_pem: str | None = None
    jwt_public_key_pem: str | None = None
    jwt_key_id: str = "talos-v1"
    jwt_previous_public_keys: str | None = None
    jwt_issuer: str = "talos-cloud"
    jwt_audience: str = "talos-web"
    token_expiry_minutes: int = 4320  # 3 days token expiry for device tokens
    session_expiry_minutes: int = 15  # 15 minutes web session access token expiry
    refresh_token_expiry_days: int = 30  # 30 days web session refresh token expiry
    talos_env: str = "development"
    redis_url: str = "redis://localhost:6379/0"
    redis_core_url: str | None = None
    rate_limit_enabled: bool = True
    rate_limit_fail_closed: bool = False
    concurrency_fail_closed: bool = False
    # Billing & Credit Control
    enable_credit_system: bool = False
    stream_billing_policy: str = "charge_actual"  # "charge_actual" or "charge_delivered"

    # Inngest Configuration
    inngest_app_id: str = "talos-cloud"
    inngest_event_key: str | None = None
    inngest_signing_key: str | None = None
    inngest_dev: bool = False

    # Provider API keys — server-side only, NEVER forwarded to clients.
    # These are used exclusively inside relay_service.py when dispatching
    # real provider calls. If any of these values ever appear in a response
    # payload sent to a local client, that is a critical security bug.
    mistral_api_key: str | None = None
    mistral_api_key_previous: str | None = None
    anthropic_api_key: str | None = None
    anthropic_api_key_previous: str | None = None
    tavily_api_key: str | None = None
    groq_api_key: str | None = None
    groq_api_key_previous: str | None = None
    zai_api_key: str | None = None
    zai_api_key_previous: str | None = None
    zhipu_api_key: str | None = None
    glm_api_key: str | None = None
    openai_api_key: str | None = None
    openai_api_key_previous: str | None = None
    deepseek_api_key: str | None = None
    deepseek_api_key_previous: str | None = None
    gemini_api_key: str | None = None
    gemini_api_key_previous: str | None = None

    # Stripe (Phase 8) — stubs for now
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None

    # OAuth Providers (Google, GitHub, Microsoft)
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    github_redirect_uri: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_redirect_uri: str | None = None
    talos_admin_secret: str | None = None
    admin_emails: str = "shikharjadav16@gmail.com"

    # Tigris / S3 Object Storage — server-side only, NEVER forwarded to clients
    tigris_access_key_id: str | None = None
    tigris_secret_access_key: str | None = None
    tigris_endpoint_url: str = "https://t3.storage.dev"
    tigris_region: str = "auto"
    tigris_bucket_name: str = "talos-marketplace"
    storage_presigned_expiry_seconds: int = 900
    storage_max_package_size_bytes: int = 52_428_800  # 50 MB
    storage_max_asset_size_bytes: int = 2_097_152     # 2 MB

    @property
    def is_storage_configured(self) -> bool:
        return bool(
            self.tigris_access_key_id
            and self.tigris_secret_access_key
            and self.tigris_endpoint_url
            and self.tigris_bucket_name
        )

    @property
    def resolved_redis_core_url(self) -> str:
        return self.redis_core_url or os.environ.get("REDIS_CORE_URL") or self.redis_url

    @property
    def resolved_redis_celery_url(self) -> str:
        return (
            os.environ.get("REDIS_CELERY_URL")
            or os.environ.get("CELERY_BROKER_URL")
            or self.redis_url
        )


    @property
    def admin_email_list(self) -> list[str]:
        if not self.admin_emails:
            return []
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    @property
    def previous_public_key_map(self) -> dict[str, str]:
        if not self.jwt_previous_public_keys:
            return {}
        try:
            import json
            data = json.loads(self.jwt_previous_public_keys)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
