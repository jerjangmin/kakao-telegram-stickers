from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "kakao-telegram-stickers" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from tele_sticker_maker import cli
from tele_sticker_maker.state import StateError, StateStore


def test_doctor_json_reports_missing_dependencies(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    exit_code = cli.main(["doctor", "--json"])

    assert exit_code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["python"]["available"] is True
    assert report["pillow"]["available"] is True
    assert report["ffmpeg"] == {"available": False, "path": None}
    assert report["ffprobe"] == {"available": False, "path": None}
    assert report["libvpx_vp9"] == {"available": False, "ffmpeg": None}


def test_canonical_run_script_outputs_only_doctor_json():
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "skills/kakao-telegram-stickers/scripts/run.py",
            "doctor",
            "--json",
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    output_lines = result.stdout.splitlines()
    assert len(output_lines) == 1
    report = json.loads(output_lines[0])
    assert report["ok"] is True


def test_download_json_processes_each_input_in_order(monkeypatch, capsys, tmp_path):
    calls = []

    class Manifest:
        def __init__(self, slug):
            self.slug = slug
            self.items = (object(),)

        def to_dict(self):
            return {"slug": self.slug}

    def fake_download(value, output):
        calls.append((value, output))
        return Manifest(value)

    monkeypatch.setattr(cli, "download_set", fake_download)

    assert cli.main(["download", "one", "two", "--output", str(tmp_path), "--json"]) == 0
    assert calls == [("one", tmp_path), ("two", tmp_path)]
    assert json.loads(capsys.readouterr().out) == {"sets": [{"slug": "one"}, {"slug": "two"}]}


def test_leading_hyphen_download_slug_preserves_input_order(monkeypatch, capsys, tmp_path):
    calls = []

    class Manifest:
        slug = "set"
        items = ()

        def to_dict(self):
            return {"slug": self.slug}

    monkeypatch.setattr(cli, "download_set", lambda value, output: calls.append((value, output)) or Manifest())

    assert cli.main(["download", "one", "-a-scary-red-panda", "two", "--output", str(tmp_path), "--json"]) == 0
    assert calls == [("one", tmp_path), ("-a-scary-red-panda", tmp_path), ("two", tmp_path)]
    assert json.loads(capsys.readouterr().out) == {"sets": [{"slug": "set"}] * 3}


@pytest.mark.parametrize("argv", [
    ["prepare", "--owner-user-id", "1", "--pack", "default", "--pack-title", "Title", "--pack-slug", "stickers", "--emoji", "🙂"],
    ["prepare", "one", "--source", "two", "--owner-user-id", "1", "--pack", "default", "--pack-title", "Title", "--pack-slug", "stickers", "--emoji", "🙂"],
    ["download", "--not-a-slug"],
])
def test_invalid_source_or_double_hyphen_argument_exits_two(argv):
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2


def test_prepare_leading_hyphen_slug_is_explicit_source(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    calls = []

    class Workflow:
        def __init__(self, config, store):
            pass

        def prepare(self, source):
            calls.append(source)
            return type("Result", (), {"summary": {}, "exit_code": 0})()

    monkeypatch.setattr(cli, "StickerWorkflow", Workflow)
    monkeypatch.setattr(cli, "StateStore", lambda path: object())

    assert cli.main(["prepare", "-a-scary-red-panda", "--owner-user-id", "1", "--pack", "default", "--pack-title", "Title", "--pack-slug", "stickers", "--emoji", "🙂", "--data-dir", str(tmp_path), "--json"]) == 0
    assert calls == ["-a-scary-red-panda"]


@pytest.mark.skipif(os.name == "nt", reason="download_stickers.sh requires a POSIX shell")
def test_shell_wrapper_json_keeps_stdout_json_only(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_path = tmp_path / "uv-args"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ARGS_PATH\"\nprintf '%s\\n' '{\"sets\": []}'\n")
    fake_uv.chmod(0o755)
    environment = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "ARGS_PATH": str(args_path)}

    result = subprocess.run(
        [str(project_root / "download_stickers.sh"), "--json", "set-name"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"sets": []}
    assert result.stdout.splitlines() == ['{"sets": []}']
    assert args_path.read_text().splitlines()[-3:] == ["download", "--json", "set-name"]


@pytest.mark.skipif(os.name == "nt", reason="download_stickers.sh requires a POSIX shell")
def test_shell_wrapper_passes_leading_hyphen_slug_unchanged(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_path = tmp_path / "uv-args"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ARGS_PATH\"\n")
    fake_uv.chmod(0o755)
    environment = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "ARGS_PATH": str(args_path)}

    result = subprocess.run([str(project_root / "download_stickers.sh"), "-a-scary-red-panda", "--json", "--output", "custom"], cwd=tmp_path, check=False, capture_output=True, text=True, env=environment)

    assert result.returncode == 0, result.stderr
    assert args_path.read_text().splitlines()[-5:] == ["download", "-a-scary-red-panda", "--json", "--output", "custom"]


@pytest.mark.parametrize("argv", [
    ["status", "--job-id", "job", "--data-dir"],
    ["packs", "--owner-user-id", "1", "--data-dir"],
    ["prepare", "source", "--owner-user-id", "1", "--pack", "default", "--pack-title", "Title", "--pack-slug", "stickers", "--emoji", "🙂", "--data-dir"],
])
def test_data_dir_file_returns_sanitized_state_error(tmp_path, monkeypatch, capsys, argv):
    data_dir = tmp_path / "existing-file"
    data_dir.write_text("not a directory")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    assert cli.main([*argv, str(data_dir), "--json"]) == 3
    error = capsys.readouterr().err
    assert "상태 데이터 디렉터리" in error
    assert str(data_dir) not in error
    assert "Traceback" not in error


@pytest.mark.parametrize("argv", [
    ["status", "--job-id", "job", "--data-dir", "/permission-denied", "--json"],
    ["packs", "--owner-user-id", "1", "--data-dir", "/permission-denied", "--json"],
    ["prepare", "source", "--owner-user-id", "1", "--pack", "default", "--pack-title", "Title", "--pack-slug", "stickers", "--emoji", "🙂", "--data-dir", "/permission-denied", "--json"],
])
def test_permission_state_errors_map_to_exit_code_three(monkeypatch, capsys, argv):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(cli, "StateStore", lambda path: (_ for _ in ()).throw(StateError("상태 데이터 디렉터리를 만들 수 없습니다")))

    assert cli.main(argv) == 3
    assert capsys.readouterr().err == "상태 데이터 디렉터리를 만들 수 없습니다\n"


def test_status_returns_current_items_counts_issues_and_pending(tmp_path, capsys):
    store = StateStore(tmp_path / "state.sqlite")
    prepare_summary = {"jobId": "job", "binding": {"ownerUserId": 9, "packAlias": "alternate", "packTitle": "Alternate", "packSlug": "alternate_pack", "emoji": "🔥"}}
    store.create_job(owner_user_id=1, source_url="source", kakao_slug="slug", target_alias="default", requested_emoji="🙂", job_id="job", summary=prepare_summary)
    store.add_item("job", item_index=1, source_sha256="a", source_kind="static_png", source_path="source-1", status="ready")
    store.add_item("job", item_index=2, source_sha256="b", source_kind="static_png", source_path="source-2", status="failed", error="conversion failed")
    store.add_item("job", item_index=3, source_sha256="c", source_kind="static_png", source_path="source-3", status="skipped_invalid")
    store.add_item("job", item_index=4, source_sha256="d", source_kind="static_png", source_path="source-4", status="publishing_item")

    assert cli.main(["status", "--job-id", "job", "--data-dir", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"job", "prepareSummary", "binding", "items", "counts", "issues", "pending"}
    assert report["prepareSummary"] == prepare_summary
    assert report["binding"] == prepare_summary["binding"]
    assert report["counts"] == {"failed": 1, "publishing_item": 1, "ready": 1, "skipped_invalid": 1}
    assert report["pending"] == [{"itemIndex": 1, "status": "ready"}, {"itemIndex": 4, "status": "publishing_item"}]
    assert report["issues"] == [
        {"itemIndex": 2, "status": "failed", "error": "conversion failed", "reason": "conversion failed"},
        {"itemIndex": 3, "status": "skipped_invalid", "error": None, "reason": "준비에서 제외되었습니다"},
        {"itemIndex": 4, "status": "publishing_item", "error": None, "reason": "등록 결과를 확인해야 합니다"},
    ]


def test_doctor_json_reports_available_vp9_encoder(monkeypatch, capsys):
    paths = {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"}
    monkeypatch.setattr(cli.shutil, "which", paths.get)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=" V....D libvpx-vp9 libvpx VP9\n", stderr=""
        ),
    )

    exit_code = cli.main(["doctor", "--json"])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["ffmpeg"] == {"available": True, "path": "/usr/bin/ffmpeg"}
    assert report["ffprobe"] == {"available": True, "path": "/usr/bin/ffprobe"}
    assert report["libvpx_vp9"] == {"available": True, "ffmpeg": "/usr/bin/ffmpeg"}
