from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "kakao-telegram-stickers" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import tele_sticker_maker.webp as webp
from tele_sticker_maker.models import MINI_LAYOUT
from tele_sticker_maker.webp import (
    CandidateValidationError,
    ToolError,
    WebPError,
    fit_size,
    inspect_animated_webp,
    make_animated_webm,
    make_static_png,
    parse_webp_timing,
    _scaled_durations,
    validate_telegram_video,
)


def _riff(*chunks: tuple[bytes, bytes]) -> bytes:
    body = b"WEBP" + b"".join(kind + len(payload).to_bytes(4, "little") + payload + (b"\0" if len(payload) % 2 else b"") for kind, payload in chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _animated_webp(path: Path, durations=(100, 200)) -> None:
    first = Image.new("RGBA", (18, 10), (255, 0, 0, 0))
    second = Image.new("RGBA", (18, 10), (0, 0, 255, 255))
    first.save(path, "WEBP", save_all=True, append_images=[second], duration=durations, loop=3, lossless=True)


def test_riff_parser_reads_anim_loop_anmf_24bit_duration_and_odd_padding():
    anim = b"\0\0\0\0" + (7).to_bytes(2, "little")
    anmf_a = b"\0" * 12 + (0).to_bytes(3, "little") + b"\0"
    anmf_b = b"\0" * 12 + (0x010203).to_bytes(3, "little") + b"\0"
    timing = parse_webp_timing(_riff((b"JUNK", b"x"), (b"ANIM", anim), (b"ANMF", anmf_a), (b"ANMF", anmf_b)))
    assert timing.loop_count == 7
    assert timing.frame_durations_ms == (1, 0x010203)


@pytest.mark.parametrize("raw", [b"RIFF\x04\0\0\0WEBP", _riff((b"ANMF", b"\0" * 15))[:-1]])
def test_riff_parser_rejects_truncated_or_inconsistent_size(raw):
    with pytest.raises(WebPError):
        parse_webp_timing(raw)


def test_static_png_is_rgba_and_fitted_without_mutating_source(tmp_path):
    source, destination = tmp_path / "source.png", tmp_path / "telegram.png"
    image = Image.new("RGBA", (100, 40), (10, 20, 30, 99))
    image.save(source)
    before = source.read_bytes()
    assert make_static_png(source, destination) == (512, 205)
    assert source.read_bytes() == before
    with Image.open(destination) as result:
        assert result.size == (512, 205)
        assert result.mode == "RGBA"
        assert result.getpixel((0, 0))[3] == 99


def test_fit_size_makes_long_side_exactly_512():
    assert fit_size(2, 9) == (114, 512)
    assert fit_size(9, 2) == (512, 114)


def test_mini_static_png_is_contained_in_centered_250_box(tmp_path):
    source, destination = tmp_path / "mini.png", tmp_path / "telegram.png"
    Image.new("RGBA", (180, 180), (10, 20, 30, 255)).save(source)
    before = source.read_bytes()

    assert make_static_png(source, destination, layout=MINI_LAYOUT) == (512, 512)

    assert source.read_bytes() == before
    with Image.open(destination) as result:
        assert result.size == (512, 512)
        assert result.getbbox() == (131, 131, 381, 381)
        assert result.getpixel((130, 130))[3] == 0
        assert result.getpixel((131, 131))[3] == 255


def test_long_animation_durations_are_proportionally_capped_with_nonzero_frames():
    scaled = _scaled_durations((1_000, 3_000))
    assert scaled == (738, 2_212)
    assert sum(scaled) <= 2_950
    assert min(scaled) >= 1
    assert scaled[1] / scaled[0] == pytest.approx(3, rel=0.01)
    assert _scaled_durations((0, 2_950)) == (2, 2_948)


def test_duration_scaling_uses_largest_remainders_without_tail_collapse():
    scaled = _scaled_durations((40,) * 100)
    assert sum(scaled) == 2_950
    assert min(scaled) >= 1
    assert max(scaled) - min(scaled) <= 1
    with pytest.raises(WebPError, match="프레임 수"):
        _scaled_durations((1,) * 2_951)


def _vp9_capable() -> bool:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return False
    result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], check=False, capture_output=True, text=True)
    return result.returncode == 0 and "libvpx-vp9" in result.stdout


