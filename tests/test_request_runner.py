from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "kakao-telegram-stickers" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
SCRIPT = SCRIPTS_DIR / "request_runner.py"
spec = importlib.util.spec_from_file_location("request_runner", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def _request(tmp_path, monkeypatch, body, request_id="session"):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "data" / "kakao-telegram-stickers" / "requests" / "request-{}.json".format(request_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _config(action, **extra):
    body = {
        "action": action,
        "ownerUserId": 1,
        "pack": "default",
        "packTitle": "Title",
        "packSlug": "stickers",
        "emoji": "🙂",
        "dataDir": "/safe/data",
    }
    body.update(extra)
    return body


def test_prepare_uses_custom_hermes_home_passes_injection_as_one_argv_value_and_cleans_request(tmp_path, monkeypatch):
    source = 'slug; $(touch should-not-run) "quotes"'
    path = _request(tmp_path, monkeypatch, _config("prepare", source=source))
    calls = []
    monkeypatch.setattr(runner.cli, "main", lambda argv: calls.append(argv) or 0)

    assert runner.main(["--request-id", "session"]) == 0
    assert calls == [["prepare", "--source", source, "--owner-user-id", "1", "--pack", "default", "--pack-title", "Title", "--pack-slug", "stickers", "--emoji", "🙂", "--data-dir", "/safe/data", "--json"]]
    assert not path.exists()
    assert not path.parent.exists()
    assert not (tmp_path / "should-not-run").exists()


def test_default_hermes_home_is_used_when_environment_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    path = tmp_path / "home" / ".hermes" / "data" / "kakao-telegram-stickers" / "requests" / "request-default.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"action":"doctor"}', encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner.cli, "main", lambda argv: calls.append(argv) or 0)

    assert runner.main(["--request-id", "default"]) == 0
    assert calls == [["doctor", "--json"]]
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("action,body,expected", [
    ("doctor", {"action": "doctor"}, ["doctor", "--json"]),
    ("download", {"action": "download", "inputs": ["one", "two"], "output": "/safe/output"}, ["download", "--input", "one", "--input", "two", "--output", "/safe/output", "--json"]),
    ("status", {"action": "status", "jobId": "job", "dataDir": "/safe/data"}, ["status", "--job-id", "job", "--data-dir", "/safe/data", "--json"]),
    ("packs", {"action": "packs", "ownerUserId": 7, "dataDir": "/safe/data"}, ["packs", "--owner-user-id", "7", "--data-dir", "/safe/data", "--json"]),
])
def test_non_mutating_actions_are_strictly_mapped(tmp_path, monkeypatch, action, body, expected):
    path = _request(tmp_path, monkeypatch, body)
    calls = []
    monkeypatch.setattr(runner.cli, "main", lambda argv: calls.append(argv) or 0)

    assert runner.main(["--request-id", "session"]) == 0
    assert calls == [expected]
    assert not path.exists()


def test_download_request_runs_cli_with_normal_and_leading_hyphen_inputs_in_order(tmp_path, monkeypatch, capsys):
    path = _request(tmp_path, monkeypatch, {"action": "download", "inputs": ["teemos", "-a-scary-red-panda"], "output": str(tmp_path / "output")})
    calls = []

    class Manifest:
        def __init__(self, slug):
            self.slug = slug

        def to_dict(self):
            return {"slug": self.slug}

    monkeypatch.setattr(runner.cli, "download_set", lambda value, output: calls.append((value, output)) or Manifest(value))

    assert runner.main(["--request-id", "session"]) == 0
    assert calls == [("teemos", tmp_path / "output"), ("-a-scary-red-panda", tmp_path / "output")]
    assert json.loads(capsys.readouterr().out) == {"sets": [{"slug": "teemos"}, {"slug": "-a-scary-red-panda"}]}
    assert not path.exists()


@pytest.mark.parametrize("body", [
    _config("prepare", source="set-name"),
    {"action": "status", "jobId": "job", "dataDir": "placeholder"},
    {"action": "packs", "ownerUserId": 1, "dataDir": "placeholder"},
])
def test_data_dir_errors_return_cli_code_and_clean_request(tmp_path, monkeypatch, capsys, body):
    data_dir = tmp_path / "existing-file"
    data_dir.write_text("not a directory")
    body = {**body, "dataDir": str(data_dir)}
    path = _request(tmp_path, monkeypatch, body)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    assert runner.main(["--request-id", "session"]) == 3
    error = capsys.readouterr().err
    assert "상태 데이터 디렉터리" in error
    assert str(data_dir) not in error
    assert "Traceback" not in error
    assert not path.exists()


def test_publish_requires_true_confirmation_and_owner_id_uses_helper(tmp_path, monkeypatch, capsys):
    rejected = _request(tmp_path, monkeypatch, _config("publish", jobId="job", confirm=False))
    calls = []
    monkeypatch.setattr(runner.cli, "main", lambda argv: calls.append(argv) or 0)
    assert runner.main(["--request-id", "session"]) == 2
    assert calls == [] and not rejected.exists()
    assert capsys.readouterr().err == "Request failed.\n"

    owner = _request(tmp_path, monkeypatch, {"action": "owner-id", "marker": "one-time"})
    monkeypatch.setattr(runner.get_owner_id, "main", lambda argv: calls.append(argv) or 0)
    assert runner.main(["--request-id", "session"]) == 0
    assert calls == [["--marker", "one-time"]]
    assert not owner.exists()


@pytest.mark.parametrize("request_id", ["", "../outside", "has.dot", "a" * 129])
def test_invalid_request_ids_are_rejected_without_touching_files(tmp_path, monkeypatch, capsys, request_id):
    path = _request(tmp_path, monkeypatch, {"action": "doctor"})
    assert runner.main(["--request-id", request_id]) == 2
    assert path.exists()
    assert capsys.readouterr().err == "Request failed.\n"


def test_symlink_and_invalid_schema_are_rejected_without_request_echo(tmp_path, monkeypatch, capsys):
    path = _request(tmp_path, monkeypatch, {"action": "doctor"})
    target = path.with_name("target.json")
    target.write_text('{"action":"doctor"}', encoding="utf-8")
    path.unlink()
    try:
        os.symlink(target, path)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")
    assert runner.main(["--request-id", "session"]) == 2
    assert path.is_symlink() and target.exists()

    path.unlink()
    path.write_text('{"action":"doctor","extra":"secret-request-value"}', encoding="utf-8")
    assert runner.main(["--request-id", "session"]) == 2
    assert "secret-request-value" not in capsys.readouterr().err
    assert not path.exists()


def test_request_parent_must_resolve_inside_hermes_data_root(tmp_path, monkeypatch):
    path = _request(tmp_path, monkeypatch, {"action": "doctor"})
    outside = tmp_path / "outside-requests"
    outside.mkdir()
    path.unlink()
    requests_dir = path.parent
    requests_dir.rmdir()
    try:
        os.symlink(outside, requests_dir)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")
    (outside / path.name).write_text('{"action":"doctor"}', encoding="utf-8")

    assert runner.main(["--request-id", "session"]) == 2
    assert (outside / path.name).exists()


def test_non_regular_or_oversized_request_is_rejected(tmp_path, monkeypatch):
    path = _request(tmp_path, monkeypatch, {"action": "doctor"})
    path.unlink()
    path.mkdir()
    assert runner.main(["--request-id", "session"]) == 2

    path.rmdir()
    path.write_bytes(b" " * (runner.MAX_REQUEST_BYTES + 1))
    assert runner.main(["--request-id", "session"]) == 2
    assert path.exists()


def test_read_only_skill_directory_does_not_block_data_root_request(tmp_path, monkeypatch):
    path = _request(tmp_path, monkeypatch, {"action": "doctor"})
    calls = []
    monkeypatch.setattr(runner.cli, "main", lambda argv: calls.append(argv) or 0)
    original_mode = SCRIPTS_DIR.parent.stat().st_mode
    try:
        SCRIPTS_DIR.parent.chmod(stat.S_IREAD | stat.S_IEXEC)
        assert runner.main(["--request-id", "session"]) == 0
    finally:
        SCRIPTS_DIR.parent.chmod(original_mode)
    assert calls == [["doctor", "--json"]]
    assert not path.exists()
