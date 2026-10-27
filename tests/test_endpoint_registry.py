"""Tests for src.endpoint_registry (uses the sample fixture, never config/endpoints.json)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.endpoint_registry import (
    Endpoint,
    Param,
    RegistryError,
    import_endpoints_json,
    load_endpoints,
    load_registry,
    missing_placeholders,
    render_payload,
    save_endpoints,
    validate_endpoint,
)

FIXTURE = Path(__file__).parent / "fixtures" / "endpoints_sample.json"


@pytest.fixture
def endpoints() -> list[Endpoint]:
    return load_endpoints(FIXTURE)


def by_id(eps: list[Endpoint], ep_id: str) -> Endpoint:
    return next(e for e in eps if e.id == ep_id)


def test_load_fixture(endpoints):
    ids = [e.id for e in endpoints]
    assert ids[:2] == ["UserDetails", "Limits"]
    assert "PlaceOrder" in ids and "GenAcsTok" in ids
    ud = by_id(endpoints, "UserDetails")
    assert ud.url_path == "/UserDetails"
    assert isinstance(ud.params[0], Param) and ud.params[0].name == "uid"
    quotes = by_id(endpoints, "GetQuotes")
    assert quotes.rate_limit_per_sec == 100 and quotes.rate_limit_per_min == 1000


def test_roundtrip_dict(endpoints):
    ep = by_id(endpoints, "GetQuotes")
    assert Endpoint.from_dict(ep.to_dict()) == ep


@pytest.mark.parametrize(
    "change, fragment",
    [
        ({"path": "UserDetails"}, "path must start"),
        ({"method": "PUT"}, "method"),
        ({"safety_class": "dangerous"}, "safety_class"),
        ({"safety_class": "state_changing", "benchmark_enabled": True}, "benchmark_enabled"),
        ({"rate_limit_per_sec": -1}, "rate_limit_per_sec"),
    ],
)
def test_validate_endpoint_problems(endpoints, change, fragment):
    d = by_id(endpoints, "UserDetails").to_dict()
    d.update(change)
    problems = validate_endpoint(Endpoint.from_dict(d))
    assert any(fragment in p for p in problems), problems


def test_valid_endpoints_have_no_problems(endpoints):
    assert all(validate_endpoint(e) == [] for e in endpoints)


def test_duplicate_ids_rejected(tmp_path):
    raw = json.loads(FIXTURE.read_text())
    raw["endpoints"].append(dict(raw["endpoints"][0]))
    path = tmp_path / "dup.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(RegistryError, match="duplicate"):
        load_registry(path)


def test_missing_file_and_bad_json(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(RegistryError):
        load_registry(bad)


def test_render_payload_placeholders_and_overrides(endpoints):
    ep = by_id(endpoints, "GetQuotes")
    ctx = {"ClientId": "AB1234", "exch": "NSE", "token": 22}
    assert render_payload(ep, ctx) == {"uid": "AB1234", "exch": "NSE", "token": 22}
    out = render_payload(ep, ctx, overrides={"exch": "BSE", "extra": "x-{{exch}}"})
    assert out["exch"] == "BSE" and out["extra"] == "x-NSE"


def test_render_payload_missing_raises(endpoints):
    ep = by_id(endpoints, "GetQuotes")
    with pytest.raises(RegistryError, match="exch"):
        render_payload(ep, {"ClientId": "AB1234", "token": "22"})
    assert missing_placeholders(ep, {"ClientId": "AB1234"}) == ["exch", "token"]
    # overriding a key removes its placeholder requirement; empty strings count as missing
    assert missing_placeholders(ep, {"ClientId": "", "token": "22"}, {"exch": "NSE"}) == ["ClientId"]


def test_import_full_registry_and_bare_list():
    text = FIXTURE.read_text()
    full = import_endpoints_json(text)
    assert len(full) == 7
    assert by_id(full, "GetQuotes").rate_limit_per_sec == 100
    bare = import_endpoints_json(json.dumps(json.loads(text)["endpoints"][:2]))
    assert [e.id for e in bare] == ["UserDetails", "Limits"]
    single = import_endpoints_json(json.dumps({"id": "Holdings", "name": "Holdings", "category": "H",
                                               "path": "/Holdings"}))
    assert single[0].id == "Holdings"


def test_import_rejects_invalid():
    with pytest.raises(RegistryError):
        import_endpoints_json("[{\"id\": \"X\", \"name\": \"X\", \"category\": \"c\", \"path\": \"nope\"}]")
    with pytest.raises(RegistryError):
        import_endpoints_json("42")
    with pytest.raises(RegistryError):
        import_endpoints_json("not json")


def test_save_preserves_top_level_keys(tmp_path, endpoints):
    path = tmp_path / "endpoints.json"
    path.write_text(FIXTURE.read_text())
    edited = [e for e in endpoints if e.id != "OrderBook"]
    by_id(edited, "UserDetails").notes = "edited"
    save_endpoints(edited, path)
    raw = json.loads(path.read_text())
    assert raw["websocket"]["heartbeat_seconds"] == 30
    assert raw["source"].startswith("https://")
    assert "OrderBook" not in [e["id"] for e in raw["endpoints"]]
    assert by_id(load_endpoints(path), "UserDetails").notes == "edited"


def test_save_refuses_invalid(tmp_path, endpoints):
    by_id(endpoints, "PlaceOrder").benchmark_enabled = True
    with pytest.raises(RegistryError):
        save_endpoints(endpoints, tmp_path / "x.json")
    assert not (tmp_path / "x.json").exists()
