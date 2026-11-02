"""Shared access token for server deployments.

On a server, all users share one TradeSmart session, so an admin's token must outlive their browser tab and
a service restart. It is kept in one file with owner-only permissions (0600), the same protection level as
the server's environment file. It never goes into the database, logs or exports. Local deployments keep using
a browser-session token and do not touch this module.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("tsbench")


def save_token(path: Path, token: str, *, saved_by: str) -> None:
    """Atomically write the token with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"token": token, "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "saved_by": saved_by}
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if os.name == "posix":  # os.open's mode only applies to a new file; a leftover .tmp keeps its old mode
                os.fchmod(fh.fileno(), 0o600)
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a stray copy of the token behind
        raise
    logger.info("Shared access token saved by %s", saved_by)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Couldn't read the shared access token file: %s", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def load_token(path: Path) -> str:
    return str(_read(path).get("token") or "")


def token_info(path: Path) -> dict:
    """``saved_at`` and ``saved_by`` for display; never includes the token."""
    data = _read(path)
    return {k: data[k] for k in ("saved_at", "saved_by") if k in data}


def clear_token(path: Path, *, cleared_by: str) -> None:
    path.unlink(missing_ok=True)
    logger.info("Shared access token cleared by %s", cleared_by)