@pytest.mark.skipif(not _vp9_capable() or os.environ.get("TELE_STICKER_SKIP_VP9_E2E") == "1", reason="real VP9 E2E disabled or encoder unavailable")
def test_animated_webp_becomes_valid_telegram_webm(tmp_path):
    source, output = tmp_path / "source.webp", tmp_path / "sticker.webm"
    _animated_webp(source)
    timing = inspect_animated_webp(source)
    assert len(timing.frame_durations_ms) == 2
    result = make_animated_webm(source, output)
    assert result.width == 512
    assert output.stat().st_size <= 256 * 1024
    assert validate_telegram_video(output).duration_ms <= 3000


@pytest.mark.skipif(not _vp9_capable() or os.environ.get("TELE_STICKER_SKIP_VP9_E2E") == "1", reason="real VP9 E2E disabled or encoder unavailable")
def test_mini_animated_webp_uses_square_transparent_canvas(tmp_path):
    source, output = tmp_path / "mini.webp", tmp_path / "mini.webm"
    _animated_webp(source)

    result = make_animated_webm(source, output, layout=MINI_LAYOUT)

    assert (result.width, result.height) == (512, 512)
    assert result.byte_size <= 256 * 1024


@pytest.mark.parametrize("durations", [(2_000, 2_000), (3_000, 3_000)])
@pytest.mark.skipif(not _vp9_capable() or os.environ.get("TELE_STICKER_SKIP_VP9_E2E") == "1", reason="real VP9 E2E disabled or encoder unavailable")
def test_long_animations_encode_within_three_second_boundary(tmp_path, durations):
    source, output = tmp_path / "source.webp", tmp_path / "sticker.webm"
    _animated_webp(source, durations)
    assert make_animated_webm(source, output).duration_ms <= 3_000


def test_validator_rejects_fps_above_30_before_alpha_decode(tmp_path, monkeypatch):
    video = tmp_path / "high-fps.webm"
    video.write_bytes(b"x")
    monkeypatch.setattr(webp, "_ffprobe", lambda *args: {
        "streams": [{"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p", "width": 512, "height": 512, "avg_frame_rate": "60/1", "tags": {"ALPHA_MODE": "1"}}],
        "format": {"format_name": "matroska,webm", "duration": "1.0"},
    })
    with pytest.raises(CandidateValidationError, match="fps"):
        validate_telegram_video(video)


def test_static_png_size_cap_preserves_old_destination(tmp_path):
    source, destination = tmp_path / "source.png", tmp_path / "telegram.png"
    Image.frombytes("RGBA", (512, 512), os.urandom(512 * 512 * 4)).save(source)
    destination.write_bytes(b"old")

    with pytest.raises(CandidateValidationError, match="512KB"):
        make_static_png(source, destination)

    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".telegram.png.tmp-*"))


def test_static_atomic_write_preserves_old_destination_on_replace_failure(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.png", tmp_path / "telegram.png"
    Image.new("RGBA", (8, 8), (1, 2, 3, 4)).save(source)
    destination.write_bytes(b"old")
    original_replace = webp.os.replace
    monkeypatch.setattr(webp.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("ENOSPC")))
    with pytest.raises(WebPError, match="저장"):
        make_static_png(source, destination)
    assert destination.read_bytes() == b"old"
    monkeypatch.setattr(webp.os, "replace", original_replace)
    assert not list(tmp_path.glob(".telegram.png.tmp-*"))


