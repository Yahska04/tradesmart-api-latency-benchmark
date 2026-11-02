# TradeSmart API Latency Benchmark: Deployment and Handover Guide

**Audience:** the Linux system team. No knowledge of the app is assumed.

**Who does what.** Throughout this guide:

- **[SYSTEM TEAM]** marks work for the system team.
- **[OWNER]** marks information or actions only the app owner can supply.
- **[APP ADMIN]** marks steps done in the app's web pages by a user listed in `TS_ADMIN_USERS`.

Replace everything in `<angle brackets>` with your own value. Never put real credentials in tickets, chat or this file.

**Contents**

1. [What this is](#1-what-this-is)
2. [How the static IP works](#2-how-the-static-ip-works)
3. [Prerequisites](#3-prerequisites)
4. [Network and firewall](#4-network-and-firewall)
5. [Install from GitHub](#5-install-from-github)
6. [First-time app setup](#6-first-time-app-setup)
7. [Roles and users](#7-roles-and-users)
8. [Operations](#8-operations)
9. [Security notes](#9-security-notes)
10. [Troubleshooting](#10-troubleshooting)
11. [Handover acceptance](#11-handover-acceptance)
12. [File reference](#12-file-reference)

---

## 1. What this is

A Streamlit web app for internal use. It measures how long TradeSmart API V2 requests take as seen from the server,
stores every sample in SQLite, and reports P50, P95 and P99 per endpoint.

Automated benchmarks and endpoint checks only send read-only requests. One opt-in page, **Order latency test**,
places **real orders on a live trading account** and cancels them straight away. Its limits: BUY LIMIT, quantity 1,
priced below the market, at most ₹5,000 per order and 1,000 orders per IST day, restricted to named users.

All users share one TradeSmart account. Only one benchmark or order batch can run at a time.

```text
 User's browser (Chrome, Edge or Firefox)
        |
        |  office network or VPN only
        v
 +----------------------- Ubuntu 24.04 server -----------------------+
 |                                                                   |
 |  Nginx :443 (TLS, username + password)                            |
 |        |   sets X-Remote-User and X-Proxy-Secret                  |
 |        v                                                          |
 |  Streamlit 127.0.0.1:8501 (service user tsbench)                  |
 |        |                                                          |
 |        |  SQLite and token file in /var/lib/tsbench               |
 +--------|----------------------------------------------------------+
          |  HTTPS / WSS started by the server
          v
 Company NAT / firewall: leaves through the registered static public IP
          |
          v
 TradeSmart / Kambala: v2api.tradesmartonline.in
```

---

## 2. How the static IP works

**Requirement.** The owner has a static IP registered with TradeSmart. TradeSmart's system must see that address on
this app's traffic. This guide makes no claim about TradeSmart's or any regulator's rules. Questions about what must
be registered, and when, go to the owner and TradeSmart.

**What TradeSmart sees.**

| Traffic | Starts from | Address TradeSmart / Kambala sees |
|---|---|---|
| REST API calls | Server | Server's public outbound IP |
| WebSocket | Server | Server's public outbound IP |
| Script master downloads | Server | Server's public outbound IP |
| Token exchange (`GenAcsTok`) | Server | Server's public outbound IP |
| **TradeSmart OAuth login page** | **User's browser** | **The user's device or office IP** |

- A user's device does not change the address on API traffic. Users talk only to Nginx; the server makes the API calls.
- The IP is the network source address. It is not a field in any request, and the app sends no forwarding headers.
- The OAuth login page is the single exception, because the admin opens it in their own browser. That step shows
  the device's IP. No other step does.

**What the network must do.** The server's outbound traffic to `v2api.tradesmartonline.in` must leave through the
registered static IP. Use one of these:

| Setup | What to configure |
|---|---|
| Server behind NAT (the usual case) | A NAT/SNAT rule on the company firewall maps this server's outbound traffic to the static IP. Leave `TS_OUTBOUND_BIND_IP` empty. |
| Server with several interfaces, with the static IP routed to one of them | Set `TS_OUTBOUND_BIND_IP` to the **local address assigned to that interface**. It must appear in `ip -brief addr`. If the public IP sits on a NAT device, this setting cannot use it; connections fail with "Cannot assign requested address". |

Do not set `HTTP_PROXY` or `HTTPS_PROXY` in the service environment unless that proxy's public address is the
registered IP. A proxy changes the source address that TradeSmart sees, and it also changes the measured latency.

**How the app verifies it.** The app asks an IP-check service what address its traffic comes from (`TS_IP_CHECK_URL`,
default `https://api.ipify.org`) and compares the answer with the configured IP. The configured IP is the value an
admin saved in Settings → Server IP, or `TS_STATIC_IP` if none was saved.

- Orders are refused if the IP is not set, cannot be detected, or does not match. This is on by default in server mode
  (`TS_ENFORCE_STATIC_IP`). Order batches run their own fresh check; the status shown on pages refreshes about every 5 minutes.
- Read-only requests only show a warning.

**Limitation of this check.** It proves where traffic to the *IP-check service* comes from, not traffic to TradeSmart.
With policy routing, the two could leave differently, so the check could pass while TradeSmart sees another address.
The app cannot ask TradeSmart what address it saw.

**Verify on the server. [SYSTEM TEAM]**

```bash
# The public IP seen by an outside service. It must equal the registered static IP.
curl -4 -s https://api.ipify.org; echo

# With TS_OUTBOUND_BIND_IP set, test from that address.
curl -4 -s --interface <local-bind-ip> https://api.ipify.org; echo

# Does TradeSmart's hostname publish IPv6 addresses, and does the server have global IPv6?
getent ahosts v2api.tradesmartonline.in
ip -6 route show default
```

If both of the last two commands show IPv6, API traffic may leave over IPv6 while the IP check uses IPv4. Ask the network
team to resolve this before go-live.

**If the network team uses policy routing,** they must confirm that both destinations use the same NAT rule:

```bash
ip route get "$(getent ahostsv4 v2api.tradesmartonline.in | awk 'NR==1{print $1}')"
ip route get "$(getent ahostsv4 api.ipify.org | awk 'NR==1{print $1}')"
```

These commands show only the route on this server. The network team must also confirm the firewall's NAT rule covers
`v2api.tradesmartonline.in` and `api.ipify.org`.

---

## 3. Prerequisites

| Item | Requirement | Provided by |
|---|---|---|
| Server | Suggested: 2 vCPU, 4 GB RAM, 20 GB disk. This is a suggestion, not a measured requirement. | System team |
| Operating system | Ubuntu 24.04 with Python 3.12 | System team |
| Static public outbound IP | See section 2. The value may arrive after install; orders stay blocked until it is set. | System team / network team, registered with TradeSmart by the owner |
| Internal DNS / hostname | A name for the server, reachable from the office network and VPN (`SERVER_NAME`) | System team |
| TLS certificate | Internal CA certificate preferred. A self-signed one is created by default. | System team |
| Allowed networks | Office and VPN CIDRs (`ALLOWED_CIDRS`) | Owner and system team |
| GitHub access | Read-only deploy key added to the private repo | Key generated by system team, added by owner |
| TradeSmart App Key | Value for `TS_APP_KEY` (secret) | Owner |
| TradeSmart Secret Key | Value for `TS_SECRET_KEY` (secret) | Owner |
| TradeSmart Client ID | Value for `TS_CLIENT_ID` (secret) | Owner |
| Redirect URL | Registered in the TradeSmart API app; value for `TS_REDIRECT_URL` (section 6.2) | Owner registers it; system team supplies the address |
| Admin usernames | Value for `TS_ADMIN_USERS` | Owner |
| Order usernames | Value for `TS_ORDER_USERS` | Owner |
| Login accounts | Created with `manage-user.sh` | System team |

---

## 4. Network and firewall

### Inbound

| Port | Protocol | From | Purpose |
|---|---|---|---|
| 443 | TCP | Office and VPN CIDRs | The app (HTTPS and WebSocket through Nginx) |
| 80 | TCP | Office and VPN CIDRs | OAuth `/callback` and redirect to HTTPS |
| 22 | TCP | Admin hosts only | SSH |

Port 8501 is not opened. Streamlit listens on 127.0.0.1 only.

Nginx also applies `ALLOWED_CIDRS` on port 443. On port 80 it has no allow-list, because port 80 only sends redirects, so
the firewall is what limits port 80. IPv4 only: Nginx does not listen on IPv6.

### Outbound

| Destination | Port | When | Purpose |
|---|---|---|---|
| `v2api.tradesmartonline.in` | 443 | Always | REST, WebSocket (WSS), script master, token exchange. **Must leave through the static IP.** |
| `api.ipify.org` (or your `TS_IP_CHECK_URL`) | 443 | Always | Outbound IP check. If blocked, orders are refused. |
| `github.com` | 22 | Install and update | Code download with the deploy key |
| `pypi.org`, `files.pythonhosted.org` | 443 | Install and update | Python packages |
| Ubuntu mirrors (`archive.ubuntu.com`, `security.ubuntu.com` or your internal mirror) | 80, 443 | Install and patching | OS packages |
| DNS and NTP servers | 53, 123 | Always | Name lookup and correct time |

UFW filters by IP address, and these hostnames can change address. If you need to filter outbound traffic by name, do
it on the company firewall or proxy.

### UFW commands [SYSTEM TEAM]

Allow SSH before enabling UFW, or you may lock yourself out. Repeat the `443` and `80` lines for each office or VPN CIDR.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from <admin-cidr> to any port 22 proto tcp
sudo ufw allow from <office-or-vpn-cidr> to any port 443 proto tcp
sudo ufw allow from <office-or-vpn-cidr> to any port 80 proto tcp
sudo ufw enable
sudo ufw status numbered
```

`install.sh` adds the `443` and `80` rules for `ALLOWED_CIDRS` itself, but it never adds an SSH rule and never turns UFW on
unless you pass `ENABLE_UFW=1` with `SSH_ALLOWED_CIDRS`.

---

## 5. Install from GitHub

### Server layout

| Item | Path |
|---|---|
| Service user and group | `tsbench` |
| Application | `/opt/tsbench/app` |
| Python virtual environment | `/opt/tsbench/venv` |
| Configuration | `/etc/tsbench/tsbench.env` (root:tsbench, 0640) |
| Data | `/var/lib/tsbench` (`benchmark.db`, `scriptmaster/`, `.access_token`, `api_run.lock`) |
| Logs | `/var/log/tsbench` |
| Backups | `/var/backups/tsbench` |
| Nginx site | `/etc/nginx/sites-available/tsbench` |
| Nginx password file | `/etc/nginx/tsbench.htpasswd` |
| Nginx proxy secret | `/etc/nginx/tsbench-proxy-secret.conf` |
| TLS certificate and key | `/etc/nginx/ssl/tsbench.crt`, `/etc/nginx/ssl/tsbench.key` |

### 5.1 Create the deploy key [SYSTEM TEAM]

```bash
sudo apt-get update
sudo apt-get install -y git
sudo mkdir -p -m 700 /root/.ssh
sudo ssh-keygen -t ed25519 -N "" -C "tsbench-deploy@$(hostname)" -f /root/.ssh/tsbench_deploy_key
sudo tee -a /root/.ssh/config >/dev/null <<'EOF'
Host github.com
    IdentityFile /root/.ssh/tsbench_deploy_key
    IdentitiesOnly yes
EOF
sudo chmod 600 /root/.ssh/config
sudo cat /root/.ssh/tsbench_deploy_key.pub
```

The kit's scripts look for the key at exactly this path. They use it for every later update.

Send the printed **public** key line to the owner. Never send the file without `.pub`.

### 5.2 Add the key to GitHub [OWNER]

1. Open `Yahska04/tradesmart-api-latency-benchmark` on GitHub.
2. Go to **Settings → Deploy keys → Add deploy key**.
3. Title it with the server name and paste the public key.
4. Leave **Allow write access** unticked.
5. Click **Add key**.

### 5.3 Test the key [SYSTEM TEAM]

Accept the host key only after comparing its fingerprint with the SSH fingerprints GitHub publishes in its documentation.

```bash
sudo ssh -T git@github.com
```

The expected reply says you authenticated successfully but that GitHub does not provide shell access. Its exit code is 1,
which is normal here.

### 5.4 Run the installer [SYSTEM TEAM]

```bash
sudo git clone git@github.com:Yahska04/tradesmart-api-latency-benchmark.git /root/tsbench-bootstrap
sudo bash /root/tsbench-bootstrap/deploy/install.sh SERVER_NAME=<hostname> ALLOWED_CIDRS=<cidr1>,<cidr2>
sudo rm -rf /root/tsbench-bootstrap
```

| Input | Required | Meaning |
|---|---|---|
| `SERVER_NAME` | Yes | DNS name or IP address users open |
| `ALLOWED_CIDRS` | Yes | Comma-separated IPv4 office and VPN networks. `0.0.0.0/0` is refused. |
| `GIT_REF` | No | Branch, tag or commit. First install defaults to `main`. A re-run keeps the installed code unless this is given. |
| `REPO_URL` | No | Repository URL. Default: the repo above |
| `DEPLOY_KEY` | No | Path of the GitHub key. Default: `/root/.ssh/tsbench_deploy_key` |
| `ENABLE_UFW` | No | `1` also turns UFW on, and then requires `SSH_ALLOWED_CIDRS`. Default: off. Not needed if you followed section 4. |
| `SSH_ALLOWED_CIDRS` | With `ENABLE_UFW=1` | Comma-separated IPv4 networks allowed to SSH in, so you can't be locked out |
| `SSH_PORT` | No | SSH port for that rule. Default: `22` |

The installer installs packages, creates the user and folders, clones the code to `/opt/tsbench/app`, and installs Python
packages from the hash-checked `requirements.lock`. It creates the env file and proxy secret, a self-signed certificate,
the Nginx site and the systemd units, and it enables the backup timer. It keeps existing configuration, secrets,
certificates, logins and data.

The first run leaves the service stopped, because the settings are still blank. Its summary lists what's missing, which is normal.

If GitHub refuses the key, the installer prints a public key and exits with code 2. Add that key (step 5.2) and run it again.

### 5.5 Check the firewall rules [SYSTEM TEAM]

```bash
sudo ufw status numbered
```

No rule for 22, 80 or 443 should show `Anywhere` in the **From** column. Make sure SSH is allowed from your admin network
before enabling UFW (section 4).

### 5.6 Fill in the configuration [SYSTEM TEAM with values from OWNER]

```bash
sudo nano /etc/tsbench/tsbench.env
```

| Variable | Value | Supplied by |
|---|---|---|
| `TS_DEPLOYMENT` | `server` | Leave as set by the template |
| `TS_ENVIRONMENT` | `live` | Leave as set by the template |
| `TS_APP_KEY`, `TS_SECRET_KEY`, `TS_CLIENT_ID` | From the TradeSmart API app | Owner |
| `TS_REDIRECT_URL` | `http://<server-ip>/callback` (section 6.2) | System team and owner |
| `TS_ADMIN_USERS` | Comma-separated login names, for example `alice,bob` | Owner |
| `TS_ORDER_USERS` | Comma-separated login names | Owner |
| `TS_AUTH_PROXY_SECRET` | Generated by `install.sh`, which copies it to Nginx | Leave as is (to change it, see section 9) |
| `TS_ENFORCE_STATIC_IP` | `1` | Leave as set by the template |
| `TS_DB_PATH` | `/var/lib/tsbench/benchmark.db` | Leave as set by the template |
| `TS_LOG_PATH` | `/var/log/tsbench/app.log` | Leave as set by the template |
| `TS_TOKEN_FILE` | `/var/lib/tsbench/.access_token` | Leave as set by the template |
| `TS_STATIC_IP` | Leave empty; an admin enters it in Settings | App admin |
| `TS_OUTBOUND_BIND_IP` | Only for a multi-interface server (section 2) | Network team |

Use `KEY=value` on one line, with no spaces around `=`. `.env.example` in the repo explains each variable.

`TS_APP_KEY`, `TS_SECRET_KEY`, `TS_CLIENT_ID`, `TS_AUTH_PROXY_SECRET` and `TS_ADMIN_USERS` must all be set, or the kit's
scripts won't start or restart the service.

### 5.7 Create login accounts [SYSTEM TEAM]

Run this once per user. It asks for the password, and also changes the password of an existing user. Use exactly the names
listed in `TS_ADMIN_USERS` and `TS_ORDER_USERS`. Names may use letters, digits, dot, underscore and dash.

```bash
sudo bash /opt/tsbench/app/deploy/manage-user.sh add <username>
sudo bash /opt/tsbench/app/deploy/manage-user.sh list
```

### 5.8 Start the service and check it [SYSTEM TEAM]

```bash
sudo systemctl start tsbench
sudo systemctl status tsbench --no-pager
sudo bash /opt/tsbench/app/deploy/healthcheck.sh
```

`healthcheck.sh` checks both services and the health endpoint, confirms Nginx asks for a login and ignores forged identity
headers, and runs the self-check below. It exits with 0 only if everything passes.

To run the self-check alone, use the service's user and environment. It sends no API requests and no credentials, and it
never prints a secret. Use this exact form:

```bash
sudo systemd-run --pipe --wait --collect -p User=tsbench -p EnvironmentFile=/etc/tsbench/tsbench.env -p WorkingDirectory=/opt/tsbench/app /opt/tsbench/venv/bin/python -m src.doctor
```

Before first-time app setup, these two results are expected:

- `[WARN] Access token: none yet`
- `[WARN] Outbound IP vs static IP` (the IP has not been set yet)

Every other line should be `PASS`. A `FAIL` must be fixed before handover.

### 5.9 Replace the self-signed certificate [SYSTEM TEAM]

Skip this only if the company accepts the self-signed certificate (see section 9). Put the server certificate first
in the `.crt` file, followed by any intermediate certificates.

```bash
sudo install -o root -g root -m 0644 <your-fullchain.crt> /etc/nginx/ssl/tsbench.crt
sudo install -o root -g root -m 0600 <your-private.key> /etc/nginx/ssl/tsbench.key
sudo nginx -t
sudo systemctl reload nginx
```

---

## 6. First-time app setup

Do this in order. The token step needs the redirect URL registered first. The order test needs market hours.

### 6.1 Log in and enter the server IP [APP ADMIN]

1. Open `https://<hostname>/` from the office network or VPN, and log in with your username and password.
2. Go to **Settings → Server IP**.
3. Enter the registered static public IP in **Server static IP** and click **Save IP**.
4. Confirm **Configured IP** and **Detected IP** match. Click **Check now** to re-test; otherwise the status refreshes about
   every 5 minutes. If they don't match, see [Troubleshooting](#10-troubleshooting).

### 6.2 Register the redirect URL [OWNER]

Once the IP is saved, the same page shows the URL to register. Register it in the TradeSmart API app:

```text
http://<server-ip>/callback
```

Set `TS_REDIRECT_URL` in `/etc/tsbench/tsbench.env` to the same URL, then restart the service.

**What happens:** after login, TradeSmart sends the browser to `http://<server-ip>/callback?code=<one-time code>`. Nginx
on port 80 redirects that to `https://<server-ip>/Settings?code=<one-time code>`. If you're logged in as an admin, Settings shows **TradeSmart sent a
login code**.

**This only works if the admin's browser can reach that address.**

- The page shows the registered **public** IP. The browser must be able to reach ports 80 and 443 on it, which needs an inbound
  NAT rule from the office/VPN. The source address that Nginx then sees must be in `ALLOWED_CIDRS`. Agree this with the
  network team. If it isn't allowed, use the paste fallback below; it doesn't need any inbound access on that address.
- Opening the app at that address counts as a separate site in the browser. Expect another login prompt, and a certificate
  warning unless the certificate also covers that IP. The token is shared, so it doesn't matter which address the admin used.
- The code travels once over plain HTTP between the browser and Nginx. It works only once, and exchanging it needs the
  Secret Key, which stays on the server.
- The documentation we have doesn't say whether TradeSmart accepts `http://` URLs or IP addresses here. If registration is
  refused, ask TradeSmart what it accepts. `https://<hostname>/callback` also reaches Settings.

### 6.3 Generate the access token [APP ADMIN]

1. Go to **Settings → Get an access token**.
2. Click **1 · Open TradeSmart login**. The login page opens in a new tab in your browser. This step shows your device's IP
   (section 2).
3. Log in to TradeSmart. You return to Settings with the **TradeSmart sent a login code** box.
4. Click **Get access token**. The app shows **Access token saved. All users now share it.**

**Paste fallback.** If you don't land on Settings, even if the browser shows a connection error, copy the full address from
the browser bar. Then go back to **Settings → Get an access token**, paste it into **2 · Paste the redirect URL (or just the code)**,
and click **Get access token**. A code works once and expires quickly. If the exchange fails, log in again to get a new code.

The token is saved in `/var/lib/tsbench/.access_token` (0600) and is shared by all users. It survives restarts.

**Token expiry.** Expect tokens to expire. The exact lifetime isn't stated in the documentation available to us. When
API calls report session errors, an admin generates a new token with the same steps.

### 6.4 Run the self-check [SYSTEM TEAM]

```bash
sudo systemd-run --pipe --wait --collect -p User=tsbench -p EnvironmentFile=/etc/tsbench/tsbench.env -p WorkingDirectory=/opt/tsbench/app /opt/tsbench/venv/bin/python -m src.doctor
```

All lines should now be `PASS`, including `Outbound IP matches static IP`. A `WARN` on order users is acceptable only if
no order users are wanted yet.

### 6.5 Run a small read-only benchmark [APP ADMIN]

1. Open **Run Benchmark**.
2. Select `UserDetails` and `Limits`.
3. Set iterations to 20, warm-up to 2, delay to 500 ms, with warm connections.
4. Review the plan, then click **Start benchmark**.
5. Open **Performance Analytics** and confirm the requests succeeded.

### 6.6 Run the first small live order test [OWNER or order user]

**This places real orders on the live account.** Do it during market hours (IST 09:15–15:30), with the owner present.

1. Open **Order latency test**. Only users in `TS_ORDER_USERS` can use it.
2. Search for a liquid NSE share priced so that one share is under ₹5,000.
3. Enter **16** orders. At 8 orders per burst, that makes **2 bursts**. Keep the default offset (3% below the market) and
   the default delay (3 s).
4. Type the confirmation exactly as shown: `PLACE 16 ORDERS`.
5. Wait for the batch to finish. There should be no red alert.
6. In TradeSmart's order book, confirm that all 16 orders show as cancelled and that none filled.

If the batch stops with a red alert, read the order number it names and check that order in TradeSmart.

---

## 7. Roles and users

| Role | How it's set | Can do | Cannot do |
|---|---|---|---|
| Any logged-in user | Account in `/etc/nginx/tsbench.htpasswd` | View dashboards and history, run read-only benchmarks, endpoint checks and the WebSocket benchmark | Open Settings, edit the endpoint registry, load demo data, place orders |
| Order user | Also listed in `TS_ORDER_USERS` | All of the above, plus the Order latency test and its open-order check | Open Settings, unless also an admin |
| Admin | Also listed in `TS_ADMIN_USERS` | Everything a logged-in user can do, plus Settings (server IP, access token, script master, data), endpoint registry edits and demo data | Place orders, unless also an order user |

The sidebar lists Settings and Order latency test for everyone. Those pages refuse users without the role.

The admin and order lists are separate. Put a person in both lists if they need both roles. List names are not
case-sensitive, but Nginx logins are, so use one spelling everywhere.

### Add a user [SYSTEM TEAM]

```bash
sudo bash /opt/tsbench/app/deploy/manage-user.sh add <username>
```

To give a role, add the name to `TS_ADMIN_USERS` or `TS_ORDER_USERS` in `/etc/tsbench/tsbench.env`. Then check that no run is
active ([section 8.9](#89-before-a-planned-restart-or-reboot)) and restart:

```bash
sudo systemctl restart tsbench
```

### Remove a user [SYSTEM TEAM]

```bash
sudo bash /opt/tsbench/app/deploy/manage-user.sh remove <username>
```

Also remove the name from both role lists. Check that no run is active, then restart the service. The restart also ends any
session the person still has open.

### List users [SYSTEM TEAM]

```bash
sudo bash /opt/tsbench/app/deploy/manage-user.sh list
```

---

## 8. Operations

### 8.1 Service commands

Check that no run is active before `stop` or `restart` ([section 8.9](#89-before-a-planned-restart-or-reboot)).

```bash
sudo systemctl status tsbench --no-pager
sudo systemctl stop tsbench
sudo systemctl start tsbench
sudo systemctl restart tsbench
```

### 8.2 Logs

```bash
sudo journalctl -u tsbench -f
sudo journalctl -u tsbench --since "1 hour ago" --no-pager
sudo tail -f /var/log/tsbench/app.log
sudo tail -f /var/log/nginx/error.log
```

Secrets are masked in the app's logs.

**Rotation.** The app rotates `/var/log/tsbench/app.log` itself at 2 MB and keeps 3 old files. `journald` rotates the service
journal under the server's own settings.

### 8.3 Update

```bash
sudo bash /opt/tsbench/app/deploy/update.sh
sudo bash /opt/tsbench/app/deploy/update.sh <branch-tag-or-commit>
sudo bash /opt/tsbench/app/deploy/healthcheck.sh
```

`update.sh` takes a database backup first, installs the code and packages, runs the self-check and restarts the service. If the new
version crashes the self-check or doesn't come up healthy, it rolls back by itself.

| Exit code | Meaning |
|---|---|
| 0 | Updated |
| 1 | Update failed; the previous version is running again, or nothing changed |
| 2 | Update and rollback both failed; check `journalctl -u tsbench` |
| 3 | A run is active, so nothing was changed. Try again when it finishes. |

`FORCE=1` skips the run check and **kills a running batch**, and it also discards local changes. Avoid it during market hours.

If the output says the Nginx template changed, re-apply it (section 8.5).

### 8.4 Roll back

```bash
sudo bash /opt/tsbench/app/deploy/update.sh rollback
sudo bash /opt/tsbench/app/deploy/healthcheck.sh
```

This returns to the commit before the last update. It restores code and Python packages only. If the newer version
changed the database, also restore the backup taken just before the update (section 8.6).

### 8.5 Change allowed networks, or re-apply the Nginx config

`/etc/nginx/sites-available/tsbench` is generated. Don't edit it by hand. Run the installer again, with the full list of networks.
It keeps the installed code; use `update.sh` to move to newer code.

```bash
sudo bash /opt/tsbench/app/deploy/install.sh SERVER_NAME=<hostname> ALLOWED_CIDRS=<cidr1>,<cidr2>
```

This also restarts the service unless a run is active. It adds firewall rules for the networks you list but doesn't remove
old ones, so delete rules for any network you took away (`sudo ufw status numbered`, then `sudo ufw delete <number>`).

### 8.6 Backups and restore

`tsbench-backup.timer` backs up the database daily at 19:30 IST to `/var/backups/tsbench/benchmark-YYYYMMDD-HHMM.db.gz` and deletes
backups older than 30 days. It's safe while the app is running. The token file and `/etc/tsbench/tsbench.env` aren't included,
so back up the env file separately in your secrets store. Copy backups off the server if you need them to survive its loss.

```bash
sudo systemctl list-timers tsbench-backup.timer --no-pager
sudo systemctl start tsbench-backup
sudo journalctl -u tsbench-backup -n 5 --no-pager
sudo ls -lt /var/backups/tsbench | head
```

**Restore:**

```bash
sudo systemctl stop tsbench
sudo -u tsbench cp -p /var/lib/tsbench/benchmark.db /var/lib/tsbench/benchmark.db.before-restore
sudo rm -f /var/lib/tsbench/benchmark.db-wal /var/lib/tsbench/benchmark.db-shm
sudo bash -c 'gunzip -c /var/backups/tsbench/<backup-file>.db.gz > /var/lib/tsbench/benchmark.db'
sudo chown tsbench:tsbench /var/lib/tsbench/benchmark.db
sudo chmod 0640 /var/lib/tsbench/benchmark.db
sudo -u tsbench sqlite3 /var/lib/tsbench/benchmark.db "PRAGMA integrity_check;"
sudo systemctl start tsbench
sudo bash /opt/tsbench/app/deploy/healthcheck.sh
```

The integrity check must print `ok`. The restore also brings back the server IP saved at backup time, so confirm it in Settings.

**After a restore, check the TradeSmart order book.** A restored database doesn't know about test orders or results
saved after the backup. That has two effects:

- The startup check may miss journalled orders. It still finds open orders tagged `latency-test` in today's order book.
- The daily order count can read low for that day, so don't rely on the cap until the next IST day.

### 8.7 Monitoring suggestions

| Check | Command or target | Healthy result |
|---|---|---|
| App process | `curl -fsS http://127.0.0.1:8501/_stcore/health` (on the server) | `ok` |
| Full stack | `sudo bash /opt/tsbench/app/deploy/healthcheck.sh` | Exit code 0 |
| Service state | `systemctl is-active tsbench` | `active` |
| Certificate | `sudo openssl x509 -in /etc/nginx/ssl/tsbench.crt -noout -enddate` | Date is well in the future |
| Disk | `df -h /var/lib/tsbench /var/backups/tsbench` | Space available |
| Backup timer | `systemctl list-timers tsbench-backup.timer` | Next run listed |

`healthcheck.sh` also fails if the self-check reports a `FAIL`, including a static IP mismatch or an unconfirmed test order. Treat
those alerts as urgent.

### 8.8 App behaviours to know

**Daily order cap.** At most 1,000 PlaceOrder attempts per IST day, across all users. It is counted from saved results, and it
resets by itself at midnight IST. There's no manual reset. Don't edit the database to change it.

**Script master (instrument lists).** The app downloads public lists from `https://v2api.tradesmartonline.in/<EXCH>_symbols.txt.zip` for
NSE, NFO, BSE, BFO, BCD, CDS and MCX, without credentials, into `/var/lib/tsbench/scriptmaster/`.

- The download runs in the background once a page is opened after a start, and is re-checked hourly while the app is in use.
  Each exchange downloads at most once per IST day.
- A failed exchange keeps its previous list and is tried again at the next check.
- An admin can force it at **Settings → Script master → Refresh script master**.

**One run at a time.** A server-wide lock (`/var/lib/tsbench/api_run.lock`) allows one benchmark or order batch at a time. The OS
releases it if the process dies. Never delete this file.

**Restart and open-order recovery.**

- On a clean stop, the app stops placing orders and gives the running burst up to 25 seconds to cancel what it placed.
  systemd allows 60 seconds before it kills the process.
- Each start runs `python -m src.recover_orders` first (`ExecStartPre` in the unit). It looks for test orders left open by a
  crash or power cut, and cancels them. It touches only orders in the app's own journal or tagged `latency-test`.
  It never cancels any other order.
- **The service starts even if this check fails or reports a problem** (the unit uses `ExecStartPre=-`). A running service
  therefore doesn't prove the check was clean. Read the journal after every restart (section 8.9).
- New order batches are refused while any test order is unconfirmed, and after a placement that got no reply, until the
  open-order check has read the order book.
- Finding an order that got no reply relies on the order book returning the `remarks` tag. The API documentation doesn't
  list that field for certain, so **confirm it during the first live test** (section 11) and check the order book by hand
  after any no-reply alert.

**When `ATTENTION` appears** (in `journalctl -u tsbench`, or when you run the command below):

| Message | Meaning | What to do |
|---|---|---|
| `ATTENTION: N test order(s) unconfirmed, but no access token is available` | Orders could not be checked | Check the order book now. An admin generates a token, then run the command below. |
| `ATTENTION: check order <number> in TradeSmart` | The order filled, or could not be confirmed closed. Expired tokens and network failures also cause this. | Tell the owner at once. Check the order in TradeSmart and cancel it by hand if it's open. |
| A summary showing `FILLED` above 0 | A real position now exists on the account. Each fill is reported only once. | Save the order number. The owner decides what to do with the position. The app doesn't close positions. |

Check again after the fix. An order user can click through **Order latency test → Open test orders**, or you can run this
with the service's environment:

```bash
sudo systemd-run --pipe --wait --collect -p User=tsbench -p EnvironmentFile=/etc/tsbench/tsbench.env -p WorkingDirectory=/opt/tsbench/app /opt/tsbench/venv/bin/python -m src.recover_orders
```

Exit code 0 means clean or nothing to do. Exit code 1 means someone must look at the printed lines. If it prints `Skipped`, a run holds the
lock; try again when it ends.

### 8.9 Before a planned restart or reboot

Do this before `systemctl restart`, OS patching or a reboot. `update.sh` checks by itself, but a reboot won't:

```bash
sudo -u tsbench flock -n /var/lib/tsbench/api_run.lock true && echo "No run active" || echo "A run is active"
sudo cat /var/lib/tsbench/api_run.lock.info 2>/dev/null
```

If a run is active, the second line shows who started it. Wait for the run to end or ask that person to press **Stop**.
Prefer outside market hours (IST 09:15–15:30) for planned work. After the restart, check the journal for `ATTENTION`:

```bash
sudo journalctl -u tsbench -b --no-pager | grep -E "ATTENTION|Open-order check"
```

---

## 9. Security notes

**Secrets and permissions.**

| Secret | Location | Owner and mode |
|---|---|---|
| TradeSmart keys, client ID, proxy secret | `/etc/tsbench/tsbench.env` | root:tsbench, 0640 |
| Shared access token | `/var/lib/tsbench/.access_token` | tsbench, 0600 |
| Proxy secret (Nginx side) | `/etc/nginx/tsbench-proxy-secret.conf` | root:root, 0640 |
| User passwords (bcrypt hashes) | `/etc/nginx/tsbench.htpasswd` | root:www-data, 0640 |
| TLS private key | `/etc/nginx/ssl/tsbench.key` | root, 0600 |
| GitHub deploy key | `/root/.ssh/tsbench_deploy_key` | root, 0600 |

Never commit `.env` or any file holding real values. `.gitignore` excludes `.env`. Database backups hold no token or keys,
but they do hold test results and order numbers, so keep them restricted (`/var/backups/tsbench` is tsbench, 0750).

**Proxy secret.** Streamlit trusts `X-Remote-User` only if `X-Proxy-Secret` matches `TS_AUTH_PROXY_SECRET`. This stops
any other process on the server from posing as a user by calling port 8501 directly. If the secret is missing or
wrong, the app denies access.

**Identity headers.** Nginx overwrites `X-Remote-User` and `X-Proxy-Secret` on every request, so a value sent by the browser
is discarded. `healthcheck.sh` tests this. Nginx also strips the password (`Authorization`) before passing requests to the app. If you
change the Nginx template, keep this behaviour.

**Network exposure.** Streamlit binds to 127.0.0.1:8501. Don't change that or open the port.

**Self-signed certificate.** Browsers show a warning, and users must accept it. This trains people to click through warnings and
doesn't protect against interception until the certificate is trusted. Prefer an internal CA certificate.

**Basic auth.**

- The browser sends the password with each request, so it relies on TLS. Never serve the app over plain HTTP.
- There's no logout. The browser remembers the login until it closes.
- There's no lockout or multi-factor login. Nginx rate-limits requests per client IP (10 per second, burst 200), which only slows
  guessing. Rely on the network restriction, and consider `fail2ban` on the Nginx log.
- **Safari** may not send basic-auth credentials on WebSocket connections, which would stop the app from loading. Recommend
  Chrome, Edge or Firefox. If Safari matters to you, verify it during acceptance.

**Rotating the proxy secret.** Generate a value with `openssl rand -hex 32` and put it in `TS_AUTH_PROXY_SECRET` in
`/etc/tsbench/tsbench.env`. Then run the installer command in section 8.5, which copies it to Nginx and restarts the service.

**OAuth callback.** TradeSmart's documented login has no `state` value, so the app can't prove a `/Settings?code=` link started from
its own login button. That's why it never exchanges a code automatically: an admin must press **Get access token**, and should do so only
right after starting a login themselves.

**Moving to SSO later.** The app only reads the two headers, so no code change is needed. Put `oauth2-proxy` (or similar)
in front through Nginx `auth_request`, set `X-Remote-User` from the SSO user name, and keep sending `X-Proxy-Secret`. Update
`TS_ADMIN_USERS` and `TS_ORDER_USERS` to the SSO names.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Login box keeps coming back (401 loop) | Wrong password, user not in the password file, or Nginx can't read the file | `sudo bash /opt/tsbench/app/deploy/manage-user.sh list`. Reset the password. Check `/var/log/nginx/error.log`. |
| 403 from Nginx | Client address is outside `ALLOWED_CIDRS` | Check the client's VPN. Change the allowed networks with the command in section 8.5. |
| 429 from Nginx | Request rate limit reached, or many users share one source address | Wait and reload. If it recurs, raise the limit in `deploy/nginx-tsbench.conf.template` and re-apply it (section 8.5). |
| "Access denied" and "did not come through the login proxy" | `X-Proxy-Secret` doesn't match `TS_AUTH_PROXY_SECRET`, or someone is calling port 8501 directly | Re-run the command in section 8.5, which copies the env value to Nginx. |
| "Access denied", `TS_AUTH_PROXY_SECRET` not configured | The variable is empty in the env file | Re-run the command in section 8.5, which generates one. |
| "The login proxy did not identify a user" | Nginx isn't sending `X-Remote-User` | Re-apply the kit's Nginx site (section 8.5). |
| "Connection error" banner, or the page spins forever | WebSocket `/_stcore/stream` fails through Nginx. Often Safari, an intermediate proxy or a changed Nginx config. | Try Chrome, Edge or Firefox. Re-apply the kit's Nginx site (section 8.5). Check `/var/log/nginx/error.log`. |
| Redirect after TradeSmart login shows a connection error | The browser can't reach `<server-ip>` on port 80 or 443 (section 6.2) | Use the paste fallback (section 6.3). |
| 502 Bad Gateway | Streamlit isn't running | `sudo systemctl status tsbench`. `sudo journalctl -u tsbench -n 100 --no-pager`. `curl -fsS http://127.0.0.1:8501/_stcore/health`. |
| Orders blocked: static IP "has not been set yet" | Nobody has entered the IP | [APP ADMIN] Settings → Server IP. |
| Orders blocked: traffic "leaves from X, but the static IP is set to Y" | Routing or NAT is wrong, or the saved IP is wrong | Compare with `curl -4 -s https://api.ipify.org`. Fix the NAT or correct the IP (section 2). Do not turn off enforcement to get around this. |
| Orders blocked: "Couldn't detect this server's public IP" | Outbound to the IP-check service is blocked | `curl -4 -sv https://api.ipify.org`. Open outbound 443 (section 4). Orders stay blocked until this works. |
| Script master failed | Outbound blocked to `v2api.tradesmartonline.in`, or a disk or permission problem | `curl -sI https://v2api.tradesmartonline.in/NSE_symbols.txt.zip`. Check `/var/lib/tsbench/scriptmaster/` is owned by `tsbench`. Retry in Settings → Script master. Earlier lists stay in use. |
| API calls report session or token errors | Access token expired | [APP ADMIN] Generate a new token (section 6.3). |
| "No token received" in Settings | The one-time code was used or expired, or the keys are wrong | Log in to TradeSmart again for a new code. Check `TS_APP_KEY` and `TS_SECRET_KEY`. |
| "database is locked" or "Permission denied" on a data file | Wrong owner, often after running something as root, or another process holds the DB | `sudo chown -R tsbench:tsbench /var/lib/tsbench`, then `sudo chmod 600 /var/lib/tsbench/.access_token`. Run commands with the `systemd-run` form shown in this guide. |
| "A run started by X is running" | Another benchmark or batch is running. Only one runs at a time. | Wait, or ask X to stop it. Don't delete the lock file. If the process died, the lock has already been released. |
| New order batch refused because test orders are unconfirmed | An earlier order couldn't be confirmed closed | See `ATTENTION` in section 8.8. Use **Order latency test → Open test orders**. |
| `ATTENTION` in the journal, but the service is running | `src.recover_orders` reported a problem. By design, this doesn't stop the service. | See section 8.8. |
| Service fails to start at the `ExecStartPre` stage | A failure of `src.recover_orders` itself is ignored, so look at the setup: a missing `/etc/tsbench/tsbench.env`, `tsbench` user, `/opt/tsbench/app`, or a folder in `ReadWritePaths`. A very slow check can also hit the 180 s start timeout. | `sudo journalctl -u tsbench -b --no-pager`, and look for `Failed at step` or `Failed to load environment files`. Fix it, or re-run the command in section 8.5. |
| Service stops retrying ("start request repeated too quickly") | More than 10 failed starts in 5 minutes | Fix the cause, then `sudo systemctl reset-failed tsbench && sudo systemctl start tsbench`. |
| `update.sh` exits with code 3 | A run is active | Try again when the run ends. |
| "Read-only file system" errors in the journal | `ProtectSystem` in the unit allows writes only to the data and log folders, and a path setting points elsewhere | Keep `TS_DB_PATH`, `TS_LOG_PATH` and `TS_TOKEN_FILE` under `/var/lib/tsbench` and `/var/log/tsbench`. Don't loosen the unit. |

---

## 11. Handover acceptance

### Checklist

**System team**

- [ ] Server meets section 3, and UFW rules are applied (section 4)
- [ ] `sudo ufw status numbered` shows no `Anywhere` rule for 22, 80 or 443 (step 5.5)
- [ ] After `curl -4 -s https://api.ipify.org` matches, the network team confirms TradeSmart traffic takes the same route
- [ ] `curl -4 -s https://api.ipify.org` returns the registered static IP
- [ ] Network team confirms the NAT or routing for `v2api.tradesmartonline.in` and `api.ipify.org`
- [ ] `/etc/tsbench/tsbench.env` is filled in, owned root:tsbench, mode 0640
- [ ] Trusted certificate installed, or self-signed accepted by the company in writing
- [ ] Login works from the office network and from VPN
- [ ] Access from outside the allowed networks is refused
- [ ] `curl http://<server>:8501` from another host fails
- [ ] `healthcheck.sh` passes
- [ ] `src.doctor` shows no `FAIL`
- [ ] A backup ran, and a restore was tested once
- [ ] `sudo systemctl restart tsbench` works, and the journal shows the open-order check
- [ ] A server reboot brings the service back without help
- [ ] `update.sh` tested once

**Owner and app admin**

- [ ] Admin can open Settings. A non-admin cannot.
- [ ] Order user can open the Order latency test. Others cannot.
- [ ] Server IP saved, and the app shows it matches
- [ ] Redirect URL registered with TradeSmart, and token generated
- [ ] Read-only benchmark completed
- [ ] 16-order live test completed, and all orders show cancelled in the order book
- [ ] Confirmed with the owner: TradeSmart's order book returns the `remarks` value `latency-test` on test orders
- [ ] Each user's browser verified to load the app (Safari only if needed)

### Tested vs not tested

**Tested, off Ubuntu:**

- The automated test suite on Python 3.12 and 3.13, with a mocked network and no real TradeSmart calls.
- The Nginx template on Nginx 1.24.0 (Ubuntu 24.04's version) running on Windows, with plain-text test passwords: login, forged
  headers, allow-list, TLS versions, `/callback` redirect, rate limit, and the live WebSocket through a real browser.
- A full rehearsal through that Nginx against a mock TradeSmart: role checks, entering the static IP in Settings, the OAuth code
  reaching the token exchange intact, a 16-order batch, an IP change refusing orders at send time, no user or proxy headers reaching the API,
  and a hard kill mid-burst with 8 orders open, all cancelled by `src.recover_orders`.
- `backup.sh` with real `sqlite3`. ShellCheck and `bash -n` on all scripts. `requirements.lock` resolves to Linux wheels with hashes.
- `install.sh`, `update.sh` (including auto-rollback) and `manage-user.sh` only with stubbed system commands.

**Not tested, because no Ubuntu machine was available:**

- A real install: `apt`, `systemd` units and hardening, `systemd-run`, `flock`, bcrypt logins, `ufw`, `healthcheck.sh`, and the deploy key against GitHub.
- Clean shutdown on `SIGTERM`. The open-order check at the next start is the backstop for this.
- Behaviour behind your real NAT, and any request against the live TradeSmart API from this server.

Treat the first install as validation, and report anything that differs from this guide to the owner.

---

## 12. File reference

| Path | What it is |
|---|---|
| `deploy/install.sh` | Install or re-apply. Inputs: `SERVER_NAME`, `ALLOWED_CIDRS`, optional `GIT_REF`, `REPO_URL`, `DEPLOY_KEY`, `ENABLE_UFW`, `SSH_ALLOWED_CIDRS`, `SSH_PORT` |
| `deploy/update.sh [ref\|rollback]` | Update or roll back; automatic backup and rollback |
| `deploy/backup.sh` | Database backup, run through `tsbench-backup.service` |
| `deploy/manage-user.sh add\|remove <username>`, `list` | Manage Nginx logins |
| `deploy/healthcheck.sh` | Check services, Nginx login, forged headers and the self-check |
| `deploy/lib.sh` | Shared paths and helpers for the scripts |
| `requirements.lock` | Hash-checked Python packages installed on the server |
| `requirements.txt`, `requirements-dev.txt` | Runtime packages; runtime plus test tools for developers |
| `deploy/tsbench.service` | Main systemd unit, with the `ExecStartPre` open-order check |
| `deploy/tsbench-backup.service`, `deploy/tsbench-backup.timer` | Scheduled backup |
| `deploy/nginx-tsbench.conf.template` | Nginx site template |
| `deploy/tsbench.env.example` | Template for `/etc/tsbench/tsbench.env` |
| `.env.example` | Explains every variable (local and server) |
| `src/doctor.py` | Self-check: `python -m src.doctor` |
| `src/recover_orders.py` | Open-order check: `python -m src.recover_orders` |
| `src/access.py` | Login header check and roles |
| `src/network_guard.py` | Static-IP check |
| `src/outbound.py` | Optional source address |
| `src/token_store.py` | Shared token file |
| `src/run_lock.py` | One-run-at-a-time lock |
| `src/order_test.py` | The only code that sends orders |
| `config/endpoints.json` | Endpoint registry and script master URLs |
| `docs/ARCHITECTURE.md` | Developer reference |
| `README.md` | Overview and local PC setup |
