"""Interaction tools: how the agent asks Stephanie something instead of guessing or nagging.

`ask_user` is the general "put a decision to her as buttons" primitive. Before it existed the
model could only ask a yes/no or pick-one question in *prose*, which she then had to answer by
typing "yes" — and that typed reply carries no routing signal, so it routinely went nowhere. She
asked for real buttons repeatedly ("please make it yes or no buttons that i can click"). This is
that.

It rides the same interrupt/resume spine as the approval gate (app/agent/confirm.py): calling it
pauses the graph, the channel renders one inline button per option, her tap resumes with the
chosen index, and the option string is returned here for the model to act on.
"""

from langchain_core.tools import tool

from app.agent.confirm import ask_choice


@tool
def ask_user(question: str, options: list[str]) -> str:
    """Ask Stephanie a question that has specific, concrete answers, and get her tapped choice.

    Use this INSTEAD of writing a yes/no or pick-one question in plain text — she gets buttons
    she can tap rather than having to type a reply. Good for: confirming a destructive action
    ("Cancel this reminder?" → Yes/No), disambiguating ("Which dentist reminder?"), or offering a
    short menu. Do NOT use it for open-ended questions (where she'd type a free answer), and do
    NOT ask permission for safe reads (calendar/inbox/memory) — just do those.

    `options` should be 2–6 short button labels. Returns the exact option she chose.
    """
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if len(opts) < 2:
        # Nothing to choose between — don't render a one-button dead-end.
        raise ValueError("ask_user needs at least two concrete options")
    idx = ask_choice(question, opts)
    if idx < 0:
        return "(no choice was made)"
    return opts[idx]


INTERACTION_TOOLS = [ask_user]
