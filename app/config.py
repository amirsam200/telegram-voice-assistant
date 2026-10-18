from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    allowed_user_ids: str = ""

    owner_name: str = ""
    owner_email: str = ""

    # Email: "gmail" sends from the owner's Gmail (Google sign-in), "resend" uses Resend.
    email_provider: str = "gmail"
    resend_api_key: str = ""
    email_from: str = "onboarding@resend.dev"

    database_url: str = "postgresql+psycopg://assistant:assistant@db:5432/assistant"

    # Speech to text
    voice_dir: str = "/app/data/voice"
    vosk_model_path: str = "/opt/vosk-model"
    stt_min_confidence: float = 0.5

    # LLM (OpenAI-compatible)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3
    # USD per 1M tokens, per model: {"model": {"input": ..., "cached_input": ..., "output": ...}}.
    # Set LLM_PRICES (JSON) to your provider's current prices; unknown models get no cost.
    llm_prices: dict[str, dict[str, float]] = {
        "deepseek-chat": {"input": 0.27, "cached_input": 0.07, "output": 1.10},
        "deepseek-reasoner": {"input": 0.55, "cached_input": 0.14, "output": 2.19},
    }

    # Google (Calendar, and Gmail when EMAIL_PROVIDER=gmail)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"

    timezone: str = "Asia/Tehran"
    debug: bool = False

    # Token budgets (prompt + completion tokens, Asia/Tehran days and months; 0 turns one off).
    # The owner gets a Telegram alert at 80 and 100 percent; with the hard stop on, the bot
    # refuses new requests once a budget is used up.
    daily_token_budget: int = 300_000
    monthly_token_budget: int = 6_000_000
    budget_hard_stop: bool = False

    # Admin API: every endpoint except /health needs this key in the X-API-Key header.
    admin_api_key: str = ""

    # Bot limits and housekeeping
    rate_limit_per_minute: int = 10  # messages per Telegram user per minute
    max_voice_seconds: int = 60
    audio_max_age_s: int = 3600  # leftover audio older than this is deleted
    audio_cleanup_interval_s: int = 600

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_user_ids.replace(" ", "").split(",") if x}

    @property
    def owner_telegram_id(self) -> int | None:
        ids = [int(x) for x in self.allowed_user_ids.replace(" ", "").split(",") if x]
        return ids[0] if ids else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
