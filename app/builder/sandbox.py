"""Sandbox for running agent-generated code. The `Sandbox` protocol lets a
`DockerSandbox` drop in later (Mac mini); `SubprocessSandbox` is the lightweight
macOS-native backend for now.

Safety layers (see docs/04-constitution.md for the honest scope):
  - HARD: the child process gets a **scrubbed environment** — an allow-list only, so no
    secrets (`.env`, DB URL, OAuth token path, Telegram/hosted keys) are ever in its env.
  - cwd is jailed to a project under `workspace/`; every run has a timeout.
  - BEST-EFFORT: `sandbox-exec` denies reads of `data/` and `.env` (the secret files).
    Network stays open (npm needs it) and non-denied files are otherwise readable — full
    FS/network isolation awaits the Docker backend.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.builder.workspace import REPO_ROOT, WORKSPACE_ROOT
from app.config import settings

# Only these environment variables are passed through to sandboxed processes.
_ALLOWED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "SHELL")
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Sandbox(Protocol):
    def run(self, cmd: list[str], *, cwd: Path, timeout: int | None = None) -> SandboxResult: ...


def _scrubbed_env() -> dict:
    env = {k: os.environ[k] for k in _ALLOWED_ENV if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    return env


def _fs_deny_prefix() -> list[str]:
    """A macOS sandbox-exec profile: allow everything, then deny reading the secret paths."""
    data = (REPO_ROOT / "data").as_posix()
    env_file = (REPO_ROOT / ".env").as_posix()
    profile = f'(version 1)(allow default)(deny file-read* (subpath "{data}") (literal "{env_file}"))'
    return [_SANDBOX_EXEC, "-p", profile]


class SubprocessSandbox:
    """Run a command as a subprocess with a scrubbed env, a workspace-jailed cwd, a
    timeout, and (best-effort) a sandbox-exec filesystem deny-list."""

    def run(self, cmd: list[str], *, cwd: Path, timeout: int | None = None) -> SandboxResult:
        cwd = Path(cwd).resolve()
        if not cwd.is_relative_to(WORKSPACE_ROOT.resolve()):
            raise ValueError(f"cwd escapes workspace: {cwd}")
        timeout = timeout or settings.builder_sandbox_timeout

        wrapped = cmd
        if settings.builder_fs_deny and Path(_SANDBOX_EXEC).exists():
            wrapped = _fs_deny_prefix() + cmd

        result = self._exec(wrapped, cwd, timeout)
        # If the sandbox-exec wrapper itself failed to launch, retry unwrapped (env-scrub
        # still applies) so the builder degrades rather than breaking.
        if wrapped is not cmd and result.returncode == 127:
            return self._exec(cmd, cwd, timeout)
        return result

    def _exec(self, cmd: list[str], cwd: Path, timeout: int) -> SandboxResult:
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd), env=_scrubbed_env(),
                capture_output=True, text=True, timeout=timeout,
            )
            return SandboxResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(124, exc.stdout or "", (exc.stderr or "") + "\n[timed out]", timed_out=True)
        except FileNotFoundError as exc:
            return SandboxResult(127, "", str(exc))
