"""Doc-lint: catch the mechanically-checkable ways docs go stale, so they can't drift silently.

Two invariants:

1. **Path existence (current-state docs only: README.md, CLAUDE.md).** Every repo path they name in
   backticks (e.g. `app/agent/profile.py`, `scripts/install_hooks.sh`) must exist. This is what rots:
   a file is renamed or moved and the doc keeps advertising the old location. Only the two docs that
   describe *what is true now* are held to this; the docs/ build guides and roadmap legitimately name
   proposed (`app/agent/safety/`) or historical paths, so they're exempt. Git-ignored dirs (data/,
   workspace/, backups/, .venv/) are skipped since a fresh checkout won't have them.
2. **Link resolution (all docs).** Every relative Markdown link `[text](path)` resolves to a real file.

Deterministic and offline, so it runs in the pre-push hook and in CI. It does NOT touch the agentic
memory files (those live outside the repo). Exit 0 if clean, 1 with a report otherwise.

    uv run python scripts/check_docs.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Docs that describe what is true NOW — held to the path-existence bar.
CURRENT_DOCS = [ROOT / "README.md", ROOT / "CLAUDE.md"]
# All human docs — held to the (weaker) link-resolution bar.
ALL_DOCS = [*CURRENT_DOCS, *sorted((ROOT / "docs").glob("*.md"))]

# Backtick paths under these prefixes are version-controlled, so a named path must exist.
CHECKED_PREFIXES = ("app/", "scripts/", "docs/", "tests/", "launchd/", "ollama/", ".github/", ".githooks/")

# A backtick token that looks like a repo path: a checked prefix followed by path chars.
_PATH_TOKEN = re.compile(r"`(" + "|".join(re.escape(p) for p in CHECKED_PREFIXES) + r")[\w./-]+`")
# Markdown links: [text](target). We only resolve relative targets (skip http(s)/mailto/anchors).
_MD_LINK = re.compile(r"\]\(([^)]+)\)")
# Trailing punctuation that a sentence can leave glued to a path inside backticks.
_TRAILING = ".,:;"


def _referenced_paths(text: str) -> set[str]:
    out = set()
    for m in _PATH_TOKEN.finditer(text):
        token = m.group(0).strip("`").rstrip(_TRAILING)
        out.add(token)
    return out


def _markdown_links(text: str) -> set[str]:
    out = set()
    for m in _MD_LINK.finditer(text):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        out.add(target.split("#", 1)[0])  # drop any in-page anchor
    return out


def main() -> int:
    problems: list[str] = []

    # 1. Path existence — current-state docs only.
    for doc in CURRENT_DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        rel = doc.relative_to(ROOT)
        problems.extend(
            f"{rel}: names `{path}` but it does not exist"
            for path in sorted(_referenced_paths(text))
            if not (ROOT / path).exists()
        )

    # 2. Link resolution — every human doc.
    for doc in ALL_DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        rel = doc.relative_to(ROOT)
        problems.extend(
            f"{rel}: link ({target}) does not resolve"
            for target in sorted(_markdown_links(text))
            if target and not (doc.parent / target).resolve().exists()
        )

    if problems:
        print("Doc-lint found stale references:")
        for p in problems:
            print(f"  - {p}")
        print(f"\n{len(problems)} problem(s). Fix the doc (or the path) so they match.")
        return 1
    print(f"Doc-lint OK — checked {len(ALL_DOCS)} docs; all referenced paths and links resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
