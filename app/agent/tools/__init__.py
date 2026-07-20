from app.agent.tools.builder_tools import BUILDER_TOOLS
from app.agent.tools.expert_tools import EXPERT_TOOLS
from app.agent.tools.google_tools import GOOGLE_TOOLS
from app.agent.tools.memory_tools import MEMORY_TOOLS
from app.agent.tools.reminder_tools import REMINDER_TOOLS
from app.agent.tools.web_tools import WEB_TOOLS

# ALL_TOOLS is the full pool (ToolNode can execute any of them). The 7B can't be *bound* with
# all ~15 at once (measured: it collapses past ~11), so the graph binds only a small relevant
# subset per turn — see app/agent/tool_select.py. That keeps the builder conversational without
# breaking the core tools. /build and /doc remain as explicit shortcuts.
ALL_TOOLS = [*MEMORY_TOOLS, *GOOGLE_TOOLS, *REMINDER_TOOLS, *EXPERT_TOOLS, *BUILDER_TOOLS, *WEB_TOOLS]
