"""Endpoint registry: the :class:`Endpoint` model plus loading, validation, saving and payload rendering.

The registry lives in ``config/endpoints.json`` (see ``docs/ARCHITECTURE.md`` section 3). Request body
templates may contain ``{{Placeholder}}`` markers (``{{ClientId}}``, ``{{exch}}``, ``{{token}}`` ...) that
are resolved at run time from a payload context and optional per-endpoint overrides.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger("tsbench")

SAFETY_CLASSES = ("read_only", "state_changing", "special_setup")
VALID_METHODS = ("POST", "GET")

DEFAULT_REGISTRY_PATH: Path = Path(__file__).resolve().parent.parent / "config" / "endpoints.json"

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_FULL_PLACEHOLDER_RE = re.compile(r"^\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*$")


class RegistryError(ValueError):
    """Raised when the registry file or an imported definition is invalid."""


@dataclass
class Param:
    """One documented request parameter."""

    name: str
    type: str = "string"
    required: bool = True
    description: str = ""
    example: str | None = None

    @classmethod
    def from_any(cls, value: Any) -> "Param":
        """Build a Param from a dict or a bare parameter name."""
        if isinstance(value, Param):
            return value
        if isinstance(value, str):
            return cls(name=value)
        if isinstance(value, dict):
            known = {f.name for f in fields(cls)}
            data = {k: v for k, v in value.items() if k in known}
            if "name" not in data:
                raise RegistryError(f"parameter definition without name: {value!r}")
            if data.get("example") is not None and not isinstance(data["example"], str):
                data["example"] = json.dumps(data["example"])
            return cls(**data)
        raise RegistryError(f"invalid parameter definition: {value!r}")


@dataclass
class Endpoint:
    """A documented REST endpoint plus benchmarking metadata."""

    id: str
    name: str
    category: str
    method: str = "POST"
    path: str = ""
    auth_required: bool = True
    required_headers: list[str] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    request_body_template: dict[str, Any] = field(default_factory=dict)
    response_format: str = ""
    safety_class: str = "read_only"
    requires_fixture: bool = False
    fixture_notes: str = ""
    rate_limit_per_sec: float | None = None
    rate_limit_per_min: float | None = None
    benchmark_enabled: bool = True
    doc_reference: str = ""
    notes: str = ""

    @property
    def url_path(self) -> str:
        """Path relative to the base URL, always starting with ``/``."""
        path = self.path or f"/{self.id}"
        return path if path.startswith("/") else f"/{path}"

    def to_dict(self) -> dict:
        """JSON-serialisable representation (inverse of :meth:`from_dict`)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Endpoint":
        """Build an Endpoint from a registry dict. Unknown keys are ignored (logged at debug level)."""
        if not isinstance(d, dict):
            raise RegistryError(f"endpoint definition must be an object, got {type(d).__name__}")
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            logger.debug("Ignoring unknown endpoint keys for %s: %s", d.get("id"), sorted(unknown))
        data = {k: copy.deepcopy(v) for k, v in d.items() if k in known}
        for required in ("id",):
            if not data.get(required):
                raise RegistryError(f"endpoint definition missing '{required}': {d!r}"[:300])
        data.setdefault("name", data["id"])
        data.setdefault("category", "Uncategorised")
        data["params"] = [Param.from_any(p) for p in (data.get("params") or [])]
        if data.get("request_body_template") is None:
            data["request_body_template"] = {}
        if data.get("required_headers") is None:
            data["required_headers"] = []
        if isinstance(data.get("method"), str):
            data["method"] = data["method"].upper()
        for key in ("rate_limit_per_sec", "rate_limit_per_min"):
            if data.get(key) is not None:
                try:
                    data[key] = float(data[key])
                except (TypeError, ValueError) as exc:
                    raise RegistryError(f"{data['id']}: {key} must be a number") from exc
        return cls(**data)


