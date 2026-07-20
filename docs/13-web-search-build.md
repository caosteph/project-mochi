# Phase 8 — web search (privacy-scrubbed, approved, audited)

Builds on Phase 4A. Mochi can now **look things up online** — weather, prices, store hours, "is X
open?", news, definitions — closing the biggest remaining gap (before this she could only answer
from the 7B's frozen knowledge + her own data). It fits the project's privacy model exactly: only a
**PII-scrubbed** query leaves, **Stephanie approves** it first, results are **synthesized on the
local model**, and **every query is audited**.

## The privacy spine (reused almost verbatim from `consult_expert`)

`web_search` (`app/agent/tools/web_tools.py`) is layered and fails closed:

1. **off / unconfigured → answer locally** (`web_search_available()` false → nothing sent).
2. **scrub** — `sanitize.redact(query)` hard-redacts known identifiers + structural PII.
3. **refuse if PII-dense** — `sanitize.is_too_personal` → answer locally (fails closed).
4. **human approval** — `require_approval("web_search", {"query": clean})` pauses the graph and
   surfaces the **scrubbed** query with Approve/Reject, so approval doubles as a preview of exactly
   what will leave the machine.
5. **rate-limit** (after approval, so the interrupt re-run doesn't double-count).
6. **audit** — a `WebSearch` row (scrubbed query, #redactions, #results), reviewable via `/sent`.
7. **untrusted results** — framed with `frame_untrusted("web search", …)` and read/synthesized by
   the **local** model (search results are attacker-influenceable content — data, never instructions).

Only a scrubbed, generic query ever leaves; the answer is composed locally. This is a written scoped
decision in [`docs/04-constitution.md`](./04-constitution.md) — notably, web search is **independent
of `LOCAL_ONLY`** (that flag governs the hosted *LLM* for personal data; a scrubbed generic query is
a smaller, separate externality with its own opt-in `WEB_SEARCH_ENABLED`).

## Pluggable provider (easy to switch; $0 to start)

`app/integrations/web_search.py` is a thin provider seam — `search(query, *, provider, api_key,
max_results, client=None) -> list[SearchResult]`:

- **`tavily`** (default) — built for agents, clean snippets; free tier (a free API key). httpx POST;
  `client` injectable so the path runs offline in tests.
- **`duckduckgo`** — **no key, no signup ($0)** via the `ddgs` package; thinner, can rate-limit.

Switching is one config value (`WEB_SEARCH_PROVIDER`). Adding SearXNG (self-hosted, full-local) or
Brave later is a new `_provider()` function — see the roadmap.

## The approval gate generalized

The Telegram approval renderer was hardcoded to draft fields (`To:/Subject:/body`). It's now
`_render_proposal(action, details)` switching on the action (draft → the draft; `web_search` → the
scrubbed query). This is the seed of the roadmap's **generalizable per-action approval layer**;
`/sent` now shows both consults and searches (one "what left the machine" view).

## What ships

- `app/integrations/web_search.py` (NEW) — provider seam + `SearchResult`.
- `app/agent/tools/web_tools.py` (NEW) — the `web_search` tool + `web_search_available()`.
- `app/memory/models.py` — `WebSearch` audit table.
- `app/agent/tools/__init__.py` — `WEB_TOOLS` in `ALL_TOOLS` (17 tools; dynamic binding caps per turn).
- `app/agent/tool_select.py` — `web_search` keywords (weather/price/open-on/who-is/current…).
- `app/channels/telegram.py` — `_render_proposal`, `web_search` status line, `/sent` merges searches.
- `app/config.py` + `.env.example` — `web_search_enabled/provider/api_key/max_results`, `TAVILY_API_KEY`.
- `pyproject.toml` — `ddgs`.
- **Persona: no edit** — no false "can't search" claim exists (line 43 already frames web content as
  data), so `web_search` fires from its tool description + keywords, deliberately avoiding the
  Phase-7 persona-regression trap. Confirmed by the HEAD-vs-mine tool-firing bisection.

## Testing

- **`tests/test_web_search.py`** (offline) — the load-bearing `test_query_is_scrubbed_before_it_leaves`
  (PII removed before the provider *and* the audit ever see it), PII-dense refusal, provider-swap
  parsing (Tavily vs DDG), framing, audit row, no-results/unavailable/rate-limit, and the **approval
  gate through a real graph** (pauses; reject searches nothing; approve searches exactly once).
- **`scripts/verify_web_search.py`** (real) — the 7B fires `web_search` for outside-world questions;
  a live provider round-trip if configured (else skips). Added to `verify_all.sh`.
- **`scripts/verify_scenarios.py`** — a `web_search` firing check.
- **Mandatory (Phase-7 lesson):** HEAD-vs-mine N=4 bisection on `add_reminder` + `create_draft`
  confirmed the new tool didn't regress tool-firing.

## Verifying

```bash
DATABASE_URL=postgresql://localhost/personal_agent_test uv run pytest tests/ -q
uv run ruff check app/ tests/ scripts/
DATABASE_URL=postgresql://localhost/personal_agent_test uv run python scripts/verify_web_search.py
./scripts/verify_all.sh
```

Live (with a Tavily key or `WEB_SEARCH_PROVIDER=duckduckgo`): "what's the weather in Tokyo?" →
Approve → a sourced answer; confirm the scrubbed query in `/sent`.

## Deferred (see the consolidated future-work list)

Deep-read a full result page via the quarantined reader (today: snippets only) · more/self-hosted
providers (SearXNG, Brave) · the generalizable per-action approval layer · Mac mini + larger model.
