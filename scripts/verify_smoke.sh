#!/usr/bin/env bash
# The fast tier of the gate — run this MID-CHANGE, after any persona/tool/graph edit.
#
# WHY: the full scripts/verify_all.sh measured ~21 min, so it was the first thing skipped under time
# pressure — and a skipped gate is how regressions reached Stephanie. This adds no coverage; it makes
# the coverage cheap enough to actually run while iterating. verify_all.sh is still the pre-push /
# definition-of-done gate (it runs every phase verify; this runs the two that catch most firing
# regressions).
#
#   ruff + session audit + doc-lint + the offline suite   (no model)
#   verify_scenarios                                       (the behavioral gate: right tool, no JSON dump)
#   verify_firing on the core tools, N=1, floor 33%        (a COLLAPSE detector: fails only when a tool
#                                                           fires on none of its prompts — the 7/12→1/12
#                                                           kind of drop. Subtle drift is verify_all's
#                                                           job, or verify_firing --baseline.)
#
# Same prereqs as verify_all.sh (Ollama up, personal_agent_test DB), bot stopped. Prints per-step time.
#   ./scripts/verify_smoke.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=.
export DATABASE_URL="postgresql://localhost/personal_agent_test"

CORE_TOOLS="add_reminder,create_draft,web_search"
fail=0
start=$SECONDS
run() {
  local t=$SECONDS
  echo; echo "=================== $* ==================="
  "$@" || { echo "!!! FAILED: $*"; fail=1; }
  echo "(took $((SECONDS - t))s)"
}

run uv run ruff check app/ tests/ scripts/
run uv run python scripts/audit_session_scope.py app
run uv run python scripts/check_docs.py
run uv run pytest tests/ -q
run uv run python scripts/verify_scenarios.py
run uv run python scripts/verify_firing.py "$CORE_TOOLS" --n 1 --floor 33

echo
echo "smoke took $((SECONDS - start))s"
if [ "$fail" -eq 0 ]; then echo "SMOKE GREEN ✅ (run verify_all.sh before pushing)"; else echo "SMOKE FAILED ❌"; fi
exit "$fail"
