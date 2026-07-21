"""The LangGraph agent. Phase 1 adds a tool-calling loop — the model can now
call the four memory tools, and the graph routes through a ToolNode and back
to the agent until the model stops requesting tools — and basic context-window
management: once the working message buffer grows past a token budget, the
oldest messages are folded into a rolling summary and removed from state, so
long conversations don't silently blow past the local model's context window.
The sensitivity router and human-in-the-loop confirmation gate still arrive in
later phases.
"""

from datetime import datetime

from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from psycopg import Connection

from app.agent import router, tool_select
from app.agent.persona import build_system_prompt
from app.agent.router import Sensitivity
from app.agent.tools import ALL_TOOLS
from app.config import settings
from app.memory.db import init_db

# Mochi's voice + soft operating principles, sourced from the versioned persona.md.
# Assembled once at import; the hard safety rules live in code, not this string.
SYSTEM_PROMPT = build_system_prompt()


class AgentState(MessagesState):
    """Adds a rolling conversation summary to the base message-list state.
    This is the "core block" beyond persona: no separate current-task tracker
    is introduced in Phase 1 — there's no natural object yet to represent
    conversational focus (the Task table is to-dos, not conversational
    state), so the summary itself stands in for that role.
    """

    summary: str


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


def _agent_node(state: AgentState) -> dict:
    """Single reasoning step.

    Message order is deliberate for latency: the *stable* persona (+ rolling
    summary, which changes rarely) leads, so Ollama's prefix KV-cache is reused
    across turns — a stable prompt re-evaluates ~10x faster than a changed one.
    The volatile current date/time therefore goes in a trailing message *after*
    the history, so it can't bust that cached prefix every minute (which it did
    when it lived in the leading system prompt).
    """
    core = SYSTEM_PROMPT
    if state.get("summary"):
        core += f"\n\n---\nSummary of earlier conversation:\n{state['summary']}"
    now = datetime.now().astimezone()
    time_note = SystemMessage(f"(For reference, the current date/time is {now:%A, %Y-%m-%d %H:%M %Z}.)")
    messages = [SystemMessage(core), *state["messages"], time_note]

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
    return {"messages": [llm.invoke(messages)]}


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
        "Summarize the following conversation concisely, preserving important facts, "
        "ongoing tasks, and open questions. If there's an existing summary, extend it "
        "rather than starting over.\n\n"
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
    graph.add_node("tools", ToolNode(ALL_TOOLS))
    graph.add_node("maybe_summarize", _maybe_summarize_node)
    graph.add_edge(START, "agent")
    # Override tools_condition's default END branch to route through
    # maybe_summarize first, instead of ending immediately.
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "maybe_summarize"})
    graph.add_edge("tools", "agent")
    graph.add_edge("maybe_summarize", END)
    return graph.compile(checkpointer=checkpointer)
