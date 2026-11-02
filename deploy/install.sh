#!/usr/bin/env bash
# Install or re-apply the TradeSmart Latency Benchmark on Ubuntu 24.04. Safe to run again: existing
# configuration, secrets, certificates, logins and data are kept.
#
#   sudo SERVER_NAME=bench.example.com ALLOWED_CIDRS=203.0.113.0/24,198.51.100.7 bash deploy/install.sh
#   sudo bash deploy/install.sh SERVER_NAME=203.0.113.10 ALLOWED_CIDRS=198.51.100.0/24 GIT_REF=main
#
# Required:
#   SERVER_NAME     DNS name or IP address users open.
#   ALLOWED_CIDRS   Comma-separated IPv4 CIDRs allowed to reach the app. 0.0.0.0/0 is refused
#                   unless ALLOW_ANYWHERE=1.
# Optional:
#   GIT_REF         Branch, tag or commit. First install: default main. Re-run: keeps the installed code unless
#                   given (use update.sh to move to newer code; it takes a backup and can roll back).
#   REPO_URL        Default: the project's GitHub repository.
#   DEPLOY_KEY      SSH key for GitHub (default /root/.ssh/tsbench_deploy_key). Generated if needed: the script
#                   then prints the public key to add to GitHub and asks you to run it again.
#   ENABLE_UFW=1    Also turn the firewall on. Requires SSH_ALLOWED_CIDRS, so SSH can't be locked out.
#   SSH_ALLOWED_CIDRS  Comma-separated IPv4 CIDRs allowed to SSH in. Only used with ENABLE_UFW=1.
#   SSH_PORT        SSH port for that rule (default 22).
#
# Only needs deploy/ on the server for the first run: everything else comes from the Git checkout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

DEFAULT_REPO_URL="git@github.com:Yahska04/tradesmart-api-latency-benchmark.git"

parse_args() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            SERVER_NAME=* | ALLOWED_CIDRS=* | ALLOW_ANYWHERE=* | GIT_REF=* | REPO_URL=* | DEPLOY_KEY=* | ENABLE_UFW=* | SSH_ALLOWED_CIDRS=* | SSH_PORT=*)
                export "${arg?}"
                ;;
            *) die "Unknown argument: $arg (use KEY=value; see the top of this script)" ;;
        esac
    done
    SERVER_NAME="${SERVER_NAME:-}"
    ALLOWED_CIDRS="${ALLOWED_CIDRS:-}"
    ALLOW_ANYWHERE="${ALLOW_ANYWHERE:-0}"
    GIT_REF="${GIT_REF:-}"
    REPO_URL="${REPO_URL:-$DEFAULT_REPO_URL}"
    ENABLE_UFW="${ENABLE_UFW:-0}"
    SSH_ALLOWED_CIDRS="${SSH_ALLOWED_CIDRS:-}"
    SSH_PORT="${SSH_PORT:-22}"
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

