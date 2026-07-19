from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "kakao-telegram-stickers" / "scripts" / "get_owner_id.py"
spec = importlib.util.spec_from_file_location("get_owner_id", SCRIPT)
assert spec and spec.loader
owner_lookup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = owner_lookup
spec.loader.exec_module(owner_lookup)


class FakeResponse:
    def __init__(self, payload: object, *, content_length: str | None = None):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = {} if content_length is None else {"Content-Length": content_length}
        self.closed = False
        self.read_size = None

    def read(self, size=-1):
        self.read_size = size
        return self.body

    def close(self):
        self.closed = True


def _payload(*messages):
    return {"ok": True, "result": [{"message": message} for message in messages]}


def test_exact_marker_returns_one_sanitized_candidate_without_exposing_token(monkeypatch, capsys):
    token = "123456:super-secret-token"
    marker = "tele-sticker-owner-random-marker"
    response = FakeResponse(_payload({"text": marker, "from": {"id": 42, "username": "owner", "first_name": "A\nlice\x00"}}))
    seen_urls = []

    def opener(request, *, timeout):
        seen_urls.append(request.full_url)
        assert timeout == 15
        return response

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    assert owner_lookup.main(["--marker", marker], opener=opener) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"candidates": [{"id": 42, "username": "owner", "first_name": "A lice"}]}
    assert response.closed and response.read_size == owner_lookup.MAX_RESPONSE_BYTES + 1
    assert token in seen_urls[0]
    assert token not in capsys.readouterr().err


def test_marker_must_match_exactly_and_multiple_users_fail_generically(monkeypatch, capsys):
    token = "123456:secret"
    marker = "exact-marker"
    response = FakeResponse(_payload(
        {"text": marker + "-suffix", "from": {"id": 1, "username": "wrong", "first_name": "Wrong"}},
        {"text": marker, "from": {"id": 2, "username": "one", "first_name": "One"}},
        {"text": marker, "from": {"id": 3, "username": "two", "first_name": "Two"}},
    ))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)

    assert owner_lookup.main(["--marker", marker], opener=lambda *_args, **_kwargs: response) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Owner lookup failed.\n"
    assert token not in captured.err and marker not in captured.err


@pytest.mark.parametrize("payload", [b"x" * (owner_lookup.MAX_RESPONSE_BYTES + 1), {"ok": True, "result": {}}])
def test_response_cap_and_schema_fail_without_raw_response_or_token(monkeypatch, capsys, payload):
    token = "123456:secret"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    response = FakeResponse(payload)

    assert owner_lookup.main(["--marker", "marker"], opener=lambda *_args, **_kwargs: response) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Owner lookup failed.\n"
    assert token not in captured.err


def test_missing_token_is_generic_lookup_exit_three(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert owner_lookup.main(["--marker", "marker"]) == 3
    assert capsys.readouterr().err == "Owner lookup failed.\n"


def test_unexpected_lookup_and_close_failures_are_generic_exit_three(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:secret")

    def opener(*_args, **_kwargs):
        raise RuntimeError("sensitive transport detail")

    assert owner_lookup.main(["--marker", "marker"], opener=opener) == 3
    assert capsys.readouterr().err == "Owner lookup failed.\n"

    response = FakeResponse(_payload({"text": "marker", "from": {"id": 1}}))
    response.close = lambda: (_ for _ in ()).throw(RuntimeError("close failure"))
    assert owner_lookup.main(["--marker", "marker"], opener=lambda *_args, **_kwargs: response) == 0


def test_declared_content_length_over_cap_is_rejected_before_body_read():
    response = FakeResponse({"ok": True, "result": []}, content_length=str(owner_lookup.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(owner_lookup.OwnerLookupError):
        owner_lookup.fetch_updates("token", opener=lambda *_args, **_kwargs: response)
    assert response.read_size is None and response.closed
