#!/usr/bin/env python3
"""Audit a generated code-only release tree before it is published."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Pattern, Set, Tuple

PUBLIC_MARKER = ".code-only-release"
PUBLIC_MARKER_CONTENT = "Generated code-only public release; private assets and history are excluded.\n"
TOP_LEVEL_FILES = {"README.md", "LICENSE", "download_stickers.sh", "pyproject.toml", "uv.lock", PUBLIC_MARKER}
TREE_PREFIXES = ("skills/kakao-telegram-stickers/", "tests/")
SINGLE_FILES = {
    ".github/workflows/test.yml",
    "docs/public-release.md",
    "scripts/export_public_tree.py",
    "scripts/audit_public_tree.py",
}
SINGLE_PARENT_DIRECTORIES = {".github", ".github/workflows", "docs", "scripts"}
MEDIA_SUFFIXES = {".png", ".webp", ".webm", ".gif", ".jpg", ".jpeg", ".bmp", ".zip", ".sqlite", ".db"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".lock", ".yml", ".yaml", ".sh", ".json"}
EXTENSIONLESS_TEXT_FILES = {"LICENSE", PUBLIC_MARKER}
REQUEST_TEMPLATE = "skills/kakao-telegram-stickers/assets/request-template.json"
FORBIDDEN_DIRECTORY_NAMES = {"stickers", "archived", "jobs", "work", "dist"}
MAX_FILE_BYTES = 2 * 1024 * 1024
SECRET_PATTERNS: Tuple[Tuple[str, Pattern[str]], ...] = (
    ("Telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")),
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("AWS secret access key", re.compile(r"\bAWS_SECRET_ACCESS_KEY[ \t]*[:=][ \t]*(?P<value>[^\s#]+)", re.IGNORECASE)),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("PEM private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|-----BEGIN " r"PGP PRIVATE KEY BLOCK-----")),
    (
        "named secret",
        re.compile(r"\b[A-Za-z0-9_]*(?:API_KEY|SECRET_KEY|CLIENT_SECRET|ACCESS_TOKEN|PASSWORD|PRIVATE_KEY|AUTH_TOKEN)[ \t]*[:=][ \t]*(?P<value>[^\s#]+)", re.IGNORECASE),
    ),
)


def _allowed(relative: str) -> bool:
    normalized = relative.rstrip("/")
    if normalized in TOP_LEVEL_FILES or normalized in SINGLE_FILES or normalized in SINGLE_PARENT_DIRECTORIES:
        return True
    return any(
        normalized == prefix.rstrip("/")
        or normalized.startswith(prefix)
        or prefix.startswith(normalized + "/")
        for prefix in TREE_PREFIXES
    )


def _is_placeholder(value: str) -> bool:
    normalized = value.strip()
    if re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", normalized):
        return True
    if len(normalized) >= 3 and normalized.startswith("<") and normalized.endswith(">"):
        return normalized[1:-1].lower() in {
            "token", "example", "secret", "password", "api-key", "api_key",
            "access-token", "access_token", "client-secret", "client_secret",
        }
    return False


def _secret_kinds(text: str) -> Set[str]:
    kinds: Set[str] = set()
    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.groupdict().get("value", match.group(0))
            normalized = value.strip().strip("'\",;)")
            if _is_placeholder(value):
                continue
            if kind in ("AWS secret access key", "named secret") or "value" not in match.groupdict() or len(normalized) >= 16:
                kinds.add(kind)
    return kinds


def _file_type_violations(relative: str) -> List[str]:
    path = Path(relative)
    if path.suffix.lower() == ".json":
        return [] if relative == REQUEST_TEMPLATE else ["JSON file is not allowed: {}".format(relative)]
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in EXTENSIONLESS_TEXT_FILES:
        return []
    return ["file format is not allowed: {}".format(relative)]


def _artifact_kinds(content: bytes) -> Set[str]:
    kinds: Set[str] = set()
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        kinds.add("PNG")
    if content.startswith(b"RIFF"):
        kinds.add("WebP" if content[8:12] == b"WEBP" else "RIFF media")
    if content.startswith(b"\x1aE\xdf\xa3"):
        kinds.add("WebM")
    if content.startswith((b"GIF87a", b"GIF89a")):
        kinds.add("GIF")
    if content.startswith(b"\xff\xd8\xff"):
        kinds.add("JPEG")
    if content.startswith(b"BM"):
        kinds.add("BMP")
    if content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        kinds.add("ZIP")
    if content.startswith(b"SQLite format 3\x00"):
        kinds.add("SQLite")
    return kinds


def _decode_public_text(content: bytes) -> Optional[str]:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if "\x00" in text or any(ord(character) < 32 and character not in "\t\n\r" for character in text):
        return None
    return text


def _path_violations(relative: str) -> List[str]:
    path = Path(relative)
    violations: List[str] = []
    if ".git" in path.parts:
        violations.append("nested Git metadata is not public: {}".format(relative))
    if not _allowed(relative):
        violations.append("path is outside the public allowlist: {}".format(relative))
    if path.name == ".env" or path.name.startswith(".env."):
        violations.append("environment file is not public: {}".format(relative))
    if path.name.startswith("request-") and path.name != "request-template.json" and path.suffix.lower() == ".json":
        violations.append("runtime request data is not public: {}".format(relative))
    if path.suffix.lower() in MEDIA_SUFFIXES:
        violations.append("media or state artifact is not public: {}".format(relative))
    if any(part in FORBIDDEN_DIRECTORY_NAMES for part in path.parts):
        violations.append("runtime or asset directory is not public: {}".format(relative))
    return violations


def _scan_file(path: Path, relative: str) -> List[str]:
    violations = _path_violations(relative) + _file_type_violations(relative)
    try:
        size = path.stat().st_size
    except OSError as error:
        return violations + ["cannot stat {}: {}".format(relative, error)]
    if size > MAX_FILE_BYTES:
        violations.append("file exceeds 2 MiB: {}".format(relative))
    if size <= MAX_FILE_BYTES:
        try:
            content = path.read_bytes()
        except OSError as error:
            violations.append("cannot read {}: {}".format(relative, error))
        else:
            violations.extend("possible {} artifact in {}".format(kind, relative) for kind in sorted(_artifact_kinds(content)))
            text = _decode_public_text(content)
            if text is None:
                violations.append("non-UTF-8 or binary text is not public: {}".format(relative))
            else:
                violations.extend("possible {} in {}".format(kind, relative) for kind in sorted(_secret_kinds(text)))
    return violations


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return subprocess.run(["git", "-C", str(root), *arguments], check=False, capture_output=True, env=environment)


def _history_violations(root: Path) -> List[str]:
    """Inspect reachable commit tree entries and direct refs without exposing data."""
    if not (root / ".git").exists():
        return []
    violations: List[str] = []
    replacement_refs = _git(root, "for-each-ref", "--format=%(refname)", "refs/replace/")
    if replacement_refs.returncode:
        violations.append("cannot inspect Git replacement refs")
    elif replacement_refs.stdout.strip():
        violations.append("Git replacement refs are not public")
    grafts = root / ".git" / "info" / "grafts"
    try:
        if grafts.is_file() and grafts.read_bytes().strip():
            violations.append("Git grafts are not public")
    except OSError:
        violations.append("cannot inspect Git grafts")

    refs = _git(root, "for-each-ref", "--format=%(objecttype) %(refname)")
    tag_objects: Set[str] = set()
    if refs.returncode:
        violations.append("cannot inspect Git refs")
    else:
        for line in refs.stdout.splitlines():
            try:
                object_type, _refname = line.decode("utf-8", errors="strict").split(" ", 1)
            except (UnicodeDecodeError, ValueError):
                violations.append("cannot inspect Git refs")
                continue
            if object_type == "tree":
                violations.append("direct Git tree refs are not public")
            elif object_type == "blob":
                violations.append("historic pathless blob is not public")
            elif object_type == "tag":
                tag_id = _git(root, "rev-parse", "--verify", _refname)
                if tag_id.returncode or not re.fullmatch(rb"[0-9a-f]{40,64}\n?", tag_id.stdout):
                    violations.append("cannot inspect tag metadata")
                else:
                    tag_objects.add(tag_id.stdout.strip().decode("ascii"))

    revisions = _git(root, "rev-list", "--all")
    if revisions.returncode:
        return violations + ["cannot inspect Git history"]
    commits: Set[str] = set()
    for object_id in revisions.stdout.decode("ascii", errors="ignore").splitlines():
        object_type = _git(root, "cat-file", "-t", object_id)
        if object_type.returncode:
            violations.append("cannot inspect historic object")
        elif object_type.stdout.strip() == b"commit":
            commits.add(object_id)
        elif object_type.stdout.strip() == b"blob":
            violations.append("historic pathless blob is not public")

    for commit in sorted(commits):
        violations.extend(_metadata_violations(root, commit, "commit", "commit metadata"))
    for tag_object in sorted(tag_objects):
        violations.extend(_metadata_violations(root, tag_object, "tag", "tag metadata"))

    blobs: Dict[str, Set[str]] = {}
    for commit in sorted(commits):
        entries = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
        if entries.returncode:
            violations.append("cannot inspect historic tree")
            continue
        for entry in entries.stdout.split(b"\0"):
            if not entry:
                continue
            try:
                metadata, raw_path = entry.split(b"\t", 1)
                mode, object_type, object_id = metadata.split(b" ", 2)
                relative = raw_path.decode("utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError):
                violations.append("cannot inspect historic tree entry")
                continue
            violations.extend("historic " + message for message in _path_violations(relative))
            if mode == b"120000":
                violations.append("historic symlink is not public: {}".format(relative))
            if mode not in (b"100644", b"100755") or object_type != b"blob":
                violations.append("historic non-regular file is not public: {}".format(relative))
                continue
            violations.extend("historic " + message for message in _file_type_violations(relative))
            blobs.setdefault(object_id.decode("ascii"), set()).add(relative)

    for object_id, paths in blobs.items():
        size_result = _git(root, "cat-file", "-s", object_id)
        try:
            size = int(size_result.stdout.strip()) if not size_result.returncode else -1
        except ValueError:
            size = -1
        if size < 0:
            violations.append("cannot inspect historic object")
            continue
        if size > MAX_FILE_BYTES:
            for path in sorted(paths):
                violations.append("historic file exceeds 2 MiB: {}".format(path))
            continue
        content = _git(root, "cat-file", "blob", object_id)
        if content.returncode:
            violations.append("cannot inspect historic object")
            continue
        blob = content.stdout
        for kind in sorted(_artifact_kinds(blob)):
            for path in sorted(paths):
                violations.append("historic possible {} artifact in {}".format(kind, path))
        text = _decode_public_text(blob)
        if text is None:
            for path in sorted(paths):
                violations.append("historic non-UTF-8 or binary text is not public: {}".format(path))
            continue
        for kind in sorted(_secret_kinds(text)):
            for path in sorted(paths):
                violations.append("historic possible {} in {}".format(kind, path))
    return violations


def _metadata_violations(root: Path, object_id: str, object_type: str, label: str) -> List[str]:
    size_result = _git(root, "cat-file", "-s", object_id)
    try:
        size = int(size_result.stdout.strip()) if not size_result.returncode else -1
    except ValueError:
        size = -1
    if size < 0:
        return ["cannot inspect {}".format(label)]
    if size > MAX_FILE_BYTES:
        return ["historic {} exceeds 2 MiB".format(label)]
    content_result = _git(root, "cat-file", object_type, object_id)
    if content_result.returncode:
        return ["cannot inspect {}".format(label)]
    text = _decode_public_text(content_result.stdout)
    if text is None:
        return ["historic non-UTF-8 or binary {} is not public".format(label)]
    return ["historic possible {} in {}".format(kind, label) for kind in sorted(_secret_kinds(text))]


def _marker_violations(root: Path) -> List[str]:
    marker = root / PUBLIC_MARKER
    if marker.is_symlink() or not marker.is_file():
        return ["missing code-only release marker"]
    try:
        content = marker.read_text(encoding="utf-8")
    except OSError:
        return ["cannot read code-only release marker"]
    if content != PUBLIC_MARKER_CONTENT:
        return ["invalid code-only release marker"]
    if not (root / ".git").exists():
        return []

    tree = _git(root, "ls-tree", "-z", "HEAD", "--", PUBLIC_MARKER)
    if tree.returncode or not tree.stdout:
        return ["code-only release marker is not committed at HEAD"]
    entry = tree.stdout.split(b"\0", 1)[0]
    try:
        mode_and_type, recorded_path = entry.split(b"\t", 1)
        mode, object_type, _object_id = mode_and_type.split(b" ", 2)
    except ValueError:
        return ["code-only release marker is not committed at HEAD"]
    if mode not in (b"100644", b"100755") or object_type != b"blob" or recorded_path != PUBLIC_MARKER.encode("utf-8"):
        return ["code-only release marker is not committed at HEAD"]
    committed = _git(root, "show", "HEAD:" + PUBLIC_MARKER)
    if committed.returncode or committed.stdout != PUBLIC_MARKER_CONTENT.encode("utf-8"):
        return ["invalid committed code-only release marker"]
    return []


def audit(root: Path) -> dict:
    root = root.expanduser().resolve()
    violations: List[str] = []
    files = 0
    if not root.is_dir():
        return {"ok": False, "root": str(root), "files": files, "violations": ["root is not a directory"]}
    violations.extend(_marker_violations(root))
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root).as_posix()
        # Only the audit root's own Git directory is needed for history checks.
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if candidate.is_symlink():
            violations.append("symlink is not public: {}".format(relative))
            continue
        if candidate.is_dir():
            violations.extend(_path_violations(relative + "/"))
            continue
        if candidate.is_file():
            files += 1
            violations.extend(_scan_file(candidate, relative))
        else:
            violations.append("non-regular path is not public: {}".format(relative))
    violations.extend(_history_violations(root))
    return {"ok": not violations, "root": str(root), "files": files, "violations": sorted(set(violations))}


def main(argv: Iterable[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    report = audit(args.root)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
