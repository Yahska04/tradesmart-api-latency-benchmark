#!/usr/bin/env bash
# Check a running install: sudo bash /opt/tsbench/app/deploy/healthcheck.sh
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

status_of() {
    curl -ks -o /dev/null --max-time 10 -w '%{http_code}' "$@"
}

systemctl is-active --quiet tsbench.service
report $? "Service tsbench is active"

systemctl is-active --quiet nginx
report $? "Service nginx is active"

health="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)"
equals "$health" ok
report $? "Streamlit health endpoint answers ok"

code="$(status_of https://127.0.0.1/)"
equals "$code" 401
report $? "Nginx asks for a login (got $code, expected 401)"

code="$(status_of -H 'X-Remote-User: healthcheck' -H 'X-Proxy-Secret: healthcheck' https://127.0.0.1/)"
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
