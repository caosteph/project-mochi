"""The `consult_expert` tool (Phase 4A) — lets the local agent get help from a stronger
FREE hosted model on hard/generic sub-problems, WITHOUT Stephanie's raw personal data
leaving the machine.

Safety is layered and fails closed:
  1. hosted off / not configured / LOCAL_ONLY → returns "answer locally" (nothing sent);
  2. deterministic scrubber strips known identifiers + PII from the question;
  3. a PII-dense question is refused (answered locally);
  4. the hosted model has NO tools (can't act);
  5. every hosted call is audited (HostedConsult → /sent) so nothing is silent.

The agent is instructed to phrase a generic, de-identified question and to re-personalize
the answer itself — the model half of de-identification is best-effort, the scrubber is the
hard backstop. See docs/04-constitution.md for the honest scope of this guarantee.
"""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from sqlmodel import Session

from app.agent import rate_limit, router, sanitize
from app.agent.router import Sensitivity
from app.memory.db import get_engine
from app.memory.models import HostedConsult

_UNAVAILABLE = "The expert model isn't available right now — answer from your own knowledge."
_TOO_PERSONAL = "That question is too personal to send externally — I'll answer it myself."
_EXPERT_SYSTEM = SystemMessage(
    "You are a knowledgeable expert assistant. Answer the question clearly, correctly, and "
    "concisely. You are given a generic, de-identified question — just answer it."
)


@tool
def consult_expert(question: str) -> str:
    """Ask a more capable external model for help with a hard reasoning, coding, or
    general-knowledge problem the local model may struggle with. You MUST phrase the
    question GENERICALLY and de-identified — no names, emails, addresses, or personal
    specifics; describe any personal context in general terms (e.g. "a vegetarian who
    dislikes cilantro", not Stephanie's name). The expert's answer comes back for YOU to
    adapt to Stephanie's real situation. If it's unavailable, just answer yourself."""
    if not router.hosted_available():
        return _UNAVAILABLE
    if not rate_limit.allow("consult_expert"):
        return "I've hit my hourly limit on external lookups — answer from your own knowledge."
    clean, hits = sanitize.redact(question)
    if sanitize.is_too_personal(hits):
        return _TOO_PERSONAL
    try:
        answer = router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.4).invoke(
            [_EXPERT_SYSTEM, HumanMessage(clean)]
        ).content
    except Exception:
        return _UNAVAILABLE
    with Session(get_engine()) as session:
        session.add(HostedConsult(sent_text=clean, answer=answer, n_redactions=hits))
        session.commit()
    return (
        "[Expert's answer to a de-identified version of your question — adapt it to "
        f"Stephanie's actual context before replying]\n{answer}"
    )


EXPERT_TOOLS = [consult_expert]
