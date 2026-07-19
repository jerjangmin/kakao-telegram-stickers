"""WebP timing inspection and Telegram-compatible sticker derivation."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image

MAX_RIFF_SIZE = 64 * 1024 * 1024
TELEGRAM_MAX_BYTES = 256 * 1024
TELEGRAM_STATIC_MAX_BYTES = 512 * 1024
TELEGRAM_MAX_DURATION_MS = 3_000
TELEGRAM_TARGET_DURATION_MS = 2_950
MAX_ALPHA_BYTES = 512 * 512 * 90
FPS_CANDIDATES = (30, 24, 20, 15)
CRF_CANDIDATES = tuple(range(32, 53, 4))


class WebPError(RuntimeError):
    """A WebP container or Telegram derivative is invalid."""


class ToolError(WebPError):
    """A required ffmpeg/ffprobe executable or its response is unusable."""


class CandidateValidationError(WebPError):
    """A generated candidate misses a Telegram media constraint."""


@dataclass(frozen=True)
class WebPTiming:
    loop_count: int
    frame_durations_ms: tuple[int, ...]

    @property
    def duration_ms(self) -> int:
        return sum(self.frame_durations_ms)


@dataclass(frozen=True)
class VideoValidation:
    width: int
    height: int
    fps: float
    duration_ms: int
    byte_size: int


def parse_webp_timing(raw: bytes, *, max_size: int = MAX_RIFF_SIZE) -> WebPTiming:
    """Parse bounded RIFF chunks, including ANIM loop count and ANMF timing."""
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        raise WebPError("유효한 RIFF/WEBP 파일이 아닙니다")
    declared = int.from_bytes(raw[4:8], "little")
    if declared > max_size or declared + 8 != len(raw):
        raise WebPError("RIFF 크기가 잘렸거나 허용 범위를 벗어났습니다")
    offset, loop_count, durations, saw_chunk = 12, 0, [], False
    while offset < len(raw):
        if offset + 8 > len(raw):
            raise WebPError("잘린 WEBP chunk header")
        kind, size = raw[offset : offset + 4], int.from_bytes(raw[offset + 4 : offset + 8], "little")
        saw_chunk = True
        payload_start = offset + 8
        payload_end = payload_start + size
        padded_end = payload_end + (size & 1)
        if size > max_size or payload_end > len(raw) or padded_end > len(raw):
            raise WebPError("잘렸거나 과도한 WEBP chunk")
        if kind == b"ANIM":
            if size < 6:
                raise WebPError("잘린 ANIM chunk")
            loop_count = int.from_bytes(raw[payload_start + 4 : payload_start + 6], "little")
        elif kind == b"ANMF":
            if size < 16:
                raise WebPError("잘린 ANMF chunk")
            durations.append(int.from_bytes(raw[payload_start + 12 : payload_start + 15], "little") or 1)
        offset = padded_end
    if offset != len(raw) or not saw_chunk:
        raise WebPError("WEBP chunk 정렬 오류")
    return WebPTiming(loop_count=loop_count, frame_durations_ms=tuple(durations))


def inspect_animated_webp(path: Path) -> WebPTiming:
    """Cross-check ANMF timing entries against Pillow's frame count."""
    timing = parse_webp_timing(path.read_bytes())
    try:
        with Image.open(path) as image:
            frames = getattr(image, "n_frames", 1)
    except OSError as error:
        raise WebPError("Pillow가 animated WebP를 열지 못했습니다") from error
    if frames < 2 or len(timing.frame_durations_ms) != frames:
        raise WebPError("Pillow 프레임 수와 ANMF timing 수가 일치하지 않습니다")
    return timing


def fit_size(width: int, height: int, maximum: int = 512) -> tuple[int, int]:
    """Fit dimensions inside a square while making the long side exactly maximum."""
    if width <= 0 or height <= 0:
        raise WebPError("이미지 크기는 양수여야 합니다")
    if width >= height:
        return maximum, max(1, round(height * maximum / width))
    return max(1, round(width * maximum / height)), maximum


def _sibling_temp(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".{}.tmp-".format(destination.name), dir=str(destination.parent))
    os.close(descriptor)
    return Path(name)


def make_static_png(source: Path, destination: Path) -> tuple[int, int]:
    """Create a 512px-long-side RGBA PNG, atomically replacing destination."""
    try:
        with Image.open(source) as image:
            rgba = image.convert("RGBA")
    except OSError as error:
        raise WebPError("정적 원본 PNG를 열지 못했습니다") from error
    size = fit_size(*rgba.size)
    if rgba.size != size:
        rgba = rgba.resize(size, Image.Resampling.LANCZOS)
    temporary = _sibling_temp(destination)
    try:
        rgba.save(temporary, format="PNG")
        if temporary.stat().st_size > TELEGRAM_STATIC_MAX_BYTES:
            raise CandidateValidationError("Telegram PNG 파일 크기가 512KB를 초과했습니다")
        os.replace(temporary, destination)
    except OSError as error:
        raise WebPError("Telegram PNG를 저장하지 못했습니다") from error
    finally:
        temporary.unlink(missing_ok=True)
    return size


