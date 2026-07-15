"""Project directory management under `workspace/` (git-ignored). Every path is
validated to stay inside the workspace — agent-supplied names/paths can't escape it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT / "workspace"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "project"


def _safe_join(base: Path, rel: str) -> Path:
    """Resolve `base/rel` and refuse anything that escapes `base` (no `../`, no absolute)."""
    base = base.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"path escapes {base}: {rel!r}")
    return target


def create_project(name: str) -> Path:
    """Create a fresh project dir under workspace/, disambiguating name collisions."""
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    base = _slug(name)
    candidate, n = base, 2
    while (WORKSPACE_ROOT / candidate).exists():
        candidate, n = f"{base}-{n}", n + 1
    path = WORKSPACE_ROOT / candidate
    path.mkdir(parents=True)
    return path


def project_path(name: str) -> Path:
    """Resolve an existing project by name (validated under the workspace)."""
    path = _safe_join(WORKSPACE_ROOT, _slug(name))
    if not path.is_dir():
        raise FileNotFoundError(f"no such project: {name!r}")
    return path


def write_file(project_dir: Path, relpath: str, content: str) -> Path:
    """Write a file inside a project, creating parent dirs. Rejects path escapes."""
    target = _safe_join(project_dir, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def list_projects() -> list[str]:
    if not WORKSPACE_ROOT.is_dir():
        return []
    return sorted(p.name for p in WORKSPACE_ROOT.iterdir() if p.is_dir())
