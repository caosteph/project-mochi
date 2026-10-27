"""Refuse to let a secret into git history — rule 5, enforced in code instead of by memory.

WHY: on a public repo a committed bot token or API key is unrecoverable. Deleting it in a later
commit leaves it in history, so the only fix is rotating the key. `.gitignore` keeps `.env` out
by default, but nothing stopped a `git add -f`, a pasted key in a test, or a token JSON copied
out of `data/`. This scans **every added line in the commits being pushed** (not just the final
tree, since a secret added and then removed is still in history) for:

  - forbidden files: `.env`, anything under `data/` (OAuth tokens, profile), `*token*.json`,
    `client_secret*.json`, private keys (`*.pem`, `*.key`)
  - the credential formats this project actually holds: Telegram bot token, Groq / Tavily /
    OpenAI-style / OpenRouter keys, Google OAuth client secret + refresh token + API key, GitHub,
    AWS, Slack, PEM private keys

Stdlib only, so it runs in the pre-push hook (before anything leaves the machine) and in CI with
no install. CI also runs gitleaks as a broader backstop; this is the narrow, project-shaped check
that runs *first*. A deliberate fake in a fixture can carry `secret-scan: allow` on its line.

    python3 scripts/check_secrets.py --all          # every commit in history (CI)
    python3 scripts/check_secrets.py --range A..B   # one range
    <pre-push stdin> | python3 scripts/check_secrets.py --pre-push   # the commits being pushed
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch

ALLOW_MARK = "secret-scan: allow"
_ZERO = "0" * 40

PATTERNS: dict[str, re.Pattern] = {
    "Telegram bot token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b"),
    "Groq API key": re.compile(r"\bgsk_[A-Za-z0-9]{40,}"),
    "Tavily API key": re.compile(r"\btvly-[A-Za-z0-9-]{20,}"),
    "OpenAI/OpenRouter-style key": re.compile(r"\bsk-(?:or-v1-|proj-)?[A-Za-z0-9_-]{32,}"),
    "Google OAuth client secret": re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}"),
    "Google OAuth refresh token": re.compile(r"\b1//0[A-Za-z0-9_-]{30,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "Private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# Paths that must never be committed, whatever they contain. data/.gitkeep is the one exception.
FORBIDDEN_PATHS = (".env", "data/*", "*token*.json", "client_secret*.json", "*.pem", "*.key")
_ALLOWED_PATHS = ("data/.gitkeep",)


@dataclass(frozen=True)
class Finding:
    commit: str
    path: str
    line: int | None
    rule: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.commit[:8]}  {where}  — {self.rule}"


def forbidden_path(path: str) -> bool:
    if path in _ALLOWED_PATHS:
        return False
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch(path, pat) or fnmatch(name, pat) for pat in FORBIDDEN_PATHS)


def scan_line(text: str) -> list[str]:
    """Names of the rules this line trips (none if it carries the allow mark)."""
    if ALLOW_MARK in text:
        return []
    return [name for name, pat in PATTERNS.items() if pat.search(text)]


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def scan_log(patch: str) -> list[Finding]:
    """Parse `git log -p --format=commit:%H` output: flag forbidden paths and secret-shaped
    added lines. Pure (takes the text), so it's tested without a repo."""
    findings: list[Finding] = []
    commit = path = ""
    lineno = 0
    for raw in patch.splitlines():
        if raw.startswith("commit:"):
            commit = raw[len("commit:"):]
        elif raw.startswith("diff --git "):
            path = raw.split(" b/", 1)[-1] if " b/" in raw else ""
            lineno = 0
        elif raw.startswith(("new file mode", "+++ b/")):
            if forbidden_path(path) and not any(f.path == path and f.line is None for f in findings):
                findings.append(Finding(commit, path, None, "file must never be committed"))
        elif m := _HUNK.match(raw):
            lineno = int(m.group(1))
        elif raw.startswith("+") and not raw.startswith("+++"):
            findings.extend(Finding(commit, path, lineno, r) for r in scan_line(raw[1:]))
            lineno += 1
        elif not raw.startswith("-"):
            lineno += 1
    return findings


def _git_log(rev_args: list[str]) -> str:
    cmd = ["git", "log", "-p", "--no-color", "--no-ext-diff", "--format=commit:%H", "-U0", *rev_args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True, errors="replace").stdout


def pre_push_ranges(stdin_text: str) -> list[list[str]]:
    """git feeds pre-push `<local ref> <local sha> <remote ref> <remote sha>` per ref. Map each to
    the rev args selecting only the commits that would be new on the remote."""
    ranges = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == _ZERO:  # malformed, or a branch deletion
            continue
        local, remote = parts[1], parts[3]
        ranges.append([local, "--not", "--remotes"] if remote == _ZERO else [f"{remote}..{local}"])
    return ranges


def main() -> int:
    ap = argparse.ArgumentParser(description="Block secrets from entering git history.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="scan every commit in history")
    mode.add_argument("--range", help="scan a rev range, e.g. origin/main..HEAD")
    mode.add_argument("--pre-push", action="store_true", help="read git's pre-push stdin")
    args = ap.parse_args()

    if args.all:
        rev_sets = [["--all"]]
    elif args.range:
        rev_sets = [[args.range]]
    else:
        rev_sets = pre_push_ranges(sys.stdin.read())

    findings = [f for revs in rev_sets for f in scan_log(_git_log(revs))]
    if not findings:
        print("secret-scan OK — no secrets or secret files in the scanned commits.")
        return 0
    print("secret-scan FAILED — these would put a secret into git history:")
    for f in findings:
        print(f"  {f}")
    print(
        "\nRemove them from the commits (amend/rebase locally — nothing has left the machine yet),"
        f"\nand rotate any real key that was ever pushed. A deliberate fake can carry '{ALLOW_MARK}'."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
