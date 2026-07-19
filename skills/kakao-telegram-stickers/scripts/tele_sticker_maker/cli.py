"""Command-line interface for Kakao Telegram sticker tools."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from .kakao import KakaoError
from .media import MediaError, download_set
from .config import ConfigError, PublishConfig, default_data_dir
from .state import JobStateError, LeaseError, StateError, StateStore
from .workflow import StickerWorkflow, WorkflowError
from .telegram import TelegramError


_LEADING_HYPHEN_SLUG = re.compile(r"^-[A-Za-z0-9][A-Za-z0-9-]*$")


class _AppendInput(argparse.Action):
    """Accumulate positional and explicit download inputs in argv order."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any, option_string: Optional[str] = None) -> None:
        inputs = list(getattr(namespace, self.dest, None) or [])
        inputs.extend(values if isinstance(values, list) else [values])
        setattr(namespace, self.dest, inputs)


def _normalize_argv(argv: Sequence[str]) -> list[str]:
    """Make a leading-hyphen Kakao slug explicit before argparse sees it."""
    values = list(argv)
    if not values or values[0] not in ("download", "prepare"):
        return values
    command = values[0]
    value_options = {
        "download": {"--input", "--output"},
        "prepare": {"--owner-user-id", "--pack", "--pack-title", "--pack-slug", "--emoji", "--data-dir", "--source", "--layout-mode"},
    }[command]
    normalized = [command]
    skip_value = False
    source_found = False
    for value in values[1:]:
        if skip_value:
            if command == "download" and normalized[-1] == "--input" and _LEADING_HYPHEN_SLUG.fullmatch(value):
                normalized[-1] = "--input=" + value
            else:
                normalized.append(value)
            skip_value = False
            continue
        if value in value_options:
            normalized.append(value)
            skip_value = True
            continue
        if value.startswith("--"):
            normalized.append(value)
            continue
        if command == "download" and not value.startswith("-"):
            # argparse cannot preserve interleaved positional/optional order;
            # normalize every raw input into one explicit ordered stream.
            normalized.append("--input=" + value)
            continue
        if _LEADING_HYPHEN_SLUG.fullmatch(value):
            if command == "download":
                normalized.append("--input=" + value)
            elif not source_found:
                normalized.append("--source=" + value)
                source_found = True
            else:
                normalized.append(value)
            continue
        if command == "prepare" and not value.startswith("-"):
            source_found = True
        normalized.append(value)
    return normalized


def _pillow_status() -> dict[str, Any]:
    try:
        pillow = importlib.import_module("PIL")
    except ImportError:
        return {"available": False, "version": None}
    return {"available": True, "version": getattr(pillow, "__version__", None)}


def _vp9_status(ffmpeg_path: Optional[str]) -> dict[str, Any]:
    if not ffmpeg_path:
        return {"available": False, "ffmpeg": None}

    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return {"available": False, "ffmpeg": ffmpeg_path}

    return {
        "available": result.returncode == 0 and "libvpx-vp9" in result.stdout,
        "ffmpeg": ffmpeg_path,
    }


def doctor_report() -> dict[str, Any]:
    """Return the availability of runtime dependencies required by conversion."""
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    report = {
        "python": {"available": True, "version": sys.version.split()[0]},
        "pillow": _pillow_status(),
        "ffmpeg": {"available": ffmpeg_path is not None, "path": ffmpeg_path},
        "ffprobe": {"available": ffprobe_path is not None, "path": ffprobe_path},
        "libvpx_vp9": _vp9_status(ffmpeg_path),
    }
    report["ok"] = all(
        component["available"]
        for name, component in report.items()
        if name != "ok"
    )
    return report


def _status_report(store: StateStore, job: dict[str, Any]) -> dict[str, Any]:
    """Return current item state plus the immutable prepare-time binding."""
    try:
        prepare_summary = json.loads(job["summary_json"])
    except (TypeError, json.JSONDecodeError):
        prepare_summary = {}
    if not isinstance(prepare_summary, dict):
        prepare_summary = {}
    binding = prepare_summary.get("binding")
    if not isinstance(binding, dict):
        binding = {}
    items = store.list_items(job["job_id"])
    counts: dict[str, int] = {}
    issues = []
    pending = []
    for item in items:
        status = item["status"]
        counts[status] = counts.get(status, 0) + 1
        if status in ("failed", "skipped_invalid", "publishing_item"):
            issues.append({
                "itemIndex": item["item_index"],
                "status": status,
                "error": item["error"],
                "reason": item["error"] or ("등록 결과를 확인해야 합니다" if status == "publishing_item" else "준비에서 제외되었습니다"),
            })
        if status in ("ready", "publishing_item"):
            pending.append({"itemIndex": item["item_index"], "status": status})
    return {"job": job, "prepareSummary": prepare_summary, "binding": binding, "items": items, "counts": counts, "issues": issues, "pending": pending}


