"""Live Phase 2 verification — exercises the real Google integration end to end.
Gated on OAuth being configured (data/google_token.json). If it isn't, prints the
setup steps and exits 0 (not-configured is not a failure).

Deterministic gate mechanics are covered offline in tests/test_confirm_gate.py and
tests/test_google_services.py; this script proves the parts those can't: real
calendar reads, real Gmail metadata, and a real draft round-trip (create -> verify
-> delete) against the actual account. Any draft it creates is addressed to
Stephanie herself and deleted before exit.

Run:
    DATABASE_URL=postgresql://localhost/personal_agent_test \
        PYTHONPATH=. uv run python scripts/verify_phase2.py
"""

import sys
import uuid


from scripts._verify_lib import bootstrap_env, check, require_scratch_db, summarize_and_exit

require_scratch_db()
bootstrap_env()

from app.integrations import google_auth, google_calendar, google_gmail  # noqa: E402

SETUP_STEPS = """\
Google OAuth is not configured yet — nothing to verify live. To set it up:
  1. console.cloud.google.com -> New Project (e.g. "mochi-agent").
  2. APIs & Services -> Library -> enable "Gmail API" and "Google Calendar API".
  3. OAuth consent screen -> External -> add your @gmail.com as a Test user -> keep status "Testing".
  4. Credentials -> Create OAuth client ID -> "Desktop app" -> Download JSON.
  5. Save it to data/google_client_secret.json.
Then run this script again; the first run opens a browser once to grant access.
"""





def main() -> None:
    if not google_auth.has_token():
        print(SETUP_STEPS)
        sys.exit(0)

    # 1. Calendar read returns a structured list.
    events = google_calendar.list_events()
    check("calendar read returns a list", isinstance(events, list), f"{len(events)} events in next 7d")

    # 2. Gmail reads surface metadata ONLY — no body/snippet keys.
    msgs = google_gmail.list_recent_metadata(max_results=3)
    keys_ok = all(set(m.keys()) == {"from", "subject", "date"} for m in msgs)
    check("gmail read is metadata-only (no body/snippet)", keys_ok, f"{len(msgs)} messages")

    # 3. Real draft round-trip: create -> confirm it exists -> delete -> confirm gone.
    to = settings_email()
    subject = f"[mochi-verify] safe to ignore {uuid.uuid4().hex[:8]}"
    draft = google_gmail.create_draft(to=to, subject=subject, body="Automated Phase 2 verification draft.")
    draft_id = draft.get("id")
    check("draft created via Gmail API", bool(draft_id), f"id={draft_id}")

    service = google_gmail._service()
    listed = service.users().drafts().list(userId="me").execute().get("drafts", [])
    exists = any(d["id"] == draft_id for d in listed)
    check("created draft is present in Gmail (unsent)", exists)

    google_gmail.delete_draft(draft_id)
    listed_after = service.users().drafts().list(userId="me").execute().get("drafts", [])
    gone = all(d["id"] != draft_id for d in listed_after)
    check("verification draft cleaned up (deleted)", gone)

    # 4. Model-driven tool-call reliability — does the model actually FIRE the tools?
    # This is what the Telegram status breadcrumb keys off, and it's the thing that
    # slipped before (verified plumbing, not behavior). Rate-based: a 7B local model
    # is probabilistic. Cleans up any drafts it happens to create.
    check_tool_reliability()

    print(
        "\nNote: the full model->interrupt->Telegram-button->resume path is still a final "
        "live human check on the phone. The gate mechanics are covered offline in "
        "tests/test_confirm_gate.py; whether the model fires the tools is measured above."
    )
    summarize_and_exit()


def check_tool_reliability(floor: float = 0.6) -> None:
    """Drive the real graph (fresh threads) and measure how often the model actually
    calls calendar_list_events and create_draft across varied phrasings."""
    import uuid as _uuid

    from langchain_core.messages import HumanMessage

    from app.agent.graph import build_agent

    agent = build_agent()

    def tool_calls_for(prompt: str) -> list[str]:
        names: list[str] = []
        cfg = {"configurable": {"thread_id": f"verify-tool-{_uuid.uuid4()}"}}
        for update in agent.stream({"messages": [HumanMessage(prompt)]}, cfg, stream_mode="updates"):
            if "__interrupt__" in update:
                names.append("__interrupt__")
            agent_payload = update.get("agent")
            if agent_payload and agent_payload.get("messages"):
                msg = agent_payload["messages"][-1]
                names += [tc["name"] for tc in (getattr(msg, "tool_calls", None) or [])]
        return names

    cal_prompts = [
        "what's on my calendar today?",
        "am I free tomorrow afternoon?",
        "what meetings do I have this week?",
    ]
    # No real personal email here (public repo). NOTE: these all fired 0/4 while the model ran
    # at num_ctx=4096 — which was misdiagnosed as "create_draft is tool-count-diluted". The real
    # cause was context exhaustion (prompts ~4,000 tokens against a 4,096 window left ~75 tokens
    # of generation headroom). At num_ctx=8192 they fire 4/4. See docs/14-future-work.md.
    draft_prompts = [
        "draft an email to me saying hi",
        "draft a quick note to myself reminding me to call mom",
        "draft a note to alex@example.com saying hello",
    ]

    # Each probe streams only to the approval interrupt and never approves, so
    # create_draft pauses BEFORE writing — no real drafts are created, nothing to
    # clean up. We're measuring whether the model *decides* to call the tool.
    cal_hits = sum("calendar_list_events" in tool_calls_for(p) for p in cal_prompts)
    draft_hits = sum("create_draft" in tool_calls_for(p) for p in draft_prompts)

    check(
        "model fires calendar_list_events (rate)",
        cal_hits / len(cal_prompts) >= floor,
        f"{cal_hits}/{len(cal_prompts)} — soft-tier (prompt) reliability on a 7B, floor {floor:.0%}",
    )
    # Restored to the normal floor: the lower one was a workaround for a MISDIAGNOSIS — the
    # create_draft "marginality" was context exhaustion at num_ctx=4096, not tool-count dilution.
    # With the 8k-context model these prompts fire 4/4. Gate correctness is separately proven
    # deterministically in tests/test_confirm_gate.py.
    check(
        "model fires create_draft (rate)",
        draft_hits / len(draft_prompts) >= floor,
        f"{draft_hits}/{len(draft_prompts)} — soft-tier (prompt) reliability on a 7B, floor {floor:.0%}",
    )


def settings_email() -> str:
    # Draft to self so it's harmless. Prefer the token's own address; fall back to a
    # placeholder that still exercises the create/delete path.
    try:
        profile = google_gmail._service().users().getProfile(userId="me").execute()
        return profile.get("emailAddress", "me@example.com")
    except Exception:
        return "me@example.com"


if __name__ == "__main__":
    main()
