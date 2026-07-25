"""The LangGraph agent. Phase 1 adds a tool-calling loop — the model can now
call the four memory tools, and the graph routes through a ToolNode and back
to the agent until the model stops requesting tools — and basic context-window
management: once the working message buffer grows past a token budget, the
oldest messages are folded into a rolling summary and removed from state, so
long conversations don't silently blow past the local model's context window.
The sensitivity router and human-in-the-loop confirmation gate still arrive in
later phases.
"""

import logging
from datetime import datetime
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from psycopg import Connection

from app.agent import profile, router, tool_select, toolcall_repair
from app.agent.persona import build_system_prompt
from app.agent.router import Sensitivity
from app.agent.tools import ALL_TOOLS
from app.config import settings
from app.memory.db import init_db

log = logging.getLogger(__name__)
_TOOL_NAMES = {t.name for t in ALL_TOOLS}

# Mochi's voice + soft operating principles, sourced from the versioned persona.md.
# Assembled once at import; the hard safety rules live in code, not this string.
SYSTEM_PROMPT = build_system_prompt()

# The always-on profile card (Stephanie's pinned facts) is read from the DB ONCE, lazily, and cached
# for the process — so `core` is byte-identical every turn and Ollama's prefix KV-cache survives (a
# per-turn DB read would bust it). It refreshes on restart, which is when new pins land anyway. Read
# lazily (not at import) so importing this module never requires the DB to be up.
_profile_card_cache: str | None = None


def _profile_card() -> str:
    global _profile_card_cache
    if _profile_card_cache is None:
        from sqlmodel import Session

        from app.memory import store
        from app.memory.db import get_engine
        try:
            with Session(get_engine()) as session:
                _profile_card_cache = profile.render_card(store.pinned_facts(session))
        except Exception:  # a memory read must never take down a turn — degrade to no card
            log.exception("profile card build failed; proceeding without it")
            _profile_card_cache = ""
    return _profile_card_cache


def invalidate_profile_card() -> None:
    """Drop the cached card so the next turn rebuilds it from the DB. Call after pins change (the
    `/pin` `/unpin` commands run in this same process), so a curated change takes effect without a
    restart. Rebuilding does re-cost one prefix warm-up, which is fine for a deliberate, rare edit."""
    global _profile_card_cache
    _profile_card_cache = None


class AgentState(MessagesState):
    """Adds a rolling conversation summary to the base message-list state.
    This is the "core block" beyond persona: no separate current-task tracker
    is introduced in Phase 1 — there's no natural object yet to represent
    conversational focus (the Task table is to-dos, not conversational
    state), so the summary itself stands in for that role.
    """

    summary: str
    # The tool names bound this turn, so the repair node can tell a mis-formatted call for a tool the
    # model WAS offered (execute it) from a call for a tool it wasn't (topic bleed → strip it).
    bound_tools: list[str]
    # How many text-tool-calls have been repaired-into-execution this user turn (loop guard).
    repair_count: int


# How many recent user turns feed tool selection. More than one because a confirmation ("yes",
# "do that") carries no routing signal of its own — the intent is in the turn before it.
TOOL_SELECT_TURNS = 3

# The main agent is the SENSITIVE path (it has memory + Google + persona) → the router
# always resolves this to the LOCAL model. Tools are bound PER TURN, not here: _agent_node
# selects a small relevant subset and binds only those (see app/agent/tool_select.py).
# Temperature 0.4 is lower than Phase 0's 0.7: tool-call adherence on a 7B degrades at higher
# temperature (see docs/05-phase1-build.md's tool-invocation-reliability gotcha).
_base_llm = router.chat_model(Sensitivity.SENSITIVE, temperature=0.4)

# Plain, tools-free local model for summarization, so the summarizer never emits a tool call.
_summarizer_llm = router.chat_model(Sensitivity.SENSITIVE, temperature=0.3)


def fact_extractor():
    """The structured local model the post-turn fact sweep runs on. It lives in the agent layer
    (which owns model selection via the router) and is injected into `app.memory.extract`, so
    memory stays a dependency-free leaf — SENSITIVE facts are always the local model (constitution)."""
    from app.memory.extract import ExtractedFacts

    return router.chat_model(Sensitivity.SENSITIVE, temperature=0).with_structured_output(
        ExtractedFacts, method="json_schema"
    )


