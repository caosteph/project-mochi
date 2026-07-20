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

    # Privacy master switch. When true, EVERYTHING runs locally regardless of the
    # hosted settings below — the router honors this first (fails closed to local).
    local_only: bool = True

    # Opt-in hosted endpoint (Phase 4A) — a FREE, open-weight, OpenAI-compatible model
    # (e.g. Groq/OpenRouter/Cerebras) used ONLY for non-sensitive work (the consult_expert
    # tool + /ask). Off by default; even when configured, `local_only` overrides it.
    hosted_enabled: bool = False
    hosted_base_url: str | None = None
    hosted_model: str | None = None
    hosted_api_key: str | None = None

    # De-identification backstop for anything sent to hosted. `redact_terms` is a
    # comma-separated list of exact strings to always hard-redact (your name, email,
    # aliases). Past `redact_max_hits` redactions, a query is treated as inherently
    # personal → not delegated (answered locally). See app/agent/sanitize.py.
    redact_terms: str = ""
    redact_max_hits: int = 4

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

    # Post-turn fact-capture sweep (reliable memory) — a dedicated local extraction that
    # backstops the flaky remember_fact tool. Runs in the background after each reply.
    fact_sweep_enabled: bool = True
    fact_dedup_similarity: float = 0.88  # skip storing a fact this similar to an existing one

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
    reminder_dedup_window_minutes: int = 60  # skip creating a same-text reminder within this of an existing one
    quiet_hours_start: int = 21            # no proactive nudges from 9pm…
    quiet_hours_end: int = 8               # …until 8am (local wall-clock)
    calendar_mirror_enabled: bool = True   # mirror timed reminders into Google Calendar events
    reminder_event_default_minutes: int = 15  # calendar-event length when the task implies no duration

    # Daily briefing (Phase 6) — one deterministic morning digest (today's calendar +
    # reminders due today + active goals/tasks). Pushed once a day; /briefing gets it
    # on demand. Assembled in code (no LLM), so it can't go incoherent or dump JSON.
    briefing_enabled: bool = True          # the morning push; /briefing (on-demand) always works
    briefing_hour: int = 8                 # local hour for the morning push (after quiet hours ends)

    # Cross-turn cap on side-effectful agent actions (draft/reminder creation) — a
    # runaway/injected-loop guard, per action type, per rolling hour.
    max_actions_per_hour: int = 30

    # Builder (Phase 4B) — Mochi scaffolds/serves web apps + generates docs in a sandbox.
    builder_port_base: int = 8100        # first port to try when serving built apps
    builder_sandbox_timeout: int = 120   # seconds cap on a sandboxed command
    builder_npm_timeout: int = 300       # npm install/build can be slow
    builder_fs_deny: bool = True         # best-effort sandbox-exec deny of data//.env reads
    cloudflared_path: str = "cloudflared"

    # Email signal ingestion (Phase 3B) — the quarantined reader scans recent mail,
    # extracts a typed actionable signal, and proactively asks before creating a reminder.
    signal_scanning_enabled: bool = False       # OFF by default — the email scanner was too noisy; re-enable once proven quiet
    signal_scan_interval_seconds: int = 21600   # ~6h between scans
    signal_scan_window_days: int = 3            # Gmail `newer_than` window per scan (overlap safety)
    signal_max_per_scan: int = 3                # cap on bodies fetched + reader calls per scan (cost bound)
    signal_default_return_days: int = 30        # fallback return window when a `return` states no date
    signal_require_due_date: bool = True        # only surface signals with a concrete date (drops vague/FYI noise)

    # On-demand email reading (Phase 7) — "what did the X email say?" searches the inbox,
    # summarizes the newest match behind the SAME quarantined reader (body never reaches
    # the privileged agent). Only the newest match is read; this bounds how many are searched.
    email_read_max_candidates: int = 5


settings = Settings()  # import this everywhere: `from app.config import settings`
