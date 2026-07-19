#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = ["Pillow==11.3.0"]
# ///
"""Run one schema-validated Hermes sticker request without shell interpolation."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tele_sticker_maker import cli
import get_owner_id

MAX_REQUEST_BYTES = 65_536
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
REQUESTS_RELATIVE_DIR = Path("data") / "kakao-telegram-stickers" / "requests"


class RequestError(RuntimeError):
    pass


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return (Path(configured).expanduser() if configured else Path.home() / ".hermes").resolve()


def _request_path(request_id: object) -> Path:
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise RequestError
    try:
        data_root = (_hermes_home() / "data").resolve()
        requests_dir = (data_root / "kakao-telegram-stickers" / "requests").resolve()
        requests_dir.relative_to(data_root)
        path = requests_dir / "request-{}.json".format(request_id)
        if path.is_symlink():
            raise RequestError
        resolved = path.resolve(strict=True)
        resolved.relative_to(requests_dir)
        if resolved.parent != requests_dir or not stat.S_ISREG(resolved.stat().st_mode):
            raise RequestError
        if resolved.stat().st_size > MAX_REQUEST_BYTES:
            raise RequestError
        return resolved
    except (OSError, ValueError):
        raise RequestError from None


def _load_request(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RequestError from None
    if not isinstance(data, dict):
        raise RequestError
    return data


def _required(request: Dict[str, Any], keys: set[str]) -> None:
    if set(request) != keys:
        raise RequestError


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError
    return value


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RequestError
    return value


def _publish_arguments(request: Dict[str, Any], *, action: str) -> List[str]:
    expected = {"action", "ownerUserId", "pack", "packTitle", "packSlug", "emoji", "dataDir"}
    if action == "prepare":
        expected.add("source")
        if "layoutMode" in request:
            expected.add("layoutMode")
    else:
        expected.update({"jobId", "confirm"})
    _required(request, expected)
    owner = _positive_int(request["ownerUserId"])
    values = {key: _string(request[key]) for key in ("pack", "packTitle", "packSlug", "emoji", "dataDir")}
    arguments = [action]
    if action == "prepare":
        arguments.extend(["--source", _string(request["source"])])
        if "layoutMode" in request:
            layout_mode = _string(request["layoutMode"])
            if layout_mode not in {"auto", "mini", "standard"}:
                raise RequestError
            arguments.extend(["--layout-mode", layout_mode])
    else:
        if request["confirm"] is not True:
            raise RequestError
        arguments.extend(["--job-id", _string(request["jobId"]), "--confirm"])
    return arguments + [
        "--owner-user-id", str(owner),
        "--pack", values["pack"],
        "--pack-title", values["packTitle"],
        "--pack-slug", values["packSlug"],
        "--emoji", values["emoji"],
        "--data-dir", values["dataDir"],
        "--json",
    ]


def build_arguments(request: Dict[str, Any]) -> tuple[Callable[[List[str]], int], List[str]]:
    action = request.get("action")
    if action == "doctor":
        _required(request, {"action"})
        return cli.main, ["doctor", "--json"]
    if action == "download":
        _required(request, {"action", "inputs", "output"})
        inputs = request["inputs"]
        if not isinstance(inputs, list) or not inputs or any(not isinstance(item, str) or not item for item in inputs):
            raise RequestError
        arguments = ["download"]
        for item in inputs:
            arguments.extend(["--input", item])
        return cli.main, arguments + ["--output", _string(request["output"]), "--json"]
    if action == "prepare":
        return cli.main, _publish_arguments(request, action=action)
    if action == "publish":
        return cli.main, _publish_arguments(request, action=action)
    if action == "status":
        _required(request, {"action", "jobId", "dataDir"})
        return cli.main, ["status", "--job-id", _string(request["jobId"]), "--data-dir", _string(request["dataDir"]), "--json"]
    if action == "packs":
        _required(request, {"action", "ownerUserId", "dataDir"})
        return cli.main, ["packs", "--owner-user-id", str(_positive_int(request["ownerUserId"])), "--data-dir", _string(request["dataDir"]), "--json"]
    if action == "owner-id":
        _required(request, {"action", "marker"})
        return get_owner_id.main, ["--marker", _string(request["marker"])]
    raise RequestError


def _cleanup_request(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return
    try:
        path.parent.rmdir()
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 2 or values[0] != "--request-id":
        print("Request failed.", file=sys.stderr)
        return 2
    path: Optional[Path] = None
    try:
        path = _request_path(values[1])
        request = _load_request(path)
        handler, arguments = build_arguments(request)
        return handler(arguments)
    except RequestError:
        print("Request failed.", file=sys.stderr)
        return 2
    finally:
        if path is not None:
            _cleanup_request(path)


if __name__ == "__main__":
    raise SystemExit(main())