def _build_messages(state: AgentState, *, messages=None) -> list:
    """Assemble the prompt: stable persona (+ profile card + rolling summary) first, history, then a
    trailing volatile time note.

    Message order is deliberate for latency: the *stable* persona (+ rolling summary, which changes
    rarely) leads, so Ollama's prefix KV-cache is reused across turns — a stable prompt re-evaluates
    ~10x faster than a changed one. The volatile current date/time therefore goes in a trailing
    message *after* the history, so it can't bust that cached prefix every minute. `messages` overrides
    the history (used by the repair node to re-answer without an offending message)."""
    core = SYSTEM_PROMPT + _profile_card()
    if state.get("summary"):
        core += (
            "\n\n---\nBackground from earlier in this conversation (context only — "
            f"answer the user's latest message, don't resume a finished topic):\n{state['summary']}"
        )
    now = datetime.now().astimezone()
    time_note = SystemMessage(f"(For reference, the current date/time is {now:%A, %Y-%m-%d %H:%M %Z}.)")
    history = state["messages"] if messages is None else messages
    return [SystemMessage(core), *history, time_note]


def _agent_node(state: AgentState) -> dict:
    """Single reasoning step: bind a small relevant tool subset and invoke the model."""
    messages = _build_messages(state)

    # Bind a small, relevant subset of tools for this turn — selected from the last few user
    # messages, not just the newest one. Selecting from the newest alone is what broke her
    # 2026-07-21 conversation: she asked to cancel a reminder, Mochi asked to confirm, she said
    # "yes" — and "yes" routes to nothing, so cancel_reminder wasn't bound and the model printed
    # the tool call as text instead of calling it, then claimed it had. Measured on her actual
    # phrasings: last-message-only bound the needed tool 1/5 on such follow-ups, recent-turns
    # 5/5, with no loss on single-turn prompts (6/6) and slightly FEWER tools bound on average.
    recent_human = [
        m.content for m in reversed(state["messages"])
        if isinstance(m, HumanMessage) and isinstance(m.content, str)
    ][:TOOL_SELECT_TURNS]
    subset = tool_select.select_tools("\n".join(reversed(recent_human)), ALL_TOOLS)
    llm = _base_llm.bind_tools(subset) if subset else _base_llm
    out = {"messages": [llm.invoke(messages)], "bound_tools": [t.name for t in subset]}
    # A fresh user turn resets the per-turn repair budget (the last message is her new HumanMessage;
    # on a post-tool re-invoke it's a ToolMessage, so the budget persists within the turn).
    if state["messages"] and isinstance(state["messages"][-1], HumanMessage):
        out["repair_count"] = 0
    return out


def _plain_reply(state: AgentState) -> str:
    """A tools-free answer to the user's actual question, used when a stripped reply would otherwise be
    empty (the model produced ONLY an unwanted tool call). Re-invokes without the offending last
    message and with no tools bound, so it can't recurse into another text tool-call."""
    try:
        history = state["messages"][:-1]  # drop the AIMessage that was just the tool-call
        result = _base_llm.invoke(_build_messages(state, messages=history))
        text = result.content if isinstance(result.content, str) else ""
        return toolcall_repair.strip_toolcall(text) or "Sorry, could you say that another way?"
    except Exception:
        log.exception("plain-reply fallback failed")
        return "Sorry, could you say that another way?"


def _repair_node(state: AgentState) -> dict:
    """Catch a tool call the model wrote as *text* instead of calling, so raw JSON never reaches her.

    - If it names a tool that WAS bound this turn (a mis-format), convert it to a real tool_call and
      let the ToolNode run it — turning the failure into the intended action. Side-effectful tools stay
      gated because they still hit the approval interrupt(). Capped at one execute-repair per turn.
    - If it names a tool that was NOT bound (topic bleed / hallucination — the selector judged it
      irrelevant to recent turns), strip the JSON and keep the prose. She gets a clean answer.
    Runs only when the last message is an AIMessage with no real tool_calls."""
    msgs = state["messages"]
    last = msgs[-1] if msgs else None
    if not isinstance(last, AIMessage) or getattr(last, "tool_calls", None):
        return {}
    content = last.content if isinstance(last.content, str) else ""
    hit = toolcall_repair.find_text_toolcall(content)
    if hit is None:
        return {}

    known = hit.name in _TOOL_NAMES
    bound = hit.name in (state.get("bound_tools") or [])
    count = state.get("repair_count", 0)
    cleaned = toolcall_repair.strip_toolcall(content)

    if known and bound and count < 1:
        log.info("repair: executing text tool-call for bound tool %r", hit.name)
        repaired = AIMessage(
            id=last.id,
            content=cleaned,
            tool_calls=[{"name": hit.name, "args": hit.args, "id": f"repair-{uuid4().hex[:8]}"}],
        )
        return {"messages": [repaired], "repair_count": count + 1}

    log.info("repair: stripping text tool-call for %r (known=%s bound=%s)", hit.name, known, bound)
    return {"messages": [AIMessage(id=last.id, content=cleaned or _plain_reply(state))]}


