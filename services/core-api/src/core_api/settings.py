from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    env: str = "dev"
    # Autonomous DB: oracle+oracledb://ADMIN:<pwd>@<tls-connect-string>; sqlite for unit tests.
    database_url: str = "sqlite:///./core.db"
    # When db_dsn is set we connect to Oracle ADB over TLS as a least-privilege app user.
    db_dsn: str = ""
    db_user: str = "SELFCARE"
    db_password: str = ""
    valkey_url: str = "redis://valkey:6379/0"
    stream_partitions: int = 4

    jwt_secret: str = "change-me"  # from OCI Vault in every real environment
    jwt_ttl_s: int = 900
    public_base_url: str = "http://localhost:8080"

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    mistral_api_key: str = ""
    mistral_model: str = "mistral-small-latest"
    mistral_timeout_s: float = 8.0
    ai_daily_limit_per_subscriber: int = 20

    bill_due_days: int = 14


@lru_cache
def get_settings() -> Settings:
    return Settings()
