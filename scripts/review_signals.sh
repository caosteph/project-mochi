#!/usr/bin/env bash
# Review what the email scanner detected in SHADOW mode — the precision hand-check before going live.
# Shadow mode logs each detection to data/mochi.log (never touches her); this tallies + lists them so
# "is it quiet and accurate?" is answerable. Run any time during the shadow observation period:
#   ./scripts/review_signals.sh            # all shadow detections in the log
#   ./scripts/review_signals.sh 2          # only the last 2 days
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="data/mochi.log"
DAYS="${1:-}"

[ -f "$LOG" ] || { echo "no $LOG yet"; exit 0; }

# All SHADOW-SIGNAL lines (optionally filtered to the last N days by the leading date).
lines="$(grep "SHADOW-SIGNAL" "$LOG" 2>/dev/null || true)"
if [ -n "$DAYS" ]; then
    cutoff="$(date -v-"${DAYS}"d '+%Y-%m-%d' 2>/dev/null || date -d "-${DAYS} days" '+%Y-%m-%d')"
    lines="$(printf '%s\n' "$lines" | awk -v c="$cutoff" '$1 >= c')"
fi

count="$(printf '%s\n' "$lines" | grep -c "SHADOW-SIGNAL" || true)"
echo "=== email scanner — shadow detections${DAYS:+ (last $DAYS days)}: $count ==="
if [ "$count" -eq 0 ]; then
    echo "(none yet — the first scan baselines the inbox and detects nothing; new mail appears on later scans)"
    exit 0
fi
echo
echo "by type:"
printf '%s\n' "$lines" | sed -n 's/.*type=\([a-z]*\).*/  \1/p' | sort | uniq -c | sort -rn
echo
echo "detections (newest last):"
printf '%s\n' "$lines" | sed -E 's/.*(SHADOW-SIGNAL.*)/  \1/'

# Also show what the filters DROPPED (already on her calendar) — proof the noise-reduction works.
skips="$(grep "SHADOW-SKIP" "$LOG" 2>/dev/null || true)"
if [ -n "$DAYS" ] && [ -n "$skips" ]; then
    skips="$(printf '%s\n' "$skips" | awk -v c="$cutoff" '$1 >= c')"
fi
skipn="$(printf '%s\n' "$skips" | grep -c "SHADOW-SKIP" || true)"
echo
echo "skipped (already on your calendar — the event IS the reminder): $skipn"
[ "$skipn" -gt 0 ] && printf '%s\n' "$skips" | sed -E 's/.*(reason=[^ ]* .*title=.*)/  \1/'
echo
echo "→ judge precision: are the SURFACED ones real, dated correctly, not spammy? When quiet, set SIGNAL_MODE=live in .env and restart."
