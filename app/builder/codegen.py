"""Code generation for the builder — delegates to the hosted model (Groq gpt-oss-120b)
via the 4A router for quality. Build descriptions are non-personal, but they still pass
through the same scrub + audit as consult_expert (defense-in-depth + transparency).

Default output is ONE self-contained index.html using Tailwind (Play CDN) + Alpine.js (CDN): the
standard no-build modern stack — polished, interactive, works on a phone with zero build step, served
as static files. Single-file HTML is generated as **raw text** (not structured-output JSON): asking a
model to re-emit a multi-KB HTML file as an escaped JSON string truncates and fails
(`json_validate_failed`), which is exactly what broke the first `revise` attempt. `revise_project`
feeds the current code back so "make it better / use tailwind / fix it" iterates instead of rebuilding
blind. The multi-file framework path still uses structured output (it needs the file list).
"""

import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.agent import router, sanitize
from app.agent.router import Sensitivity
from app.memory.db import get_engine
from app.memory.models import HostedConsult

log = logging.getLogger(__name__)

# A single-file Tailwind+Alpine app is a few KB of HTML; give generation ample room so the file is
# never truncated mid-tag (truncation was what made the first revise fail).
_MAX_OUTPUT_TOKENS = 8000
# Cap how much existing code we send back on an edit — keeps a runaway file from blowing the prompt.
_MAX_CODE_CHARS = 24_000

_FENCE = re.compile(r"^\s*```[a-zA-Z]*\n|\n```\s*$")


class FileSpec(BaseModel):
    path: str = Field(description="Project-relative file path, e.g. 'index.html' or 'src/App.jsx'.")
    content: str = Field(description="The full text content of the file.")


class GeneratedProject(BaseModel):
    files: list[FileSpec] = Field(default_factory=list)
    framework: bool = Field(default=False, description="True if this is a Node/Vite project needing npm install.")
    install_cmd: str | None = Field(default=None, description="e.g. 'npm install' (framework only).")
    run_cmd: str | None = Field(default=None, description="e.g. 'npm run dev' (framework only).")


# The design brief. The hosted model is strong (120b); quality comes from ASKING for a real design +
# the modern no-build stack, which the old generic "clean, modern, responsive" prompt never did.
_BRIEF = (
    "Build ONE self-contained, production-quality index.html — no build step, no external files or "
    "assets beyond the two CDNs below.\n"
    "- Load Tailwind via the Play CDN: <script src=\"https://cdn.tailwindcss.com\"></script>\n"
    "- Load Alpine.js: <script defer src=\"https://unpkg.com/alpinejs@3/dist/cdn.min.js\"></script>\n"
    "- Mobile-first and fully responsive — it opens on a phone. Polished, not a wireframe.\n"
    "- A cohesive visual identity: a real palette, generous spacing, clear type hierarchy, rounded "
    "cards, subtle shadows/borders, and hover/focus states. Dark-mode friendly.\n"
    "- Semantic, accessible HTML (labels, alt text, visible focus rings, aria where needed).\n"
    "- Genuinely interactive with Alpine (x-data/x-on/x-show), persisting to localStorage where it "
    "helps. Real, specific content — never lorem ipsum or a single bare paragraph.\n"
    "- Icons via inline SVG or emoji (no external images)."
)
_RAW_ONLY = "\n\nOutput ONLY the complete raw HTML of index.html. No markdown code fences, no commentary."

_BUILD_SYSTEM = f"You are a senior front-end designer-engineer.\n\n{_BRIEF}{_RAW_ONLY}"
_REVISE_SYSTEM = (
    "You are a senior front-end designer-engineer improving an EXISTING single-file web app. You are "
    "given its current code and a change request. Return the COMPLETE updated index.html — a full "
    "rewrite, not a diff. Keep what already works, apply the requested change, and raise the overall "
    f"visual quality.\n\n{_BRIEF}{_RAW_ONLY}"
)
_FRAMEWORK_SYSTEM = SystemMessage(
    "You are an expert web developer. Output a minimal React + Vite project (package.json + index.html "
    "+ src/) as files with full contents; set install_cmd, run_cmd, and framework=true. Modern, "
    "responsive, Tailwind if easy. Output only the files."
)


def _raw_model():
    return router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.3).bind(max_tokens=_MAX_OUTPUT_TOKENS)


def _generate(system_text: str, user_text: str) -> str:
    """Generate raw HTML text from the hosted model, stripping any stray markdown fence."""
    msg = _raw_model().invoke([SystemMessage(system_text), HumanMessage(user_text)])
    text = msg.content if isinstance(msg.content, str) else ""
    return _FENCE.sub("", text.strip()).strip()


def _single_file(html: str) -> list[FileSpec]:
    return [FileSpec(path="index.html", content=html)] if html.strip() else []


def _audit(label: str, n_files: int, hits: int) -> None:
    if router.hosted_available():  # audit what actually left the machine
        with Session(get_engine()) as session:
            session.add(HostedConsult(sent_text=label, answer=f"{n_files} files generated", n_redactions=hits))
            session.commit()


def generate_project(description: str, *, framework: bool = False, generator=None) -> GeneratedProject:
    """Generate a project's files from a description. `generator` is injectable for tests
    (a fake taking (description, framework)); in production it routes to the hosted model."""
    if generator is not None:
        return generator(description, framework)

    clean, hits = sanitize.redact(description)
    if framework:  # rare multi-file path keeps structured output (it needs the file list)
        model = router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.3).with_structured_output(
            GeneratedProject, method="json_schema"
        )
        project = model.invoke([_FRAMEWORK_SYSTEM, HumanMessage(f"Description: {clean}")])
    else:
        project = GeneratedProject(files=_single_file(_generate(_BUILD_SYSTEM, f"Description: {clean}")))
    _audit(f"[build] {clean}", len(project.files), hits)
    return project


def revise_project(existing_files: list[FileSpec], change: str, *, generator=None) -> GeneratedProject:
    """Revise an existing project: feed its current code + the change request back to the model and
    get the full updated index.html. `generator` is injectable for tests (takes (existing_files, change))."""
    if generator is not None:
        return generator(existing_files, change)

    clean, hits = sanitize.redact(change)
    current = "\n\n".join(f"--- {f.path} ---\n{f.content[:_MAX_CODE_CHARS]}" for f in existing_files)
    html = _generate(_REVISE_SYSTEM, f"Change request: {clean}\n\nCurrent index.html:\n{current}")
    project = GeneratedProject(files=_single_file(html))
    _audit(f"[edit] {clean}", len(project.files), hits)
    return project
