"""Safe Kakao item resolution, API retrieval, and CDN downloads."""

from __future__ import annotations

import json
import re
from http.client import HTTPException
from dataclasses import dataclass
from typing import Any, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import KakaoSetMetadata

PAGE_HOST = "e.kakao.com"
ITEM_HOST = "emoticon.kakao.com"
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
TIMEOUT_SECONDS = 15


class KakaoError(RuntimeError):
    """A rejected Kakao input, response, or remote resource."""


@dataclass(frozen=True)
class RemoteSticker:
    """One item normalized from either supported Kakao API schema."""

    source_url: str
    thumbnail_url: Optional[str]
    animated_url: Optional[str]
    api_width: Optional[int]
    api_height: Optional[int]


@dataclass(frozen=True)
class KakaoSetResponse:
    """One normalized Kakao item API response with set-level traits."""

    api_url: str
    metadata: KakaoSetMetadata
    items: tuple[RemoteSticker, ...]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _default_opener():  # type: ignore[no-untyped-def]
    return build_opener(_NoRedirect())


def _parse_url(url: str):  # type: ignore[no-untyped-def]
    try:
        return urlparse(url)
    except ValueError as error:
        raise KakaoError("유효한 HTTPS URL이 아닙니다") from error


def _host(url: str) -> str:
    try:
        parsed = _parse_url(url)
        hostname = parsed.hostname
    except ValueError as error:
        raise KakaoError("유효한 HTTPS URL이 아닙니다") from error
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise KakaoError("HTTPS URL만 허용됩니다")
    return hostname.lower().rstrip(".")


def _is_cdn_host(host: str) -> bool:
    return host == "kakaocdn.net" or host.endswith(".kakaocdn.net") or host == "kakaocdn.com" or host.endswith(".kakaocdn.com")


