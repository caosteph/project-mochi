"""The LangGraph agent.

Phase 0 is intentionally a single reasoning node backed by a local model, with a
durable Postgres checkpointer so conversation state survives restarts. Memory
tools, the Google/MCP tools, the sensitivity router, and the human-in-the-loop
confirmation gate all arrive in later phases (see docs/00-plan.md).
"""

from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from psycopg import Connection

from app.agent.persona import build_system_prompt
from app.config import settings

# Mochi's voice + soft operating principles, sourced from the versioned persona.md.
# Assembled once at import; the hard safety rules live in code, not this string.
SYSTEM_PROMPT = build_system_prompt()

_llm = ChatOpenAI(
    base_url=settings.ollama_base_url,
    api_key="ollama",  # Ollama ignores the key, but the OpenAI client requires one
    model=settings.local_model,
    temperature=0.7,
)


def _agent_node(state: MessagesState) -> dict:
    """Single reasoning step: prepend the system prompt, call the local model."""
    messages = [SystemMessage(SYSTEM_PROMPT), *state["messages"]]
    return {"messages": [_llm.invoke(messages)]}


def build_agent():
    """Compile the graph with a persistent Postgres checkpointer.

    A long-lived autocommit connection is used (rather than the `from_conn_string`
    context manager) because this process stays up; the checkpointer must keep its
    connection open for the lifetime of the app.
    """
    conn = Connection.connect(
        settings.database_url,
        autocommit=True,
        prepare_threshold=0,
    )
    checkpointer = PostgresSaver(conn)
    checkpointer.setup()  # idempotent: creates checkpoint tables on first run

    graph = StateGraph(MessagesState)
    graph.add_node("agent", _agent_node)
    graph.add_edge(START, "agent")
    graph.add_edge("agent", END)
    return graph.compile(checkpointer=checkpointer)