check_inputs() {
    [[ -n "$SERVER_NAME" ]] || die "SERVER_NAME is required (DNS name or IP address)."
    [[ "$SERVER_NAME" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "SERVER_NAME is not a valid name or IP address."
    [[ -n "$ALLOWED_CIDRS" ]] || die "ALLOWED_CIDRS is required, for example ALLOWED_CIDRS=203.0.113.0/24."
    command -v python3 >/dev/null || die "python3 is required (it is part of Ubuntu Server)."
    CIDR_LINES="$(normalize_cidrs "$ALLOWED_CIDRS" "$ALLOW_ANYWHERE")" || die "ALLOWED_CIDRS rejected."
    SSH_CIDR_LINES=""
    if [[ "$ENABLE_UFW" == "1" ]]; then
        [[ -n "$SSH_ALLOWED_CIDRS" ]] || die "ENABLE_UFW=1 needs SSH_ALLOWED_CIDRS, or SSH access would be lost."
        [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || die "SSH_PORT must be a number."
        SSH_CIDR_LINES="$(normalize_cidrs "$SSH_ALLOWED_CIDRS" 0)" || die "SSH_ALLOWED_CIDRS rejected."
    fi
}

install_packages() {
    log "Installing system packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3.12-venv python3-pip git nginx apache2-utils sqlite3 openssl ufw curl
}

create_user_and_dirs() {
    log "Creating service user and directories"
    getent group "$SERVICE_USER" >/dev/null || groupadd --system "$SERVICE_USER"
    id -u "$SERVICE_USER" >/dev/null 2>&1 \
        || useradd --system --gid "$SERVICE_USER" --home-dir "$DATA_DIR" --no-create-home \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    install -d -o root -g "$SERVICE_USER" -m 0750 "$OPT_DIR" "$ETC_DIR"
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
        git clone --quiet --origin origin "$REPO_URL" "$APP_DIR"
    fi
    local commit
    if [[ -z "$GIT_REF" ]] && git -C "$APP_DIR" rev-parse --verify --quiet HEAD >/dev/null \
        && [[ -f "$APP_DIR/deploy/nginx-tsbench.conf.template" ]]; then
        commit="$(git -C "$APP_DIR" rev-parse HEAD)"   # a re-run keeps the installed code
    else
        commit="$(resolve_ref "${GIT_REF:-main}")"
    fi
    git -C "$APP_DIR" checkout --quiet --force --detach "$commit"
    fix_app_permissions
    [[ -f "$APP_DIR/deploy/nginx-tsbench.conf.template" ]] || die "Commit $commit has no deploy/ directory."
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

ensure_tls_cert() {
    install -d -o root -g root -m 0750 "$TLS_DIR"
    if [[ -f "$TLS_CERT" && -f "$TLS_KEY" ]]; then
        log "Keeping existing TLS certificate"
        return 0
    fi
    [[ ! -e "$TLS_CERT" && ! -e "$TLS_KEY" ]] || die "Only one of $TLS_CERT and $TLS_KEY exists. Fix or remove it, then run this again."
    log "Creating a self-signed TLS certificate for $SERVER_NAME"
    if ! (umask 077 && make_self_signed_cert "$SERVER_NAME" "$TLS_CERT" "$TLS_KEY"); then
        rm -f "$TLS_CERT" "$TLS_KEY"
        die "Could not create the TLS certificate."
    fi
    chmod 0644 "$TLS_CERT"
    chmod 0600 "$TLS_KEY"
}

configure_nginx() {
    log "Configuring Nginx"
    # Nginx workers read the password file on each login, so it must be readable by www-data.
    [[ -f "$HTPASSWD_FILE" ]] || : >"$HTPASSWD_FILE"
    chown root:www-data "$HTPASSWD_FILE"
    chmod 0640 "$HTPASSWD_FILE"

    local previous=""
    if [[ -f "$NGINX_SITE" ]]; then
        previous="$(mktemp)"
        cp -p "$NGINX_SITE" "$previous"
    fi
    render_nginx_conf "$APP_DIR/deploy/nginx-tsbench.conf.template" "$SERVER_NAME" "$CIDR_LINES" >"$NGINX_SITE"
    chmod 0644 "$NGINX_SITE"
    ln -sfn "$NGINX_SITE" "$NGINX_SITE_LINK"
    if ! nginx -t; then
        if [[ -n "$previous" ]]; then
            mv -f "$previous" "$NGINX_SITE"
        else
            rm -f "$NGINX_SITE" "$NGINX_SITE_LINK"
        fi
        die "The Nginx configuration failed its test; the previous site was restored."
    fi
    [[ -z "$previous" ]] || rm -f "$previous"
    rm -f /etc/nginx/sites-enabled/default
    systemctl enable --quiet nginx
    if systemctl is-active --quiet nginx; then
        systemctl reload nginx
    else
        systemctl start nginx
    fi
}

configure_firewall() {
    # Web ports from the allowed networks only. SSH is touched only when enabling the firewall here.
    local cidr
    while IFS= read -r cidr; do
        ufw allow from "$cidr" to any port 443 proto tcp >/dev/null
        ufw allow from "$cidr" to any port 80 proto tcp >/dev/null
    done <<<"$CIDR_LINES"
    if [[ "$ENABLE_UFW" == "1" ]]; then
        while IFS= read -r cidr; do
            ufw allow from "$cidr" to any port "$SSH_PORT" proto tcp >/dev/null
        done <<<"$SSH_CIDR_LINES"
        ufw --force enable >/dev/null
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
    local users ufw_state
    users="$(grep -c . "$HTPASSWD_FILE" || true)"
    ufw_state="$(ufw status | sed -n 1p)"
    cat <<EOF

--------------------------------------------------------------------------------
TradeSmart Latency Benchmark
  App code:      $APP_DIR ($(git -C "$APP_DIR" rev-parse --short HEAD))
  Config:        $ENV_FILE
  URL:           https://$SERVER_NAME/
  Allowed:       $(printf '%s' "$CIDR_LINES" | tr '\n' ' ')
  Service:       $SERVICE_STATE
  Login users:   $users
  Firewall:      $ufw_state
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
    if [[ -n "$MISSING" ]]; then
        printf '  %d. Start the app: sudo systemctl start tsbench\n' "$step"
        step=$((step + 1))
    fi
    printf '  %d. Check everything: sudo bash %s/deploy/healthcheck.sh\n' "$step" "$APP_DIR"
    step=$((step + 1))
    printf '  %d. Set TS_REDIRECT_URL to the redirect registered with TradeSmart, for example https://%s/callback\n' "$step" "$SERVER_NAME"
    step=$((step + 1))
    printf '  %d. As an admin, open Settings and set the static IP registered with TradeSmart.\n' "$step"
    if [[ "$ufw_state" != *" active"* ]]; then
        printf '\nThe firewall is off. Rules for 80 and 443 from the allowed networks are ready. Add an SSH rule first\n(sudo ufw allow from <admin-cidr> to any port 22 proto tcp), then run: sudo ufw enable\n'
    fi
    printf -- '--------------------------------------------------------------------------------\n'
}

main() {
    parse_args "$@"
    check_platform
    check_inputs
    install_packages
    create_user_and_dirs
    checkout_app
    install_python_env
    configure_env_file
    write_secret_snippet
    ensure_tls_cert
    configure_nginx
    configure_firewall
    install_systemd_units
    start_service
    print_summary
}

main "$@"
