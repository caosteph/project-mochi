"""Agent-callable builder tools (Phase 4B). High-level, single-call tools — the local
7B chains tool calls poorly, so each of these does a whole flow (generate → write →
serve, or render → deliver) in one call rather than making the model orchestrate.
"""

import logging
import threading
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from app.agent import rate_limit, router
from app.agent.router import Sensitivity
from app.builder import codegen, docs, serve, workspace

log = logging.getLogger(__name__)

# Documents produced this turn, drained + sent by the channel after the reply.
_lock = threading.Lock()
_pending_docs: list[str] = []


def drain_artifacts() -> list[str]:
    with _lock:
        out = list(_pending_docs)
        _pending_docs.clear()
        return out


@tool
def build_web_app(description: str, framework: bool = False) -> str:
    """Build a web app from a description and serve it so Stephanie can open it on her
    phone. `description` is what to build (e.g. "a landing page for my bakery"). Leave
    `framework` false for a fast single-page static site; set it true only for an
    interactive app that genuinely needs React. Returns the link to open."""
    if not rate_limit.allow("build_web_app"):
        return "I've hit my hourly build limit — try again in a bit."
    try:
        project = codegen.generate_project(description, framework=framework)
    except Exception as exc:
        log.exception("codegen failed")
        return f"I couldn't generate that app — {exc}."
    if not project.files:
        return "The generator came back empty — mind rephrasing what you'd like?"

    proj_dir = workspace.create_project(description[:40])
    for f in project.files:
        try:
            workspace.write_file(proj_dir, f.path, f.content)
        except ValueError:
            log.warning("skipped unsafe file path from codegen: %s", f.path)

    if project.framework or framework:
        return (
            f"I scaffolded the React project in workspace/{proj_dir.name}. Live serving for "
            "framework apps is coming in the next step — for now I can build and serve static sites."
        )
    url = serve.serve_static(proj_dir)
    return f"Done! Your site is live at {url} — open it on your phone (same wifi). Ask me to make it public to view anywhere."


_DOC_SYSTEM = SystemMessage(
    "Write a clear, well-structured document for the request. Use Markdown: a '# Title' line, "
    "'## Section' headings, and '- ' bullets. Output ONLY the document."
)


def _write_document(description: str) -> str:
    """Generate document content on the LOCAL model (personal content stays local)."""
    return router.chat_model(Sensitivity.SENSITIVE, temperature=0.4).invoke(
        [_DOC_SYSTEM, HumanMessage(description)]
    ).content


@tool
def make_document(description: str, format: str = "pdf") -> str:
    """Create a document (PDF, or Word .docx if she asks) about a topic and send it to
    Stephanie. `description` is what to write — e.g. "a one-page plan for my week", "a summary
    of the French Revolution". `format` is "pdf" or "docx". Just call this with the topic; you
    do NOT write the content yourself — it's generated and sent to her as a file."""
    if not rate_limit.allow("make_document"):
        return "I've hit my hourly document limit — try again in a bit."
    fmt = "docx" if str(format).lower().startswith("doc") else "pdf"
    try:
        content = _write_document(description)
    except Exception as exc:
        log.exception("doc content generation failed")
        return f"I couldn't write that document — {exc}."
    title = description[:60]
    workspace.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    fname = f"{workspace._slug(title)}-{datetime.now():%Y%m%d-%H%M%S}.{fmt}"
    path = workspace.WORKSPACE_ROOT / fname
    try:
        (docs.generate_docx if fmt == "docx" else docs.generate_pdf)(title, content, path)
    except Exception as exc:
        log.exception("doc rendering failed")
        return f"I couldn't build that document — {exc}."
    with _lock:
        _pending_docs.append(str(path))
    return f"Made your {fmt.upper()}: “{title}” — sending it over now."


@tool
def serve_project(name: str) -> str:
    """Serve an existing built project again and return its link."""
    try:
        proj_dir = workspace.project_path(name)
    except FileNotFoundError:
        return f"I don't have a project named {name!r}."
    if not (proj_dir / "index.html").exists():
        return f"{name} isn't a static site I can serve directly yet."
    return f"Live again at {serve.serve_static(proj_dir)}."


@tool
def list_projects() -> str:
    """List the projects Mochi has built, noting any that are currently live."""
    projects = workspace.list_projects()
    if not projects:
        return "You haven't had me build anything yet."
    live = serve.running()
    return "\n".join(f"- {p}" + (f" — live at {live[p]}" if p in live else "") for p in projects)


BUILDER_TOOLS = [build_web_app, make_document, serve_project, list_projects]