def _doctor_command(json_output: bool) -> int:
    report = doctor_report()
    if json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        for name, status in report.items():
            if name == "ok":
                continue
            print(f"{name}: {'ok' if status['available'] else 'missing'}")
        print(f"overall: {'ok' if report['ok'] else 'failed'}")
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tele-sticker-maker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor", help="Check conversion dependencies")
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")
    download_parser = subparsers.add_parser("download", help="Download Kakao sticker source files")
    download_parser.add_argument("inputs", nargs="*", action=_AppendInput, default=[], help="Kakao slug or approved Kakao URL")
    download_parser.add_argument("--input", dest="inputs", action=_AppendInput, help="One Kakao slug or approved Kakao URL")
    download_parser.add_argument("--output", default="stickers", help="Output root (default: stickers)")
    download_parser.add_argument("--json", action="store_true", dest="json_output")
    for command, help_text in (("prepare", "Prepare a Kakao set without publishing"), ("publish", "Publish a prepared job")):
        sub = subparsers.add_parser(command, help=help_text)
        if command == "prepare":
            sub.add_argument("source", nargs="?", help="Kakao slug or approved Kakao URL")
            sub.add_argument("--source", dest="explicit_source", help="Kakao slug or approved Kakao URL")
            sub.add_argument("--layout-mode", choices=("auto", "mini", "standard"), default="auto")
        else:
            sub.add_argument("--job-id", required=True)
            sub.add_argument("--confirm", action="store_true")
        sub.add_argument("--owner-user-id", required=True, type=int)
        sub.add_argument("--pack", required=True, help="Local pack alias")
        sub.add_argument("--pack-title", required=True)
        sub.add_argument("--pack-slug", required=True)
        sub.add_argument("--emoji", required=True)
        sub.add_argument("--data-dir", type=Path)
        sub.add_argument("--json", action="store_true", dest="json_output")
    status = subparsers.add_parser("status", help="Show one import job")
    status.add_argument("--job-id", required=True); status.add_argument("--data-dir", type=Path); status.add_argument("--json", action="store_true", dest="json_output")
    packs = subparsers.add_parser("packs", help="Show locally tracked packs")
    packs.add_argument("--owner-user-id", required=True, type=int); packs.add_argument("--data-dir", type=Path); packs.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(_normalize_argv(sys.argv[1:] if argv is None else argv))
    if args.command == "download" and not args.inputs:
        build_parser().error("download requires at least one input")
    if args.command == "prepare":
        sources = [source for source in (args.source, args.explicit_source) if source is not None]
        if len(sources) != 1:
            build_parser().error("prepare requires exactly one source")
        args.source = sources[0]
    if args.command == "doctor":
        return _doctor_command(args.json_output)
    if args.command == "download":
        manifests = []
        try:
            for value in args.inputs:
                manifest = download_set(value, Path(args.output))
                manifests.append(manifest.to_dict())
                if not args.json_output:
                    print("Saved {} ({} items)".format(Path(args.output) / manifest.slug / "json" / "manifest.json", len(manifest.items)))
        except (KakaoError, MediaError) as error:
            print(str(error), file=sys.stderr)
            return 3
        if args.json_output:
            print(json.dumps({"sets": manifests}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "publish" and not args.confirm:
        print(json.dumps({"jobId": args.job_id, "requiresConfirmation": True}, ensure_ascii=False, sort_keys=True))
        return 7
    if args.command in ("prepare", "publish"):
        try:
            config = PublishConfig.from_values(
                owner_user_id=args.owner_user_id,
                pack=args.pack,
                pack_title=args.pack_title,
                pack_slug=args.pack_slug,
                emoji=args.emoji,
                data_dir=args.data_dir,
                layout_mode=getattr(args, "layout_mode", "auto"),
            )
            result = StickerWorkflow(config, StateStore(config.data_dir / "state.sqlite")).prepare(args.source) if args.command == "prepare" else StickerWorkflow(config, StateStore(config.data_dir / "state.sqlite")).publish(args.job_id, confirm=args.confirm)
        except ConfigError as error:
            print(str(error), file=sys.stderr); return 2
        except MediaError as error:
            print(str(error), file=sys.stderr); return 4
        except TelegramError as error:
            print(str(error), file=sys.stderr); return 5
        except (JobStateError, LeaseError) as error:
            print(str(error), file=sys.stderr); return 7
        except (WorkflowError, KakaoError, StateError) as error:
            print(str(error), file=sys.stderr); return 3
        print(json.dumps(result.summary, ensure_ascii=False, sort_keys=True))
        return result.exit_code
    if args.command == "status":
        try:
            data_dir = args.data_dir or default_data_dir(); store = StateStore(data_dir / "state.sqlite"); job = store.get_job(args.job_id)
            report = _status_report(store, job) if job is not None else None
        except StateError as error:
            print(str(error), file=sys.stderr); return 3
        if report is None: print("작업을 찾을 수 없습니다", file=sys.stderr); return 7
        print(json.dumps(report, ensure_ascii=False, sort_keys=True)); return 0
    if args.command == "packs":
        try:
            data_dir = args.data_dir or default_data_dir(); records = StateStore(data_dir / "state.sqlite").list_packs(args.owner_user_id)
        except StateError as error:
            print(str(error), file=sys.stderr); return 3
        print(json.dumps({"packs": records}, ensure_ascii=False, sort_keys=True)); return 0
    raise AssertionError(f"Unhandled command: {args.command}")