def _estimate_tokens(messages) -> int:
    # Rough chars/4 estimate, not a real tokenizer — good enough for a trim
    # trigger, swappable for something precise later without touching the
    # rest of this design.
    return sum(len(m.content or "") for m in messages) // 4


def _trim_boundary(messages, keep_recent: int) -> int:
    """Return the index to trim before: messages[:boundary] get summarized,
    messages[boundary:] are kept verbatim.

    A kept sequence must never START with a ToolMessage, or the next model call
    sends a tool response with no preceding AIMessage(tool_calls). Ollama
    tolerates this, but the OpenAI-compatible endpoints this project is designed
    to swap in do not. Advance the boundary forward past any leading
    ToolMessage(s) so an orphaned tool response is folded into the summary
    alongside its (already-trimmed) call. Pure function so it's unit-testable
    without invoking the model.
    """
    boundary = len(messages) - keep_recent
    while boundary < len(messages) and isinstance(messages[boundary], ToolMessage):
        boundary += 1
    return boundary


def _maybe_summarize_node(state: AgentState) -> dict:
    """Known, explicitly-flagged gap: this only compresses the conversation
    into `summary` — it does not decide anything here is durable enough to
    promote into the Fact table. If something important is said and never
    explicitly "remembered," repeated re-summarization can dilute it over a
    long conversation. That's a real limitation, not an oversight; the right
    fix is Phase 5's LangMem-based background consolidation, which can
    proactively extract durable facts. This design's state shape (summary as
    its own field, a decoupled node) is a clean seam for that upgrade rather
    than a rewrite.
    """
    messages = state["messages"]
    if len(messages) <= settings.working_buffer_keep_recent:
        return {}
    if _estimate_tokens(messages) < settings.working_buffer_max_tokens:
        return {}

    cut = _trim_boundary(messages, settings.working_buffer_keep_recent)
    if cut <= 0:
        return {}

    to_summarize = messages[:cut]
    prior_summary = state.get("summary", "")
    prompt = (
        "Summarize the conversation so far concisely, as context for future turns. Preserve durable "
        "facts and genuinely OPEN threads. Crucially, mark anything that was completed, answered, "
        "declined, or abandoned as resolved, so a finished topic does not look like pending work and "
        "keep-steering later replies. Lead with the current topic. If there's an existing summary, "
        "update it rather than starting over.\n\n"
        f"Existing summary:\n{prior_summary or '(none yet)'}\n\n"
        "Conversation to fold in:\n"
        + "\n".join(f"{m.type}: {m.content}" for m in to_summarize)
    )
    new_summary = _summarizer_llm.invoke([HumanMessage(prompt)]).content
    removals = [RemoveMessage(id=m.id) for m in to_summarize]
    return {"messages": removals, "summary": new_summary}


def build_agent():
    """Compile the graph with a persistent Postgres checkpointer.

    A long-lived autocommit connection is used (rather than the `from_conn_string`
    context manager) because this process stays up; the checkpointer must keep its
    connection open for the lifetime of the app.
    """
    init_db()  # idempotent: creates memory tables + indexes on first run

    conn = Connection.connect(
        settings.database_url,
        autocommit=True,
        prepare_threshold=0,
    )
    checkpointer = PostgresSaver(conn)
    checkpointer.setup()  # idempotent: creates checkpoint tables on first run

    graph = StateGraph(AgentState)
    graph.add_node("agent", _agent_node)
    graph.add_node("repair", _repair_node)
    graph.add_node("tools", ToolNode(ALL_TOOLS))
    graph.add_node("maybe_summarize", _maybe_summarize_node)
    graph.add_edge(START, "agent")
    # If the model emitted a real tool_call, run it. Otherwise pass through `repair`, which catches a
    # tool call written as TEXT (executes it if the tool was bound, strips it if not) so raw JSON never
    # reaches the user; repair then routes to tools (if it produced a call) or on to summarization.
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "repair"})
    graph.add_conditional_edges("repair", tools_condition, {"tools": "tools", END: "maybe_summarize"})
    graph.add_edge("tools", "agent")
    graph.add_edge("maybe_summarize", END)
    return graph.compile(checkpointer=checkpointer)