def _scaled_durations(durations: Sequence[int], target_ms: int = TELEGRAM_TARGET_DURATION_MS) -> tuple[int, ...]:
    normalized = [max(1, value) for value in durations]
    total = sum(normalized)
    if len(normalized) > target_ms:
        raise WebPError("프레임 수가 Telegram 최소 프레임 시간 한도를 초과했습니다")
    if total <= target_ms:
        return tuple(normalized)
    remaining = target_ms - len(normalized)
    quotas = [value * remaining / total for value in normalized]
    allocations = [int(quota) for quota in quotas]
    leftover = remaining - sum(allocations)
    # Largest remainder allocation is stable by original frame order on ties.
    for index in sorted(range(len(quotas)), key=lambda position: (quotas[position] - allocations[position], -position), reverse=True)[:leftover]:
        allocations[index] += 1
    return tuple(1 + allocation for allocation in allocations)


def _run(command: Sequence[str], *, text: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=text)
    except OSError as error:
        raise ToolError("ffmpeg 또는 ffprobe를 실행할 수 없습니다") from error


def _ffprobe(path: Path, ffprobe: str) -> dict:
    result = _run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if result.returncode:
        raise ToolError("ffprobe 검증 실패: " + str(result.stderr).strip())
    try:
        data = json.loads(str(result.stdout))
    except json.JSONDecodeError as error:
        raise ToolError("ffprobe JSON 응답이 잘못되었습니다") from error
    if not isinstance(data, dict) or not isinstance(data.get("streams"), list) or not isinstance(data.get("format"), dict):
        raise ToolError("ffprobe JSON schema가 잘못되었습니다")
    if not all(isinstance(stream, dict) for stream in data["streams"]):
        raise ToolError("ffprobe stream schema가 잘못되었습니다")
    return data