def _read_limited(response: Any, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise KakaoError("응답 크기 제한을 초과했습니다")
        chunks.append(chunk)
    return b"".join(chunks)


def _request(url: str, accept: str) -> Request:
    return Request(url, headers={"User-Agent": "tele-sticker-maker/0.1", "Accept": accept})


def _validate_slug(slug: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise KakaoError("유효한 이모티콘 slug가 아닙니다")
    return slug


def _slug_from_page(url: str) -> str:
    parsed = _parse_url(url)
    if _host(url) != PAGE_HOST:
        raise KakaoError("카카오 이모티콘 페이지 host가 아닙니다")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "t" or not parts[1]:
        raise KakaoError("카카오 이모티콘 상세 URL 형식이 아닙니다")
    return _validate_slug(parts[1])


def resolve_input(value: str, opener: Optional[Any] = None) -> tuple[str, str]:
    """Resolve a legacy slug or approved Kakao URL to ``(slug, source_page)``."""
    value = value.strip()
    parsed = _parse_url(value)
    if not parsed.scheme and not parsed.netloc:
        slug = value.strip("/")
        slug = _validate_slug(slug)
        return slug, "https://e.kakao.com/t/" + slug

    host = _host(value)
    if host == PAGE_HOST:
        slug = _slug_from_page(value)
        return slug, "https://e.kakao.com/t/" + slug
    if host != ITEM_HOST or not parsed.path.startswith("/items/"):
        raise KakaoError("허용되지 않은 카카오 URL입니다")

    active_opener = opener or _default_opener()
    current = value
    for _ in range(MAX_REDIRECTS):
        try:
            response = active_opener.open(_request(current, "text/html"), timeout=TIMEOUT_SECONDS)
        except HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                raise KakaoError("이모티콘 URL을 해석하지 못했습니다") from error
            response = error
        except (HTTPException, URLError, OSError) as error:
            raise KakaoError("이모티콘 URL을 해석하지 못했습니다") from error
        status = getattr(response, "status", getattr(response, "code", 200))
        if status not in (301, 302, 303, 307, 308):
            final_url = response.geturl() if hasattr(response, "geturl") else current
            return _slug_from_page(final_url), "https://e.kakao.com/t/" + _slug_from_page(final_url)
        location = response.headers.get("Location")
        if not location:
            raise KakaoError("리디렉션 위치가 없습니다")
        current = urljoin(current, location)
        redirect_host = _host(current)
        if redirect_host not in (ITEM_HOST, PAGE_HOST):
            raise KakaoError("허용되지 않은 리디렉션 host입니다")
        if redirect_host == PAGE_HOST:
            slug = _slug_from_page(current)
            return slug, "https://e.kakao.com/t/" + slug
    raise KakaoError("리디렉션 제한을 초과했습니다")


def _optional_positive_int(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KakaoError("{}는 양의 정수여야 합니다".format(field))
    return value


def _optional_url(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KakaoError("{}은 비어 있지 않은 URL 문자열이어야 합니다".format(field))
    return value.strip()


def parse_items(payload: Any) -> list[RemoteSticker]:
    """Normalize and validate the current and legacy Kakao item API payloads."""
    if not isinstance(payload, dict):
        raise KakaoError("카카오 API 최상위 응답은 객체여야 합니다")
    if isinstance(payload.get("contents"), dict) and isinstance(payload["contents"].get("items"), list):
        raw_items: Sequence[Any] = payload["contents"]["items"]
        items = []
        for index, item in enumerate(raw_items, 1):
            if not isinstance(item, dict):
                raise KakaoError("contents.items[{}]는 객체여야 합니다".format(index))
            animated_url = _optional_url(item.get("animatedUrl"), "animatedUrl")
            thumbnail_url = _optional_url(item.get("thumbnailUrl"), "thumbnailUrl")
            if not animated_url and not thumbnail_url:
                raise KakaoError("contents.items[{}]에 이미지 URL이 없습니다".format(index))
            items.append(RemoteSticker(
                source_url=animated_url or thumbnail_url or "",
                thumbnail_url=thumbnail_url,
                animated_url=animated_url,
                api_width=_optional_positive_int(item.get("width"), "width"),
                api_height=_optional_positive_int(item.get("height"), "height"),
            ))
    elif isinstance(payload.get("result"), dict) and isinstance(payload["result"].get("thumbnailUrls"), list):
        items = []
        for index, url in enumerate(payload["result"]["thumbnailUrls"], 1):
            parsed_url = _optional_url(url, "result.thumbnailUrls[{}]".format(index))
            if parsed_url is None:
                raise KakaoError("result.thumbnailUrls[{}]가 없습니다".format(index))
            items.append(RemoteSticker(parsed_url, parsed_url, None, None, None))
    else:
        raise KakaoError("알 수 없는 카카오 API 응답 구조입니다")
    if not items:
        raise KakaoError("다운로드할 이미지가 없습니다")
    return items


def _optional_bool(value: Any, field: str) -> Optional[bool]:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise KakaoError("{}는 불리언이어야 합니다".format(field))
    return value


def parse_set(payload: Any, api_url: str) -> KakaoSetResponse:
    """Parse item media plus authoritative set-level layout traits."""
    items = tuple(parse_items(payload))
    contents = payload.get("contents") if isinstance(payload, dict) else None
    if isinstance(contents, dict):
        metadata = KakaoSetMetadata(
            is_mini=_optional_bool(contents.get("isMini"), "contents.isMini"),
            is_big=_optional_bool(contents.get("isBig"), "contents.isBig"),
            is_sound=_optional_bool(contents.get("isSound"), "contents.isSound"),
        )
    else:
        metadata = KakaoSetMetadata(None, None, None)
    return KakaoSetResponse(api_url, metadata, items)


class KakaoClient:
    """Small stdlib HTTP client with explicit host, redirect, and size limits."""

    def __init__(self, opener: Optional[Any] = None, timeout: int = TIMEOUT_SECONDS, max_bytes: int = MAX_RESPONSE_BYTES):
        self.opener = opener or _default_opener()
        self.timeout = timeout
        self.max_bytes = max_bytes

    def resolve(self, value: str) -> tuple[str, str]:
        return resolve_input(value, self.opener)

    def fetch_set(self, slug: str) -> KakaoSetResponse:
        last_error: Optional[Exception] = None
        for path in ("/api/items/", "/api/v1/items/t/"):
            api_url = "https://e.kakao.com" + path + slug
            try:
                response = self.opener.open(_request(api_url, "application/json"), timeout=self.timeout)
                if _host(response.geturl()) != PAGE_HOST:
                    raise KakaoError("API 리디렉션 host가 허용되지 않습니다")
                payload = json.loads(_read_limited(response, self.max_bytes).decode("utf-8"))
                return parse_set(payload, api_url)
            except (HTTPException, URLError, OSError, ValueError, KakaoError) as error:
                last_error = error
        raise KakaoError("카카오 이모티콘 정보를 가져오지 못했습니다") from last_error

    def fetch_items(self, slug: str) -> tuple[str, list[RemoteSticker]]:
        """Backward-compatible media-only view for existing callers."""
        response = self.fetch_set(slug)
        return response.api_url, list(response.items)

    def download(self, url: str) -> tuple[bytes, Optional[str]]:
        """Download media, following only bounded redirects within Kakao's CDN."""
        current = url
        for _ in range(MAX_REDIRECTS):
            if not _is_cdn_host(_host(current)):
                raise KakaoError("허용되지 않은 카카오 CDN host입니다")
            try:
                response = self.opener.open(_request(current, "image/webp,image/png"), timeout=self.timeout)
            except HTTPError as error:
                if error.code not in (301, 302, 303, 307, 308):
                    raise KakaoError("CDN 이미지를 가져오지 못했습니다") from error
                response = error
            except (HTTPException, URLError, OSError) as error:
                raise KakaoError("CDN 이미지를 가져오지 못했습니다") from error
            status = getattr(response, "status", getattr(response, "code", 200))
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise KakaoError("CDN 리디렉션 위치가 없습니다")
                current = urljoin(current, location)
                continue
            final_url = response.geturl() if hasattr(response, "geturl") else current
            if not _is_cdn_host(_host(final_url)):
                raise KakaoError("CDN 리디렉션 host가 허용되지 않습니다")
            try:
                return _read_limited(response, self.max_bytes), response.headers.get("Content-Type")
            except (HTTPException, URLError, OSError) as error:
                raise KakaoError("CDN 이미지를 가져오지 못했습니다") from error
        raise KakaoError("CDN 리디렉션 제한을 초과했습니다")
