#!/usr/bin/env bash
# Update the app to a branch, tag or commit, and roll back automatically if it doesn't come up healthy.
#
#   sudo bash /vns-work/code/tsolc.tradesmartonline.in/deploy/update.sh               update to main
#   sudo bash /vns-work/code/tsolc.tradesmartonline.in/deploy/update.sh v1.2.0        update to a branch, tag or commit
#   sudo bash /vns-work/code/tsolc.tradesmartonline.in/deploy/update.sh rollback      go back to the commit before the last update
#   sudo FORCE=1 bash /vns-work/code/tsolc.tradesmartonline.in/deploy/update.sh       update even during a run (the run is killed)
#
# Rollback restores code and Python packages only. A database backup is taken before every update
# (in /var/backups/tsbench) in case a new version changed the database.
#
# Exit codes: 0 updated, 1 update failed and the previous version is running again (or nothing changed),
# 2 update and rollback both failed, 3 a run is active.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

FORCE="${FORCE:-0}"

# Holding the app's run lock also stops a new run from starting while the update is in progress.
hold_run_lock() {
    [[ "$FORCE" != "1" ]] || return 0
    [[ -e "$RUN_LOCK" ]] || install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0640 /dev/null "$RUN_LOCK"
    exec 9<>"$RUN_LOCK"
    if ! flock -n 9; then
        printf 'A benchmark or order batch is running, so nothing was changed.\n' >&2
        [[ ! -r "$RUN_LOCK.info" ]] || printf 'Current run: %s\n' "$(cat "$RUN_LOCK.info")" >&2
        printf 'Try again when it finishes, or use FORCE=1 to kill the run.\n' >&2
        exit 3
    fi
}

# The service's own start-up order check needs the lock, so it must be free before a restart.
release_run_lock() {
    exec 9>&-
}

backup_database() {
    log "Backing up the database"
    if systemctl start tsbench-backup.service; then
        return 0
    fi
    [[ "$FORCE" == "1" ]] || die "The backup failed, so nothing was changed. Use FORCE=1 to update without one."
    warn "The backup failed; continuing because FORCE=1."
}

install_commit() {
    git -C "$APP_DIR" checkout --quiet --force --detach "$1" || return 1
    fix_app_permissions || return 1
    pip_install_lock || return 1
    fix_app_permissions || return 1
    sync_systemd_units
}

# A doctor that finishes and reports FAIL lines usually means configuration or network, which a rollback wouldn't
# fix, so it only warns. A doctor that crashes means the new code is broken.
run_doctor() {
    log "Running the deployment self-check"
    local output rc=0
    output="$(run_as_service "$VENV_DIR/bin/python" -m src.doctor 2>&1)" || rc=$?
    printf '%s\n' "$output"
    if [[ "$output" != *"No failures."* && "$output" != *"failure(s)."* ]]; then
        warn "The self-check crashed (exit code $rc)."
        return 1
    fi
    [[ "$rc" == "0" ]] || warn "The self-check reported failures. They don't block the update; review them above."
}

restart_and_check() {
    log "Restarting the service"
    systemctl restart tsbench.service || return 1
    wait_for_health || return 1
    systemctl is-active --quiet tsbench.service
}

roll_back_to() {
    warn "Rolling back to $1"
    release_run_lock
    if ! install_commit "$1"; then
        die_rollback "$1"
    fi
    if [[ -z "$(missing_settings)" ]] && ! restart_and_check; then
        die_rollback "$1"
    fi
    printf 'Update failed. Version %s is running again.\n' "$1" >&2
    exit 1
}

die_rollback() {
    printf 'ERROR: rollback to %s also failed. Check: journalctl -u tsbench\n' "$1" >&2
    exit 2
}

main() {
    require_root
    [[ -d "$APP_DIR/.git" ]] || die "$APP_DIR is not a Git checkout. Run install.sh first."
    local ref="${1:-${GIT_REF:-main}}"
    if [[ "$ref" == "rollback" ]]; then
        [[ -s "$PREVIOUS_COMMIT_FILE" ]] || die "No previous version is recorded yet."
        ref="$(cat "$PREVIOUS_COMMIT_FILE")"
        log "Rolling back to the previous version: $ref"
    fi

    hold_run_lock
    if [[ "$FORCE" != "1" ]] && [[ -n "$(git -C "$APP_DIR" status --porcelain --untracked-files=no)" ]]; then
        die "$APP_DIR has local changes to tracked files. Review them, or use FORCE=1 to discard them."
    fi

    setup_git_ssh
    local previous target
    previous="$(git -C "$APP_DIR" rev-parse HEAD)"
    target="$(resolve_ref "$ref")"
    log "Updating ${previous:0:12} -> ${target:0:12}"

    backup_database
    install_commit "$target" || roll_back_to "$previous"
    run_doctor || roll_back_to "$previous"
    release_run_lock

    local missing
    missing="$(missing_settings)"
    if [[ -n "$missing" ]]; then
        warn "Service not restarted because these settings are blank: $missing"
    else
        restart_and_check || roll_back_to "$previous"
    fi

    if [[ "$previous" != "$target" ]]; then
        printf '%s\n' "$previous" >"$PREVIOUS_COMMIT_FILE"
    fi
    if ! git -C "$APP_DIR" diff --quiet "$previous" "$target" -- "$NGINX_SITE_SOURCE"; then
        warn "The Nginx site file changed in this update. Your live copy was not touched; compare it with $APP_DIR/$NGINX_SITE_SOURCE."
    fi
    log "Now on ${target:0:12} (previous: ${previous:0:12}). To go back: sudo bash $APP_DIR/deploy/update.sh rollback"
}

main "$@"
