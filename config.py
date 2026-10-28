import os
from pydantic import BaseModel

class Config(BaseModel):
    PORT: int = int(os.getenv("TALOS_CLOUD_PORT", "8000"))
    HOST: str = os.getenv("TALOS_CLOUD_HOST", "0.0.0.0")
    
    # Environment mode: 'development' or 'production'
    TALOS_ENV: str = os.getenv("TALOS_ENV", "development").lower()
    
    # Internal auth token required strictly from talos-backend
    TALOS_INTERNAL_SECRET: str = os.getenv("TALOS_INTERNAL_SECRET", "talos_internal_default_secret_key_v6")
    
    # External Vendor Credentials
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    
    # Mistral AI credentials & endpoint
    MISTRAL_API_KEY: str = os.getenv("MISTRAL_API_KEY", "mstrl_8JVtvxxLTxB1EKIwM5caxF86S3XTkECh_1S9IKh")
    MISTRAL_BASE_URL: str = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
    
    # Enable mock mode if no real key provided
    MOCK_MODE: bool = os.getenv("TALOS_MOCK_MODE", "false").lower() in ("true", "1", "yes")

    # Compatibility paths for cross-module test fixtures
    TALOS_CHECKPOINTS_PATH: str = os.getenv("TALOS_CHECKPOINTS_PATH", "")
    TALOS_DB_PATH: str = os.getenv("TALOS_DB_PATH", "")

    # PostgreSQL Connection URL (Neon Cloud Postgres)
    POSTGRES_URL: str = os.getenv(
        "TALOS_POSTGRES_URL",
        "postgresql://neondb_owner:npg_M3sy7XuIZYve@ep-summer-breeze-a19m47fw-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
    )

    # Tigris Global Object Storage
    TIGRIS_ENDPOINT: str = os.getenv("TIGRIS_ENDPOINT", "https://t3.storage.dev")
    TIGRIS_BUCKET: str = os.getenv("TIGRIS_BUCKET", "talos-marketplace")
    TIGRIS_ACCESS_KEY: str = os.getenv("AWS_ACCESS_KEY_ID", "tid_dMuueTNqHnnhVFsPCSZlmiswHvuETbkScpLVVfUC_SYXTxmasi")
    TIGRIS_SECRET_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "tsec_9mNqRJUXQ6tG1eOBHm58WglfsyzSZdT6lxIJpba1ftlFz4Nlh3RVlKf7ILZ9RFRMyc5sEE")

    # Upstash Serverless Redis
    UPSTASH_REDIS_REST_URL: str = os.getenv(
        "UPSTASH_REDIS_REST_URL",
        "https://amazed-eft-176812.upstash.io"
    )
    UPSTASH_REDIS_REST_TOKEN: str = os.getenv(
        "UPSTASH_REDIS_REST_TOKEN",
        "gQAAAAAAArKsAAIgcDJkN2FkNWQ3YmExMGY0ZDE1OGU4ZjJkOWFiNTQ0MDZjMg"
    )

    def validate_security(self):
        """Enforce fail-closed security in production."""
        if self.TALOS_ENV == "production":
            if not self.TALOS_INTERNAL_SECRET or self.TALOS_INTERNAL_SECRET == "talos_internal_default_secret_key_v6":
                raise RuntimeError(
                    "[SECURITY ALERT] TALOS_INTERNAL_SECRET must be explicitly set to a strong secret in production."
                )

config = Config()
config.validate_security()
