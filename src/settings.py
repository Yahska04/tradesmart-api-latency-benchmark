"""Application settings loaded from ``.env`` (python-dotenv) and the process environment.

Real environment variables win over values in ``.env``. Secrets are never hardcoded; use
:meth:`Settings.masked` for anything shown in the UI or logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import dotenv_values

from src.redaction import mask_secret

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
APP_VERSION = "1.0.0"

SECRET_FIELDS: frozenset[str] = frozenset({"app_key", "secret_key", "client_id", "access_token"})
ENVIRONMENTS: tuple[str, ...] = ("live", "uat", "mock")

DEFAULTS: dict[str, str] = {
    "TS_BASE_URL": "https://v2api.tradesmartonline.in/NorenWClientAPIv2",
    "TS_WS_URL": "wss://v2api.tradesmartonline.in/NorenWSAPI/",
    "TS_OAUTH_URL": "https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth",
    "TS_APP_KEY": "",
    "TS_SECRET_KEY": "",
    "TS_CLIENT_ID": "",
    "TS_REDIRECT_URL": "",
    "TS_ACCESS_TOKEN": "",
    "TS_ENVIRONMENT": "live",
    "TS_STATIC_IP": "",
    "TS_DB_PATH": "data/benchmark.db",
    "TS_LOG_PATH": "data/logs/app.log",
}


@dataclass
class Settings:
    """Resolved runtime settings. Secret fields: app_key, secret_key, client_id, access_token."""

    base_url: str
    ws_url: str
    oauth_url: str
    app_key: str
    secret_key: str
    client_id: str
    redirect_url: str
    access_token: str
    environment: str
    static_ip: str
    db_path: Path
    log_path: Path

    def is_live(self) -> bool:
        """True when the configured environment is the live (production) account."""
        return self.environment == "live"

    def has_credentials(self) -> bool:
        """True when both an access token and a client id are configured."""
        return bool(self.access_token and self.client_id)

    def secret_values(self) -> list[str]:
        """Non-empty secret values, for registering with the redaction layer."""
        return [v for v in (getattr(self, f) for f in sorted(SECRET_FIELDS)) if v]

    def masked(self) -> dict[str, str]:
        """All fields as strings with secret fields masked (safe to display)."""
        out: dict[str, str] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = mask_secret(value) if f.name in SECRET_FIELDS else str(value)
        return out

    def __repr__(self) -> str:  # never leak secrets through repr/logging
        inner = ", ".join(f"{k}={v!r}" for k, v in self.masked().items())
        return f"Settings({inner})"


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from ``env_file`` (default ``PROJECT_ROOT/.env``) merged with ``os.environ``.

    Real environment variables take precedence. Unknown environments fall back to ``live`` so
    safety guards stay at their strictest.
    """
    env_path = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
    file_values = {k: v for k, v in dotenv_values(env_path).items() if v is not None} if env_path.is_file() else {}

    def get(name: str) -> str:
        if name in os.environ:
            return os.environ[name].strip()
        if name in file_values:
            return str(file_values[name]).strip()
        return DEFAULTS[name]

    environment = get("TS_ENVIRONMENT").lower() or "live"
    if environment not in ENVIRONMENTS:
        environment = "live"

    return Settings(
        base_url=get("TS_BASE_URL").rstrip("/") or DEFAULTS["TS_BASE_URL"],
        ws_url=get("TS_WS_URL") or DEFAULTS["TS_WS_URL"],
        oauth_url=get("TS_OAUTH_URL") or DEFAULTS["TS_OAUTH_URL"],
        app_key=get("TS_APP_KEY"),
        secret_key=get("TS_SECRET_KEY"),
        client_id=get("TS_CLIENT_ID"),
        redirect_url=get("TS_REDIRECT_URL"),
        access_token=get("TS_ACCESS_TOKEN"),
        environment=environment,
        static_ip=get("TS_STATIC_IP"),
        db_path=_resolve_path(get("TS_DB_PATH") or DEFAULTS["TS_DB_PATH"]),
        log_path=_resolve_path(get("TS_LOG_PATH") or DEFAULTS["TS_LOG_PATH"]),
    )
