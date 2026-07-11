"""Central configuration. One place for every setting; fails loudly if a secret is missing.

Values load from environment variables, falling back to a local `.env` file
(git-ignored). Field names map to upper-case env vars, e.g. `telegram_bot_token`
reads `TELEGRAM_BOT_TOKEN`.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: int  # the only chat allowed to talk to the agent (whitelist)

    # Database
    database_url: str = "postgresql://localhost/personal_agent"

    # Local inference (Ollama, OpenAI-compatible)
    ollama_base_url: str = "http://localhost:11434/v1"
    local_model: str = "qwen2.5:7b"

    # Privacy master switch. Keep true until the Phase 4 router exists.
    local_only: bool = True


settings = Settings()  # import this everywhere: `from app.config import settings`
