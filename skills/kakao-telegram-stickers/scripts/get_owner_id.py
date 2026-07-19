#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# ///
"""Safely find the Telegram user who sent an exact one-time marker."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Callable, Optional

MAX_RESPONSE_BYTES = 1_048_576
MAX_MARKER_LENGTH = 256
ENV_NAME = "TELEGRAM_BOT_TOKEN"


class OwnerLookupError(RuntimeError):
    """A deliberately non-sensitive lookup failure."""


def _safe_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return cleaned[:128] or None


def _read_json(response: Any) -> dict[str, Any]:
    headers = getattr(response, "headers", {})
    content_length = headers.get("Content-Length") if hasattr(headers, "get") else None
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise OwnerLookupError
        except ValueError:
            raise OwnerLookupError from None

    body = response.read(MAX_RESPONSE_BYTES + 1)
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise OwnerLookupError
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OwnerLookupError from None
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("result"), list):
        raise OwnerLookupError
    return payload


def fetch_updates(token: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&limit=100",
        headers={"Accept": "application/json"},
    )
    response = None
    try:
        response = opener(request, timeout=15)
        return _read_json(response)
    except Exception:
        raise OwnerLookupError from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def marker_candidates(payload: dict[str, Any], marker: str) -> list[dict[str, Any]]:
    """Return at most one sanitized record per Telegram user ID."""
    candidates: dict[int, dict[str, Any]] = {}
    for update in payload["result"]:
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        if not isinstance(message, dict) or message.get("text") != marker:
            continue
        sender = message.get("from")
        if not isinstance(sender, dict):
            continue
        user_id = sender.get("id")
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            continue
        candidates[user_id] = {
            "id": user_id,
            "username": _safe_text(sender.get("username")),
            "first_name": _safe_text(sender.get("first_name")),
        }
    return [candidates[user_id] for user_id in sorted(candidates)]


def main(argv: Optional[list[str]] = None, *, opener: Callable[..., Any] = urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser(description="Find one Telegram owner by an exact marker")
    parser.add_argument("--marker", required=True)
    args = parser.parse_args(argv)
    token = os.getenv(ENV_NAME, "")
    if not token.strip() or not args.marker or len(args.marker) > MAX_MARKER_LENGTH:
        print("Owner lookup failed.", file=sys.stderr)
        return 3
    try:
        candidates = marker_candidates(fetch_updates(token.strip(), opener=opener), args.marker)
        if len(candidates) != 1:
            raise OwnerLookupError
    except Exception:
        print("Owner lookup failed.", file=sys.stderr)
        return 3
    print(json.dumps({"candidates": candidates}, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
