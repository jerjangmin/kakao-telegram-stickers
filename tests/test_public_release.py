"""Regression tests for the code-only public release boundary."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


exporter = _load("export_public_tree", "scripts/export_public_tree.py")
auditor = _load("audit_public_tree", "scripts/audit_public_tree.py")

SECRET_CASES = (
    ("Telegram bot token", "TELEGRAM_BOT_TOKEN=" + "123456789:" + "abcdefghijklmnopqrst" + "uvwxyzABCDE"),
    ("AWS access key ID", "AWS_ACCESS_KEY_ID=" + "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("AWS secret access key", "AWS_SECRET_" + "ACCESS_KEY=" + "abcdefghijklmnop"),
    ("GitHub token", "ghp_" + "abcdefghijklmnopqrst"),
    ("GitHub fine-grained PAT", "github_pat_" + "abcdefghijklmnopqrst"),
    ("PEM private key", "-----BEGIN " + "PRIVATE KEY-----"),
    ("PEM private key", "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----"),
    ("PEM private key", "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----"),
    ("named secret", "API_" + "KEY=" + "abcdefghijklmnop"),
    ("named secret", "SECRET_" + "KEY=" + "abcdefghijklmnop"),
    ("named secret", "CLIENT_" + "SECRET=" + "abcdefghijklmnop"),
    ("named secret", "ACCESS_" + "TOKEN=" + "abcdefghijklmnop"),
    ("named secret", "PASS" + "WORD=" + "abcdefghijklmnop"),
    ("named secret", "PASS" + "WORD=" + "hunter2"),
    ("named secret", "API_" + "KEY=" + "short-real-key"),
    ("named secret", "CLIENT_" + "SECRET=" + "abc123"),
    ("named secret", "PASS" + "WORD=" + "your_password_is_real"),
    ("named secret", "API_" + "KEY=" + "example-production"),
    ("named secret", "OPENAI_API_" + "KEY=" + "short-real-key"),
    ("named secret", "DATABASE_PASS" + "WORD=" + "hunter2"),
    ("named secret", "STRIPE_SECRET_" + "KEY=" + "abc123"),
    ("named secret", "SERVICE_PRIVATE_" + "KEY=" + "private-value"),
    ("named secret", "SERVICE_AUTH_" + "TOKEN=" + "auth-value"),
    ("named secret", "API_" + "KEY=<abcdefghijklmnop>"),
    ("named secret", "API_" + "KEY=<real-production-secret>"),
)
ARTIFACT_CASES = (
    ("PNG", b"\x89PNG\r\n\x1a\n"),
    ("WebP", b"RIFF\x00\x00\x00\x00WEBP"),
    ("RIFF media", b"RIFF\x00\x00\x00\x00WAVE"),
    ("WebM", b"\x1aE\xdf\xa3"),
    ("GIF", b"GIF89a"),
    ("JPEG", b"\xff\xd8\xff"),
    ("BMP", b"BM"),
    ("ZIP", b"PK\x03\x04"),
    ("SQLite", b"SQLite format 3\x00"),
)
PLACEHOLDER_VALUES = (
    "TELEGRAM_BOT_TOKEN=<example>",
    "AWS_SECRET_" + "ACCESS_KEY=${EXAMPLE_SECRET}",
    "ghp_<token>",
    "github_pat_<token>",
    "API_" + "KEY=<example>",
    "SECRET_" + "KEY=${EXAMPLE_SECRET}",
    "CLIENT_" + "SECRET=<example>",
    "ACCESS_" + "TOKEN=${EXAMPLE_TOKEN}",
    "PASS" + "WORD=<example>",
)


def _git(root, *arguments):
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True)


def _initialize_git(root):
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")


def _commit_all(root, message):
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


def test_export_is_deterministic_code_only_tree_and_audit_passes(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)

    report = auditor.audit(destination)
    assert report["ok"], report["violations"]
    assert (destination / "README.md").is_file()
    assert (destination / "skills/kakao-telegram-stickers/SKILL.md").is_file()
    assert not (destination / "archived").exists()
    assert not (destination / "stickers").exists()
    assert not list(destination.rglob("__pycache__"))
    assert not list(destination.rglob("*.pyc"))
    assert (destination / ".code-only-release").read_text(encoding="utf-8") == exporter.PUBLIC_MARKER_CONTENT
    assert (destination / "skills/kakao-telegram-stickers/assets/request-template.json").is_file()


def test_audit_requires_generated_code_only_marker(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    (destination / ".code-only-release").unlink()

    report = auditor.audit(destination)
    assert "missing code-only release marker" in report["violations"]


def test_audit_rejects_uncommitted_marker_when_git_exists(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)

    report = auditor.audit(destination)
    assert "code-only release marker is not committed at HEAD" in report["violations"]


def test_documented_no_project_export_and_audit_do_not_create_venv(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    exporter.export(source)
    export = subprocess.run(
        ["uv", "run", "--no-project", "python", "scripts/export_public_tree.py", str(destination)],
        cwd=source,
        check=False,
        capture_output=True,
        text=True,
    )
    assert export.returncode == 0, export.stdout + export.stderr
    audit = subprocess.run(
        ["uv", "run", "--no-project", "python", "scripts/audit_public_tree.py", str(destination)],
        cwd=source,
        check=False,
        capture_output=True,
        text=True,
    )
    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert not (source / ".venv").exists()
    assert not (destination / ".venv").exists()


def test_exported_skill_bundle_contract_passes(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    result = subprocess.run(
        ["uv", "run", "pytest", "-q", "tests/test_skill_package.py"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_clean_export_committed_to_main_passes_history_audit(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")

    report = auditor.audit(destination)
    assert report["ok"], report["violations"]


def _add_pathless_blob_ref(root, reference, payload):
    blob = subprocess.run(
        ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
        input=payload,
        check=True,
        capture_output=True,
    ).stdout.decode("ascii").strip()
    _git(root, "update-ref", reference, blob)


@pytest.mark.parametrize("reference,payload", [
    ("refs/tags/private-png", b"\x89PNG\r\n\x1a\n"),
    ("refs/tags/private-secret", b"API_" + b"KEY=" + b"short-real-key"),
])
def test_audit_rejects_pathless_reachable_blob_refs(tmp_path, reference, payload):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")
    _add_pathless_blob_ref(destination, reference, payload)

    report = auditor.audit(destination)
    assert "historic pathless blob is not public" in report["violations"]


def test_audit_rejects_replacement_ref_hiding_private_history(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    archived = destination / "archived" / "private.png"
    archived.parent.mkdir()
    archived.write_bytes(b"\x89PNG\r\n\x1a\n")
    _commit_all(destination, "private asset")
    private_commit = _git(destination, "rev-parse", "HEAD").stdout.strip()
    archived.unlink()
    archived.parent.rmdir()
    _commit_all(destination, "clean public tree")
    clean_commit = _git(destination, "rev-parse", "HEAD").stdout.strip()
    _git(destination, "replace", private_commit, clean_commit)

    report = auditor.audit(destination)
    assert "Git replacement refs are not public" in report["violations"]
    assert "historic possible PNG artifact in archived/private.png" in report["violations"]


@pytest.mark.parametrize("kind, metadata", [
    ("named secret", "API_" + "KEY=short-real-key"),
    ("PEM private key", "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----"),
    ("Telegram bot token", "123456789:" + "abcdefghijklmnopqrstuvwxyzABCDE"),
])
def test_audit_rejects_secrets_in_reachable_commit_metadata(tmp_path, kind, metadata):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")
    _git(destination, "commit", "--allow-empty", "-m", metadata)

    report = auditor.audit(destination)
    assert "historic possible {} in commit metadata".format(kind) in report["violations"]


def test_audit_accepts_clean_commit_metadata(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")

    assert auditor.audit(destination)["ok"]


@pytest.mark.parametrize("kind, metadata", [
    ("named secret", "API_" + "KEY=short-real-key"),
    ("PEM private key", "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----"),
    ("Telegram bot token", "123456789:" + "abcdefghijklmnopqrstuvwxyzABCDE"),
])
def test_audit_rejects_secrets_in_annotated_tag_metadata(tmp_path, kind, metadata):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")
    _git(destination, "tag", "-a", "release", "-m", metadata)

    report = auditor.audit(destination)
    assert "historic possible {} in tag metadata".format(kind) in report["violations"]


def test_audit_accepts_clean_annotated_tag_metadata(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")
    _git(destination, "tag", "-a", "release", "-m", "clean public release")

    assert auditor.audit(destination)["ok"]


def test_audit_maps_one_historic_blob_to_every_tree_path(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")
    readme = destination / "README.md"
    original_readme = readme.read_text(encoding="utf-8")
    shared = b"shared safe text\n"
    readme.write_bytes(shared)
    private = destination / "tests" / "private.bin"
    private.write_bytes(shared)
    _commit_all(destination, "same blob on allowed and forbidden paths")
    readme.write_text(original_readme, encoding="utf-8")
    private.unlink()
    _commit_all(destination, "remove private blob")

    report = auditor.audit(destination)
    assert "historic file format is not allowed: tests/private.bin" in report["violations"]


def test_audit_rejects_deleted_historic_symlink(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")
    link = destination / "tests" / "old_link.py"
    try:
        link.symlink_to(destination / "README.md")
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable on this platform")
    _commit_all(destination, "add symlink")
    link.unlink()
    _commit_all(destination, "remove symlink")

    report = auditor.audit(destination)
    assert "historic symlink is not public: tests/old_link.py" in report["violations"]


def test_audit_rejects_direct_tree_ref(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")
    tree = _git(destination, "rev-parse", "HEAD^{tree}").stdout.strip()
    _git(destination, "update-ref", "refs/tags/private-tree", tree)

    report = auditor.audit(destination)
    assert "direct Git tree refs are not public" in report["violations"]


def test_audit_rejects_nonempty_git_grafts(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    _commit_all(destination, "clean public tree")
    grafts = destination / ".git" / "info" / "grafts"
    grafts.write_text("invalid graft\n", encoding="utf-8")

    report = auditor.audit(destination)
    assert "Git grafts are not public" in report["violations"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode is not portable to Windows")
def test_export_preserves_shell_wrapper_executable_mode_in_git(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    assert destination.joinpath("download_stickers.sh").stat().st_mode & stat.S_IXUSR

    _initialize_git(destination)
    _commit_all(destination, "preserve executable wrapper")
    assert _git(destination, "ls-files", "--stage", "download_stickers.sh").stdout.startswith("100755 ")


def test_export_refuses_source_or_nonempty_destinations(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        exporter.export(ROOT)

    destination = tmp_path / "nonempty"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        exporter.export(destination)
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_export_rejects_nested_git_metadata_in_allowed_source_tree(tmp_path):
    source = tmp_path / "source"
    exporter.export(source)
    (source / "tests" / ".git").mkdir()

    with pytest.raises(ValueError, match="nested Git metadata"):
        exporter.export(tmp_path / "destination", source_root=source)


@pytest.mark.parametrize("relative, content", [
    ("stickers/source.webp", "asset"),
    (".env", "TELEGRAM_BOT_TOKEN=" + "123456789:" + "abcdefghijklmnopqrst" + "uvwxyzABCDE"),
    ("notes.txt", "123456789:" + "abcdefghijklmnopqrst" + "uvwxyzABCDE"),
])
def test_audit_rejects_assets_environment_and_tokens(tmp_path, relative, content):
    destination = tmp_path / "public"
    exporter.export(destination)
    planted = destination / relative
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(content, encoding="utf-8")

    report = auditor.audit(destination)
    assert report["ok"] is False
    assert report["violations"]


@pytest.mark.parametrize("relative", [
    ".github/workflows/test.yml.bak",
    "docs/public-release.md.private",
    "scripts/audit_public_tree.py.bak",
])
def test_audit_rejects_single_file_allowlist_prefix_bypass(tmp_path, relative):
    destination = tmp_path / "public"
    exporter.export(destination)
    planted = destination / relative
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("not allowlisted", encoding="utf-8")

    report = auditor.audit(destination)
    assert "path is outside the public allowlist: {}".format(relative) in report["violations"]


@pytest.mark.parametrize("relative", [
    ".github/workflows/test.yml.bak",
    "docs/public-release.md.private",
    "scripts/audit_public_tree.py.bak",
])
def test_audit_rejects_single_file_allowlist_prefix_bypass_in_history(tmp_path, relative):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    planted = destination / relative
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("not allowlisted", encoding="utf-8")
    _commit_all(destination, "add disallowed backup")
    planted.unlink()

    report = auditor.audit(destination)
    assert "historic path is outside the public allowlist: {}".format(relative) in report["violations"]


@pytest.mark.parametrize("historical", [False, True])
def test_audit_rejects_oversized_uv_lock_in_current_and_history(tmp_path, historical):
    destination = tmp_path / "public"
    exporter.export(destination)
    lock = destination / "uv.lock"
    clean_content = lock.read_bytes()
    lock.write_bytes(b"x" * (auditor.MAX_FILE_BYTES + 1))
    if historical:
        _initialize_git(destination)
        _commit_all(destination, "oversized lock")
        lock.write_bytes(clean_content)
    report = auditor.audit(destination)

    expected = "historic file exceeds 2 MiB: uv.lock" if historical else "file exceeds 2 MiB: uv.lock"
    assert expected in report["violations"]


def test_audit_rejects_disallowed_file_format_inside_allowlisted_tree(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    candidate = destination / "tests" / "private.bin"
    candidate.write_text("valid UTF-8 but not an allowed format", encoding="utf-8")

    report = auditor.audit(destination)
    assert "file format is not allowed: tests/private.bin" in report["violations"]


def test_audit_accepts_allowed_utf8_file_format(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    (destination / "tests" / "valid_extra.py").write_text("VALUE = 'safe text'\n", encoding="utf-8")

    report = auditor.audit(destination)
    assert report["ok"], report["violations"]


@pytest.mark.parametrize("kind, payload", ARTIFACT_CASES)
def test_audit_detects_magic_bytes_in_allowlisted_tree(tmp_path, kind, payload):
    destination = tmp_path / "public"
    exporter.export(destination)
    candidate = destination / "tests" / "private.dat"
    candidate.write_bytes(payload)

    report = auditor.audit(destination)
    assert "possible {} artifact in tests/private.dat".format(kind) in report["violations"]


@pytest.mark.parametrize("historical", [False, True])
def test_audit_rejects_utf16le_secret_text_in_current_and_history(tmp_path, historical):
    destination = tmp_path / "public"
    exporter.export(destination)
    candidate = destination / "tests" / "private.py"
    candidate.write_bytes(("API_" + "KEY=" + "short-real-key").encode("utf-16le"))
    if historical:
        _initialize_git(destination)
        _commit_all(destination, "add UTF-16 secret")
        candidate.unlink()

    report = auditor.audit(destination)
    expected = "historic non-UTF-8 or binary text is not public: tests/private.py" if historical else "non-UTF-8 or binary text is not public: tests/private.py"
    assert expected in report["violations"]


@pytest.mark.parametrize("historical", [False, True])
def test_audit_rejects_non_template_json_in_current_and_history(tmp_path, historical):
    destination = tmp_path / "public"
    exporter.export(destination)
    candidate = destination / "tests" / "session.json"
    candidate.write_text('{"owner": 1, "job": "private"}', encoding="utf-8")
    if historical:
        _initialize_git(destination)
        _commit_all(destination, "add runtime session")
        candidate.unlink()

    report = auditor.audit(destination)
    expected = "historic JSON file is not allowed: tests/session.json" if historical else "JSON file is not allowed: tests/session.json"
    assert expected in report["violations"]


def test_audit_detects_renamed_png_magic_bytes_in_history(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    candidate = destination / "tests" / "private.dat"
    candidate.write_bytes(b"\x89PNG\r\n\x1a\n")
    _commit_all(destination, "add renamed image")
    candidate.unlink()

    report = auditor.audit(destination)
    assert "historic possible PNG artifact in tests/private.dat" in report["violations"]


def test_audit_rejects_runtime_request_json_inside_allowlisted_tree(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    request = destination / "skills" / "kakao-telegram-stickers" / "assets" / "request-session.json"
    request.write_text("{}", encoding="utf-8")

    report = auditor.audit(destination)
    assert "runtime request data is not public: skills/kakao-telegram-stickers/assets/request-session.json" in report["violations"]


def test_audit_rejects_runtime_request_json_in_history(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    request = destination / "skills" / "kakao-telegram-stickers" / "assets" / "request-session.json"
    request.write_text("{}", encoding="utf-8")
    _commit_all(destination, "add runtime request")
    request.unlink()

    report = auditor.audit(destination)
    assert "historic runtime request data is not public: skills/kakao-telegram-stickers/assets/request-session.json" in report["violations"]


def test_audit_rejects_nested_git_repository(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    nested = destination / "tests" / "nested-repository"
    nested.mkdir()
    subprocess.run(["git", "init", str(nested)], check=True, capture_output=True, text=True)

    report = auditor.audit(destination)
    assert report["ok"] is False
    assert any("nested Git metadata" in violation for violation in report["violations"])


def test_audit_rejects_symlink_when_platform_allows_it(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    try:
        (destination / "tests/link.py").symlink_to(destination / "README.md")
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable on this platform")

    report = auditor.audit(destination)
    assert any("symlink" in violation for violation in report["violations"])


def test_audit_rejects_disallowed_reachable_git_history(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    (destination / "archived").mkdir()
    (destination / "archived/source.png").write_text("asset", encoding="utf-8")
    _commit_all(destination, "bad historic asset")
    (destination / "archived/source.png").unlink()
    (destination / "archived").rmdir()

    report = auditor.audit(destination)
    assert report["ok"] is False
    assert any(violation.startswith("historic ") for violation in report["violations"])


def test_audit_rejects_token_removed_from_reachable_history(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    readme = destination / "README.md"
    clean_text = readme.read_text(encoding="utf-8")
    readme.write_text(clean_text + "\nTOKEN=" + "123456789:" + "abcdefghijklmnopqrst" + "uvwxyzABCDE\n", encoding="utf-8")
    _commit_all(destination, "accidentally add token")
    readme.write_text(clean_text, encoding="utf-8")
    _commit_all(destination, "remove token")

    report = auditor.audit(destination)
    assert report["ok"] is False
    assert "historic possible Telegram bot token in README.md" in report["violations"]


@pytest.mark.parametrize("kind, secret", SECRET_CASES)
def test_audit_rejects_each_named_secret_in_allowlisted_current_file(tmp_path, kind, secret):
    destination = tmp_path / "public"
    exporter.export(destination)
    readme = destination / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n" + secret + "\n", encoding="utf-8")

    report = auditor.audit(destination)
    assert "possible {} in README.md".format(kind) in report["violations"]


@pytest.mark.parametrize("kind, secret", SECRET_CASES)
def test_audit_rejects_each_named_secret_in_allowlisted_history(tmp_path, kind, secret):
    destination = tmp_path / "public"
    exporter.export(destination)
    _initialize_git(destination)
    readme = destination / "README.md"
    clean_text = readme.read_text(encoding="utf-8")
    readme.write_text(clean_text + "\n" + secret + "\n", encoding="utf-8")
    _commit_all(destination, "accidentally add credential")
    readme.write_text(clean_text, encoding="utf-8")
    _commit_all(destination, "remove credential")

    report = auditor.audit(destination)
    assert "historic possible {} in README.md".format(kind) in report["violations"]


def test_audit_accepts_documented_placeholders(tmp_path):
    destination = tmp_path / "public"
    exporter.export(destination)
    readme = destination / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n" + "\n".join(PLACEHOLDER_VALUES) + "\n", encoding="utf-8")

    report = auditor.audit(destination)
    assert report["ok"], report["violations"]


def test_audit_cli_outputs_json(tmp_path, capsys):
    destination = tmp_path / "public"
    exporter.export(destination)
    assert auditor.main([str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_public_release_document_has_private_staging_then_public_smoke_order():
    text = (ROOT / "docs" / "public-release.md").read_text(encoding="utf-8")
    private_create = "gh repo create <owner>/<repo> --private --source=. --remote=origin --push"
    public_transition = "gh repo edit <owner>/<repo> --visibility public --accept-visibility-change-consequences"
    inspect = "hermes skills inspect <owner>/<repo>/skills/kakao-telegram-stickers"
    assert "git add .code-only-release" in text
    assert "gh repo edit <owner>/<repo> --delete-branch-on-merge" in text
    assert "uv run --no-project python scripts/audit_public_tree.py ." in text
    assert text.index(private_create) < text.index(public_transition) < text.index(inspect)
    assert "gh repo edit <owner>/<repo> --visibility private --accept-visibility-change-consequences" in text


def test_ci_release_mode_step_fails_closed_for_source_and_public_checkouts(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("CI release-mode step requires bash")
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    mode_step = next(step for step in workflow["jobs"]["test"]["steps"] if step.get("name") == "Verify source or code-only release mode")

    def run_mode(repository, marker):
        checkout = tmp_path / repository.replace("/", "-")
        checkout.mkdir(exist_ok=True)
        marker_path = checkout / ".code-only-release"
        if marker:
            marker_path.write_text("marker\n", encoding="utf-8")
        elif marker_path.exists():
            marker_path.unlink()
        return subprocess.run(
            [bash, "-c", mode_step["run"]],
            cwd=checkout,
            env={**os.environ, "GITHUB_REPOSITORY": repository},
            check=False,
            capture_output=True,
            text=True,
        )

    assert run_mode("jerjangmin/TeleSticker-Maker", False).returncode == 0
    assert run_mode("jerjangmin/TeleSticker-Maker", True).returncode == 1
    assert run_mode("new-owner/tele-sticker-maker", True).returncode == 0
    assert run_mode("new-owner/tele-sticker-maker", False).returncode == 1


def test_ci_workflow_declares_three_os_matrix_and_no_live_credentials():
    workflow = ROOT / ".github/workflows/test.yml"
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    matrix = data["jobs"]["test"]["strategy"]["matrix"]
    assert data["permissions"] == {"contents": "read"}
    assert matrix["os"] == ["ubuntu-latest", "macos-latest", "windows-latest"]
    assert matrix["python"] == ["3.9", "3.11", "3.12"]
    text = workflow.read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in text
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1" in text
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0" in text
    assert "astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86 # v5.4.2" in text
    assert "fetch-depth: 0" in text
    assert "uv sync --locked --python" in text
    assert "doctor --json" in text
    assert "export_public_tree.py" in text and "audit_public_tree.py" in text
    assert "jerjangmin/TeleSticker-Maker" in text
    assert "Verify source or code-only release mode" in text
    assert "hashFiles('.code-only-release')" in text
    assert "Audit checked-out public release history" in text
    assert "uv run --directory ../tele-sticker-maker-public" in text
    names = [step.get("name") for step in data["jobs"]["test"]["steps"]]
    assert names.index("Audit checked-out public release history") < names.index("Install locked dependencies")
    assert names.count("Audit checked-out public release history") == 1
    assert "TELEGRAM_BOT_TOKEN" not in text
    assert "e.kakao.com" not in text
