"""The LangGraph agent. Phase 1 adds a tool-calling loop — the model can now
call the four memory tools, and the graph routes through a ToolNode and back
to the agent until the model stops requesting tools — and basic context-window
management: once the working message buffer grows past a token budget, the
oldest messages are folded into a rolling summary and removed from state, so
long conversations don't silently blow past the local model's context window.
The sensitivity router and human-in-the-loop confirmation gate still arrive in
later phases.
"""

from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from psycopg import Connection

from app.agent.persona import build_system_prompt
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


_llm = ChatOpenAI(
    base_url=settings.ollama_base_url,
    api_key="ollama",  # Ollama ignores the key, but the OpenAI client requires one
    model=settings.local_model,
    # Lower than Phase 0's 0.7: tool-call adherence on a 7B local model degrades
    # at higher temperature, and a broken "I'll remember that" promise is worse
    # than slightly less playful phrasing. Verified empirically, not assumed —
    # see docs/05-phase1-build.md's tool-invocation-reliability gotcha.
    temperature=0.4,
).bind_tools(ALL_TOOLS)

# Plain, tools-free model for summarization calls, so the summarizer itself
# never tries to emit a tool call.
_summarizer_llm = ChatOpenAI(
    base_url=settings.ollama_base_url,
    api_key="ollama",
    model=settings.local_model,
    temperature=0.3,
)


def _agent_node(state: AgentState) -> dict:
    """Single reasoning step: prepend the system prompt (+ rolling summary,
    if any), call the local model."""
    core = SYSTEM_PROMPT
    if state.get("summary"):
        core += f"\n\n---\nSummary of earlier conversation:\n{state['summary']}"
    messages = [SystemMessage(core), *state["messages"]]
    return {"messages": [_llm.invoke(messages)]}


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
