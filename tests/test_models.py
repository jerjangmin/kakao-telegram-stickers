from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "kakao-telegram-stickers" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from tele_sticker_maker.models import (
    ItemStatus,
    KakaoStickerItem,
    ManifestV2,
    PreparedSticker,
    SourceKind,
    TelegramFormat,
)


def test_manifest_v2_serializes_legacy_item_fields_and_status():
    item = KakaoStickerItem(
        index=1,
        source_url="https://cdn.example/sticker.webp",
        thumbnail_url="https://cdn.example/thumbnail.png",
        animated_url="https://cdn.example/sticker.webp",
        file="webp/sticker_01.webp",
        preview_file="png/sticker_01.png",
        content_type="image/webp",
        image_format="WEBP",
        width=360,
        height=360,
        api_width=360,
        api_height=360,
        frames=12,
        animated=True,
        byte_size=1234,
        source_kind=SourceKind.ANIMATED_WEBP,
        status=ItemStatus.DOWNLOADED,
        source_sha256="a" * 64,
        duration_ms=600,
    )

    manifest = ManifestV2(
        slug="example",
        source_page="https://e.kakao.com/t/example",
        api_url="https://e.kakao.com/api/items/example",
        items=(item,),
    )

    assert manifest.to_dict() == {
        "schemaVersion": 2,
        "slug": "example",
        "sourcePage": "https://e.kakao.com/t/example",
        "apiUrl": "https://e.kakao.com/api/items/example",
        "count": 1,
        "items": [
            {
                "index": 1,
                "sourceUrl": "https://cdn.example/sticker.webp",
                "thumbnailUrl": "https://cdn.example/thumbnail.png",
                "animatedUrl": "https://cdn.example/sticker.webp",
                "file": "webp/sticker_01.webp",
                "previewFile": "png/sticker_01.png",
                "contentType": "image/webp",
                "format": "WEBP",
                "width": 360,
                "height": 360,
                "apiWidth": 360,
                "apiHeight": 360,
                "frames": 12,
                "animated": True,
                "bytes": 1234,
                "sourceKind": "animated_webp",
                "status": "downloaded",
                "sourceSha256": "a" * 64,
                "durationMs": 600,
            }
        ],
    }


def test_prepared_sticker_adds_optional_telegram_metadata_without_mutating_source():
    source = KakaoStickerItem(
        index=2,
        source_url="https://cdn.example/sticker.png",
        thumbnail_url=None,
        animated_url=None,
        file="png/sticker_02.png",
        preview_file="png/sticker_02.png",
        content_type="image/png",
        image_format="PNG",
        width=512,
        height=512,
        api_width=None,
        api_height=None,
        frames=1,
        animated=False,
        byte_size=456,
        source_kind=SourceKind.STATIC_PNG,
    )
    prepared = PreparedSticker(
        source=source,
        telegram_path="telegram/sticker_02.png",
        telegram_sha256="b" * 64,
        telegram_format=TelegramFormat.STATIC,
        duration_ms=None,
        status=ItemStatus.READY,
    )

    assert source.status is ItemStatus.DISCOVERED
    assert prepared.to_manifest_dict() == {
        **source.to_manifest_dict(),
        "telegram": {
            "file": "telegram/sticker_02.png",
            "sha256": "b" * 64,
            "format": "static",
            "durationMs": None,
            "status": "ready",
        },
    }
