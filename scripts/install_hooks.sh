#!/usr/bin/env bash
# One-time: point git at the version-controlled hooks in .githooks/ (git ignores that dir by default).
# Idempotent; re-run after a fresh clone. Undo with `git config --unset core.hooksPath`.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
chmod +x .githooks/*
git config core.hooksPath .githooks
echo "Installed git hooks from .githooks/ (core.hooksPath set). Pre-push now runs ruff + CI-parity tests."
