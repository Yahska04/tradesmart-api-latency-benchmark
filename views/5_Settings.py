"""Settings: server IP, environment, saved defaults, OAuth token helper and data management (admins only)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src import network_guard, token_store  # noqa: E402
from src.ui import components as C  # noqa: E402
from src.ui.state import (  # noqa: E402
    TOKEN_SESSION_KEY, active_access_token, clear_caches, clear_demo_data, clear_egress_status, current_identity,
    egress_label, egress_status, get_db, get_settings, load_demo_data, token_source,
)
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Settings", subtitle="Secrets live in your .env file. Everything else is saved here.")
settings = get_settings()
identity = current_identity()
server = settings.is_server()

if not identity.is_admin:
    st.query_params.pop("code", None)  # a one-time login code shouldn't stay in the address bar
    st.warning("Only admins can open Settings. Ask an admin if something here needs changing.",
               icon=":material/lock:")
    st.stop()

db = get_db()
FLASH_KEY = "settings_flash"
FLASH = {"success": (st.success, ":material/check_circle:"), "warning": (st.warning, ":material/warning:"),
         "error": (st.error, ":material/error:")}


def _flash(kind: str, message: str) -> None:
    """Show a message after the next rerun."""
    st.session_state.setdefault(FLASH_KEY, []).append((kind, message))


def _exchange_code(code: str) -> tuple[bool, str]:
    """Swap a one-time login code for an access token and keep it. Returns (worked, message)."""
    from src.auth import generate_access_token
    from src.redaction import register_secrets
    from src.ui.state import get_logger

    get_logger()  # route GenAcsTok outcomes (redacted) to the log file
    with st.spinner("Exchanging the code…"):
        result = generate_access_token(settings, code)
    if not (result.ok and result.access_token):
        return False, (f"No token received: {result.error or 'unknown error'}. Codes work once and expire fast — "
                       "sign in again and use the new code.")
    register_secrets([result.access_token])
    if not server:
        st.session_state[TOKEN_SESSION_KEY] = result.access_token
        return True, "Access token ready for this session."
    try:
        token_store.save_token(settings.token_file, result.access_token, saved_by=identity.username)
    except OSError as exc:
        return False, (f"Got a token, but couldn't save it ({type(exc).__name__}). Check that the service can "
                       "write to the token file's folder, then sign in again.")
    return True, "Access token saved. All users now share it."


# ============================================================================ login code from the redirect
callback_code = st.query_params.get("code")
if callback_code:
    with st.container(border=True):
        st.markdown("#### TradeSmart sent a login code")
        if settings.app_key and settings.secret_key:
            st.markdown("Press the button to finish signing in. Continue only if you just signed in at TradeSmart.")
        else:
            st.warning("Add `TS_APP_KEY` and `TS_SECRET_KEY` to `.env`, then reload the settings.",
                       icon=":material/key_off:")
        if st.button("Get access token", type="primary", icon=":material/key:", key="callback_get_token",
                     disabled=not (settings.app_key and settings.secret_key)):
            # Query decoding turns the "+" of a base64-style code into a space.
            worked, message = _exchange_code(callback_code.replace(" ", "+"))
            st.query_params.pop("code", None)
            _flash("success" if worked else "error", message)
            st.rerun()

for flash_kind, flash_message in st.session_state.pop(FLASH_KEY, []):
    show, flash_icon = FLASH[flash_kind]
    show(flash_message, icon=flash_icon)

tab_ip, tab_env, tab_defaults, tab_token, tab_sm, tab_data = st.tabs(
    ["Server IP", "Environment", "Benchmark defaults", "Get an access token", "Script master", "Data"])

# ============================================================================ server IP
with tab_ip:
    st.markdown(
        "All TradeSmart traffic is sent by this server, so TradeSmart sees the server's public IP, whichever "
        "device people use to open this dashboard. The one exception is the TradeSmart login page, which opens "
        "in your own browser.")
    configured = network_guard.effective_static_ip(settings, db)
    check = egress_status()
    status_text, status_kind = egress_label(check)
    st.markdown(f"Status &nbsp; {C.pill(status_text, status_kind)}", unsafe_allow_html=True)
    ip1, ip2, ip3 = st.columns(3)
    with ip1:
        C.metric_card("Configured IP", configured or "Not set",
                      sub="The IP registered with TradeSmart", tone="accent" if configured else "neutral")
    with ip2:
        C.metric_card("Detected IP", check.detected or "Unknown", sub="Where this server's traffic comes from",
                      tone="success" if check.matches else "neutral")
    with ip3:
        C.metric_card("Enforcement", "On" if check.enforced else "Off",
                      sub="Orders need a matching IP" if check.enforced else "Orders are not blocked by the IP",
                      tone="accent" if check.enforced else "neutral")
    st.write("")
    if not configured:
        st.info("No server IP is set yet. Enter it below once you know it. "
                + ("Orders stay blocked until it is set. " if check.enforced else "")
                + "Read-only requests still run.", icon=":material/public:")
    elif check.problem:
        st.warning(check.problem, icon=":material/warning:")
    if st.button("Check now", icon=":material/refresh:", help="Asks the IP-check service again."):
        clear_egress_status()
        st.rerun()

    with st.form("server_ip"):
        typed_ip = st.text_input("Server static IP", value=configured, placeholder="e.g. 203.0.113.10",
                                 help="The public IP registered with TradeSmart. Save it empty to clear the "
                                      "saved value; `TS_STATIC_IP` from .env then applies, if set.")
        save_ip = st.form_submit_button("Save IP", type="primary", icon=":material/save:")
    if save_ip:
        try:
            saved_ip = network_guard.save_static_ip(db, typed_ip, changed_by=identity.username)
        except ValueError:
            st.error(f"“{typed_ip.strip()}” isn't a valid IP address. Enter it like 203.0.113.10.",
                     icon=":material/error:")
        except Exception as exc:
            st.error(f"Couldn't save the IP: {exc}. Check the database path, then try again.",
                     icon=":material/error:")
        else:
            clear_egress_status()
            _flash("success", f"Server IP saved: {saved_ip}." if saved_ip else "Saved server IP cleared.")
            if saved_ip and not network_guard.is_public_ip(saved_ip):
                _flash("warning", f"{saved_ip} isn't a public IP address. TradeSmart sees the public one, so "
                                  "check you have the right address.")
            st.rerun()

    st.markdown("#### Redirect URL to register with TradeSmart")
    if configured:
        host = f"[{configured}]" if ":" in configured else configured
        st.code(f"http://{host}/callback", language="text")
        st.caption("After login, TradeSmart sends the code to this address and you land back on this page.")
    else:
        st.caption("It appears here once the server IP is set.")

# ============================================================================ environment
with tab_env:
    env = settings.environment
    kind = {"live": "live", "uat": "warning", "mock": "info"}.get(env, "neutral")
    st.markdown(f"Current environment &nbsp; {C.pill(env.upper(), kind)}", unsafe_allow_html=True)
    C.live_warning_banner(env)
    labels = {
        "base_url": "API base URL", "ws_url": "WebSocket URL", "oauth_url": "OAuth login URL",
        "app_key": "App key", "secret_key": "Secret key", "client_id": "Client ID",
        "redirect_url": "Redirect URL", "access_token": "Access token (.env)",
        "environment": "Environment", "static_ip": "Static IP (.env fallback)",
        "db_path": "Database file", "log_path": "Log file",
        "deployment": "Deployment", "auth_proxy_secret": "Login proxy secret", "admin_users": "Admin users",
        "order_users": "Users who can place orders", "enforce_static_ip": "Enforce static IP for orders",
        "outbound_bind_ip": "Outbound bind IP", "ip_check_url": "IP-check service",
        "token_file": "Shared token file",
    }
    env_vars = {
        "base_url": "TS_BASE_URL", "ws_url": "TS_WS_URL", "oauth_url": "TS_OAUTH_URL",
        "app_key": "TS_APP_KEY", "secret_key": "TS_SECRET_KEY", "client_id": "TS_CLIENT_ID",
        "redirect_url": "TS_REDIRECT_URL", "access_token": "TS_ACCESS_TOKEN",
        "environment": "TS_ENVIRONMENT", "static_ip": "TS_STATIC_IP", "db_path": "TS_DB_PATH",
        "log_path": "TS_LOG_PATH", "deployment": "TS_DEPLOYMENT", "auth_proxy_secret": "TS_AUTH_PROXY_SECRET",
        "admin_users": "TS_ADMIN_USERS", "order_users": "TS_ORDER_USERS",
        "enforce_static_ip": "TS_ENFORCE_STATIC_IP", "outbound_bind_ip": "TS_OUTBOUND_BIND_IP",
        "ip_check_url": "TS_IP_CHECK_URL", "token_file": "TS_TOKEN_FILE",
    }
    masked = settings.masked()
    masked.update(admin_users=", ".join(settings.admin_users), order_users=", ".join(settings.order_users))
    st.dataframe(pd.DataFrame([{
        "Setting": labels.get(k, k), "Value (secrets masked)": v or "not set",
        "Env variable": env_vars.get(k, "")} for k, v in masked.items()]), hide_index=True)
    src = token_source()
    overrides = settings.access_token and active_access_token() != settings.access_token
    st.caption(f"Active access token: {'from ' + src if src else 'none'}"
               + (f" ({'shared' if server else 'session'} token overrides .env)" if overrides else ""))
    with st.expander("How to set secrets in .env", icon=":material/key:"):
        st.markdown(
            "1. Copy `.env.example` to `.env` in the project folder (it is git-ignored).\n"
            "2. Fill in your values, for example:\n")
        st.code("TS_APP_KEY=your-app-key\nTS_SECRET_KEY=your-secret-key\nTS_CLIENT_ID=your-client-id\n"
                "TS_REDIRECT_URL=https://your-registered-redirect\nTS_ACCESS_TOKEN=\n"
                "TS_ENVIRONMENT=live", language="ini")
        st.markdown("3. Restart the app (or press **Reload settings** below). Secrets never get "
                    "saved to the database, logs or exports.")
    if st.button("Reload settings", icon=":material/refresh:"):
        clear_caches()
        st.rerun()

# ============================================================================ defaults
with tab_defaults:
    st.markdown("These prefill the Run benchmark page. They are stored in the local database and "
                "contain no secrets.")

    def _get(key: str, fallback):
        try:
            v = db.get_setting(key, fallback)
            return fallback if v is None else v
        except Exception:
            return fallback

    with st.form("defaults"):
        d1, d2, d3 = st.columns(3)
        it = d1.number_input("Iterations per endpoint", 1, 1000, int(_get("default_iterations", 20)))
        wu = d2.number_input("Warm-up requests", 0, 20, int(_get("default_warmup", 2)))
        to = d3.number_input("Timeout (s)", 1.0, 120.0, float(_get("default_timeout_s", 10.0)))
        d4, d5, d6 = st.columns(3)
        dl = d4.number_input("Delay between requests (ms)", 0, 60000, int(_get("default_delay_ms", 500)),
                             step=100)
        sf = d5.slider("Rate-limit safety factor", 0.1, 1.0, float(_get("default_safety_factor", 0.5)),
                       step=0.05)
        cm = d6.radio("Connection mode", ["warm", "cold"], horizontal=True,
                      index=0 if _get("default_connection_mode", "warm") == "warm" else 1,
                      format_func={"warm": "Warm (reuse)", "cold": "Cold (new each time)"}.get)
        if st.form_submit_button("Save defaults", type="primary", icon=":material/save:"):
            try:
                for k, v in {"default_iterations": int(it), "default_warmup": int(wu),
                             "default_timeout_s": float(to), "default_delay_ms": int(dl),
                             "default_safety_factor": float(sf),
                             "default_connection_mode": cm}.items():
                    db.set_setting(k, v)
                st.success("Defaults saved.", icon=":material/check_circle:")
            except Exception as exc:
                st.error(f"Couldn't save defaults: {exc}. Check the database path, then try again.",
                         icon=":material/error:")

# ============================================================================ OAuth helper
with tab_token:
    if server:
        st.markdown(
            "TradeSmart uses an OAuth login. Sign in, and TradeSmart sends you back to this page with a one-time "
            "code. If it doesn't, paste the page address you land on below. The app exchanges the code for an "
            "access token (`GenAcsTok`). **On this server the token is shared by all users.** It is stored in a "
            "file with owner-only permissions, and never in the database, logs or exports.")
    else:
        st.markdown(
            "TradeSmart uses an OAuth login. Sign in, copy the page address you land on, and paste it "
            "here. The app exchanges the one-time code for an access token (`GenAcsTok`). **The token "
            "stays in this browser session only** — it is never written to the database, logs or "
            "exports. Closing the tab forgets it.")
    if not (settings.app_key and settings.secret_key):
        st.warning("Add `TS_APP_KEY` and `TS_SECRET_KEY` to `.env`, then press **Reload settings** "
                    "on the Environment tab.", icon=":material/key_off:")
    else:
        try:
            from src.auth import build_authorize_url

            url = build_authorize_url(settings.oauth_url, settings.app_key)
            st.link_button("1 · Open TradeSmart login", url, icon=":material/open_in_new:")
            st.caption("Opens in a new tab. Your app key is sent to TradeSmart's login page only.")
        except Exception as exc:
            st.error(f"Couldn't build the login link: {exc}", icon=":material/error:")

        with st.form("token_form", clear_on_submit=True):
            pasted = st.text_input("2 · Paste the redirect URL (or just the code)", type="password",
                                   help="Looks like https://your-redirect/?code=…  The code works "
                                        "once and expires quickly.")
            go = st.form_submit_button("Get access token", type="primary", icon=":material/key:")
        if go:
            if not pasted.strip():
                st.error("Paste the redirect URL first.", icon=":material/error:")
            else:
                worked, message = _exchange_code(pasted.strip())
                if worked:
                    st.success(message, icon=":material/check_circle:")
                else:
                    st.error(message, icon=":material/error:")

    from src.redaction import mask_secret

    if server:
        shared = token_store.load_token(settings.token_file)
        if shared:
            info = token_store.token_info(settings.token_file)
            st.markdown(f"Shared token: `{mask_secret(shared)}`")
            st.caption(f"Saved by {info.get('saved_by') or 'unknown'} on {C.fmt_ts(info.get('saved_at'))}.")
            if st.button("Clear shared token", icon=":material/logout:",
                         help="Signs every user out of TradeSmart until someone gets a new token."):
                try:
                    token_store.clear_token(settings.token_file, cleared_by=identity.username)
                    _flash("success", "Shared token cleared.")
                    st.rerun()
                except OSError as exc:
                    st.error(f"Couldn't clear the token file ({type(exc).__name__}). Check its permissions, "
                             "then try again.", icon=":material/error:")
        else:
            st.caption("No shared token saved yet.")
    else:
        tok = st.session_state.get(TOKEN_SESSION_KEY)
        if tok:
            st.markdown(f"Session token: `{mask_secret(tok)}`")
            if st.button("Forget session token", icon=":material/logout:"):
                st.session_state.pop(TOKEN_SESSION_KEY, None)
                st.rerun()

# ============================================================================ script master
with tab_sm:
    from src import script_master as sm
    from src.ui.instrument_picker import instrument_picker, option_label

    st.markdown(
        "The script master lists every tradable instrument with its **token** and **trading symbol**. "
        "The app downloads TradeSmart's public files once per IST day when it starts (no credentials "
        "are sent), so pages can fill `{{exch}}`, `{{token}}` and `{{tsym}}` from a search box.")

    @st.fragment(run_every=2.0 if sm.is_refreshing() else None)
    def _script_master_panel() -> None:
        info = sm.status()
        state = info["state"]
        labels = {"ready": ("READY", "success"), "partial": ("PARTIAL", "warning"),
                  "error": ("FAILED", "failure"), "downloading": ("DOWNLOADING", "info"),
                  "idle": ("NOT DOWNLOADED", "neutral"), "pending": ("PARTLY DOWNLOADED", "info")}
        text, kind = labels.get(state, (state.upper(), "neutral"))
        stamp = sm.to_ist(info.get("updated_at"))
        st.markdown(f"Status &nbsp; {C.pill(text, kind)} &nbsp; {info['total_rows']:,} instruments"
                    + (f" · last download {stamp:%d %b %H:%M} IST" if stamp else ""),
                    unsafe_allow_html=True)
        if state == "downloading":
            st.caption(f"Downloading {info.get('current') or '…'} "
                       f"({info.get('done') or 0} of {info.get('total') or '…'} exchanges done).")
        rows = []
        for exch, p in info["per_exchange"].items():
            dl = sm.to_ist(p["downloaded_at"])
            rows.append({
                "Exchange": exch, "Segment": p["segment"], "Instruments": p["rows"],
                "Downloaded (IST)": f"{dl:%d %b %H:%M}" if dl else "—",
                "Last attempt": ("OK" if p["ok"] else "Failed") if p["ok"] is not None else "Not yet",
                "Error": p["error"] or "",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if state in ("partial", "error"):
            st.warning("Some exchanges failed to download. Their previous lists (if any) are still "
                       "used. Check your internet connection, then press **Refresh script master**.",
                       icon=":material/warning:")
        b1, b2, _ = st.columns([1.2, 1.2, 3])
        busy = state == "downloading"
        if b1.button("Refresh script master", type="primary", icon=":material/refresh:", disabled=busy,
                     help="Downloads all exchanges again now, even if they were downloaded today."):
            sm.start_background_refresh(force=True)
            st.rerun(scope="app")
        if b2.button("Download missing", icon=":material/cloud_download:", disabled=busy,
                     help="Only exchanges not yet downloaded today, or that failed."):
            sm.start_background_refresh()
            st.rerun(scope="app")
        st.caption(f"Stored in `{info['storage']}` · source URLs in `config/endpoints.json` "
                   "(`script_master`). Set `TS_SCRIPT_MASTER_AUTOSTART=0` to skip the download at start-up.")

    _script_master_panel()

    st.markdown("#### Try a search")
    found = instrument_picker("settings_sm_try", label="Instrument")
    if found is not None:
        st.caption(f"{option_label(found)} · instrument {found.instrument or '—'} · tick {found.tick_size:g}"
                   + (f" · {found.option_type} {found.strike:g}" if found.option_type and found.strike is not None
                      else ""))

# ============================================================================ data
with tab_data:
    st.markdown(f"**Database:** `{settings.db_path}`  \n**Log file:** `{settings.log_path}`")
    st.markdown("#### Demo data")
    st.caption("Synthetic runs are labelled SYNTHETIC DEMO everywhere and are never real measurements.")
    m1, m2, _ = st.columns([1, 1, 3])
    if m1.button("Load demo data", icon=":material/science:"):
        try:
            n = load_demo_data()
            st.success(f"Loaded {n} synthetic demo runs.", icon=":material/check_circle:")
        except Exception as exc:
            st.error(f"Couldn't create demo data: {exc}", icon=":material/error:")
    if m2.button("Clear demo data", icon=":material/delete_sweep:"):
        try:
            n = clear_demo_data()
            st.success(f"Removed {n} synthetic demo runs. Measured runs were not touched.",
                       icon=":material/check_circle:")
        except Exception as exc:
            st.error(f"Couldn't clear demo data: {exc}", icon=":material/error:")

    st.markdown("#### Delete a run")
    try:
        all_runs = db.list_runs(include_synthetic=True)
    except Exception:
        all_runs = pd.DataFrame()
    if all_runs is None or not len(all_runs):
        st.caption("No runs stored yet.")
    else:
        by_id = {r["run_id"]: r for r in all_runs.to_dict("records")}
        victim = st.selectbox("Run", list(by_id), format_func=lambda r: C.run_label(by_id[r]))
        confirm = st.checkbox(f"Yes, permanently delete run {victim} and all its results")
        if st.button(f"Delete run {victim}", type="primary", disabled=not confirm,
                     icon=":material/delete:"):
            try:
                db.delete_run(victim)
                st.toast(f"Deleted {victim}", icon=":material/delete:")
                st.rerun()
            except Exception as exc:
                st.error(f"Couldn't delete the run: {exc}. Try again.", icon=":material/error:")