def _decode_alpha(path: Path, ffmpeg: str, width: int, height: int) -> tuple[int, int, int]:
    """Stream alpha bytes without retaining the decoded plane in memory."""
    command = [ffmpeg, "-v", "error", "-c:v", "libvpx-vp9", "-i", str(path), "-vf", "alphaextract", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr)
            try:
                if process.stdout is None:  # pragma: no cover - guaranteed by PIPE
                    raise ToolError("ffmpeg alpha stdout pipe를 열지 못했습니다")
                total, minimum, maximum = 0, 255, 0
                while True:
                    chunk = process.stdout.read(min(64 * 1024, MAX_ALPHA_BYTES - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ALPHA_BYTES:
                        process.kill()
                        process.wait()
                        raise CandidateValidationError("VP9 alpha plane이 허용 메모리 한도를 초과했습니다")
                    minimum = min(minimum, min(chunk))
                    maximum = max(maximum, max(chunk))
                returncode = process.wait()
                if returncode:
                    stderr.seek(0)
                    raise CandidateValidationError("VP9 alpha plane decode에 실패했습니다: " + stderr.read().decode("utf-8", "replace").strip())
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
    except OSError as error:
        raise ToolError("ffmpeg alpha decoder를 실행할 수 없습니다") from error
    pixels = width * height
    if total == 0 or total < pixels or total % pixels:
        raise CandidateValidationError("VP9 alpha plane 프레임 정렬이 잘못되었습니다")
    return total, minimum, maximum


def validate_telegram_video(
    path: Path,
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    expected_transparency: bool = False,
) -> VideoValidation:
    """Validate media constraints and decode its real VP9 alpha plane."""
    if not path.is_file() or path.stat().st_size > TELEGRAM_MAX_BYTES:
        raise CandidateValidationError("Telegram WebM 파일 크기가 256KB를 초과했습니다")
    data = _ffprobe(path, ffprobe)
    streams, format_data = data["streams"], data["format"]
    format_name = format_data.get("format_name")
    format_tokens = [token.strip().lower() for token in format_name.split(",")] if isinstance(format_name, str) else []
    if "webm" not in format_tokens:
        raise CandidateValidationError("Telegram 컨테이너는 WebM이어야 합니다")
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(video) != 1 or audio:
        raise CandidateValidationError("Telegram WebM에는 비디오 하나만 있고 오디오가 없어야 합니다")
    stream = video[0]
    required_types = {
        "codec_type": str, "codec_name": str, "pix_fmt": str, "width": int,
        "height": int, "avg_frame_rate": str,
    }
    if any(not isinstance(stream.get(name), type_) for name, type_ in required_types.items()) or not isinstance(stream.get("tags", {}), dict):
        raise ToolError("ffprobe video stream field schema가 잘못되었습니다")
    if any(type(stream[name]) is not int or stream[name] <= 0 for name in ("width", "height")):
        raise CandidateValidationError("Telegram WebM 크기는 양의 정수여야 합니다")
    if not isinstance(format_data.get("duration"), (str, int, float)):
        raise ToolError("ffprobe format duration schema가 잘못되었습니다")
    if stream.get("codec_name") != "vp9" or stream.get("pix_fmt") not in {"yuv420p", "yuva420p"}:
        raise CandidateValidationError("Telegram WebM은 VP9 yuva420p 스트림이어야 합니다")
    tags = {str(key).upper(): value for key, value in (stream.get("tags") or {}).items()}
    if str(tags.get("ALPHA_MODE", "")) != "1":
        raise CandidateValidationError("VP9 alpha metadata가 없습니다")
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    if max(width, height) != 512 or min(width, height) > 512:
        raise CandidateValidationError("Telegram WebM 크기가 512px 규격이 아닙니다")
    try:
        numerator, denominator = str(stream.get("avg_frame_rate", "0/1")).split("/", 1)
        fps = float(numerator) / float(denominator)
        duration_seconds = float(format_data["duration"])
    except (ValueError, ZeroDivisionError) as error:
        raise CandidateValidationError("WebM 시간 정보가 잘못되었습니다") from error
    if not math.isfinite(fps) or fps <= 0 or fps > 30:
        raise CandidateValidationError("Telegram WebM fps가 규격을 벗어났습니다")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0 or duration_seconds > TELEGRAM_MAX_DURATION_MS / 1000:
        raise CandidateValidationError("Telegram WebM 길이가 규격을 벗어났습니다")
    duration_ms = round(duration_seconds * 1000)
    _, alpha_minimum, alpha_maximum = _decode_alpha(path, ffmpeg, width, height)
    if alpha_maximum == 0:
        raise CandidateValidationError("VP9 alpha plane이 완전히 투명합니다")
    if expected_transparency and alpha_minimum == 255:
        raise CandidateValidationError("투명 원본의 VP9 alpha 값이 보존되지 않았습니다")
    return VideoValidation(width, height, fps, duration_ms, path.stat().st_size)


def _sample_frame_indices(durations: Sequence[int], fps: int) -> tuple[int, ...]:
    """Sample duration timeline at FPS tick centres, never exceeding 2.95 seconds."""
    total = sum(durations)
    count = max(1, min((TELEGRAM_TARGET_DURATION_MS * fps) // 1000, (total * fps) // 1000))
    endpoints, running = [], 0
    for duration in durations:
        running += duration
        endpoints.append(running)
    indices = []
    for tick in range(count):
        centre_ms = (tick + 0.5) * 1000 / fps
        index = next((position for position, endpoint in enumerate(endpoints) if centre_ms < endpoint), len(endpoints) - 1)
        indices.append(index)
    return tuple(indices)


def make_animated_webm(source: Path, destination: Path, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> VideoValidation:
    """Pillow-decode WebP frames and encode bounded, sampled VP9 alpha WebM."""
    timing = inspect_animated_webp(source)
    durations = _scaled_durations(timing.frame_durations_ms)
    try:
        image = Image.open(source)
    except OSError as error:
        raise WebPError("animated WebP를 열지 못했습니다") from error
    with tempfile.TemporaryDirectory(prefix="tele-sticker-") as temporary_name:
        temporary = Path(temporary_name)
        source_frames, expected_transparency = [], False
        try:
            for index in range(image.n_frames):
                image.seek(index)
                frame = image.convert("RGBA")
                expected_transparency = expected_transparency or frame.getchannel("A").getextrema()[0] < 255
                size = fit_size(*frame.size)
                if frame.size != size:
                    frame = frame.resize(size, Image.Resampling.LANCZOS)
                frame_path = temporary / "source_{:05d}.png".format(index)
                frame.save(frame_path, format="PNG")
                source_frames.append(frame_path)
        finally:
            image.close()
        for fps in FPS_CANDIDATES:
            sequence = temporary / "sequence-{}".format(fps)
            sequence.mkdir()
            for output_index, source_index in enumerate(_sample_frame_indices(durations, fps)):
                shutil.copyfile(source_frames[source_index], sequence / "frame_{:05d}.png".format(output_index))
            for crf in CRF_CANDIDATES:
                candidate = temporary / "candidate-{}-{}.webm".format(fps, crf)
                result = _run([
                    ffmpeg, "-y", "-v", "error", "-framerate", str(fps), "-start_number", "0",
                    "-i", str(sequence / "frame_%05d.png"), "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                    "-crf", str(crf), "-b:v", "0", "-auto-alt-ref", "0", "-deadline", "good", str(candidate),
                ])
                if result.returncode:
                    raise ToolError("ffmpeg VP9 인코딩 실패: " + str(result.stderr).strip())
                try:
                    validation = validate_telegram_video(candidate, ffprobe=ffprobe, ffmpeg=ffmpeg, expected_transparency=expected_transparency)
                except CandidateValidationError:
                    continue
                destination_temp = _sibling_temp(destination)
                try:
                    shutil.copyfile(candidate, destination_temp)
                    os.replace(destination_temp, destination)
                except OSError as error:
                    raise WebPError("Telegram WebM을 저장하지 못했습니다") from error
                finally:
                    destination_temp.unlink(missing_ok=True)
                return validation
    raise CandidateValidationError("256KB Telegram VP9 WebM을 생성하지 못했습니다")
