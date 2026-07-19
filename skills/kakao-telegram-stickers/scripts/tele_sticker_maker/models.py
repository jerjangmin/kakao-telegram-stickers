"""Explicit, JSON-serializable domain models for sticker import manifests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence, Union


class SourceKind(str, Enum):
    """The source media selected from Kakao's item response."""

    ANIMATED_WEBP = "animated_webp"
    STATIC_PNG = "static_png"


class TelegramFormat(str, Enum):
    """The Telegram format produced from a source sticker."""

    VIDEO = "video"
    STATIC = "static"


class ItemStatus(str, Enum):
    """Lifecycle states for an imported sticker item."""

    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    CONVERTED = "converted"
    READY = "ready"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_INVALID = "skipped_invalid"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass(frozen=True)
class KakaoStickerItem:
    """A source item and its preserved downloader manifest metadata."""

    index: int
    source_url: str
    thumbnail_url: Optional[str]
    animated_url: Optional[str]
    file: str
    preview_file: str
    content_type: Optional[str]
    image_format: str
    width: int
    height: int
    api_width: Optional[int]
    api_height: Optional[int]
    frames: int
    animated: bool
    byte_size: int
    source_kind: SourceKind
    status: ItemStatus = ItemStatus.DISCOVERED
    source_sha256: Optional[str] = None
    duration_ms: Optional[int] = None

    def to_manifest_dict(self) -> dict[str, Any]:
        """Serialize using existing manifest names plus v2 metadata."""
        return {
            "index": self.index,
            "sourceUrl": self.source_url,
            "thumbnailUrl": self.thumbnail_url,
            "animatedUrl": self.animated_url,
            "file": self.file,
            "previewFile": self.preview_file,
            "contentType": self.content_type,
            "format": self.image_format,
            "width": self.width,
            "height": self.height,
            "apiWidth": self.api_width,
            "apiHeight": self.api_height,
            "frames": self.frames,
            "animated": self.animated,
            "bytes": self.byte_size,
            "sourceKind": self.source_kind.value,
            "status": self.status.value,
            "sourceSha256": self.source_sha256,
            "durationMs": self.duration_ms,
        }


@dataclass(frozen=True)
class PreparedSticker:
    """Telegram conversion metadata associated with one source sticker."""

    source: KakaoStickerItem
    telegram_path: Optional[str]
    telegram_sha256: Optional[str]
    telegram_format: Optional[TelegramFormat]
    duration_ms: Optional[int]
    status: ItemStatus
    error: Optional[str] = None

    def to_manifest_dict(self) -> dict[str, Any]:
        """Add optional Telegram-derived data without changing source fields."""
        manifest_item = self.source.to_manifest_dict()
        telegram = {
            "file": self.telegram_path,
            "sha256": self.telegram_sha256,
            "format": self.telegram_format.value if self.telegram_format else None,
            "durationMs": self.duration_ms,
            "status": self.status.value,
        }
        if self.error is not None:
            telegram["error"] = self.error
        manifest_item["telegram"] = telegram
        return manifest_item


@dataclass(frozen=True)
class ManifestV2:
    """Version 2 of the on-disk Kakao download manifest."""

    slug: str
    source_page: str
    api_url: str
    items: Sequence[Union[KakaoStickerItem, PreparedSticker]]

    @property
    def schema_version(self) -> int:
        return 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "slug": self.slug,
            "sourcePage": self.source_page,
            "apiUrl": self.api_url,
            "count": len(self.items),
            "items": [item.to_manifest_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ImportSummary:
    """Counts returned after preparing or publishing an import job."""

    discovered: int
    ready: int
    skipped: int
    failed: int

    def to_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "ready": self.ready,
            "skipped": self.skipped,
            "failed": self.failed,
        }
