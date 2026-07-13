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

    # Embeddings — always local; deliberately no separate/hosted embedding URL setting.
    embedding_model: str = "nomic-embed-text"
    embedding_dims: int = 768

    # Retrieval tunables
    recall_default_k: int = 8
    recall_candidate_limit: int = 30
    recall_similarity_weight: float = 0.45
    recall_keyword_weight: float = 0.20
    recall_recency_weight: float = 0.20
    recall_confidence_weight: float = 0.15
    recall_recency_half_life_days: float = 30.0

    # Context-window management
    working_buffer_max_tokens: int = 3000
    working_buffer_keep_recent: int = 6

    # Google (Phase 2). Paths only — OAuth scopes are a constant in
    # app/integrations/google_auth.py (least-privilege, not env-tunable). Both
    # files live under git-ignored data/ and never leave the machine.
    google_client_secret_path: str = "data/google_client_secret.json"
    google_token_path: str = "data/google_token.json"

    # Keep the local model resident so a message after an idle gap isn't slow to
    # reload (Ollama unloads after ~5 min idle by default). A background ping every
    # this-many seconds keeps it warm; 0 disables. Must be < Ollama's idle timeout.
    keep_warm_interval_seconds: int = 240

    # Proactive reminders (Phase 3A).
    proactivity_enabled: bool = True       # runtime kill-switch seed (toggle via /pause /resume)
    reminder_tick_interval_seconds: int = 60
    reminder_lead_days: int = 3            # nudge this many days before a return window closes
    reminder_snooze_days: int = 1
    quiet_hours_start: int = 21            # no proactive nudges from 9pm…
    quiet_hours_end: int = 8               # …until 8am (local wall-clock)
    calendar_mirror_enabled: bool = True   # mirror timed reminders into Google Calendar events
    reminder_event_default_minutes: int = 15  # calendar-event length when the task implies no duration

    # Cross-turn cap on side-effectful agent actions (draft/reminder creation) — a
    # runaway/injected-loop guard, per action type, per rolling hour.
    max_actions_per_hour: int = 30

    # Email signal ingestion (Phase 3B) — the quarantined reader scans recent mail,
    # extracts a typed actionable signal, and proactively asks before creating a reminder.
    signal_scanning_enabled: bool = True        # kill-switch for reading email bodies at all
    signal_scan_interval_seconds: int = 21600   # ~6h between scans
    signal_scan_window_days: int = 3            # Gmail `newer_than` window per scan (overlap safety)
    signal_max_per_scan: int = 5                # cap on bodies fetched + reader calls per scan (cost bound)
    signal_default_return_days: int = 30        # fallback return window when a `return` states no date


settings = Settings()  # import this everywhere: `from app.config import settings`
