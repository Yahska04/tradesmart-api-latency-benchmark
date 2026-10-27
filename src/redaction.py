"""Secret masking and redacting log setup.

Everything that may leave the process (log lines, CSV exports, error messages stored in SQLite)
is passed through :func:`redact_text`. Redaction is idempotent: running it twice yields the same
string. Secret values registered via :func:`register_secrets` are always redacted, in addition to
any secrets passed explicitly.
"""
from __future__ import annotations

import copy
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"

SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "access_token", "accesstoken", "susertoken", "token", "checksum", "code", "secret",
        "secret_key", "secretkey", "app_key", "appkey", "password", "pwd", "uid", "actid",
        "apikey", "api_key", "authorization", "jkey", "client_id", "clientid", "accno", "bankaccno", "pan",
    }
)

LOGGER_NAME = "tsbench"
_MIN_SECRET_LEN = 4  # shorter literal "secrets" would mangle ordinary text

_registered: set[str] = set()
_lock = threading.Lock()

_KEYS_ALT = "|".join(sorted((re.escape(k) for k in SENSITIVE_KEYS), key=len, reverse=True))
# Bearer <token>
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[^\s\"',;}\]]+")
# "key": "value"  (also tolerates escaped quotes inside an embedded JSON string: \"key\":\"value\")
_JSON_STR_RE = re.compile(rf'(?i)(\\?"(?:{_KEYS_ALT})\\?"\s*:\s*\\?")((?:[^"\\]|\\(?!"))*)')
# "key": 12345 / true / null-less scalars
_JSON_SCALAR_RE = re.compile(rf'(?i)(\\?"(?:{_KEYS_ALT})\\?"\s*:\s*)(-?\d[\d.eE+-]*|true|false)')
# key=value (form bodies, query strings, log text)
_KV_RE = re.compile(rf"(?i)(?<![A-Za-z0-9_])((?:{_KEYS_ALT})\s*=\s*)([^&\s\"',;}}\]]+)")
# long digit runs that look like account / order / phone numbers
_DIGITS_RE = re.compile(r"(?<!\d)\d{9,}(?!\d)")


def mask_secret(value: str | None, visible: int = 2) -> str:
    """Mask a secret for display: ``""`` -> ``""``, short -> ``"****"``, else ``"ab****yz"``."""
    if not value:
        return ""
    value = str(value)
    if visible <= 0 or len(value) <= visible * 2 + 2:
        return "****"
    return f"{value[:visible]}****{value[-visible:]}"


def register_secrets(secrets: Iterable[str]) -> None:
    """Add secret values to the global set that every redaction call removes."""
    with _lock:
        for s in secrets:
            if s and isinstance(s, str) and len(s) >= _MIN_SECRET_LEN:
                _registered.add(s)


def registered_secrets() -> frozenset[str]:
    """Return a snapshot of the globally registered secret values."""
    with _lock:
        return frozenset(_registered)


def _all_secrets(secrets: Iterable[str]) -> list[str]:
    combined = {s for s in secrets if s and isinstance(s, str) and len(s) >= _MIN_SECRET_LEN}
    combined |= registered_secrets()
    # Never treat parts of the marker itself as secrets (keeps redaction idempotent).
    combined = {s for s in combined if s not in REDACTED}
    return sorted(combined, key=len, reverse=True)


def redact_text(text: str | None, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with secrets, bearer tokens, sensitive key/values and long digit runs masked.

    Idempotent: ``redact_text(redact_text(x)) == redact_text(x)``. ``None`` becomes ``""``.
    """
    if text is None:
        return ""
    out = str(text)
    for secret in _all_secrets(secrets):
        out = out.replace(secret, REDACTED)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _JSON_STR_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _JSON_SCALAR_RE.sub(lambda m: f'{m.group(1)}"{REDACTED}"', out)
    out = _KV_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _DIGITS_RE.sub(REDACTED, out)
    return out


def redact_mapping(obj: Any, secrets: Iterable[str] = (), keep_keys: Iterable[str] = ()) -> Any:
    """Deep-copy ``obj`` replacing values of sensitive keys with REDACTED and redacting strings.

    ``keep_keys`` exempts specific keys from key-based masking for on-screen previews (e.g. ``token``, which in
    TradeSmart request/response bodies is the instrument token, not a credential). String values are still
    passed through :func:`redact_text`, so registered secrets never leak via a kept key.
    """
    secret_list = list(secrets)
    keep = {k.lower() for k in keep_keys}

    def _walk(o: Any) -> Any:
        if isinstance(o, Mapping):
            result = {}
            for k, v in o.items():
                if isinstance(k, str) and k.lower() in SENSITIVE_KEYS and k.lower() not in keep:
                    result[k] = REDACTED
                else:
                    result[k] = _walk(v)
            return result
        if isinstance(o, list):
            return [_walk(v) for v in o]
        if isinstance(o, tuple):
            return tuple(_walk(v) for v in o)
        if isinstance(o, str):
            return redact_text(o, secret_list)
        return copy.deepcopy(o)

    return _walk(obj)


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message (msg % args) and exception text."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets: set[str] = {s for s in secrets if s}

    def add_secrets(self, secrets: Iterable[str]) -> None:
        """Add extra secret values to this filter."""
        self.secrets |= {s for s in secrets if s}

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API name
        try:
            message = record.getMessage()
        except Exception:  # malformed format args: fall back to the raw msg
            message = str(record.msg)
        record.msg = redact_text(message, self.secrets)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text, self.secrets)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, self.secrets)
        return True


def setup_logging(
    log_path: Path, secrets: Iterable[str] = (), level: int = logging.INFO
) -> logging.Logger:
    """Configure the ``tsbench`` logger with a redacting rotating file handler.

    Idempotent: calling it again re-uses the existing handler for the same file (only the secret
    set and level are updated). The log directory is created automatically.
    """
    secrets = list(secrets)
    register_secrets(secrets)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    target = str(log_path.resolve())

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    existing = None
    for handler in list(logger.handlers):
        if getattr(handler, "_tsbench_handler", False):
            if getattr(handler, "baseFilename", None) == target and existing is None:
                existing = handler
            else:
                logger.removeHandler(handler)
                handler.close()

    if existing is None:
        handler = RotatingFileHandler(target, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler._tsbench_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handler.addFilter(RedactingFilter(secrets))
        logger.addHandler(handler)
        existing = handler
    else:
        for f in existing.filters:
            if isinstance(f, RedactingFilter):
                f.add_secrets(secrets)
    existing.setLevel(level)

    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
