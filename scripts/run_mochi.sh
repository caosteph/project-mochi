#!/usr/bin/env bash
# The supervised entrypoint: preflight the dependencies, then exec the bot.
#
# `exec` matters — it replaces this shell with the Python process so launchd supervises the bot
# itself (accurate PID, signals delivered correctly, KeepAlive restarts the real thing).
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

./scripts/preflight.sh || {
  echo "[run_mochi] preflight failed — exiting so launchd retries"
  exit 1
}

echo "[run_mochi] starting Mochi ($(date))"
exec uv run python -m app.main