# --------------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------------
def validate_endpoint(ep: Endpoint) -> list[str]:
    """Return a list of problems for one endpoint (``[]`` means valid)."""
    problems: list[str] = []
    label = ep.id or "<missing id>"
    if not ep.id or not str(ep.id).strip():
        problems.append("id is required")
    if not ep.name or not str(ep.name).strip():
        problems.append(f"{label}: name is required")
    if ep.method not in VALID_METHODS:
        problems.append(f"{label}: method must be one of {VALID_METHODS}, got {ep.method!r}")
    if not isinstance(ep.path, str) or not ep.path.startswith("/"):
        problems.append(f"{label}: path must start with '/', got {ep.path!r}")
    if ep.safety_class not in SAFETY_CLASSES:
        problems.append(f"{label}: safety_class must be one of {SAFETY_CLASSES}, got {ep.safety_class!r}")
    if ep.safety_class == "state_changing" and ep.benchmark_enabled:
        problems.append(f"{label}: state_changing endpoints must have benchmark_enabled = false")
    if not isinstance(ep.request_body_template, dict):
        problems.append(f"{label}: request_body_template must be an object")
    for key in ("rate_limit_per_sec", "rate_limit_per_min"):
        value = getattr(ep, key)
        if value is not None and (not isinstance(value, (int, float)) or value <= 0):
            problems.append(f"{label}: {key} must be a positive number or null")
    return problems


def validate_endpoints(endpoints: list[Endpoint]) -> list[str]:
    """Validate a list of endpoints, including id uniqueness."""
    problems: list[str] = []
    seen: set[str] = set()
    for ep in endpoints:
        problems.extend(validate_endpoint(ep))
        if ep.id in seen:
            problems.append(f"duplicate endpoint id: {ep.id}")
        seen.add(ep.id)
    return problems


def _apply_registry_rate_limits(endpoints: list[Endpoint], rate_limits: Any) -> None:
    """Fill endpoint rate limits from the top-level ``rate_limits`` list when not set per endpoint."""
    if not isinstance(rate_limits, list):
        return
    by_id = {ep.id: ep for ep in endpoints}
    for rule in rate_limits:
        if not isinstance(rule, dict):
            continue
        for ep_id in rule.get("applies_to") or []:
            ep = by_id.get(ep_id)
            if ep is None:
                continue
            if ep.rate_limit_per_sec is None and rule.get("per_second") is not None:
                ep.rate_limit_per_sec = float(rule["per_second"])
            if ep.rate_limit_per_min is None and rule.get("per_minute") is not None:
                ep.rate_limit_per_min = float(rule["per_minute"])


def _parse_endpoint_list(items: Any) -> list[Endpoint]:
    if not isinstance(items, list):
        raise RegistryError("'endpoints' must be a list")
    endpoints: list[Endpoint] = []
    errors: list[str] = []
    for i, item in enumerate(items):
        try:
            endpoints.append(Endpoint.from_dict(item))
        except (RegistryError, TypeError) as exc:
            errors.append(f"endpoint #{i}: {exc}")
    if errors:
        raise RegistryError("; ".join(errors))
    return endpoints


# --------------------------------------------------------------------------------------------------
# Load / save / import
# --------------------------------------------------------------------------------------------------
def load_registry(path: Path | None = None) -> dict:
    """Load and validate the raw registry JSON. Raises :class:`RegistryError` on any problem."""
    path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or "endpoints" not in raw:
        raise RegistryError("registry must be an object with an 'endpoints' list")
    problems = validate_endpoints(_parse_endpoint_list(raw["endpoints"]))
    if problems:
        raise RegistryError("invalid registry: " + "; ".join(problems))
    return raw


def load_endpoints(path: Path | None = None) -> list[Endpoint]:
    """Load the registry and return validated :class:`Endpoint` objects (top-level rate limits applied)."""
    raw = load_registry(path)
    endpoints = _parse_endpoint_list(raw["endpoints"])
    _apply_registry_rate_limits(endpoints, raw.get("rate_limits"))
    return endpoints


