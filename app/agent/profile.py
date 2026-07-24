"""The always-on profile card: a compact block of Stephanie's pinned facts appended to the system
prompt every turn, so her core rules shape *every* reply instead of only surfacing when the model
happens to call the `recall` tool.

Deliberately small and behavioral (pinned facts are curated at import time, see
scripts/import_profile.py). The bulk of memory stays in recall for on-demand lookup — this is only
the handful that must be present unconditionally. Rendering is pure; the DB read + caching live in
app/agent/graph.py so this stays a trivial, testable formatter.
"""

from app.memory.models import Fact

_HEADER = (
    "What you know about Stephanie, and how she wants you to work. Treat these as standing "
    "instructions and follow them in every reply:"
)


def render_card(facts: list[Fact]) -> str:
    """Format pinned facts into a leading system-prompt block. Empty string when there are none, so
    a fresh DB / CI (no pinned facts) leaves the prompt exactly as it was."""
    if not facts:
        return ""
    lines = "\n".join(f"- {f.text}" for f in facts)
    return f"\n\n---\n{_HEADER}\n{lines}"
