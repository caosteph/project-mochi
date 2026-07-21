"""Live Phase 4A verification — the sensitivity router + de-identified hosted delegation.

Offline tests (tests/test_router|sanitize|expert_tool.py) prove the logic + the
sensitive-always-local invariant. This script proves the parts that touch the real model:
the deterministic scrubber at 100%, the local 7B's *best-effort* de-identification quality
(measured, since that's the model-mediated half of the hybrid), and a real local /ask
round-trip — plus an optional real hosted round-trip if you've opted in.

Run (Ollama must be serving the local model):
    PYTHONPATH=. uv run python scripts/verify_phase4a.py
"""


from scripts._verify_lib import bootstrap_env, check, skip, summarize_and_exit

bootstrap_env()

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from app.agent import router, sanitize  # noqa: E402
from app.agent.router import Sensitivity  # noqa: E402
from app.config import settings  # noqa: E402





# Personal questions with KNOWN identifiers we expect to be gone after de-identification.
_DEID_CASES = [
    ("My name is Stephanie Cao — help me write a short professional bio.", ["stephanie", "cao"]),
    ("Email jordan.lee@example.com to ask about the API rate limits.", ["jordan", "example.com"]),
    ("Call me at 415-555-1212 to review my resume feedback.", ["415-555-1212"]),
    ("I'm Priya and I live at 24 Elm Street — suggest a house-warming menu.", ["priya", "elm street"]),
    ("My coworker Marcus keeps missing deadlines; how do I raise it with him?", ["marcus"]),
]

_DEID_SYSTEM = SystemMessage(
    "Rewrite the user's question into a generic, de-identified version to send to an external "
    "assistant. Remove ALL names, email addresses, phone numbers, and personal specifics; keep the "
    "general intent. Output ONLY the rewritten question, nothing else."
)


def main() -> None:
    print(f"Local model: {settings.local_model} @ {settings.ollama_base_url}")
    print(f"hosted_available: {router.hosted_available()}\n")

    # 1. Routing table against toggled settings (no network).
    def _local(m):
        return str(m.openai_api_base) == settings.ollama_base_url and m.model_name == settings.local_model

    orig = (settings.hosted_enabled, settings.local_only, settings.hosted_base_url,
            settings.hosted_model, settings.hosted_api_key)
    try:
        settings.hosted_enabled, settings.local_only = True, False
        settings.hosted_base_url, settings.hosted_model, settings.hosted_api_key = (
            "https://api.example.com/v1", "big-model", "sk-x")
        ok = (
            router.hosted_available()
            and not _local(router.chat_model(Sensitivity.NON_SENSITIVE))
            and _local(router.chat_model(Sensitivity.SENSITIVE))  # invariant: sensitive → local
        )
        settings.local_only = True  # LOCAL_ONLY overrides everything
        ok = ok and not router.hosted_available() and _local(router.chat_model(Sensitivity.NON_SENSITIVE))
    finally:
        (settings.hosted_enabled, settings.local_only, settings.hosted_base_url,
         settings.hosted_model, settings.hosted_api_key) = orig
    check("routing table (hosted for non-sensitive; sensitive always local; LOCAL_ONLY wins)", ok)

    # 2. Deterministic scrubber — 100% on known terms + PII.
    settings.redact_terms = "Stephanie, Steph Cao"
    clean, hits = sanitize.redact("Stephanie (aka Steph Cao) — email s@x.com, call 415-555-1212, ssn 123-45-6789")
    det_ok = all(t not in clean for t in ("Stephanie", "Steph Cao", "s@x.com", "415-555-1212", "123-45-6789"))
    check("deterministic scrubber removes all known identifiers + PII", det_ok, f"{hits} redactions")

    # 3. Local model de-identification quality (best-effort half — MEASURED, not assumed).
    settings.redact_terms = ""
    deid_model = router.chat_model(Sensitivity.SENSITIVE, temperature=0)  # local
    hits_ok = 0
    for question, identifiers in _DEID_CASES:
        try:
            out = deid_model.invoke([_DEID_SYSTEM, HumanMessage(question)]).content.lower()
        except Exception as exc:
            check(f"de-id: {question[:32]}…", False, f"model error: {str(exc)[:60]}")
            continue
        gone = all(ident not in out for ident in identifiers)
        hits_ok += gone
        check(f"de-id: {question[:32]}…", gone, f"identifiers {'removed' if gone else 'LEAKED'}")
    rate = hits_ok / len(_DEID_CASES)
    check("de-identification rate (model-mediated, best-effort)", rate >= 0.6,
          f"{hits_ok}/{len(_DEID_CASES)} — floor 60%; the deterministic scrubber backstops the rest")

    # 4. Real local /ask round-trip (hosted off → NON_SENSITIVE falls back to local).
    try:
        ans = router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.3).invoke(
            [SystemMessage("You are a concise assistant."), HumanMessage("What is 2+2? Answer with just the number.")]
        ).content
        check("local /ask round-trip", "4" in ans, f"answer={ans[:40]!r}")
    except Exception as exc:
        check("local /ask round-trip", False, str(exc)[:80])

    # 5. Optional real hosted round-trip (only if opted in).
    if router.hosted_available():
        try:
            ans = router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.3).invoke(
                [SystemMessage("You are a concise assistant."), HumanMessage("Say 'hello from hosted'.")]
            ).content
            check("real hosted round-trip", bool(ans and ans.strip()), f"answer={ans[:40]!r}")
            check("invariant live: sensitive still local with hosting on", _local(router.chat_model(Sensitivity.SENSITIVE)))
        except Exception as exc:
            check("real hosted round-trip", False, str(exc)[:80])
    else:
        skip("real hosted round-trip", "hosting not enabled/configured (fully-local mode)")

    summarize_and_exit()


if __name__ == "__main__":
    main()
