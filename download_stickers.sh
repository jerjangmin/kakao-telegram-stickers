#!/bin/bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
runner="$script_dir/skills/kakao-telegram-stickers/scripts/run.py"

if [ "$#" -eq 0 ]; then
  set -- \
    "nyang-nyang-special" \
    "baseball-fan-choonsik" \
    "hangyodon-x-kakao-friends" \
    "baby-choonsik-2" \
    "baby-choonsik" \
    "summercat-choonsik" \
    "sloppy-choonsik" \
    "sloppy-choonsik-again" \
    "choonsik-meow" \
    "choonsik-and-mystery-nyan" \
    "choonsusan-is-open" \
    "mini-mongmong-is-here"
fi

uv run --quiet "$runner" download "$@"
