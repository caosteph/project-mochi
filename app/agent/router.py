"""The deterministic sensitivity router (Phase 4A) — the single authority that picks
a local vs hosted model, by DATA ORIGIN, in code (never by an LLM's judgment).

Guiding principle #1 of the project: sensitive data (Gmail/Calendar/Drive/memory/
personal) → local model only; non-sensitive work MAY use an opt-in hosted model; the
choice fails closed (anything uncertain → local); `LOCAL_ONLY` forces everything local.

Sensitivity is tagged by the CALLER in code — the main agent and the quarantined reader
pass SENSITIVE (always local); only the de-identified `consult_expert` payload passes
NON_SENSITIVE. So the "sensitive → local" guarantee lives here, in one auditable place.
"""

from enum import StrEnum

from langchain_openai import ChatOpenAI

from app.config import settings


class Sensitivity(StrEnum):
    SENSITIVE = "sensitive"          # personal/origin-tagged data → local, always
    NON_SENSITIVE = "non_sensitive"  # generic/de-identified → may use hosted (if opted in)


def hosted_available() -> bool:
    """True only if hosting is opted in AND fully configured AND not overridden by
    LOCAL_ONLY. Any missing piece → False (fails closed to local)."""
    return bool(
        settings.hosted_enabled
        and not settings.local_only
        and settings.hosted_base_url
        and settings.hosted_model
        and settings.hosted_api_key
    )


def chat_model(sensitivity: Sensitivity, *, temperature: float = 0.4, tools=None):
    """Build the model for a task of the given sensitivity. NON_SENSITIVE routes to the
    hosted endpoint only when `hosted_available()`; EVERYTHING else (SENSITIVE, unknown,
    hosted-off, misconfigured) routes local. Optionally binds tools."""
    if sensitivity == Sensitivity.NON_SENSITIVE and hosted_available():
        model = ChatOpenAI(
            base_url=settings.hosted_base_url,
            api_key=settings.hosted_api_key,
            model=settings.hosted_model,
            temperature=temperature,
        )
    else:
        model = ChatOpenAI(
            base_url=settings.ollama_base_url,
            api_key="ollama",  # Ollama ignores the key; the OpenAI client requires one
            model=settings.local_model,
            temperature=temperature,
        )
    if tools:
        model = model.bind_tools(tools)
    return model
