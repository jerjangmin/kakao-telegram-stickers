from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "kakao-telegram-stickers" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from tele_sticker_maker.kakao import KakaoError, KakaoSetResponse, RemoteSticker
from tele_sticker_maker import media
from tele_sticker_maker.media import MediaError, _SetLock, download_set, prepare_telegram_item
from tele_sticker_maker.models import ItemStatus, KakaoSetMetadata, TelegramFormat


def png_bytes(size=(8, 6), color=(255, 0, 0, 128)):
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


def animated_webp_bytes(size=(8, 6)):
    output = BytesIO()
    first = Image.new("RGBA", size, (255, 0, 0, 0))
    second = Image.new("RGBA", size, (0, 0, 255, 255))
    first.save(output, format="WEBP", save_all=True, append_images=[second], duration=100, loop=0, lossless=True)
    return output.getvalue()


def static_webp_bytes(size=(8, 6)):
    output = BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 128)).save(output, format="WEBP", lossless=True)
    return output.getvalue()


def animated_png_bytes(size=(8, 6)):
    output = BytesIO()
    first = Image.new("RGBA", size, (255, 0, 0, 0))
    second = Image.new("RGBA", size, (0, 0, 255, 255))
    first.save(output, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
    return output.getvalue()


class Client:
    def __init__(self, remotes, blobs):
        self.remotes = remotes
        self.blobs = blobs

    def resolve(self, value):
        return "set-name", "https://e.kakao.com/t/set-name"

    def fetch_items(self, slug):
        return "https://e.kakao.com/api/items/set-name", self.remotes

    def download(self, url):
        return self.blobs[url], "image/webp" if url.endswith(".webp") else "image/png"


def test_static_png_is_preserved_with_manifest_v2_and_sha256(tmp_path):
    raw = png_bytes()
    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    manifest = download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))
    output = tmp_path / "set-name"

    assert (output / "png/sticker_01.png").read_bytes() == raw
    assert manifest.items[0].source_sha256 == hashlib.sha256(raw).hexdigest()
    saved = json.loads((output / "json/manifest.json").read_text())
    assert saved["schemaVersion"] == 2
    assert saved["items"][0]["file"] == "png/sticker_01.png"
    assert saved["items"][0]["previewFile"] == "png/sticker_01.png"


def test_static_telegram_derivative_does_not_mutate_preserved_source(tmp_path):
    raw = png_bytes((80, 20), (10, 20, 30, 77))
    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 80, 20)
    manifest = download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))
    source = tmp_path / "set-name" / manifest.items[0].file
    before = source.read_bytes()

    prepared = prepare_telegram_item(manifest.items[0], tmp_path / "set-name")

    assert prepared.status is ItemStatus.READY
    assert prepared.telegram_format is TelegramFormat.STATIC
    assert prepared.telegram_path == "telegram/sticker_01.png"
    assert source.read_bytes() == before
    with Image.open(tmp_path / "set-name" / prepared.telegram_path) as image:
        assert image.size == (512, 128)
        assert image.getpixel((0, 0))[3] == 77


def test_mini_set_records_layout_and_prepares_centered_derivative(tmp_path):
    raw = png_bytes((180, 180), (10, 20, 30, 255))
    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 180, 180)

    class MiniClient(Client):
        def fetch_set(self, slug):
            return KakaoSetResponse(
                "https://e.kakao.com/api/items/set-name",
                KakaoSetMetadata(True, False, False),
                (remote,),
            )

    manifest = download_set("set-name", tmp_path, MiniClient([remote], {remote.source_url: raw}), layout_mode="auto")
    prepared = prepare_telegram_item(manifest.items[0], tmp_path / "set-name")
    saved = json.loads((tmp_path / "set-name/json/manifest.json").read_text())

    assert saved["schemaVersion"] == 3
    assert saved["sourceTraits"]["isMini"] is True
    assert saved["layout"] == {
        "kind": "mini",
        "requestedMode": "auto",
        "decisionSource": "contents.isMini",
        "manualOverride": False,
        "canvas": [512, 512],
        "contentBox": [250, 250],
        "warnings": [],
    }
    with Image.open(tmp_path / "set-name" / prepared.telegram_path) as image:
        assert image.size == (512, 512)
        assert image.getbbox() == (131, 131, 381, 381)


def test_telegram_conversion_failure_is_structured_on_the_prepared_item(tmp_path):
    raw = png_bytes()
    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    manifest = download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))
    (tmp_path / "set-name" / manifest.items[0].file).unlink()

    prepared = prepare_telegram_item(manifest.items[0], tmp_path / "set-name")

    assert prepared.status is ItemStatus.FAILED
    assert prepared.telegram_path is None
    assert prepared.error


def test_animated_webp_is_byte_exact_and_preview_is_first_frame_png(tmp_path):
    raw = animated_webp_bytes()
    remote = RemoteSticker("https://item.kakaocdn.net/a.webp", "https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.webp", 8, 6)
    download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))
    output = tmp_path / "set-name"

    assert (output / "webp/sticker_01.webp").read_bytes() == raw
    with Image.open(output / "png/sticker_01.png") as preview:
        assert preview.format == "PNG"
        assert preview.size == (8, 6)


