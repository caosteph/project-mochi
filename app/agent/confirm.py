"""Human-in-the-loop confirmation gate — the reusable mechanism every
side-effectful tool routes through so external writes pause for Stephanie's
explicit approval.

Uses LangGraph's interrupt(): calling it pauses the graph, the proposal surfaces
in the invoke result under `__interrupt__`, the channel pushes Approve/Reject to
Telegram, and Command(resume={"approved": bool}) delivers the decision back here
as interrupt()'s return value.

CRITICAL: interrupt() re-runs the enclosing node from the top on resume, so a tool
MUST call require_approval() BEFORE any side effect and perform the write only after
this returns True. Never write before the gate.
"""

from langgraph.types import interrupt


def require_approval(action: str, details: dict) -> bool:
    """Pause for approval of a side-effectful action. Returns True iff approved.

    `action` is a short machine name (e.g. "create_draft"); `details` is the
    human-readable proposal (recipient/subject/body) shown in the Telegram prompt.
    """
    decision = interrupt({"type": "approval_request", "action": action, "details": details})
    if isinstance(decision, dict):
        return bool(decision.get("approved"))
    return bool(decision)