def test_validator_rejects_opaque_alpha_for_transparent_source(tmp_path, monkeypatch):
    video = tmp_path / "opaque-alpha.webm"
    video.write_bytes(b"x")
    monkeypatch.setattr(webp, "_ffprobe", lambda *args: {
        "streams": [{"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p", "width": 512, "height": 512, "avg_frame_rate": "30/1", "tags": {"ALPHA_MODE": "1"}}],
        "format": {"format_name": "matroska,webm", "duration": "1.0"},
    })
    monkeypatch.setattr(webp, "_decode_alpha", lambda *args: (512 * 512, 255, 255))
    with pytest.raises(CandidateValidationError, match="보존"):
        validate_telegram_video(video, expected_transparency=True)


@pytest.mark.skipif(not _vp9_capable() or os.environ.get("TELE_STICKER_SKIP_VP9_E2E") == "1", reason="real VP9 E2E disabled or encoder unavailable")
def test_animated_atomic_write_preserves_old_destination_on_replace_failure(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.webp", tmp_path / "telegram.webm"
    _animated_webp(source)
    destination.write_bytes(b"old")
    monkeypatch.setattr(webp.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("ENOSPC")))
    with pytest.raises(WebPError, match="저장"):
        make_animated_webm(source, destination)
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".telegram.webm.tmp-*"))


