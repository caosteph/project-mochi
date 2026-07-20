"""The `web_search` tool (Phase 8) — lets the LOCAL model look things up online WITHOUT
Stephanie's raw personal data leaving the machine. Same layered, fail-closed spine as
`consult_expert`, plus a human-in-the-loop approval:

  1. off / unconfigured → answer locally (nothing sent);
  2. deterministic scrubber strips known identifiers + PII from the query;
  3. a PII-dense query is refused (answered locally);
  4. Stephanie APPROVES the (scrubbed) query before it runs — so approval doubles as a
     preview of exactly what will leave the machine;
  5. every query is audited (WebSearch → /sent);
  6. results are UNTRUSTED web content — framed as data, never instructions, and read by
     the LOCAL model.

Only a scrubbed, generic query ever leaves; the answer is synthesized locally. See
docs/04-constitution.md for the scoped-decision note (this is independent of LOCAL_ONLY).
"""

from langchain_core.tools import tool
from sqlmodel import Session

from app.agent import rate_limit, sanitize
from app.agent.confirm import require_approval
from app.agent.tools.google_tools import frame_untrusted
from app.config import settings
from app.integrations import web_search as search_api
from app.memory.db import get_engine
from app.memory.models import WebSearch

_UNAVAILABLE = (
    "Web search isn't set up right now — answer from your own knowledge, and say you "
    "couldn't look it up if it needs current info."
)
_TOO_PERSONAL = (
    "That query is too personal to send to a search engine — answer it yourself from what "
    "you know instead."
)
_NO_RESULTS = "The web search came back empty — tell her you couldn't find anything on that."


def web_search_available() -> bool:
    """True only if web search is enabled AND the chosen provider is usable (tavily needs a
    key; duckduckgo needs nothing). Independent of LOCAL_ONLY — a scrubbed generic query is
    a smaller, separate externality than the hosted LLM (docs/04-constitution.md)."""
    if not settings.web_search_enabled:
        return False
    provider = (settings.web_search_provider or "").lower()
    if provider == "duckduckgo":
        return True
    if provider == "tavily":
        return bool(settings.web_search_api_key)
    return False


@tool
def web_search(query: str) -> str:
    """Search the web for CURRENT or factual info you don't already know and can't get from
    her data — weather, prices, store hours, "is X open", news, sports scores, definitions,
    "who/what/when is …". Pass a short, GENERIC query with NO personal details (no names,
    emails, addresses). Results come back for YOU to read and summarize in plain language,
    citing what you found. Use this instead of guessing whenever she asks about the outside
    world."""
    if not web_search_available():
        return _UNAVAILABLE
    clean, hits = sanitize.redact(query)
    if sanitize.is_too_personal(hits):
        return _TOO_PERSONAL
    # Approve BEFORE anything leaves; she sees the scrubbed query she's approving.
    if not require_approval("web_search", {"query": clean}):
        return "Search cancelled — nothing was sent."
    # Cap AFTER approval so the interrupt re-run doesn't double-count.
    if not rate_limit.allow("web_search"):
        return "I've hit my hourly limit on web searches — answer from your own knowledge for now."
    try:
        results = search_api.search(clean)
    except Exception:
        return "The web search failed to run — answer from your own knowledge and mention you couldn't look it up."
    with Session(get_engine()) as session:
        session.add(WebSearch(query=clean, n_redactions=hits, n_results=len(results)))
        session.commit()
    if not results:
        return _NO_RESULTS
    body = "\n\n".join(f"- {r.title}\n  {r.url}\n  {r.snippet}" for r in results)
    return frame_untrusted("web search", body)


WEB_TOOLS = [web_search]
