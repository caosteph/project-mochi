from app.agent.tools.expert_tools import EXPERT_TOOLS
from app.agent.tools.google_tools import GOOGLE_TOOLS
from app.agent.tools.memory_tools import MEMORY_TOOLS
from app.agent.tools.reminder_tools import REMINDER_TOOLS

# NOTE: builder tools (app/agent/tools/builder_tools.py) are deliberately NOT bound into
# the agent's tool set. Measured on the local 7B: 11 tools fire reliably, but 13–15 collapse
# tool-calling entirely (add_reminder AND build_web_app dropped to 0). Until dynamic per-turn
# tool binding exists, the builder is exposed via explicit /build and /doc commands instead
# (like /ask), which need no tool-selection. See docs/10-phase4b-build.md.
ALL_TOOLS = [*MEMORY_TOOLS, *GOOGLE_TOOLS, *REMINDER_TOOLS, *EXPERT_TOOLS]
