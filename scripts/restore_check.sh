#!/usr/bin/env bash
# Prove the newest backup is actually restorable — a backup you've never restored is a guess.
# Restores the latest dump into a throwaway DB, checks every table's row count matches the live DB,
# then drops the throwaway. Read-only against production. Run once at setup, and any time you want
# reassurance:  ./scripts/restore_check.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

DB_URL="${DATABASE_URL:-postgresql://localhost/personal_agent}"
SCRATCH="personal_agent_restore_check"   # a DB NAME (createdb/dropdb take a name, not a URL)

newest="$(ls -t backups/personal_agent-*.dump 2>/dev/null | head -1)"
if [ -z "$newest" ]; then
    echo "restore_check: no backups found in backups/ — run scripts/backup_db.sh first"; exit 1
fi
echo "restore_check: restoring $newest into $SCRATCH"

cleanup() { dropdb --if-exists "$SCRATCH" 2>/dev/null || true; }
trap cleanup EXIT
cleanup
createdb "$SCRATCH"

# pg_restore returns non-zero on benign notices (e.g. the vector extension already present); we
# judge success by the row-count parity below, not its exit code — so capture, don't gate on it.
pg_restore --no-owner -d "$SCRATCH" "$newest" 2>/tmp/restore_check.err || true

# Every table that exists in prod must have the same row count in the restore.
tables="$(psql -tAc \
  "select tablename from pg_tables where schemaname='public' order by tablename" "$DB_URL")"

fail=0
printf '%-22s %10s %10s\n' "table" "prod" "restored"
for t in $tables; do
    p="$(psql -tAc "select count(*) from \"$t\"" "$DB_URL" 2>/dev/null || echo ERR)"
    r="$(psql -tAc "select count(*) from \"$t\"" "$SCRATCH" 2>/dev/null || echo MISSING)"
    mark=""
    if [ "$p" != "$r" ]; then mark="  <-- MISMATCH"; fail=1; fi
    printf '%-22s %10s %10s%s\n' "$t" "$p" "$r" "$mark"
done

echo
if [ "$fail" -eq 0 ]; then
    echo "restore_check: PASS — $newest restores cleanly, all row counts match prod."
else
    echo "restore_check: FAIL — row counts differ (see above). pg_restore stderr: /tmp/restore_check.err"
fi
exit "$fail"
