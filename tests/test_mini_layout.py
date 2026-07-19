from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "kakao-telegram-stickers" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from tele_sticker_maker.kakao import parse_set
from tele_sticker_maker.models import LayoutError, LayoutKind, resolve_layout


def _payload(is_mini):
    return {
        "contents": {
            "isMini": is_mini,
            "isBig": False,
            "isSound": False,
            "items": [
                {
                    "animatedUrl": "https://item.kakaocdn.net/a.webp",
                    "thumbnailUrl": "https://item.kakaocdn.net/a.png",
                    "width": 180,
                    "height": 180,
                }
            ],
        }
    }


def test_api_is_mini_true_selects_mini_layout():
    response = parse_set(_payload(True), "https://e.kakao.com/api/items/mini")

    decision = resolve_layout(response.metadata, "auto")

    assert response.metadata.is_mini is True
    assert decision.kind is LayoutKind.MINI
    assert decision.layout.canvas_size == (512, 512)
    assert decision.layout.content_box == (250, 250)
    assert decision.decision_source == "contents.isMini"


def test_api_is_mini_false_selects_standard_layout():
    response = parse_set(_payload(False), "https://e.kakao.com/api/items/standard")

    decision = resolve_layout(response.metadata, "auto")

    assert decision.kind is LayoutKind.STANDARD
    assert decision.layout.canvas_size is None
    assert decision.layout.content_box == (512, 512)


def test_manual_standard_override_records_conflict_with_mini_api():
    response = parse_set(_payload(True), "https://e.kakao.com/api/items/mini")

    decision = resolve_layout(response.metadata, "standard")

    assert decision.kind is LayoutKind.STANDARD
    assert decision.manual_override is True
    assert decision.decision_source == "manual_override"
    assert decision.warnings == ("manual_layout_override_conflict",)
