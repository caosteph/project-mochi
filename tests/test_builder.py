"""Phase 4B (step 1) — the builder. Sandbox runs real local subprocesses (no network);
model + serving are faked. Verifies the safety-critical bits: env scrub, path-safety,
timeout, and the build/doc flows.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

from app.agent.tools import builder_tools
from app.builder import codegen, docs, sandbox, workspace
from tests.support import FakeMessage, make_update


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


# --- TPM-budget sizing (the Groq free-tier 413 fix) ----------------------------

def test_effective_max_tokens_fits_a_normal_build(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "builder_tpm_budget", 8000)
    monkeypatch.setattr(settings, "builder_max_output_tokens", 6000)
    # A small build prompt leaves room for the full cap; crucially prompt + cap stays under budget
    # (the old flat 8000 cap made every request 413 because 8000 + prompt > 8000 TPM).
    cap = codegen._effective_max_tokens(prompt_chars=1500)
    assert cap == 6000
    assert 1500 // 3 + cap < 8000


def test_effective_max_tokens_bails_when_prompt_too_big(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "builder_tpm_budget", 8000)
    # A huge existing file (revise path) can't leave room for a usable completion → clear error,
    # not a provider 413.
    with pytest.raises(codegen.BuilderBudgetError):
        codegen._effective_max_tokens(prompt_chars=24_000)


def test_effective_max_tokens_bails_rather_than_truncate_a_revise(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "builder_tpm_budget", 8000)
    monkeypatch.setattr(settings, "builder_max_output_tokens", 6000)
    # Revise of a ~16KB page (like her coffee-shop build): the prompt alone fits, but there's no room
    # left to REPRODUCE the whole file, so it must bail — not return a small cap that truncates the page.
    with pytest.raises(codegen.BuilderBudgetError):
        codegen._effective_max_tokens(prompt_chars=17_000, min_output=4_000)
    # A small app leaves room for a full rewrite → returns a cap big enough to reproduce it.
    assert codegen._effective_max_tokens(prompt_chars=7_000, min_output=1_500) >= 1_500


def test_effective_max_tokens_no_clamp_when_budget_disabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "builder_tpm_budget", 0)  # paid tier / no per-minute limit
    monkeypatch.setattr(settings, "builder_max_output_tokens", 6000)
    assert codegen._effective_max_tokens(prompt_chars=24_000) == 6000


def test_build_web_app_reports_budget_error_clearly(tmp_path, monkeypatch):
    # When codegen can't fit the tier, build_web_app returns a readable message, never a stack trace.
    def boom(description, framework=False):
        raise codegen.BuilderBudgetError("it's too large for the current model's free-tier rate limit")
    monkeypatch.setattr(builder_tools.codegen, "generate_project", boom)
    out = builder_tools.build_web_app.invoke({"description": "an enormous app"})
    assert "free-tier rate limit" in out and "couldn't generate" in out


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


def _update(text):
    return make_update(message=FakeMessage(text))


def test_on_build_command_replies_with_link(channel, ctx, fake_bot, monkeypatch):
    monkeypatch.setattr(builder_tools, "build_web_app", _FakeTool("Done! live at http://192.168.1.9:8100"))
    asyncio.run(channel._on_build(_update("/build a bakery landing page"), ctx))
    assert any("http://192.168.1.9:8100" in m for m in fake_bot.texts)


def test_on_doc_command_generates_locally_and_sends_file(channel, ctx, fake_bot, monkeypatch):
    # /doc generates content (mocked here) and sends a file.
    monkeypatch.setattr(builder_tools, "_write_document", lambda desc: "# Plan\n\n- do x\n- do y")
    asyncio.run(channel._on_doc(_update("/doc a one-page plan for my week"), ctx))
    assert len(fake_bot.documents) == 1 and fake_bot.documents[0][1].endswith(".pdf")
    # clean up the produced file
    for f in workspace.WORKSPACE_ROOT.glob("*.pdf"):
        f.unlink(missing_ok=True)
