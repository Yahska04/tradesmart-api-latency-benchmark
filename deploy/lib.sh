#!/usr/bin/env bash
# Shared paths and helpers for the deploy scripts. Sourced, not run.
# shellcheck disable=SC2034  # the constants are used by the scripts that source this file

SERVICE_USER=tsbench
OPT_DIR=/opt/tsbench
APP_DIR=/opt/tsbench/app
VENV_DIR=/opt/tsbench/venv
ETC_DIR=/etc/tsbench
ENV_FILE=/etc/tsbench/tsbench.env
DATA_DIR=/var/lib/tsbench
LOG_DIR=/var/log/tsbench
BACKUP_DIR_DEFAULT=/var/backups/tsbench
RUN_LOCK=/var/lib/tsbench/api_run.lock
NGINX_SITE=/etc/nginx/sites-available/tsbench
NGINX_SITE_LINK=/etc/nginx/sites-enabled/tsbench
HTPASSWD_FILE=/etc/nginx/tsbench.htpasswd
SECRET_SNIPPET=/etc/nginx/tsbench-proxy-secret.conf
TLS_DIR=/etc/nginx/ssl
TLS_CERT=/etc/nginx/ssl/tsbench.crt
TLS_KEY=/etc/nginx/ssl/tsbench.key
SYSTEMD_DIR=/etc/systemd/system
PREVIOUS_COMMIT_FILE=/opt/tsbench/previous_commit
HEALTH_URL=http://127.0.0.1:8501/_stcore/health
REQUIRED_SETTINGS="TS_APP_KEY TS_SECRET_KEY TS_CLIENT_ID TS_AUTH_PROXY_SECRET TS_ADMIN_USERS"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    [[ "$(id -u)" == "0" ]] || die "Run this as root (sudo)."
}

# Print one value from the env file without echoing anything else. Last assignment wins, like systemd.
env_value() {
    local value
    value="$(awk -v key="$1" 'index($0, key "=") == 1 { v = substr($0, length(key) + 2) } END { print v }' "$ENV_FILE")"
    value="${value%$'\r'}"
    value="${value#\"}"
    value="${value%\"}"
    printf '%s' "$value"
}

# Names of required settings that are blank (names only, never values).
missing_settings() {
    local name missing=""
    for name in $REQUIRED_SETTINGS; do
        [[ -n "$(env_value "$name")" ]] || missing+="$name "
    done
    printf '%s' "${missing% }"
}

# Run a command exactly as the service sees it: same user, same env file parsing, same working directory.
run_as_service() {
    systemd-run --quiet --wait --pipe --collect \
        --uid="$SERVICE_USER" --gid="$SERVICE_USER" \
        --working-directory="$APP_DIR" \
        --property=EnvironmentFile="$ENV_FILE" \
        --property=Environment=HOME="$DATA_DIR"         --property=Environment=PYTHONUTF8=1 \
        --property=UMask=0027 \
        -- "$@"
}

# True while a benchmark or order batch holds the app's flock (see src/run_lock.py).
run_is_active() {
    [[ -e "$RUN_LOCK" ]] || return 1
    ! flock -n "$RUN_LOCK" true
}

wait_for_health() {
    local attempt
    for attempt in $(seq 1 45); do
        if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
            log "Health check passed (attempt $attempt)."
            return 0
        fi
        sleep 2
    done
    return 1
}

fix_app_permissions() {
    chown -R "root:$SERVICE_USER" "$APP_DIR"
    chmod -R g+rX,o-rwx "$APP_DIR"
    if [[ -d "$VENV_DIR" ]]; then
        chown -R "root:$SERVICE_USER" "$VENV_DIR"
        chmod -R g+rX,o-rwx "$VENV_DIR"
    fi
}

pip_install_lock() {
    "$VENV_DIR/bin/pip" install --no-input --disable-pip-version-check --quiet \
        --require-hashes --only-binary=:all: -r "$APP_DIR/requirements.lock"
}

