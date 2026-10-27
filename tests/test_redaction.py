"""Tests for src.redaction: masking, text redaction, logging filter."""
from __future__ import annotations

import logging

import pytest

from src.redaction import (
    REDACTED,
    RedactingFilter,
    mask_secret,
    redact_mapping,
    redact_text,
    register_secrets,
    setup_logging,
)


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret(None) == ""
    assert mask_secret("abc") == "****"
    assert mask_secret("abcdefghij") == "ab****ij"
    assert "cdefgh" not in mask_secret("abcdefghij")


def test_bearer_token_redacted():
    out = redact_text("Authorization: Bearer eyJhbGciOi.abc-123_xyz")
    assert "eyJhbGciOi" not in out and f"Bearer {REDACTED}" in out


def test_json_sensitive_keys_redacted():
    text = '{"uid":"FA12345","actid": "FA12345","exch":"NSE","susertoken":"tok123","pwd":"p@ss"}'
    out = redact_text(text)
    assert "FA12345" not in out and "tok123" not in out and "p@ss" not in out
    assert '"exch":"NSE"' in out


def test_jdata_payload_redacted():
    text = 'jData={"uid":"FA12345","actid":"FA12345","token":"22"}&jKey=abcdef123'
    out = redact_text(text)
    assert "FA12345" not in out and "abcdef123" not in out
    assert out.startswith("jData={")


def test_key_value_pairs_redacted():
    out = redact_text("code=AUTHCODE987 checksum=deadbeef&client_id=FA777 other=keep")
    assert "AUTHCODE987" not in out and "deadbeef" not in out and "FA777" not in out
    assert "other=keep" in out


def test_numeric_json_values_and_long_digits():
    out = redact_text('{"accno": 123456789012, "qty": 5} order 123456789012345 at 20261016')
    assert "123456789012" not in out
    assert '"qty": 5' in out
    assert "20261016" in out  # 8 digits: a date, not an account number


def test_literal_secrets_redacted():
    out = redact_text("token is my-literal-secret!", secrets=["my-literal-secret"])
    assert "my-literal-secret" not in out


def test_registered_secrets_always_redacted():
    register_secrets(["globally-registered-value"])
    assert "globally-registered-value" not in redact_text("x globally-registered-value y")


def test_redaction_idempotent():
    samples = [
        'Bearer abc.def jData={"uid":"FA1","pwd":"x"} code=123 accno 1234567890',
        '{"Authorization": "Bearer zzz", "susertoken": 12345}',
        "nothing sensitive here",
        "secretvalue appears",
    ]
    for s in samples:
        once = redact_text(s, ["secretvalue"])
        assert redact_text(once, ["secretvalue"]) == once


def test_redact_text_none():
    assert redact_text(None) == ""


def test_redact_mapping_deep_copy():
    original = {"uid": "FA1", "nested": {"Access_Token": "t", "list": [{"pwd": "p"}, "Bearer qq"]}, "n": 3}
    out = redact_mapping(original)
    assert out["uid"] == REDACTED
    assert out["nested"]["Access_Token"] == REDACTED
    assert out["nested"]["list"][0]["pwd"] == REDACTED
    assert "qq" not in out["nested"]["list"][1]
    assert out["n"] == 3
    assert original["uid"] == "FA1"  # untouched


def test_redacting_filter_on_record():
    f = RedactingFilter(["filtersecret99"])
    record = logging.LogRecord("tsbench", logging.INFO, __file__, 1, "token=%s body %s",
                               ("filtersecret99", 'jData={"uid":"FA123"}'), None)
    assert f.filter(record)
    msg = record.getMessage()
    assert "filtersecret99" not in msg and "FA123" not in msg


def test_setup_logging_redacts_and_no_duplicate_handlers(tmp_path):
    log_path = tmp_path / "logs" / "nested" / "app.log"
    logger = setup_logging(log_path, secrets=["logsecretABC"])
    logger2 = setup_logging(log_path, secrets=["anotherSecretXYZ"])
    assert logger is logger2
    file_handlers = [h for h in logger.handlers if getattr(h, "_tsbench_handler", False)]
    assert len(file_handlers) == 1
    logger.info("using %s and anotherSecretXYZ", "logsecretABC")
    logger.info("Authorization: Bearer abc.def.ghi")
    logging.getLogger("tsbench.child").warning('payload jData={"uid":"FA55555","pwd":"hunter2"}')
    try:
        raise RuntimeError("boom with logsecretABC")
    except RuntimeError:
        logger.exception("failure")
    for h in logger.handlers:
        h.flush()
    content = log_path.read_text(encoding="utf-8")
    assert log_path.parent.is_dir()
    for leaked in ("logsecretABC", "anotherSecretXYZ", "abc.def.ghi", "FA55555", "hunter2"):
        assert leaked not in content
    assert REDACTED in content and "failure" in content


def test_setup_logging_new_path_replaces_handler(tmp_path):
    logger = setup_logging(tmp_path / "a.log")
    logger = setup_logging(tmp_path / "b.log")
    handlers = [h for h in logger.handlers if getattr(h, "_tsbench_handler", False)]
    assert len(handlers) == 1 and handlers[0].baseFilename.endswith("b.log")


@pytest.fixture(autouse=True)
def _close_handlers():
    yield
    logger = logging.getLogger("tsbench")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def test_redact_mapping_keep_keys_shows_instrument_token_but_still_masks_secrets():
    from src.redaction import REDACTED, redact_mapping

    body = {"uid": "AB1234", "exch": "NSE", "token": "3045", "susertoken": "sess-abc", "access_token": "tok-xyz"}
    out = redact_mapping(body, keep_keys=("token",))
    assert out["token"] == "3045" and out["exch"] == "NSE"
    assert out["uid"] == out["susertoken"] == out["access_token"] == REDACTED
    assert redact_mapping(body)["token"] == REDACTED                      # default unchanged (logs/exports)
    leaked = redact_mapping({"token": "SECRET-TOKEN-99"}, ["SECRET-TOKEN-99"], keep_keys=("token",))
    assert "SECRET-TOKEN-99" not in str(leaked)                            # registered secrets never shown
