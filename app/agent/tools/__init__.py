from app.agent.tools.builder_tools import BUILDER_TOOLS
from app.agent.tools.expert_tools import EXPERT_TOOLS
from app.agent.tools.google_tools import GOOGLE_TOOLS
from app.agent.tools.memory_tools import MEMORY_TOOLS
from app.agent.tools.reminder_tools import REMINDER_TOOLS
from app.agent.tools.web_tools import WEB_TOOLS

# ALL_TOOLS is the full pool (ToolNode can execute any of them). The graph binds only a small
# relevant subset per turn — see app/agent/tool_select.py.
# NOTE (2026-07-20): the old "the 7B collapses past ~11 bound tools" claim was a MISDIAGNOSIS —
# it was context exhaustion at Ollama's default num_ctx 4096 (~95 prompt tokens per bound tool).
# On the 8k-context model all 17 bind and fire 3/3. Per-turn selection is kept because it's
# cheaper (~665 fewer prompt tokens/turn), not because a wall forces it.
ALL_TOOLS = [*MEMORY_TOOLS, *GOOGLE_TOOLS, *REMINDER_TOOLS, *EXPERT_TOOLS, *BUILDER_TOOLS, *WEB_TOOLS]
