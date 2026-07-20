#!/usr/bin/env bash
# One-command full regression: ruff + the offline suite + every verify_*.py, run
# SEQUENTIALLY (Ollama serializes model calls — running these in parallel gives
# misleading 0/0 results). Prereqs: Ollama up with the local model, a
# personal_agent_test DB, and (for phase2/3/3b/4b) a Google token + hosted key if you
# want those live checks to run rather than skip.
#
#   ./scripts/verify_all.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=.
export DATABASE_URL="postgresql://localhost/personal_agent_test"

fail=0
run() {
  echo; echo "=================== $* ==================="
  "$@" || { echo "!!! FAILED: $*"; fail=1; }
}

run uv run ruff check app/ tests/ scripts/
run uv run pytest tests/ -q
for s in verify_phase1 verify_phase2 verify_phase3 verify_phase3b verify_phase4a verify_phase4b verify_dynamic_tools verify_web_search verify_scenarios; do
  run uv run python "scripts/$s.py"
done

echo
if [ "$fail" -eq 0 ]; then echo "ALL GREEN ✅"; else echo "SOME CHECKS FAILED ❌"; fi
exit "$fail"
