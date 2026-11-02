#!/usr/bin/env bash
# Manage Nginx logins.
#
#   sudo bash /opt/tsbench/app/deploy/manage-user.sh add <username>      add a user or change a password (prompts)
#   sudo bash /opt/tsbench/app/deploy/manage-user.sh remove <username>
#   sudo bash /opt/tsbench/app/deploy/manage-user.sh list
#
# Nginx reads the password file on every request, so no reload is needed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
    echo "Usage: $0 add|remove <username>  |  $0 list" >&2
    exit 1
}

valid_username() {
    # A leading dash would be read as an htpasswd option.
    [[ "$1" =~ ^[A-Za-z0-9._][A-Za-z0-9._-]*$ ]] || die "Usernames may only use letters, digits, dot, underscore and dash, and can't start with a dash."
}

fix_permissions() {
    chown root:www-data "$HTPASSWD_FILE"
    chmod 0640 "$HTPASSWD_FILE"
}

role_reminder() {
    cat <<EOF
Roles are set in $ENV_FILE, and need a restart afterwards (sudo systemctl restart tsbench):
  TS_ADMIN_USERS  comma-separated logins who can open Settings
  TS_ORDER_USERS  comma-separated logins who can run the order latency test
EOF
}

main() {
    local action="${1:-}"
    case "$action" in
        add | remove) [[ $# -eq 2 ]] || usage ;;
        list) [[ $# -eq 1 ]] || usage ;;
        *) usage ;;
    esac
    require_root

    case "$action" in
        add)
            valid_username "$2"
            if [[ -f "$HTPASSWD_FILE" ]]; then
                htpasswd -B "$HTPASSWD_FILE" "$2"
            else
                htpasswd -B -c "$HTPASSWD_FILE" "$2"
            fi
            fix_permissions
            role_reminder
            ;;
        remove)
            valid_username "$2"
            [[ -f "$HTPASSWD_FILE" ]] || die "No password file yet."
            htpasswd -D "$HTPASSWD_FILE" "$2"
            fix_permissions
            echo "Also remove $2 from TS_ADMIN_USERS and TS_ORDER_USERS in $ENV_FILE if listed, then restart: sudo systemctl restart tsbench"
            ;;
        list)
            [[ -f "$HTPASSWD_FILE" ]] || die "No password file yet."
            cut -d: -f1 "$HTPASSWD_FILE"
            ;;
    esac
}

main "$@"