def test_ffprobe_schema_rejects_non_object_json(tmp_path, monkeypatch):
    monkeypatch.setattr(webp, "_run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "[]", ""))
    with pytest.raises(ToolError, match="schema"):
        webp._ffprobe(tmp_path / "video.webm", "ffprobe")


def _valid_probe(**overrides):
    stream = {"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p", "width": 512, "height": 512, "avg_frame_rate": "30/1", "tags": {"ALPHA_MODE": "1"}}
    format_data = {"format_name": "matroska, webm", "duration": "1.0"}
    for name, value in overrides.items():
        (format_data if name in format_data else stream)[name] = value
    return {"streams": [stream], "format": format_data}


@pytest.mark.parametrize(("field", "value", "message"), [
    ("format_name", "notwebm-container", "WebM"),
    ("width", 0, "양의"),
    ("height", False, "양의"),
    ("avg_frame_rate", "nan/1", "fps"),
    ("avg_frame_rate", "inf/1", "fps"),
    ("duration", "nan", "길이"),
    ("duration", "inf", "길이"),
    ("duration", "3.0004", "길이"),
])
def test_validator_rejects_invalid_container_or_numeric_bounds(tmp_path, monkeypatch, field, value, message):
    video = tmp_path / "video.webm"
    video.write_bytes(b"x")
    monkeypatch.setattr(webp, "_ffprobe", lambda *args: _valid_probe(**{field: value}))
    with pytest.raises(CandidateValidationError, match=message):
        validate_telegram_video(video)


def test_validator_rejects_fully_invisible_alpha_and_accepts_mixed_alpha(tmp_path, monkeypatch):
    video = tmp_path / "alpha.webm"
    video.write_bytes(b"x")
    monkeypatch.setattr(webp, "_ffprobe", lambda *args: _valid_probe())
    monkeypatch.setattr(webp, "_decode_alpha", lambda *args: (512 * 512, 0, 0))
    with pytest.raises(CandidateValidationError, match="완전히 투명"):
        validate_telegram_video(video)

    monkeypatch.setattr(webp, "_decode_alpha", lambda *args: (512 * 512, 0, 255))
    assert validate_telegram_video(video, expected_transparency=True).width == 512


def test_alpha_decode_streams_and_kills_at_memory_limit(monkeypatch, tmp_path):
    processes = []

    class LargeStdout:
        def __init__(self):
            self.remaining = webp.MAX_ALPHA_BYTES + 1
            self.requests = []

        def read(self, size):
            self.requests.append(size)
            count = min(size, self.remaining)
            self.remaining -= count
            return b"x" * count

        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdout = LargeStdout()
            self.killed = False

        def kill(self):
            self.killed = True

        def wait(self):
            return 0

        def poll(self):
            return None if not self.killed else 0

    def fake_popen(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(webp.subprocess, "Popen", fake_popen)
    with pytest.raises(CandidateValidationError, match="메모리"):
        webp._decode_alpha(tmp_path / "video.webm", "ffmpeg", 512, 512)
    assert processes[0].killed
    assert max(processes[0].stdout.requests) <= 64 * 1024


def test_alpha_decode_returns_streamed_total_minimum_and_maximum(monkeypatch, tmp_path):
    class Stdout:
        def __init__(self):
            self.payload = b"\0\x7f\xff\x10"

        def read(self, size):
            value, self.payload = self.payload, b""
            return value

        def close(self):
            pass

    class Process:
        def __init__(self):
            self.stdout = Stdout()

        def wait(self):
            return 0

        def poll(self):
            return 0

        def kill(self):
            raise AssertionError("completed process must not be killed")

    monkeypatch.setattr(webp.subprocess, "Popen", lambda *args, **kwargs: Process())
    assert webp._decode_alpha(tmp_path / "video.webm", "ffmpeg", 2, 2) == (4, 0, 255)


@pytest.mark.parametrize("payload", [b"", b"x"])
def test_alpha_decode_rejects_empty_or_misaligned_output(monkeypatch, tmp_path, payload):
    class Stdout:
        def __init__(self):
            self.payload = payload

        def read(self, size):
            value, self.payload = self.payload, b""
            return value

        def close(self):
            pass

    class Process:
        def __init__(self):
            self.stdout = Stdout()

        def wait(self):
            return 0

        def poll(self):
            return 0

        def kill(self):
            raise AssertionError("completed process must not be killed")

    monkeypatch.setattr(webp.subprocess, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(CandidateValidationError, match="정렬"):
        webp._decode_alpha(tmp_path / "video.webm", "ffmpeg", 512, 512)


def test_ffprobe_invalid_json_does_not_retry_all_candidates(tmp_path, monkeypatch):
    source = tmp_path / "source.webp"
    _animated_webp(source)
    commands = []

    def invalid_probe(command, **kwargs):
        commands.append(command)
        if "-show_streams" in command:
            return subprocess.CompletedProcess(command, 0, "not-json", "")
        Path(command[-1]).write_bytes(b"candidate")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(webp, "_run", invalid_probe)
    with pytest.raises(ToolError, match="JSON"):
        make_animated_webm(source, tmp_path / "out.webm")
    assert len([command for command in commands if "-framerate" in command]) == 1


def test_bounded_search_uses_next_candidate_after_validation_rejection(tmp_path, monkeypatch):
    source, output = tmp_path / "source.webp", tmp_path / "sticker.webm"
    _animated_webp(source)
    commands = []

    def fake_run(command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"candidate")
        return subprocess.CompletedProcess(command, 0, "", "")

    validations = []

    def fake_validate(path, **kwargs):
        validations.append(path)
        if len(validations) == 1:
            raise CandidateValidationError("first candidate rejected")
        return webp.VideoValidation(512, 284, 30, 300, path.stat().st_size)

    monkeypatch.setattr(webp, "_run", fake_run)
    monkeypatch.setattr(webp, "validate_telegram_video", fake_validate)
    result = make_animated_webm(source, output)

    assert result.duration_ms == 300
    assert output.read_bytes() == b"candidate"
    assert len(commands) == 2
    assert commands[0][commands[0].index("-crf") + 1] == "32"
    assert commands[1][commands[1].index("-crf") + 1] == "36"


def test_bounded_search_reports_error_after_all_candidates_fail(tmp_path, monkeypatch):
    source = tmp_path / "source.webp"
    _animated_webp(source)
    commands = []

    def fake_run(command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"candidate")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(webp, "_run", fake_run)
    monkeypatch.setattr(webp, "validate_telegram_video", lambda *args, **kwargs: (_ for _ in ()).throw(CandidateValidationError("invalid")))

    with pytest.raises(CandidateValidationError, match="256KB"):
        make_animated_webm(source, tmp_path / "sticker.webm")
    assert len(commands) == 24  # 4 fps candidates × 6 CRF candidates