def test_animated_url_with_static_png_body_is_preserved_as_static_png(tmp_path):
    raw = png_bytes()
    animated_url = "https://item.kakaocdn.net/a.webp"
    remote = RemoteSticker(animated_url, "https://item.kakaocdn.net/a.png", animated_url, 8, 6)

    manifest = download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))
    saved = json.loads((tmp_path / "set-name" / "json" / "manifest.json").read_text())

    assert (tmp_path / "set-name" / "png/sticker_01.png").read_bytes() == raw
    assert manifest.items[0].source_kind.value == "static_png"
    assert manifest.items[0].animated is False
    assert saved["items"][0]["animatedUrl"] == animated_url
    assert saved["items"][0]["sourceKind"] == "static_png"
    assert saved["items"][0]["animated"] is False


@pytest.mark.parametrize("raw", [static_webp_bytes(), animated_png_bytes()])
def test_unsupported_static_webp_and_animated_png_are_rejected(tmp_path, raw):
    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    with pytest.raises(MediaError, match="animated WebP 또는 static PNG"):
        download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))


def test_static_url_animated_webp_and_dimension_mismatch_are_rejected(tmp_path):
    raw = png_bytes()
    static_url = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    with pytest.raises(MediaError, match="animatedUrl 없는 원본"):
        download_set("set-name", tmp_path, Client([static_url], {static_url.source_url: animated_webp_bytes()}))

    width_mismatch = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 9, None)
    with pytest.raises(MediaError, match="너비"):
        download_set("set-name", tmp_path, Client([width_mismatch], {width_mismatch.source_url: raw}))

    height_mismatch = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, None, 7)
    with pytest.raises(MediaError, match="높이"):
        download_set("set-name", tmp_path, Client([height_mismatch], {height_mismatch.source_url: raw}))


def test_failed_redownload_keeps_existing_set_bytes_and_manifest(tmp_path):
    raw = png_bytes()
    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))
    output = tmp_path / "set-name"
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    invalid = RemoteSticker("https://item.kakaocdn.net/b.png", "https://item.kakaocdn.net/b.png", None, 8, 6)
    with pytest.raises(MediaError, match="animatedUrl 없는 원본"):
        download_set("set-name", tmp_path, Client([invalid], {invalid.source_url: animated_webp_bytes()}))

    after = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    assert after == before
    assert not list(tmp_path.glob(".set-name.staging-*"))


def test_swap_failure_restores_existing_set_bytes_and_manifest(tmp_path, monkeypatch):
    raw = png_bytes()
    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))
    output = tmp_path / "set-name"
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    replace = media.os.replace

    def fail_staging_swap(source, destination):
        if Path(source).name.startswith(".set-name.staging-") and Path(destination) == output:
            raise OSError("simulated swap failure")
        return replace(source, destination)

    monkeypatch.setattr(media.os, "replace", fail_staging_swap)
    with pytest.raises(MediaError, match="교체"):
        download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))

    after = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    assert after == before
    assert not list(tmp_path.glob(".set-name.backup-*"))


def test_orphaned_backup_is_restored_and_stale_lock_can_be_reacquired(tmp_path):
    output = tmp_path / "set-name"
    old_backup = tmp_path / ".set-name.backup-old"
    newest_backup = tmp_path / ".set-name.backup-new"
    stale_staging = tmp_path / ".set-name.staging-crashed"
    old_backup.mkdir()
    newest_backup.mkdir()
    stale_staging.mkdir()
    (old_backup / "marker").write_text("old")
    (newest_backup / "marker").write_text("newest")
    os.utime(old_backup, (1, 1))
    os.utime(newest_backup, (2, 2))
    (tmp_path / ".set-name.lock").write_text("left by a crashed process")

    class FailingClient(Client):
        def fetch_items(self, slug):
            raise KakaoError("offline")

    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    with pytest.raises(KakaoError, match="offline"):
        download_set("set-name", tmp_path, FailingClient([remote], {remote.source_url: png_bytes()}))

    assert (output / "marker").read_text() == "newest"
    assert not old_backup.exists()
    assert not newest_backup.exists()
    assert not stale_staging.exists()
    assert (tmp_path / ".set-name.lock").exists()

    download_set("set-name", tmp_path, Client([remote], {remote.source_url: png_bytes()}))
    assert (output / "png/sticker_01.png").exists()


