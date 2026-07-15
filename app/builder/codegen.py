"""Code generation for the builder — delegates to the hosted model (Groq gpt-oss-120b)
via the 4A router for quality. Build descriptions are non-personal, but they still pass
through the same scrub + audit as consult_expert (defense-in-depth + transparency).
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.agent import router, sanitize
from app.agent.router import Sensitivity
from app.memory.db import get_engine
from app.memory.models import HostedConsult

log = logging.getLogger(__name__)


class FileSpec(BaseModel):
    path: str = Field(description="Project-relative file path, e.g. 'index.html' or 'src/App.jsx'.")
    content: str = Field(description="The full text content of the file.")


class GeneratedProject(BaseModel):
    files: list[FileSpec] = Field(default_factory=list)
    framework: bool = Field(default=False, description="True if this is a Node/Vite project needing npm install.")
    install_cmd: str | None = Field(default=None, description="e.g. 'npm install' (framework only).")
    run_cmd: str | None = Field(default=None, description="e.g. 'npm run dev' (framework only).")


_SYSTEM = SystemMessage(
    "You are an expert web developer. Given a short description, output a COMPLETE, small, "
    "self-contained web project as a list of files with their full contents. Write clean, modern, "
    "responsive code. Output only the files (no explanations)."
)


def generate_project(description: str, *, framework: bool = False, generator=None) -> GeneratedProject:
    """Generate a project's files from a description. `generator` is injectable for tests
    (a fake taking (description, framework)); in production it routes to the hosted model."""
    if generator is not None:
        return generator(description, framework)

    clean, hits = sanitize.redact(description)
    kind = (
        "Build a minimal React + Vite project (package.json + index.html + src/). Set install_cmd "
        "and run_cmd, and framework=true."
        if framework
        else "Build a SINGLE self-contained static site as index.html with inline CSS and JS — no build "
        "step. framework=false."
    )
    model = router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.3).with_structured_output(
        GeneratedProject, method="json_schema"
    )
    project = model.invoke([_SYSTEM, HumanMessage(f"{kind}\n\nDescription: {clean}")])

    if router.hosted_available():  # audit what actually left the machine
        with Session(get_engine()) as session:
            session.add(HostedConsult(sent_text=f"[build] {clean}", answer=f"{len(project.files)} files generated", n_redactions=hits))
            session.commit()
    return project