# Make Git use the deploy key when there is one. Never prompts.
setup_git_ssh() {
    local key="${DEPLOY_KEY:-/root/.ssh/tsbench_deploy_key}"
    GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    if [[ -f "$key" ]]; then
        GIT_SSH_COMMAND+=" -i $key -o IdentitiesOnly=yes"
    fi
    export GIT_SSH_COMMAND
}

# Fetch, then print the commit that a branch, tag or commit id refers to.
resolve_ref() {
    local ref="$1"
    [[ "$ref" =~ ^[A-Za-z0-9._/][A-Za-z0-9._/-]*$ ]] || die "Invalid Git ref: $ref"
    git -C "$APP_DIR" fetch --quiet --prune --tags --force origin >&2 || die "git fetch failed."
    git -C "$APP_DIR" rev-parse --verify --quiet "origin/$ref^{commit}" \
        || git -C "$APP_DIR" rev-parse --verify --quiet "$ref^{commit}" \
        || die "Git ref not found: $ref"
}

# Copy systemd units from the checkout when they differ. Returns after a daemon-reload if anything changed.
sync_systemd_units() {
    local unit changed=0
    for unit in tsbench.service tsbench-backup.service tsbench-backup.timer; do
        if ! cmp -s "$APP_DIR/deploy/$unit" "$SYSTEMD_DIR/$unit"; then
            install -o root -g root -m 0644 "$APP_DIR/deploy/$unit" "$SYSTEMD_DIR/$unit"
            changed=1
        fi
    done
    if [[ "$changed" == "1" ]]; then
        systemctl daemon-reload
    fi
}

# Print the allow-list as validated CIDRs, one per line. IPv4 only because Nginx listens on IPv4 only.
# Exit codes: 2 invalid input, 3 "anywhere" without permission.
normalize_cidrs() {
    python3 -c '
import ipaddress, sys
out = []
for raw in sys.argv[1].split(","):
    raw = raw.strip()
    if not raw:
        continue
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        print(f"Not a valid CIDR: {raw}", file=sys.stderr)
        sys.exit(2)
    if net.version != 4:
        print(f"IPv6 is not supported (Nginx listens on IPv4 only): {raw}", file=sys.stderr)
        sys.exit(2)
    if net.prefixlen == 0 and sys.argv[2] != "1":
        print("0.0.0.0/0 opens the app to the whole internet. Set ALLOW_ANYWHERE=1 if you really mean it.", file=sys.stderr)
        sys.exit(3)
    if str(net) not in out:
        out.append(str(net))
if not out:
    print("ALLOWED_CIDRS has no addresses.", file=sys.stderr)
    sys.exit(2)
print("\n".join(out))
' "$1" "${2:-0}"
}

# render_nginx_conf TEMPLATE SERVER_NAME CIDR_LINES > rendered
# Loopback is always allowed so that healthcheck.sh on the server can see the login prompt; login still applies.
render_nginx_conf() {
    local rules="allow 127.0.0.1/32;" cidr
    while IFS= read -r cidr; do
        rules+=$'\n'"allow $cidr;"
    done <<<"$3"
    rules+=$'\n'"deny all;"
    SERVER_NAME_VALUE="$2" ALLOW_RULES_VALUE="$rules" awk '
        index($0, "__ALLOW_RULES__") {
            indent = $0
            sub(/[^ ].*$/, "", indent)
            n = split(ENVIRON["ALLOW_RULES_VALUE"], rule, "\n")
            for (i = 1; i <= n; i++) print indent rule[i]
            next
        }
        { gsub(/__SERVER_NAME__/, ENVIRON["SERVER_NAME_VALUE"]); print }
    ' "$1"
}

# make_self_signed_cert NAME CERT KEY: subject alternative name is an IP or DNS entry to match NAME.
make_self_signed_cert() {
    local san="DNS:$1"
    if [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        san="IP:$1"
    fi
    openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 \
        -subj "/CN=$1" -addext "subjectAltName=$san" \
        -keyout "$3" -out "$2" >/dev/null 2>&1
}
