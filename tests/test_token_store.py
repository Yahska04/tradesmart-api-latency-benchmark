"""Tests for src.token_store: the shared access token file."""
from __future__ import annotations

import json
import os
import stat

import pytest

from src import token_store as ts

TOKEN = "dummy-shared-token-value"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "sub" / ".access_token"


def test_save_load_info_clear(path):
    assert ts.load_token(path) == "" and ts.token_info(path) == {}    # nothing saved yet
    ts.save_token(path, TOKEN, saved_by="alice")
    assert ts.load_token(path) == TOKEN
    info = ts.token_info(path)
    assert info["saved_by"] == "alice" and info["saved_at"]
    ts.clear_token(path, cleared_by="alice")
    assert not path.exists() and ts.load_token(path) == ""
    ts.clear_token(path, cleared_by="alice")                          # clearing twice is fine


def test_info_never_includes_the_token(path):
    ts.save_token(path, TOKEN, saved_by="alice")
    info = ts.token_info(path)
    assert set(info) == {"saved_at", "saved_by"} and TOKEN not in str(info)


def test_overwrite_replaces_and_leaves_no_temp_file(path):
    ts.save_token(path, "first-token", saved_by="alice")
    ts.save_token(path, TOKEN, saved_by="bob")
    assert ts.load_token(path) == TOKEN and ts.token_info(path)["saved_by"] == "bob"
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_failed_write_keeps_the_old_token_and_cleans_up(path, monkeypatch):
    ts.save_token(path, "old-token", saved_by="alice")

    def boom(*args):
        raise OSError("disk full")

    monkeypatch.setattr(ts.os, "replace", boom)
    with pytest.raises(OSError):
        ts.save_token(path, "new-token", saved_by="bob")
    monkeypatch.undo()
    assert ts.load_token(path) == "old-token"
    assert [p.name for p in path.parent.iterdir()] == [path.name]     # no stray copy of the token


@pytest.mark.parametrize("content", [b"{not json", b"", b"[1, 2]", b'"text"', b"\xff\xfe\x00", b'{"token": null}'])
def test_corrupt_file_reads_as_empty(path, content):
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert ts.load_token(path) == "" and ts.token_info(path) == {}


def test_file_content_is_json_with_the_expected_fields(path):
    ts.save_token(path, TOKEN, saved_by="alice")
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"token", "saved_at", "saved_by"}


def test_requests_owner_only_mode_when_creating(path, monkeypatch):
    seen = []
    real = os.open
    monkeypatch.setattr(ts.os, "open", lambda p, flags, mode=0o777: seen.append(mode) or real(p, flags, mode))
    ts.save_token(path, TOKEN, saved_by="alice")
    assert seen == [0o600]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_posix_mode_is_0600(path):
    ts.save_token(path, TOKEN, saved_by="alice")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_posix_mode_is_0600_even_with_a_loose_leftover_temp_file(path):
    path.parent.mkdir(parents=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("old")
    tmp.chmod(0o644)
    ts.save_token(path, TOKEN, saved_by="alice")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
