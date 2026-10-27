"""Settings: environment, saved defaults, OAuth token helper and data management."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src.ui import components as C  # noqa: E402
from src.ui.state import (  # noqa: E402
    TOKEN_SESSION_KEY, clear_caches, clear_demo_data, get_db, get_settings, load_demo_data,
    token_source,
)
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Settings", subtitle="Secrets live in your .env file. Everything else is saved here.")
settings = get_settings()
db = get_db()

tab_env, tab_defaults, tab_token, tab_sm, tab_data = st.tabs(
    ["Environment", "Benchmark defaults", "Get an access token", "Script master", "Data"])

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
        "environment": "Environment", "static_ip": "Static IP (informational)",
        "db_path": "Database file", "log_path": "Log file",
    }
    env_vars = {
        "base_url": "TS_BASE_URL", "ws_url": "TS_WS_URL", "oauth_url": "TS_OAUTH_URL",
        "app_key": "TS_APP_KEY", "secret_key": "TS_SECRET_KEY", "client_id": "TS_CLIENT_ID",
        "redirect_url": "TS_REDIRECT_URL", "access_token": "TS_ACCESS_TOKEN",
        "environment": "TS_ENVIRONMENT", "static_ip": "TS_STATIC_IP", "db_path": "TS_DB_PATH",
        "log_path": "TS_LOG_PATH",
    }
    masked = settings.masked()
    st.dataframe(pd.DataFrame([{
        "Setting": labels.get(k, k), "Value (secrets masked)": v or "not set",
        "Env variable": env_vars.get(k, "")} for k, v in masked.items()]), hide_index=True)
    session_tok = st.session_state.get(TOKEN_SESSION_KEY)
    src = token_source()
    st.caption(f"Active access token: {'from ' + src if src else 'none'}"
               + (" (session token overrides .env)" if session_tok and settings.access_token else ""))
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
                from src.auth import generate_access_token
                from src.ui.state import get_logger

                get_logger()  # route GenAcsTok outcomes (redacted) to data/logs/app.log
                with st.spinner("Exchanging the code…"):
                    result = generate_access_token(settings, pasted.strip())
                if result.ok and result.access_token:
                    st.session_state[TOKEN_SESSION_KEY] = result.access_token
                    try:
                        from src.redaction import register_secrets

                        register_secrets([result.access_token])
                    except Exception:
                        pass
                    st.success("Access token ready for this session.", icon=":material/check_circle:")
                else:
                    st.error(f"No token received: {result.error or 'unknown error'}. Codes work once "
                             "and expire fast — sign in again and paste the new redirect URL.",
                             icon=":material/error:")

    tok = st.session_state.get(TOKEN_SESSION_KEY)
    if tok:
        from src.redaction import mask_secret

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