def test_windows_lock_uses_a_single_fixed_byte(monkeypatch, tmp_path):
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.calls = []

        def locking(self, descriptor, mode, length):
            self.calls.append((mode, os.lseek(descriptor, 0, os.SEEK_CUR), length))

    fake_msvcrt = FakeMsvcrt()
    monkeypatch.setattr(media.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    first = _SetLock(tmp_path, "set-name")
    second = _SetLock(tmp_path, "set-name")
    with first:
        assert first.path.stat().st_size == 1
    with second:
        assert second.path.stat().st_size == 1

    assert fake_msvcrt.calls == [
        (fake_msvcrt.LK_NBLCK, 0, 1),
        (fake_msvcrt.LK_UNLCK, 0, 1),
        (fake_msvcrt.LK_NBLCK, 0, 1),
        (fake_msvcrt.LK_UNLCK, 0, 1),
    ]


def test_windows_lock_failure_releases_the_process_thread_lock(monkeypatch, tmp_path):
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.fail_next_lock = True

        def locking(self, descriptor, mode, length):
            if mode == self.LK_NBLCK and self.fail_next_lock:
                self.fail_next_lock = False
                raise OSError("simulated locking failure")

    fake_msvcrt = FakeMsvcrt()
    monkeypatch.setattr(media.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    with pytest.raises(OSError, match="simulated"):
        with _SetLock(tmp_path, "set-name"):
            pass
    with _SetLock(tmp_path, "set-name"):
        pass


def test_windows_lock_retries_contention_beyond_msvcrt_legacy_limit(monkeypatch, tmp_path):
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.attempts = 0

        def locking(self, descriptor, mode, length):
            if mode == self.LK_NBLCK:
                self.attempts += 1
                if self.attempts <= 15:
                    raise OSError(errno.EACCES, "busy")

    fake_msvcrt = FakeMsvcrt()
    sleeps = []
    monkeypatch.setattr(media.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(media.time, "sleep", sleeps.append)

    with _SetLock(tmp_path, "set-name"):
        pass

    assert fake_msvcrt.attempts == 16
    assert sleeps == [0.1] * 15


def test_windows_lock_propagates_non_contention_errors(monkeypatch, tmp_path):
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def locking(self, descriptor, mode, length):
            if mode == self.LK_NBLCK:
                raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(media.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt())

    with pytest.raises(OSError, match="denied"):
        with _SetLock(tmp_path, "set-name"):
            pass


@pytest.mark.skipif(os.name == "nt", reason="TerminateProcess lock release timing is nondeterministic on hosted Windows runners")
def test_process_termination_releases_advisory_lock(tmp_path):
    child_code = "\n".join([
        "import sys",
        "import time",
        "from pathlib import Path",
        "sys.path.insert(0, sys.argv[1])",
        "from tele_sticker_maker.media import _SetLock",
        "with _SetLock(Path(sys.argv[2]), 'set-name'):",
        "    print('locked', flush=True)",
        "    time.sleep(30)",
    ])
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            str(SCRIPTS_DIR),
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        child.terminate()
        assert child.wait(timeout=5) != 0

        raw = png_bytes()
        remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
        download_set("set-name", tmp_path, Client([remote], {remote.source_url: raw}))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="real msvcrt smoke is Windows-only")
def test_windows_real_lock_round_trip_completes(tmp_path):
    child_code = "\n".join([
        "import sys",
        "from pathlib import Path",
        "sys.path.insert(0, sys.argv[1])",
        "from tele_sticker_maker.media import _SetLock",
        "with _SetLock(Path(sys.argv[2]), 'set-name'):",
        "    pass",
    ])
    result = subprocess.run(
        [sys.executable, "-c", child_code, str(SCRIPTS_DIR), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_orphaned_backup_and_staging_are_removed_when_output_exists(tmp_path):
    output = tmp_path / "set-name"
    output.mkdir()
    (output / "marker").write_text("live")
    backup = tmp_path / ".set-name.backup-crashed"
    staging = tmp_path / ".set-name.staging-crashed"
    backup.mkdir()
    staging.mkdir()

    class FailingClient(Client):
        def fetch_items(self, slug):
            raise KakaoError("offline")

    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    with pytest.raises(KakaoError, match="offline"):
        download_set("set-name", tmp_path, FailingClient([remote], {remote.source_url: png_bytes()}))

    assert (output / "marker").read_text() == "live"
    assert not backup.exists()
    assert not staging.exists()


def test_same_slug_downloads_are_serialized(tmp_path):
    raw = png_bytes()
    remote = RemoteSticker("https://item.kakaocdn.net/a.png", "https://item.kakaocdn.net/a.png", None, 8, 6)
    entered = threading.Event()
    release = threading.Event()
    fetch_calls = []

    class BlockingClient(Client):
        def fetch_items(self, slug):
            fetch_calls.append(slug)
            if len(fetch_calls) == 1:
                entered.set()
                assert release.wait(timeout=2)
            return super().fetch_items(slug)

    client = BlockingClient([remote], {remote.source_url: raw})
    errors = []

    def run_download():
        try:
            download_set("set-name", tmp_path, client)
        except Exception as error:  # pragma: no cover - assertion is below
            errors.append(error)

    first = threading.Thread(target=run_download)
    second = threading.Thread(target=run_download)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    time.sleep(0.15)
    assert fetch_calls == ["set-name"]
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert fetch_calls == ["set-name", "set-name"]
