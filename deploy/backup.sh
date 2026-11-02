#!/usr/bin/env bash
# Daily database backup, run by tsbench-backup.timer. To run it by hand: sudo systemctl start tsbench-backup
#
# Writes /var/backups/tsbench/benchmark-YYYYMMDD-HHMM.db.gz using SQLite's online backup, so it is safe while the
# app is running. Only the database is copied: the access token file is never included.
set -euo pipefail
umask 027

DB_PATH="${DB_PATH:-/var/lib/tsbench/benchmark.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/tsbench}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "No database at $DB_PATH yet; nothing to back up."
    exit 0
fi

target="$BACKUP_DIR/benchmark-$(date +%Y%m%d-%H%M).db.gz"
snapshot="$BACKUP_DIR/.snapshot-$$.db"
compressed="$target.tmp-$$"
trap 'rm -f "$snapshot" "$compressed"' EXIT

sqlite3 -batch -bail -cmd ".timeout 30000" "$DB_PATH" ".backup '$snapshot'"

result="$(sqlite3 -batch -bail "$snapshot" "PRAGMA integrity_check;" | tr -d '\r')"
if [[ "$result" != "ok" ]]; then
    echo "ERROR: the backup failed its integrity check; keeping older backups untouched." >&2
    exit 1
fi

gzip -c "$snapshot" >"$compressed"
gzip -t "$compressed"
mv -f "$compressed" "$target"

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'benchmark-*.db.gz' -mtime "+$RETENTION_DAYS" -delete

echo "Backup written: $target ($(du -h "$target" | cut -f1))"
