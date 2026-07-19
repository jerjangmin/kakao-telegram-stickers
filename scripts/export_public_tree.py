#!/usr/bin/env python3
"""Export the repository's code-only public release tree without Git history."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Iterable, Optional

SOURCE_ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL_FILES = ("README.md", "LICENSE", "download_stickers.sh", "pyproject.toml", "uv.lock")
TREE_ROOTS = (
    Path("skills") / "kakao-telegram-stickers",
    Path("tests"),
)
SINGLE_FILES = (
    Path(".github") / "workflows" / "test.yml",
    Path("docs") / "public-release.md",
    Path("scripts") / "export_public_tree.py",
    Path("scripts") / "audit_public_tree.py",
)
IGNORED_NAMES = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc"}
PUBLIC_MARKER = ".code-only-release"
PUBLIC_MARKER_CONTENT = "Generated code-only public release; private assets and history are excluded.\n"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _should_ignore(path: Path) -> bool:
    return (
        path.name in IGNORED_NAMES
        or path.suffix in IGNORED_SUFFIXES
        or (path.name.startswith("request-") and path.name != "request-template.json" and path.suffix == ".json")
    )


def _copy_tree(source: Path, destination: Path) -> None:
    for candidate in sorted(source.rglob("*")):
        relative = candidate.relative_to(source)
        if candidate.is_symlink():
            raise ValueError("public export does not allow symlinks: {}".format(relative))
        if ".git" in relative.parts:
            raise ValueError("public export does not allow nested Git metadata: {}".format(relative))
        if any(_should_ignore(Path(part)) for part in relative.parts) or _should_ignore(candidate):
            continue
        target = destination / relative
        if candidate.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif candidate.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)
        else:
            raise ValueError("public export only supports regular files: {}".format(relative))


def export(destination: Path, source_root: Path = SOURCE_ROOT) -> None:
    """Copy the explicit public allowlist into an empty destination."""
    source_root = source_root.resolve()
    destination = destination.expanduser().resolve()
    if destination == source_root or _is_relative_to(destination, source_root):
        raise ValueError("destination must be outside the source repository")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("destination must be nonexistent or empty; refusing to delete files")
    destination.mkdir(parents=True, exist_ok=True)

    for relative in TOP_LEVEL_FILES:
        source = source_root / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError("required regular file is missing: {}".format(relative))
        shutil.copy2(source, destination / relative)
    for relative in TREE_ROOTS:
        source = source_root / relative
        if not source.is_dir() or source.is_symlink():
            raise ValueError("required directory is missing: {}".format(relative))
        _copy_tree(source, destination / relative)
    for relative in SINGLE_FILES:
        source = source_root / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError("required regular file is missing: {}".format(relative))
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (destination / PUBLIC_MARKER).write_text(PUBLIC_MARKER_CONTENT, encoding="utf-8")


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        export(args.destination)
    except (OSError, ValueError) as error:
        print("Export failed: {}".format(error), file=sys.stderr)
        return 2
    print("Exported code-only public tree to {}".format(args.destination.expanduser().resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
