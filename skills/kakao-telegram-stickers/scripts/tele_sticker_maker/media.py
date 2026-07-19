"""Raw Kakao media preservation and manifest writing."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
import time
import threading
import uuid
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, UnidentifiedImageError

from .kakao import KakaoClient, KakaoError, RemoteSticker
from .models import ItemStatus, KakaoStickerItem, ManifestV2, PreparedSticker, SourceKind, TelegramFormat
from .webp import WebPError, make_animated_webm, make_static_png

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
WEBP_MAGIC = b"RIFF"


class MediaError(RuntimeError):
    """A downloaded file does not meet the source preservation contract."""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _decode(raw: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(raw)) as image:
            image.verify()
        image = Image.open(BytesIO(raw))
        image.load()
        return image
    except (UnidentifiedImageError, OSError) as error:
        raise MediaError("이미지 magic 또는 Pillow decode 검증에 실패했습니다") from error


def _inspect(raw: bytes, remote: RemoteSticker) -> tuple[Image.Image, SourceKind]:
    image = _decode(raw)
    is_webp = raw.startswith(WEBP_MAGIC) and raw[8:12] == b"WEBP" and image.format == "WEBP"
    is_png = raw.startswith(PNG_MAGIC) and image.format == "PNG"
    frames = getattr(image, "n_frames", 1)
    animated = bool(getattr(image, "is_animated", False) or frames > 1)
    if is_webp and animated:
        if not remote.animated_url:
            raise MediaError("animatedUrl 없는 원본은 static PNG여야 합니다")
        kind = SourceKind.ANIMATED_WEBP
    elif is_png and not animated:
        kind = SourceKind.STATIC_PNG
    else:
        raise MediaError("원본은 animated WebP 또는 static PNG여야 합니다")
    if remote.api_width is not None and image.width != remote.api_width:
        raise MediaError("API 너비와 실제 이미지 너비가 일치하지 않습니다")
    if remote.api_height is not None and image.height != remote.api_height:
        raise MediaError("API 높이와 실제 이미지 높이가 일치하지 않습니다")
    return image, kind


def _save_preview(image: Image.Image, path: Path) -> None:
    image.seek(0)
    rendered = BytesIO()
    image.convert("RGBA").save(rendered, format="PNG")
    _atomic_write(path, rendered.getvalue())


def _store_item(index: int, remote: RemoteSticker, raw: bytes, content_type: Optional[str], output: Path) -> KakaoStickerItem:
    image, kind = _inspect(raw, remote)
    extension = "webp" if kind is SourceKind.ANIMATED_WEBP else "png"
    relative_file = "{}/sticker_{:02d}.{}".format(extension, index, extension)
    relative_preview = "png/sticker_{:02d}.png".format(index)
    _atomic_write(output / relative_file, raw)
    preview_path = output / relative_preview
    if kind is SourceKind.ANIMATED_WEBP:
        _save_preview(image, preview_path)
    elif preview_path.as_posix() != (output / relative_file).as_posix():
        _atomic_write(preview_path, raw)
    frames = getattr(image, "n_frames", 1)
    return KakaoStickerItem(
        index=index,
        source_url=remote.source_url,
        thumbnail_url=remote.thumbnail_url,
        animated_url=remote.animated_url,
        file=relative_file,
        preview_file=relative_preview,
        content_type=content_type,
        image_format=image.format or extension.upper(),
        width=image.width,
        height=image.height,
        api_width=remote.api_width,
        api_height=remote.api_height,
        frames=frames,
        animated=kind is SourceKind.ANIMATED_WEBP,
        byte_size=len(raw),
        source_kind=kind,
        status=ItemStatus.DOWNLOADED,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class _SetLock:
    """Cross-platform advisory file lock, released automatically on process exit."""

    def __init__(self, root: Path, slug: str):
        self.path = root / ".{}.lock".format(slug)
        self._thread_lock: Optional[threading.Lock] = None
        self._handle = None

    def __enter__(self) -> "_SetLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(str(self.path.resolve()), threading.Lock())
        self._thread_lock.acquire()
        try:
            self._handle = self.path.open("a+b")
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0, os.SEEK_END)
                if self._handle.tell() == 0:
                    self._handle.write(b"\0")
                    self._handle.flush()
                self._handle.seek(0)
                while True:
                    try:
                        msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        time.sleep(0.1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            return self
        except BaseException:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # type: ignore[no-untyped-def]
        try:
            if self._handle is not None:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()
                self._handle = None
        finally:
            if self._thread_lock is not None:
                self._thread_lock.release()


def _recover_orphans(output: Path) -> None:
    """Restore the newest interrupted swap backup and remove stale temporary sets."""
    backups = sorted(
        output.parent.glob(".{}.backup-*".format(output.name)),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not output.exists() and backups:
        os.replace(backups.pop(0), output)
    for backup in backups:
        shutil.rmtree(backup, ignore_errors=True)
    for staging in output.parent.glob(".{}.staging-*".format(output.name)):
        shutil.rmtree(staging, ignore_errors=True)


def _swap_set(staging: Path, output: Path) -> None:
    """Replace a complete set directory while retaining a rollback copy on failure."""
    backup: Optional[Path] = None
    try:
        if output.exists():
            backup = output.parent / ".{}.backup-{}".format(output.name, uuid.uuid4().hex)
            os.replace(output, backup)
        os.replace(staging, output)
    except OSError as error:
        if backup is not None and backup.exists():
            try:
                if output.exists():
                    shutil.rmtree(output)
                os.replace(backup, output)
            except OSError:
                pass
        raise MediaError("세트 교체에 실패했습니다") from error
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def promote_set(source_set: Path, canonical_root: Path) -> None:
    """Atomically promote a complete private workspace set to its canonical path."""
    source = Path(source_set)
    root = Path(canonical_root)
    output = root / source.name
    staging: Optional[Path] = None
    try:
        if not source.is_dir() or source.is_symlink():
            raise MediaError("완전한 작업 세트를 찾을 수 없습니다")
        with _SetLock(root, source.name):
            _recover_orphans(output)
            staging = Path(tempfile.mkdtemp(prefix=".{}.staging-".format(source.name), dir=str(root)))
            shutil.copytree(source, staging, dirs_exist_ok=True)
            _swap_set(staging, output)
            staging = None
    except MediaError:
        raise
    except OSError as error:
        raise MediaError("세트 파일을 저장하지 못했습니다") from error
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def prepare_telegram_item(item: KakaoStickerItem, set_root: Path, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> PreparedSticker:
    """Create one Telegram derivative while leaving preserved source files untouched.

    Conversion failures are represented in the returned item so batch preparation can
    continue and persist an actionable manifest status.
    """
    source = set_root / item.file
    extension = "webm" if item.source_kind is SourceKind.ANIMATED_WEBP else "png"
    relative = "telegram/sticker_{:02d}.{}".format(item.index, extension)
    destination = set_root / relative
    try:
        if item.source_kind is SourceKind.ANIMATED_WEBP:
            validation = make_animated_webm(source, destination, ffmpeg=ffmpeg, ffprobe=ffprobe)
            duration_ms = validation.duration_ms
            format_ = TelegramFormat.VIDEO
        else:
            make_static_png(source, destination)
            duration_ms = None
            format_ = TelegramFormat.STATIC
        derived = destination.read_bytes()
        return PreparedSticker(
            source=item,
            telegram_path=relative,
            telegram_sha256=hashlib.sha256(derived).hexdigest(),
            telegram_format=format_,
            duration_ms=duration_ms,
            status=ItemStatus.READY,
        )
    except (OSError, WebPError) as error:
        return PreparedSticker(
            source=item,
            telegram_path=None,
            telegram_sha256=None,
            telegram_format=None,
            duration_ms=None,
            status=ItemStatus.FAILED,
            error=str(error),
        )


def download_set(value: str, output_root: Path = Path("stickers"), client: Optional[KakaoClient] = None) -> ManifestV2:
    """Download one complete set to staging, then atomically replace ``{slug}``."""
    kakao = client or KakaoClient()
    slug, source_page = kakao.resolve(value)
    root = Path(output_root)
    output = root / slug
    staging: Optional[Path] = None
    try:
        with _SetLock(root, slug):
            _recover_orphans(output)
            staging = Path(tempfile.mkdtemp(prefix=".{}.staging-".format(slug), dir=str(root)))
            api_url, remote_items = kakao.fetch_items(slug)
            items = []
            for index, remote in enumerate(remote_items, 1):
                try:
                    raw, content_type = kakao.download(remote.source_url)
                    items.append(_store_item(index, remote, raw, content_type, staging))
                except KakaoError as error:
                    raise MediaError("sticker_{:02d}: {}".format(index, error)) from error
            manifest = ManifestV2(slug=slug, source_page=source_page, api_url=api_url, items=tuple(items))
            _atomic_write(staging / "json" / "manifest.json", (json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            _swap_set(staging, output)
            staging = None
            return manifest
    except (KakaoError, MediaError):
        raise
    except OSError as error:
        raise MediaError("세트 파일을 저장하지 못했습니다") from error
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
