"""The iterative builder: edit_web_app + revise_project + the structural validator + latest_project.

The 2026-07-25 failure was that "make it richer / use tailwind / fix it" never rebuilt — there was no
edit path. These prove the edit path revises the current app IN PLACE with its existing code as context.
Hermetic: model + serving are faked, workspace is redirected to tmp_path.
"""

import os
import time

import pytest

from app.agent.tools import builder_tools
from app.builder import codegen, validate, workspace


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


def _proj(name, files):
    d = workspace.create_project(name)
    for rel, content in files.items():
        workspace.write_file(d, rel, content)
    return d


# --- structural validator ("verify / validate the code") -----------------------

_GOOD = (
    '<!doctype html><html><head>'
    '<script src="https://cdn.tailwindcss.com"></script>'
    '<script defer src="https://unpkg.com/alpinejs@3/dist/cdn.min.js"></script>'
    '</head><body class="p-6">' + "content " * 200 + "</body></html>"
)


def test_validator_flags_a_bare_page():
    ok, note = validate.validate_index_html("<h1>hi</h1>")
    assert not ok and "Tailwind" in note


def test_validator_passes_a_tailwind_alpine_page():
    ok, note = validate.validate_index_html(_GOOD)
    assert ok and note == ""


def test_validator_catches_an_unclosed_script():
    bad = _GOOD.replace("</body></html>", "") + '<script>var x = 1' + "z" * 50
    ok, note = validate.validate_index_html(bad)
    assert not ok and "script" in note.lower()


# --- latest_project (what "the web app" resolves to) ----------------------------

def test_latest_project_picks_most_recent(ws):
    workspace.create_project("alpha")
    beta = workspace.create_project("beta")
    os.utime(beta, (time.time() + 10, time.time() + 10))  # make beta newest
    assert workspace.latest_project() == "beta"


def test_latest_project_none_when_empty(ws):
    assert workspace.latest_project() is None


# --- revise_project feeds the existing code back to the model -------------------

def test_revise_project_prompt_includes_existing_code(monkeypatch):
    captured = {}

    def fake_generate(system_text, user_text, *, min_output=0):
        captured["user"] = user_text
        captured["min_output"] = min_output
        return "<html>new</html>"

    monkeypatch.setattr(codegen, "_generate", fake_generate)
    monkeypatch.setattr(codegen, "_audit", lambda *a, **k: None)
    existing = [codegen.FileSpec(path="index.html", content="OLD_MARKER_CONTENT")]
    out = codegen.revise_project(existing, "use tailwind")
    assert out.files[0].content == "<html>new</html>"
    assert "OLD_MARKER_CONTENT" in captured["user"] and "use tailwind" in captured["user"]
    assert captured["min_output"] > 0  # revise sizes min_output from the file it must reproduce


def test_revise_project_generator_seam():
    def fake(existing, change):
        return codegen.GeneratedProject(files=[codegen.FileSpec(path="index.html", content=f"{change}:{len(existing)}")])

    out = codegen.revise_project([codegen.FileSpec(path="index.html", content="x")], "richer", generator=fake)
    assert out.files[0].content == "richer:1"


# --- edit_web_app end to end (fake revise + fake serve) -------------------------

def _fake_revise(content):
    return lambda existing, change: codegen.GeneratedProject(
        files=[codegen.FileSpec(path="index.html", content=content)]
    )


def test_edit_web_app_revises_latest_in_place(ws, monkeypatch):
    _proj("hangover-guide", {"index.html": "BARE PLACEHOLDER"})
    monkeypatch.setattr(builder_tools.codegen, "revise_project", _fake_revise("RICH TAILWIND VERSION"))
    monkeypatch.setattr(builder_tools.serve, "running", dict)
    monkeypatch.setattr(builder_tools.serve, "serve_static", lambda d, **k: "http://192.168.1.5:8102")
    out = builder_tools.edit_web_app.invoke({"change": "make it richer, use tailwind"})
    assert "http://192.168.1.5:8102" in out
    assert (ws / "hangover-guide" / "index.html").read_text() == "RICH TAILWIND VERSION"


def test_edit_web_app_reuses_the_running_url(ws, monkeypatch):
    _proj("myapp", {"index.html": "old"})
    monkeypatch.setattr(builder_tools.codegen, "revise_project", _fake_revise("new"))
    monkeypatch.setattr(builder_tools.serve, "running", lambda: {"myapp": "http://live:9000"})

    def _no_serve(*a, **k):
        raise AssertionError("should reuse the running server, not re-serve")

    monkeypatch.setattr(builder_tools.serve, "serve_static", _no_serve)
    out = builder_tools.edit_web_app.invoke({"change": "tweak the header"})
    assert "http://live:9000" in out


def test_edit_web_app_feeds_the_existing_code(ws, monkeypatch):
    _proj("app1", {"index.html": "EXISTING_CODE_XYZ"})
    seen = {}

    def fake_revise(existing, change):
        seen["existing"], seen["change"] = existing, change
        return codegen.GeneratedProject(files=[codegen.FileSpec(path="index.html", content="updated")])

    monkeypatch.setattr(builder_tools.codegen, "revise_project", fake_revise)
    monkeypatch.setattr(builder_tools.serve, "running", dict)
    monkeypatch.setattr(builder_tools.serve, "serve_static", lambda d, **k: "http://x:1")
    builder_tools.edit_web_app.invoke({"change": "use tailwind"})
    assert seen["change"] == "use tailwind"
    assert any("EXISTING_CODE_XYZ" in f.content for f in seen["existing"])


def test_edit_web_app_when_nothing_built_yet(ws):
    out = builder_tools.edit_web_app.invoke({"change": "make it nicer"})
    assert "haven't built" in out.lower()
