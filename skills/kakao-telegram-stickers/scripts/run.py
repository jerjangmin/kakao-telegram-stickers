#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = ["Pillow==11.3.0"]
# ///
"""Standalone entry point for the Kakao Telegram sticker tools."""

from tele_sticker_maker.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
