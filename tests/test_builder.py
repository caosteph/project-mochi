"""Phase 4B (step 1) — the builder. Sandbox runs real local subprocesses (no network);
model + serving are faked. Verifies the safety-critical bits: env scrub, path-safety,
timeout, and the build/doc flows.
"""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.tools import builder_tools
from app.builder import codegen, docs, sandbox, workspace
from app.channels import telegram
from app.config import settings


@pytest.fixture
def project():
    d = workspace.create_project("unit-test-proj")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# --- workspace path-safety -----------------------------------------------------

def test_path_escape_is_blocked():
    with pytest.raises(ValueError):
        workspace._safe_join(workspace.WORKSPACE_ROOT, "../../etc/passwd")
    with pytest.raises(ValueError):
        workspace._safe_join(workspace.WORKSPACE_ROOT, "/etc/passwd")


def test_write_file_stays_in_project(project):
    p = workspace.write_file(project, "sub/dir/index.html", "<h1>hi</h1>")
    assert p.read_text() == "<h1>hi</h1>" and p.is_relative_to(project)
    with pytest.raises(ValueError):
        workspace.write_file(project, "../escape.txt", "nope")


# --- sandbox -------------------------------------------------------------------

def test_sandbox_scrubs_secrets_from_env(project, monkeypatch):
    monkeypatch.setenv("SECRET_CANARY", "leakme")
    r = sandbox.SubprocessSandbox().run(
        ["python3", "-c", "import os;print('LEAK' if 'SECRET_CANARY' in os.environ else 'clean')"],
        cwd=project,
    )
    assert r.ok and r.stdout.strip() == "clean"  # the child never saw the secret


def test_sandbox_times_out():
    d = workspace.create_project("sb-timeout")
    try:
        r = sandbox.SubprocessSandbox().run(["python3", "-c", "import time;time.sleep(5)"], cwd=d, timeout=1)
        assert r.timed_out and not r.ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sandbox_rejects_cwd_outside_workspace():
    with pytest.raises(ValueError):
        sandbox.SubprocessSandbox().run(["echo", "hi"], cwd=workspace.REPO_ROOT)


# --- docs ----------------------------------------------------------------------

def test_generate_pdf_and_docx(tmp_path):
    pdf = docs.generate_pdf("Plan", "# Goal\n\nDo it.\n\n- a\n- b", tmp_path / "p.pdf")
    assert pdf.read_bytes()[:5] == b"%PDF-" and pdf.stat().st_size > 400
    dx = docs.generate_docx("Plan", "## S\n\n- a\n- b\n\ntext", tmp_path / "p.docx")
    assert dx.exists() and dx.stat().st_size > 0


# --- codegen (injected generator, no model) ------------------------------------

def test_generate_project_uses_injected_generator():
    def fake(desc, fw):
        return codegen.GeneratedProject(files=[codegen.FileSpec(path="index.html", content="<h1>hi</h1>")])

    proj = codegen.generate_project("a page", generator=fake)
    assert len(proj.files) == 1 and proj.files[0].path == "index.html"


# --- build_web_app flow (fake codegen + fake serve) ----------------------------

def test_build_web_app_writes_files_and_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(builder_tools.workspace, "create_project", lambda name: tmp_path)
    monkeypatch.setattr(
        builder_tools.codegen, "generate_project",
        lambda description, framework=False: codegen.GeneratedProject(
            files=[codegen.FileSpec(path="index.html", content="<h1>Bakery</h1>")]
        ),
    )
    served = {}
    monkeypatch.setattr(builder_tools.serve, "serve_static", lambda d, **k: served.setdefault("url", "http://192.168.1.5:8100"))
    out = builder_tools.build_web_app.invoke({"description": "a bakery landing page"})
    assert "http://192.168.1.5:8100" in out
    assert (tmp_path / "index.html").read_text() == "<h1>Bakery</h1>"


def test_build_web_app_framework_scaffolds_without_serving(tmp_path, monkeypatch):
    monkeypatch.setattr(builder_tools.workspace, "create_project", lambda name: tmp_path)
    monkeypatch.setattr(
        builder_tools.codegen, "generate_project",
        lambda description, framework=False: codegen.GeneratedProject(
            files=[codegen.FileSpec(path="package.json", content="{}")], framework=True
        ),
    )

    def _no_serve(*a, **k):
        raise AssertionError("framework apps must not static-serve in step 1")

    monkeypatch.setattr(builder_tools.serve, "serve_static", _no_serve)
    out = builder_tools.build_web_app.invoke({"description": "a react app", "framework": True})
    assert "scaffolded" in out.lower()


# --- make_document queues a file for delivery ----------------------------------

def test_make_document_creates_and_queues_file(monkeypatch):
    monkeypatch.setattr(builder_tools, "_write_document", lambda desc: "# Hi\n\n- x")  # no model call
    builder_tools.drain_artifacts()  # clear
    out = builder_tools.make_document.invoke({"description": "My Plan", "format": "pdf"})
    assert "PDF" in out
    queued = builder_tools.drain_artifacts()
    assert len(queued) == 1 and queued[0].endswith(".pdf")

    p = Path(queued[0])
    assert p.read_bytes()[:5] == b"%PDF-"
    p.unlink(missing_ok=True)
    assert builder_tools.drain_artifacts() == []  # drained


# --- /build and /doc command handlers ------------------------------------------

class _FakeTool:
    def __init__(self, ret):
        self.ret = ret

    def invoke(self, args):
        return self.ret


class _Bot:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, chat_id, text, **k):
        self.messages.append(text)

    async def send_document(self, chat_id, document, filename=None, **k):
        self.documents.append(filename)


def _chan():
    return telegram.TelegramChannel.__new__(telegram.TelegramChannel)


def _update(text):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=settings.telegram_chat_id),
        message=SimpleNamespace(text=text, reply_text=None),
    )


def test_on_build_command_replies_with_link(monkeypatch):
    monkeypatch.setattr(builder_tools, "build_web_app", _FakeTool("Done! live at http://192.168.1.9:8100"))
    chan = _chan()
    monkeypatch.setattr(chan, "_log_one", lambda *a: None)
    bot = _Bot()
    asyncio.run(chan._on_build(_update("/build a bakery landing page"), SimpleNamespace(bot=bot)))
    assert any("http://192.168.1.9:8100" in m for m in bot.messages)


def test_on_doc_command_generates_locally_and_sends_file(monkeypatch):
    # /doc generates content (mocked here) and sends a file.
    monkeypatch.setattr(builder_tools, "_write_document", lambda desc: "# Plan\n\n- do x\n- do y")
    chan = _chan()
    monkeypatch.setattr(chan, "_log_one", lambda *a: None)
    bot = _Bot()
    asyncio.run(chan._on_doc(_update("/doc a one-page plan for my week"), SimpleNamespace(bot=bot)))
    assert len(bot.documents) == 1 and bot.documents[0].endswith(".pdf")
    # clean up the produced file
    for f in workspace.WORKSPACE_ROOT.glob("*.pdf"):
        f.unlink(missing_ok=True)
