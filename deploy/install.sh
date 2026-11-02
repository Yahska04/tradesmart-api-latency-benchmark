#!/usr/bin/env bash
# Install or re-apply the TradeSmart Latency Benchmark on Ubuntu 24.04. Safe to run again: existing
# configuration, secrets, logins, data and the live Nginx site file are kept.
#
#   sudo bash deploy/install.sh
#   sudo bash deploy/install.sh GIT_REF=v1.2.0
#
# Designed for a server shared with other sites. It never enables, edits or removes any Nginx site, never creates
# a certificate and never changes the firewall. It copies this app's site file into sites-available once; the
# system team fills in the certificate and allowed networks and enables it (docs/DEPLOYMENT.md, step 5.7).
#
# Optional:
#   GIT_REF         Branch, tag or commit. First install: default main. Re-run: keeps the installed code unless
#                   given (use update.sh to move to newer code; it takes a backup and can roll back).
#   REPO_URL        Default: the project's GitHub repository.
#   DEPLOY_KEY      SSH key for GitHub (default /root/.ssh/tsbench_deploy_key). Generated if needed: the script
#                   then prints the public key to add to GitHub and asks you to run it again.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

DEFAULT_REPO_URL="git@github.com:Yahska04/tradesmart-api-latency-benchmark.git"

parse_args() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            GIT_REF=* | REPO_URL=* | DEPLOY_KEY=*)
                export "${arg?}"
                ;;
            *) die "Unknown argument: $arg (use KEY=value; see the top of this script)" ;;
        esac
    done
    GIT_REF="${GIT_REF:-}"
    REPO_URL="${REPO_URL:-$DEFAULT_REPO_URL}"
}

check_platform() {
    require_root
    local os_id="" os_version=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091  # only exists on the target system
        os_id="$(. /etc/os-release && printf '%s' "${ID:-}")"
        # shellcheck disable=SC1091
        os_version="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
    fi
    [[ "$os_id" == "ubuntu" ]] || die "This installer supports Ubuntu only (found: ${os_id:-unknown})."
    [[ "$os_version" == "24.04" ]] || warn "Written for Ubuntu 24.04; found $os_version."
}

# Another app on this shared server may already use the port. Stop before changing anything.
check_port_free() {
    if ss -ltnH "sport = :$APP_PORT" 2>/dev/null | grep -q . && ! systemctl is-active --quiet tsbench.service; then
        die "Port $APP_PORT is already used by another program on this server. Free it first."
    fi
}

install_packages() {
    log "Installing system packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3.12-venv python3-pip git nginx apache2-utils sqlite3 openssl curl
}

create_user_and_dirs() {
    log "Creating service user and directories"
    getent group "$SERVICE_USER" >/dev/null || groupadd --system "$SERVICE_USER"
    id -u "$SERVICE_USER" >/dev/null 2>&1 \
        || useradd --system --gid "$SERVICE_USER" --home-dir "$DATA_DIR" --no-create-home \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    install -d -o root -g "$SERVICE_USER" -m 0750 "$OPT_DIR" "$ETC_DIR"
    mkdir -p "$(dirname "$APP_DIR")"   # an existing company folder keeps its owner and permissions
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR" "$LOG_DIR" "$BACKUP_DIR_DEFAULT"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"
}

