#!/usr/bin/env bash
# Daily backup of Mochi's Postgres — the memory DB holds everything the project is for (reminders,
# facts, goals, message history) and had ZERO backups. A rotated, restore-verified pg_dump.
#
# Scope (decided): LOCAL rotated dumps. Covers what Time Machine can't cleanly undo — a bad
# migration, an accidental DROP/delete, corruption, an app bug (logical point-in-time state,
# restorable in seconds into a scratch DB). Disk failure is an accepted risk (whole-disk/Time
# Machine covers the physical disk). No offsite, no dump-encryption here.
#
# Prove a dump is restorable with scripts/restore_check.sh — a backup you can't restore is worthless.
#
# Install the daily schedule:
#   cp launchd/com.mochi.backup.plist ~/Library/LaunchAgents/
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mochi.backup.plist
# Run once now:  ./scripts/backup_db.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

DB_URL="${DATABASE_URL:-postgresql://localhost/personal_agent}"
BACKUP_DIR="backups"
KEEP="${BACKUP_KEEP:-14}"      # how many daily dumps to retain
LOG="data/backup.log"

mkdir -p "$BACKUP_DIR" data
ts="$(date +%Y%m%d-%H%M%S)"
final="$BACKUP_DIR/personal_agent-$ts.dump"
tmp="$final.tmp"

# Append to the log; also echo to the terminal for manual runs. NOT `tee`: under launchd, stdout is
# already redirected to $LOG, so tee would write every line twice. `[ -t 1 ]` = only echo to a real
# terminal, so the file has each line exactly once either way.
log() {
    local line; line="$(date '+%Y-%m-%d %H:%M:%S') $*"
    printf '%s\n' "$line" >> "$LOG"
    [ -t 1 ] && printf '%s\n' "$line"
}

# -Fc custom format: compressed, restorable via pg_restore (selective/parallel), dumps the vector
# extension + pgvector columns. --no-owner/--no-acl → restores under any role. Write to .tmp then
# rename, so a crash mid-dump can't leave a truncated file that looks like a valid backup.
if ! pg_dump -Fc --no-owner --no-acl "$DB_URL" > "$tmp" 2>>"$LOG"; then
    rm -f "$tmp"
    log "BACKUP FAILED (pg_dump non-zero) — db=$DB_URL"
    exit 1
fi
if [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    log "BACKUP FAILED (empty dump) — db=$DB_URL"
    exit 1
fi
mv "$tmp" "$final"
size="$(du -h "$final" | cut -f1)"
log "backup ok: $final ($size)"

# Rotate: keep the newest $KEEP dumps, delete the rest. (Never touches a stray .tmp.)
# Portable to macOS's stock bash 3.2 — no mapfile. Newest-first, skip the first $KEEP, rm the tail.
ls -t "$BACKUP_DIR"/personal_agent-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while IFS= read -r f; do
    rm -f "$f" && log "rotated out: $f"
done
exit 0
