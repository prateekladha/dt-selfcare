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

    # RS256 keys (PEM) from OCI Vault. Only core-api (the token issuer) gets the private key; every other
    # service gets just the public key. Neither set = a throwaway in-process keypair (tests, local runs).
    jwt_private_key: str = ""
    jwt_public_key: str = ""
    # set in the cluster: never fall back to a throwaway keypair (fail loudly if a key is missing)
    require_jwt_keys: bool = False
    jwt_ttl_s: int = 900
    public_base_url: str = "http://localhost:8080"

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # LLM providers (selected per country by the `ai-provider` flag)
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    mistral_api_key: str = ""
    mistral_model: str = "mistral-small-latest"
    selfhosted_llm_url: str = ""  # vLLM in-region (OpenAI-compatible)
    selfhosted_llm_key: str = ""
    selfhosted_llm_model: str = "deepseek-ai/DeepSeek-V3"
    llm_timeout_s: float = 30.0
    # LLM gateway (LiteLLM): provider keys, routing, fallback and rate limits live in the gateway;
    # services only know the gateway URL, a gateway key and a model alias.
    llm_gateway_url: str = ""
    llm_gateway_key: str = ""
    llm_model_alias: str = "billing-llm"
    llm_title_alias: str = "title-llm"
    bss_max_rps: float = 200.0         # legacy BSS capacity, enforced by Temporal across all workers
    bss_max_concurrency: int = 50      # per worker pod (legacy calls take 0.5-3 s)  # nano model for chat titles (falls back inside the gateway)
    llm_max_concurrency: int = 32  # per pod
    ai_daily_limit_per_subscriber: int = 20

    bill_due_days: int = 14


@lru_cache
def get_settings() -> Settings:
    return Settings()