# Stops with instructions when GitHub doesn't accept this server yet.
ensure_repo_access() {
    setup_git_ssh
    if git ls-remote --exit-code "$REPO_URL" HEAD >/dev/null 2>&1; then
        return 0
    fi
    [[ "$REPO_URL" == git@* || "$REPO_URL" == ssh://* ]] || die "Cannot read $REPO_URL."
    local key="${DEPLOY_KEY:-/root/.ssh/tsbench_deploy_key}"
    if [[ ! -f "$key" ]]; then
        log "Generating a deploy key at $key"
        install -d -m 0700 "$(dirname "$key")"
        ssh-keygen -q -t ed25519 -N '' -C "tsbench-deploy@$(hostname)" -f "$key"
    fi
    cat <<EOF

GitHub does not accept this server yet. Add this public key at
GitHub > repository > Settings > Deploy keys > Add deploy key (leave "Allow write access" off):

$(cat "$key.pub")

Then run this installer again.
EOF
    exit 2
}

checkout_app() {
    log "Fetching application code (${GIT_REF:-keep installed version, or main on first install})"
    ensure_repo_access
    if [[ -d "$APP_DIR/.git" ]]; then
        git -C "$APP_DIR" remote set-url origin "$REPO_URL"
    elif [[ -e "$APP_DIR" ]] && [[ -n "$(ls -A "$APP_DIR")" ]]; then
        die "$APP_DIR exists and is not a Git checkout. Move it away and run this again."
    else
        # Git refuses to work as root in a folder another user owns, so an empty pre-created folder is taken over first.
        [[ ! -d "$APP_DIR" ]] || chown "root:$SERVICE_USER" "$APP_DIR"
        git clone --quiet --origin origin "$REPO_URL" "$APP_DIR"
    fi
    local commit
    if [[ -z "$GIT_REF" ]] && git -C "$APP_DIR" rev-parse --verify --quiet HEAD >/dev/null \
        && [[ -f "$APP_DIR/$NGINX_SITE_SOURCE" ]]; then
        commit="$(git -C "$APP_DIR" rev-parse HEAD)"   # a re-run keeps the installed code
    else
        commit="$(resolve_ref "${GIT_REF:-main}")"
    fi
    git -C "$APP_DIR" checkout --quiet --force --detach "$commit"
    fix_app_permissions
    [[ -f "$APP_DIR/$NGINX_SITE_SOURCE" ]] || die "Commit $commit has no $NGINX_SITE_SOURCE."
    runuser -u "$SERVICE_USER" -- test -r "$APP_DIR/app.py" \
        || die "User $SERVICE_USER can't read $APP_DIR. Allow it to enter the parent folders (for example: chmod o+x /vns-work /vns-work/code), then run this again."
}

install_python_env() {
    log "Installing Python dependencies (hash-checked)"
    if ! { [[ -x "$VENV_DIR/bin/python" ]] && "$VENV_DIR/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; }; then
        python3.12 -m venv --clear "$VENV_DIR"
    fi
    pip_install_lock
    fix_app_permissions
}

# Create the env file once. In an existing file, only a blank proxy secret is filled in.
configure_env_file() {
    if [[ ! -f "$ENV_FILE" ]]; then
        log "Creating $ENV_FILE from the template"
        install -o root -g "$SERVICE_USER" -m 0640 "$APP_DIR/deploy/tsbench.env.example" "$ENV_FILE"
    else
        log "Keeping existing $ENV_FILE"
    fi
    chown "root:$SERVICE_USER" "$ENV_FILE"
    chmod 0640 "$ENV_FILE"
    if [[ -z "$(env_value TS_AUTH_PROXY_SECRET)" ]]; then
        log "Generating the login proxy secret"
        local secret tmp
        secret="$(openssl rand -hex 32)"
        tmp="$(mktemp "$ETC_DIR/.tsbench.env.XXXXXX")"
        NEW_SECRET="$secret" awk '
            index($0, "TS_AUTH_PROXY_SECRET=") == 1 { if (!done) print "TS_AUTH_PROXY_SECRET=" ENVIRON["NEW_SECRET"]; done = 1; next }
            { print }
            END { if (!done) print "TS_AUTH_PROXY_SECRET=" ENVIRON["NEW_SECRET"] }
        ' "$ENV_FILE" >"$tmp"
        cat "$tmp" >"$ENV_FILE"
        rm -f "$tmp"
    fi
}

write_secret_snippet() {
    local secret tmp
    secret="$(env_value TS_AUTH_PROXY_SECRET)"
    [[ "$secret" =~ ^[A-Za-z0-9._~+/=-]+$ ]] \
        || die "TS_AUTH_PROXY_SECRET has characters that are unsafe in Nginx config. Use: openssl rand -hex 32"
    tmp="$(mktemp /etc/nginx/.tsbench-secret.XXXXXX)"
    printf 'proxy_set_header X-Proxy-Secret "%s";\n' "$secret" >"$tmp"
    chown root:root "$tmp"
    chmod 0640 "$tmp"
    mv -f "$tmp" "$SECRET_SNIPPET"
}

# Copy the site file once and prepare the files it includes. Only this app's files are touched.
configure_nginx() {
    log "Preparing Nginx files for $SERVER_NAME"
    [[ -f "$HTPASSWD_FILE" ]] || : >"$HTPASSWD_FILE"
    chown root:www-data "$HTPASSWD_FILE"
    chmod 0640 "$HTPASSWD_FILE"
    if [[ ! -f "$NGINX_SITE" ]]; then
        install -o root -g root -m 0644 "$APP_DIR/$NGINX_SITE_SOURCE" "$NGINX_SITE"
    else
        log "Keeping existing $NGINX_SITE"
    fi
    # A rotated proxy secret only takes effect after a reload, and only if the whole config is valid.
    if [[ -e "$NGINX_SITE_LINK" ]] && systemctl is-active --quiet nginx; then
        if nginx -t >/dev/null 2>&1; then
            systemctl reload nginx
        else
            warn "nginx -t failed, so Nginx was not reloaded. Run: sudo nginx -t"
        fi
    fi
}

install_systemd_units() {
    log "Installing systemd units"
    sync_systemd_units
    systemctl daemon-reload
    systemctl enable --quiet tsbench.service
    systemctl enable --now --quiet tsbench-backup.timer
}

start_service() {
    MISSING="$(missing_settings)"
    SERVICE_STATE="not started"
    if [[ -n "$MISSING" ]]; then
        return 0
    fi
    if run_is_active; then
        warn "A benchmark or order batch is running, so the service was not restarted. Restart it when the run finishes."
        SERVICE_STATE="left running (a run is active)"
        return 0
    fi
    log "Starting the service"
    systemctl restart tsbench.service
    if wait_for_health; then
        SERVICE_STATE="running"
    else
        SERVICE_STATE="NOT healthy: check journalctl -u tsbench"
    fi
}

print_summary() {
    local users site_state="enabled"
    users="$(grep -c . "$HTPASSWD_FILE" || true)"
    [[ -e "$NGINX_SITE_LINK" ]] || site_state="NOT enabled yet"
    cat <<EOF

--------------------------------------------------------------------------------
TradeSmart Latency Benchmark
  App code:      $APP_DIR ($(git -C "$APP_DIR" rev-parse --short HEAD))
  Config:        $ENV_FILE
  Nginx site:    $NGINX_SITE ($site_state)
  URL:           https://$SERVER_NAME/
  Service:       $SERVICE_STATE
  Login users:   $users
EOF
    local step=1
    printf '\nNext steps:\n'
    if [[ -n "$MISSING" ]]; then
        printf '  %d. Fill in these settings in %s: %s\n' "$step" "$ENV_FILE" "$MISSING"
        step=$((step + 1))
    fi
    if [[ "$users" == "0" ]]; then
        printf '  %d. Add a login: sudo bash %s/deploy/manage-user.sh add <username>\n' "$step" "$APP_DIR"
        step=$((step + 1))
    fi
    if [[ ! -e "$NGINX_SITE_LINK" ]]; then
        printf '  %d. Replace the CHANGE_ME values in %s, then enable it:\n' "$step" "$NGINX_SITE"
        printf '       sudo ln -s %s %s && sudo nginx -t && sudo systemctl reload nginx\n' "$NGINX_SITE" "$NGINX_SITE_LINK"
        step=$((step + 1))
    fi
    if [[ -n "$MISSING" ]]; then
        printf '  %d. Start the app: sudo systemctl start tsbench\n' "$step"
        step=$((step + 1))
    fi
    printf '  %d. Check everything: sudo bash %s/deploy/healthcheck.sh\n' "$step" "$APP_DIR"
    step=$((step + 1))
    printf '  %d. As an admin, open Settings and set the static IP registered with TradeSmart.\n' "$step"
    printf -- '--------------------------------------------------------------------------------\n'
}

main() {
    parse_args "$@"
    check_platform
    check_port_free
    install_packages
    create_user_and_dirs
    checkout_app
    install_python_env
    configure_env_file
    write_secret_snippet
    configure_nginx
    install_systemd_units
    start_service
    print_summary
}

main "$@"
