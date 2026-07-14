from app.agent.tools.expert_tools import EXPERT_TOOLS
from app.agent.tools.google_tools import GOOGLE_TOOLS
from app.agent.tools.memory_tools import MEMORY_TOOLS
from app.agent.tools.reminder_tools import REMINDER_TOOLS

ALL_TOOLS = [*MEMORY_TOOLS, *GOOGLE_TOOLS, *REMINDER_TOOLS, *EXPERT_TOOLS]
