"""TradeSmart OAuth helpers: authorize URL, checksum, auth-code extraction and ``GenAcsTok`` exchange.

Flow (docs/TradeSmart_API_V2.md sections 3-6): the user opens the authorize URL, logs in, and is redirected
to ``<redirect_url>?code=<one-time code>``. The code is exchanged once via ``POST {base}/GenAcsTok`` with
``jData={"code": ..., "checksum": sha256(app_key + secret_key + code)}`` and *no* Bearer header.

Secrets (app key, secret key, code, checksum, token) are never logged and are redacted from errors.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlencode, urlparse

import httpx

from src.redaction import redact_text

if TYPE_CHECKING:
    from src.settings import Settings

logger = logging.getLogger("tsbench")

_TOKEN_KEYS = ("access_token", "accesstoken", "susertoken", "token")


def build_authorize_url(oauth_url: str, app_key: str) -> str:
    """Return the OAuth login URL: ``<oauth_url>?client_id=<app_key>``."""
    if not app_key:
        raise ValueError("app key is required to build the authorize URL")
    sep = "&" if urlparse(oauth_url).query else "?"
    return f"{oauth_url}{sep}{urlencode({'client_id': app_key})}"


def compute_checksum(app_key: str, secret_key: str, auth_code: str) -> str:
    """SHA-256 hex digest of ``app_key + secret_key + auth_code`` (no separators)."""
    return hashlib.sha256((app_key + secret_key + auth_code).encode("utf-8")).hexdigest()


_CODE_PARAM_NAMES = ("code", "auth_code", "authcode", "authorization_code")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_CODE_PARAM = re.compile(r"(?:^|[?&#])(?:code|auth_code|authcode|authorization_code)=", re.IGNORECASE)


def _query_params(part: str) -> dict[str, str]:
    """Parse ``a=1&b=2`` keeping a literal ``+`` (``parse_qs`` would turn it into a space, corrupting
    base64-style codes). Percent-escapes are decoded; keys are lower-cased."""
    params: dict[str, str] = {}
    for pair in part.split("&"):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            params.setdefault(unquote(key.strip()).lower(), unquote(value.strip()))
    return params


def extract_auth_code(redirected_url_or_code: str) -> str:
    """Return the authorization code from a full redirect URL (``...?code=XYZ``) or a bare code.

    ``+`` characters are preserved and percent-escapes (``%2B``, ``%3D``) are decoded, so the code sent to
    ``GenAcsTok`` is byte-for-byte the one TradeSmart issued. Raises ``ValueError`` when nothing usable is
    supplied.
    """
    text = (redirected_url_or_code or "").strip().strip("'\"").strip()
    if not text:
        raise ValueError("no authorization code supplied")
    looks_like_url = "://" in text or text.startswith("?") or bool(_CODE_PARAM.search(text))
    if looks_like_url:
        parsed = urlparse(text if "://" in text or text.startswith("?") else "?" + text.split("?", 1)[-1])
        for part in (parsed.query, parsed.fragment):
            params = _query_params(part)
            for name in _CODE_PARAM_NAMES:
                if params.get(name):
                    return params[name]
        raise ValueError("the redirect URL does not contain a 'code' parameter — copy the full address "
                         "from the browser bar after TradeSmart redirects you")
    if any(ch.isspace() for ch in text):
        raise ValueError("authorization code must not contain whitespace")
    return unquote(text) if _PERCENT_ESCAPE.search(text) else text


@dataclass
class TokenResult:
    """Outcome of a GenAcsTok exchange. ``raw_keys`` lists response keys only (never values)."""

    ok: bool
    access_token: str | None
    error: str | None
    raw_keys: list[str] = field(default_factory=list)

    def __repr__(self) -> str:  # never leak the token via repr
        token = "***" if self.access_token else None
        return f"TokenResult(ok={self.ok}, access_token={token!r}, error={self.error!r}, raw_keys={self.raw_keys})"


def generate_access_token(
    settings: "Settings",
    auth_code: str,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> TokenResult:
    """Exchange a one-time authorization code for an access token via ``POST {base}/GenAcsTok``.

    ``auth_code`` may be a bare code or the full redirect URL. No Bearer header is sent. Never raises for
    network/API errors; returns ``TokenResult(ok=False, error=<redacted message>)`` instead.
    """
    if not settings.app_key or not settings.secret_key:
        return TokenResult(False, None, "TS_APP_KEY and TS_SECRET_KEY must be configured", [])
    try:
        code = extract_auth_code(auth_code)
    except ValueError as exc:
        return TokenResult(False, None, str(exc), [])

    checksum = compute_checksum(settings.app_key, settings.secret_key, code)
    secrets = [settings.app_key, settings.secret_key, code, checksum]
    body = "jData=" + json.dumps({"code": code, "checksum": checksum}, separators=(",", ":"))
    url = f"{settings.base_url.rstrip('/')}/GenAcsTok"

    own_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        logger.info("Requesting access token via GenAcsTok")
        response = http.post(url, content=body.encode("utf-8"), timeout=timeout)
    except httpx.HTTPError as exc:
        msg = redact_text(f"{type(exc).__name__}: {exc}", secrets)
        logger.warning("GenAcsTok request failed: %s", msg)
        return TokenResult(False, None, msg, [])
    finally:
        if own_client:
            http.close()

    try:
        data = response.json()
    except ValueError:
        return TokenResult(False, None, f"HTTP {response.status_code}: response is not valid JSON", [])
    raw_keys = sorted(str(k) for k in data) if isinstance(data, dict) else []
    if not isinstance(data, dict):
        return TokenResult(False, None, f"HTTP {response.status_code}: unexpected response shape", raw_keys)

    token = next((str(data[k]) for k in _TOKEN_KEYS if data.get(k)), None)
    stat = str(data.get("stat", "")).lower()
    if response.status_code == 200 and token and stat in ("", "ok"):
        logger.info("Access token received (keys: %s)", ", ".join(raw_keys))
        return TokenResult(True, token, None, raw_keys)

    emsg = data.get("emsg") or ("no access token in response" if response.status_code == 200 else "request failed")
    msg = redact_text(f"HTTP {response.status_code}: {emsg}", [*secrets, *([token] if token else [])])
    logger.warning("GenAcsTok failed: %s", msg)
    return TokenResult(False, None, msg, raw_keys)
