#!/usr/bin/env bash
# Check a running install: sudo bash /vns-work/code/tsolc.tradesmartonline.in/deploy/healthcheck.sh
# Exit code 0 when every check passes, 1 otherwise. Prints no secrets and sends no credentials to TradeSmart.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

require_root

failures=0

report() {
    if [[ "$1" == "0" ]]; then
        printf '[PASS] %s\n' "$2"
    else
        printf '[FAIL] %s\n' "$2"
        failures=$((failures + 1))
    fi
}

equals() {
    [[ "$1" == "$2" ]]
}

# Ask for this site by name on this machine, so no other site on the server answers instead.
status_of() {
    curl -ks -o /dev/null --max-time 10 -w '%{http_code}' \
        --resolve "$SERVER_NAME:443:127.0.0.1" "$@"
}

systemctl is-active --quiet tsbench.service
report $? "Service tsbench is active"

systemctl is-active --quiet nginx
report $? "Service nginx is active"

health="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)"
equals "$health" ok
report $? "Streamlit health endpoint answers ok"

if [[ -e "$NGINX_SITE_LINK" ]]; then report 0 "Nginx site is enabled"; else report 1 "Nginx site is enabled"; fi

if grep -q CHANGE_ME "$NGINX_SITE" 2>/dev/null; then
    report 1 "Nginx site has no CHANGE_ME values left"
else
    report 0 "Nginx site has no CHANGE_ME values left"
fi

code="$(status_of "https://$SERVER_NAME/")"
equals "$code" 401
report $? "Nginx asks for a login (got $code, expected 401)"

code="$(status_of -H 'X-Remote-User: healthcheck' -H 'X-Proxy-Secret: healthcheck' "https://$SERVER_NAME/")"
equals "$code" 401
report $? "Nginx ignores forged identity headers (got $code, expected 401)"

printf '\nDeployment self-check (python -m src.doctor):\n'
run_as_service "$VENV_DIR/bin/python" -m src.doctor
report $? "Deployment self-check"

printf '\n'
if [[ "$failures" -gt 0 ]]; then
    printf '%d check(s) failed.\n' "$failures"
    exit 1
fi
printf 'All checks passed.\n'
