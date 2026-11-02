"""Who is using the app and what they may do.

``local`` deployment: one person on their own PC, so every feature is available.

``server`` deployment: Nginx authenticates users (HTTP basic auth or an SSO proxy) and forwards the name in
``X-Remote-User``, together with a shared secret in ``X-Proxy-Secret``. Streamlit listens on localhost only,
so the secret is what proves a request really came through Nginx; without it the user header is ignored
and access is denied (fail closed).
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from src.settings import Settings

USER_HEADER = "x-remote-user"
SECRET_HEADER = "x-proxy-secret"
LOCAL_USER = "local"


@dataclass(frozen=True)
class Identity:
    """The current user. ``denied_reason`` is set when ``authenticated`` is False."""

    username: str
    authenticated: bool
    is_admin: bool = False
    can_place_orders: bool = False
    denied_reason: str = ""


def _in_list(username: str, allowed: tuple[str, ...]) -> bool:
    return username.casefold() in {u.casefold() for u in allowed}


def resolve_identity(settings: "Settings", headers: Mapping[str, str] | None) -> Identity:
    """Work out the user from request headers. Pure function: no Streamlit, no network."""
    if not settings.is_server():
        return Identity(LOCAL_USER, authenticated=True, is_admin=True, can_place_orders=True)

    lowered = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    if not settings.auth_proxy_secret:
        return Identity("", False, denied_reason="TS_AUTH_PROXY_SECRET is not configured on the server.")
    supplied = lowered.get(SECRET_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), settings.auth_proxy_secret.encode()):
        return Identity("", False, denied_reason="This request did not come through the login proxy.")
    username = lowered.get(USER_HEADER, "").strip()
    if not username:
        return Identity("", False, denied_reason="The login proxy did not identify a user.")
    return Identity(username, authenticated=True, is_admin=_in_list(username, settings.admin_users),
                    can_place_orders=_in_list(username, settings.order_users))
