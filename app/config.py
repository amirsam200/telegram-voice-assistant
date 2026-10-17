from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    allowed_user_ids: str = ""

    owner_name: str = ""
    owner_email: str = ""

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

    # Google Calendar
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"

    timezone: str = "Asia/Tehran"
    debug: bool = False

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
