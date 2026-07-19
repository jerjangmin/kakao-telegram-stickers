"""Explicit, JSON-serializable domain models for sticker import manifests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence, Union


class LayoutKind(str, Enum):
    """The visual sizing policy selected for one Kakao set."""

    MINI = "mini"
    STANDARD = "standard"
    UNKNOWN = "unknown"


class LayoutError(ValueError):
    """A Kakao set layout cannot be selected safely."""


@dataclass(frozen=True)
class KakaoSetMetadata:
    """Set-level type flags returned by Kakao's current item API."""

    is_mini: Optional[bool]
    is_big: Optional[bool]
    is_sound: Optional[bool]


@dataclass(frozen=True)
class RenderLayout:
    """A contain-and-center policy used to render Telegram derivatives."""

    kind: LayoutKind
    content_box: tuple[int, int]
    canvas_size: Optional[tuple[int, int]] = None


@dataclass(frozen=True)
class LayoutDecision:
    kind: LayoutKind
    layout: RenderLayout
    requested_mode: str
    decision_source: str
    manual_override: bool = False
    warnings: tuple[str, ...] = ()


STANDARD_LAYOUT = RenderLayout(LayoutKind.STANDARD, (512, 512))
MINI_LAYOUT = RenderLayout(LayoutKind.MINI, (250, 250), (512, 512))


def resolve_layout(metadata: KakaoSetMetadata, requested_mode: str = "auto") -> LayoutDecision:
    """Resolve Kakao's explicit mini flag into an immutable render policy."""
    if requested_mode not in {"auto", "mini", "standard"}:
        raise LayoutError("layout mode는 auto, mini, standard 중 하나여야 합니다")
    if requested_mode == "auto" and metadata.is_mini is True:
        return LayoutDecision(LayoutKind.MINI, MINI_LAYOUT, requested_mode, "contents.isMini")
    if requested_mode == "auto" and metadata.is_mini is False:
        return LayoutDecision(LayoutKind.STANDARD, STANDARD_LAYOUT, requested_mode, "contents.isMini")
    if requested_mode in {"mini", "standard"}:
        kind = LayoutKind.MINI if requested_mode == "mini" else LayoutKind.STANDARD
        layout = MINI_LAYOUT if kind is LayoutKind.MINI else STANDARD_LAYOUT
        conflict = metadata.is_mini is not None and metadata.is_mini != (kind is LayoutKind.MINI)
        warnings = ("manual_layout_override_conflict",) if conflict else ()
        return LayoutDecision(kind, layout, requested_mode, "manual_override", True, warnings)
    raise LayoutError("카카오 API에서 미니 이모지 여부를 판별할 수 없습니다")


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
    render_layout: RenderLayout = STANDARD_LAYOUT

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
    """Backward-compatible manifest with optional v3 set traits and layout."""

    slug: str
    source_page: str
    api_url: str
    items: Sequence[Union[KakaoStickerItem, PreparedSticker]]
    source_traits: Optional[KakaoSetMetadata] = None
    layout_decision: Optional[LayoutDecision] = None

    @property
    def schema_version(self) -> int:
        return 3 if self.layout_decision is not None else 2

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schemaVersion": self.schema_version,
            "slug": self.slug,
            "sourcePage": self.source_page,
            "apiUrl": self.api_url,
            "count": len(self.items),
            "items": [item.to_manifest_dict() for item in self.items],
        }
        if self.source_traits is not None:
            result["sourceTraits"] = {
                "isMini": self.source_traits.is_mini,
                "isBig": self.source_traits.is_big,
                "isSound": self.source_traits.is_sound,
            }
        if self.layout_decision is not None:
            decision = self.layout_decision
            result["layout"] = {
                "kind": decision.kind.value,
                "requestedMode": decision.requested_mode,
                "decisionSource": decision.decision_source,
                "manualOverride": decision.manual_override,
                "canvas": list(decision.layout.canvas_size) if decision.layout.canvas_size else None,
                "contentBox": list(decision.layout.content_box),
                "warnings": list(decision.warnings),
            }
        return result


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
