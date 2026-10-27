#!/usr/bin/env bash
# Review what memory consolidation PROPOSED in SHADOW mode — the precision hand-check before going live.
# Shadow logs each proposal to data/mochi.log and changes nothing; this tallies + lists them so "would
# it have fixed real duplicates/contradictions, and nothing true?" is answerable. Any time during shadow:
#   ./scripts/review_consolidation.sh          # every shadow proposal in the log
#   ./scripts/review_consolidation.sh 3        # only the last 3 days
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="data/mochi.log"
DAYS="${1:-}"

[ -f "$LOG" ] || { echo "no $LOG yet"; exit 0; }

lines="$(grep "SHADOW-CONSOLIDATE" "$LOG" 2>/dev/null || true)"
if [ -n "$DAYS" ]; then
    cutoff="$(date -v-"${DAYS}"d '+%Y-%m-%d' 2>/dev/null || date -d "-${DAYS} days" '+%Y-%m-%d')"
    lines="$(printf '%s\n' "$lines" | awk -v c="$cutoff" '$1 >= c')"
fi
count="$(printf '%s\n' "$lines" | grep -c "SHADOW-CONSOLIDATE" || true)"
echo "=== memory consolidation — shadow proposals${DAYS:+ (last $DAYS days)}: $count ==="
runs="$(grep -c "consolidation run .* done:" "$LOG" 2>/dev/null || true)"
echo "completed runs in the log: $runs"
if [ "$count" -eq 0 ]; then
    echo "(none — either no run has completed yet (it needs an idle hour), or nothing crossed the threshold)"
    exit 0
fi
echo
echo "by relation:"
printf '%s\n' "$lines" | sed -n 's/.*relation=\([a-z]*\).*/  \1/p' | sort | uniq -c | sort -rn
blocked="$(printf '%s\n' "$lines" | grep -c "BLOCKED=pinned" || true)"
echo "  ($blocked touch a pinned fact — those are never applied automatically)"
echo
echo "proposals (KEEP ← would stay, DROP ← would be superseded):"
printf '%s\n' "$lines" | sed -E "s/.*relation=([a-z]+) sim=([0-9.]+)( BLOCKED=pinned)?.*keep_text=('[^']*'|\"[^\"]*\") drop_text=('[^']*'|\"[^\"]*\") reason=(.*)$/  [\1 \2\3]\n     KEEP \4\n     DROP \5\n     why  \6/"
echo
echo "→ judge precision: is every DROP really outdated or duplicated? One wrong DROP of a true fact is a"
echo "  reason not to go live yet. Tune CONSOLIDATION_SIMILARITY_THRESHOLD (see calibrate_consolidation.py)."
