"""Cached access to settings, database, endpoint registry and session-scoped UI state."""
from __future__ import annotations

import logging
from typing import Any

import streamlit as st

from src.settings import Settings, load_settings

SYNTHETIC_TOGGLE_KEY = "include_synthetic"
TOKEN_SESSION_KEY = "oauth_access_token"
HTTP_RUN_TYPES = ["http", "manual", "demo"]


@st.cache_resource(show_spinner=False)
def get_settings() -> Settings:
    return load_settings()


@st.cache_resource(show_spinner=False)
def get_db():  # -> src.database.Database
    from src.database import get_database

    return get_database(get_settings())


@st.cache_data(show_spinner=False)
def _load_endpoints_cached(path_str: str | None) -> list:
    from pathlib import Path

    from src.endpoint_registry import load_endpoints

    return load_endpoints(Path(path_str) if path_str else None)


def get_endpoints() -> list:
    """Endpoints from config/endpoints.json (a fresh copy each call; safe to mutate)."""
    return _load_endpoints_cached(None)


def endpoints_by_id() -> dict[str, Any]:
    return {ep.id: ep for ep in get_endpoints()}


def clear_endpoint_cache() -> None:
    _load_endpoints_cached.clear()


@st.cache_resource(show_spinner=False)
def get_logger() -> logging.Logger:
    from src.redaction import register_secrets, setup_logging

    settings = get_settings()
    logger = setup_logging(settings.log_path, settings.secret_values())
    register_secrets(settings.secret_values())
    return logger


@st.cache_resource(show_spinner=False, ttl="1h")
def ensure_script_master() -> bool:
    """Start the background script-master download once per server process (re-checked hourly).

    Exchanges already downloaded on the current IST date are skipped, so the hourly re-check only
    downloads after midnight IST or after a failure. Set ``TS_SCRIPT_MASTER_AUTOSTART=0`` to disable
    (manual refresh in Settings still works). Returns True when a refresh was started.
    """
    import os

    from src import script_master

    script_master.set_storage_dir(get_settings().db_path.parent / "scriptmaster")
    if os.environ.get("TS_SCRIPT_MASTER_AUTOSTART", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    try:
        get_logger()
        script_master.start_background_refresh()
        return True
    except Exception as exc:  # never block the app on the instrument download
        logging.getLogger("tsbench").warning("Script master: couldn't start the download (%s)", exc)
        return False


def active_access_token() -> str:
    """Session token from the Settings-page OAuth helper wins; otherwise TS_ACCESS_TOKEN from env."""
    token = st.session_state.get(TOKEN_SESSION_KEY) or ""
    return token or get_settings().access_token or ""


def token_source() -> str | None:
    if st.session_state.get(TOKEN_SESSION_KEY):
        return "session (Settings page OAuth helper)"
    if get_settings().access_token:
        return "environment (.env TS_ACCESS_TOKEN)"
    return None


def all_secrets() -> list[str]:
    secrets = list(get_settings().secret_values())
    tok = st.session_state.get(TOKEN_SESSION_KEY)
    if tok:
        secrets.append(tok)
    return secrets


def _has_real_runs() -> bool:
    try:
        runs = get_db().list_runs(include_synthetic=False, run_types=HTTP_RUN_TYPES)
        return runs is not None and len(runs) > 0
    except Exception:
        return False


def _has_any_runs() -> bool:
    try:
        runs = get_db().list_runs(include_synthetic=True, run_types=HTTP_RUN_TYPES)
        return runs is not None and len(runs) > 0
    except Exception:
        return False


def include_synthetic_toggle() -> bool:
    """Global sidebar toggle. Defaults OFF when real runs exist, ON when only demo data exists."""
    if SYNTHETIC_TOGGLE_KEY not in st.session_state:
        st.session_state[SYNTHETIC_TOGGLE_KEY] = not _has_real_runs()
    # Widget state is dropped on pages that don't render it, so mirror it into a plain key.
    widget_key = "_w_" + SYNTHETIC_TOGGLE_KEY
    st.session_state[widget_key] = st.session_state[SYNTHETIC_TOGGLE_KEY]

    def _sync() -> None:
        st.session_state[SYNTHETIC_TOGGLE_KEY] = st.session_state[widget_key]

    with st.sidebar:
        st.toggle(
            "Include synthetic demo data",
            key=widget_key,
            on_change=_sync,
            help="Synthetic demo runs are generated locally and are never real measurements. "
                 "They are always badged SYNTHETIC DEMO.",
        )
        env = get_settings().environment
        st.caption(f"Environment: **{env.upper()}**")
    return bool(st.session_state[SYNTHETIC_TOGGLE_KEY])


def reset_synthetic_default() -> None:
    """Re-derive the toggle default (after loading or clearing demo data)."""
    st.session_state.pop(SYNTHETIC_TOGGLE_KEY, None)


def clear_caches() -> None:
    st.cache_data.clear()
    get_settings.clear()
    get_db.clear()
    get_logger.clear()
    ensure_script_master.clear()  # re-derive the storage folder from a possibly changed TS_DB_PATH


def set_selection(*, run_id: str | None = None, endpoint_id: str | None = None) -> None:
    """Persist the selected run / endpoint in session state and the URL (?run=..&endpoint=..)."""
    if run_id is not None:
        st.session_state["selected_run"] = run_id
        st.query_params["run"] = run_id
    if endpoint_id is not None:
        st.session_state["selected_endpoint"] = endpoint_id
        st.query_params["endpoint"] = endpoint_id


def get_selection() -> tuple[str | None, str | None]:
    run_id = st.query_params.get("run") or st.session_state.get("selected_run")
    endpoint_id = st.query_params.get("endpoint") or st.session_state.get("selected_endpoint")
    return run_id, endpoint_id


def load_demo_data() -> int:
    """Generate clearly-labelled synthetic demo runs; switches the synthetic toggle on."""
    from src.demo_data import generate_demo_runs

    ids = generate_demo_runs(get_db(), get_endpoints())
    st.session_state[SYNTHETIC_TOGGLE_KEY] = True
    return len(ids)


def clear_demo_data() -> int:
    from src.demo_data import clear_demo_runs

    removed = clear_demo_runs(get_db())
    reset_synthetic_default()
    return removed