def save_endpoints(endpoints: list[Endpoint], path: Path | None = None) -> None:
    """Validate and write endpoints, preserving all other top-level registry keys. Atomic replace."""
    path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    problems = validate_endpoints(endpoints)
    if problems:
        raise RegistryError("refusing to save invalid registry: " + "; ".join(problems))
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                raw = existing
        except json.JSONDecodeError:
            logger.warning("Existing registry at %s is not valid JSON; rewriting endpoints only", path)
    raw["endpoints"] = [ep.to_dict() for ep in endpoints]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".endpoints-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def import_endpoints_json(text: str) -> list[Endpoint]:
    """Parse endpoint definitions from JSON text: a full registry, a bare list, or one endpoint object."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"not valid JSON: {exc}") from exc
    rate_limits = None
    if isinstance(data, dict) and "endpoints" in data:
        items = data["endpoints"]
        rate_limits = data.get("rate_limits")
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "id" in data:
        items = [data]
    else:
        raise RegistryError("expected a registry object with 'endpoints', a list, or an endpoint object")
    endpoints = _parse_endpoint_list(items)
    problems = validate_endpoints(endpoints)
    if problems:
        raise RegistryError("; ".join(problems))
    _apply_registry_rate_limits(endpoints, rate_limits)
    return endpoints


# --------------------------------------------------------------------------------------------------
# Payload rendering
# --------------------------------------------------------------------------------------------------
def _lookup(context: dict[str, Any], key: str) -> tuple[bool, Any]:
    """Find ``key`` in context (exact match first, then case-insensitive). Empty/None counts as missing."""
    if key in context:
        value = context[key]
    else:
        lowered = {str(k).lower(): v for k, v in context.items()}
        if key.lower() not in lowered:
            return False, None
        value = lowered[key.lower()]
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return False, None
    return True, value


def _placeholders_in(obj: Any) -> list[str]:
    found: list[str] = []
    if isinstance(obj, str):
        found.extend(PLACEHOLDER_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_placeholders_in(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found.extend(_placeholders_in(v))
    return found


def _render(obj: Any, context: dict[str, Any], missing: list[str]) -> Any:
    if isinstance(obj, str):
        full = _FULL_PLACEHOLDER_RE.match(obj)
        if full:
            ok, value = _lookup(context, full.group(1))
            if not ok:
                missing.append(full.group(1))
                return obj
            return value

        def sub(m: re.Match[str]) -> str:
            ok, value = _lookup(context, m.group(1))
            if not ok:
                missing.append(m.group(1))
                return m.group(0)
            return str(value)

        return PLACEHOLDER_RE.sub(sub, obj)
    if isinstance(obj, dict):
        return {k: _render(v, context, missing) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render(v, context, missing) for v in obj]
    return copy.deepcopy(obj)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def missing_placeholders(ep: Endpoint, context: dict[str, Any], overrides: dict | None = None) -> list[str]:
    """Placeholder names that cannot be resolved (top-level keys replaced by ``overrides`` are ignored)."""
    overrides = overrides or {}
    template = {k: v for k, v in (ep.request_body_template or {}).items() if k not in overrides}
    missing: list[str] = []
    for name in _placeholders_in(template) + _placeholders_in(overrides):
        ok, _ = _lookup(context or {}, name)
        if not ok:
            missing.append(name)
    return _dedupe(missing)


def render_payload(ep: Endpoint, context: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict:
    """Render ``ep.request_body_template`` with ``context``; top-level ``overrides`` win.

    A value that is exactly one placeholder keeps the context value's type; embedded placeholders are
    string-substituted. Raises :class:`RegistryError` listing any unresolved placeholders.
    """
    context = context or {}
    overrides = overrides or {}
    missing: list[str] = []
    template = {k: v for k, v in (ep.request_body_template or {}).items() if k not in overrides}
    payload = _render(template, context, missing)
    payload.update(_render(dict(overrides), context, missing))
    if missing:
        raise RegistryError(f"{ep.id}: unresolved placeholders: {', '.join(_dedupe(missing))}")
    return payload
