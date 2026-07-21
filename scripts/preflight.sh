#!/usr/bin/env bash
# Startup preflight: make sure Mochi's dependencies are actually healthy before the bot starts.
#
# WHY: both failure modes below happened for real. (1) The machine died and Postgres then refused
# to start because a stale postmaster.pid survived the unclean shutdown — brew services just
# flapped into "error" and Mochi was silently dead. (2) The bot process itself died with nothing
# to restart it. This script fixes (1) and launchd's KeepAlive fixes (2).
#
# Safe by construction: the stale-lock file is only removed after verifying that NO postmaster is
# actually running (a PID in postmaster.pid can be recycled to an unrelated process, which is
# exactly what we saw). Removing a live lock could corrupt data, so we never guess.
#
# Exits non-zero if it can't reach a healthy state, so launchd retries instead of starting a bot
# that will fail every turn.
set -uo pipefail

PG_SERVICE="postgresql@17"
PG_DATA="/opt/homebrew/var/postgresql@17"
OLLAMA_URL="http://localhost:11434/api/tags"
MODEL="qwen2.5:7b-8k"
MODELFILE="$(cd "$(dirname "$0")/.." && pwd)/ollama/Modelfile.qwen2.5-7b-8k"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

log() { echo "[preflight] $*"; }

wait_for() {  # wait_for <seconds> <command...>
  local secs="$1"; shift
  for _ in $(seq 1 "$secs"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

# --- Postgres -------------------------------------------------------------
if pg_isready -q 2>/dev/null; then
  log "postgres: ok"
else
  log "postgres: not accepting connections — investigating"
  pidfile="$PG_DATA/postmaster.pid"
  if [ -f "$pidfile" ]; then
    pid="$(head -1 "$pidfile" 2>/dev/null)"
    # Only treat the lock as stale if NOTHING that looks like postgres is running AND the recorded
    # PID isn't a live postgres process. Otherwise leave it strictly alone.
    if ! pgrep -x postgres >/dev/null 2>&1 && ! ps -p "$pid" -o command= 2>/dev/null | grep -qi postgres; then
      log "postgres: stale lock (pid $pid is not a postmaster) — removing $pidfile"
      rm -f "$pidfile"
    else
      log "postgres: lock looks LIVE (pid $pid) — not touching it"
    fi
  fi
  brew services start "$PG_SERVICE" >/dev/null 2>&1 || brew services restart "$PG_SERVICE" >/dev/null 2>&1
  if wait_for 30 pg_isready -q; then log "postgres: recovered"; else log "postgres: FAILED to start"; exit 1; fi
fi

# --- Ollama ---------------------------------------------------------------
if curl -fsS --max-time 3 "$OLLAMA_URL" >/dev/null 2>&1; then
  log "ollama: ok"
else
  log "ollama: down — starting"
  brew services start ollama >/dev/null 2>&1
  if wait_for 30 curl -fsS --max-time 3 "$OLLAMA_URL"; then log "ollama: recovered"; else log "ollama: FAILED"; exit 1; fi
fi

# --- The 8k-context model (tool-calling silently degrades without it) ------
# Exact match on the NAME column — the model name contains regex metacharacters ('.', ':'),
# so compare literally rather than as a pattern.
if ollama list 2>/dev/null | awk '{print $1}' | grep -qxF "$MODEL"; then
  log "model: $MODEL present"
elif [ -f "$MODELFILE" ]; then
  log "model: $MODEL missing — creating from $MODELFILE"
  ollama create "$MODEL" -f "$MODELFILE" >/dev/null 2>&1 \
    && log "model: created" || { log "model: FAILED to create"; exit 1; }
else
  log "model: $MODEL missing and no Modelfile at $MODELFILE"; exit 1
fi

log "all dependencies healthy"
