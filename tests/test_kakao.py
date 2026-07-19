from __future__ import annotations

import json
import sys
from http.client import HTTPException, IncompleteRead
from io import BytesIO
from pathlib import Path
from urllib.error import URLError

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "kakao-telegram-stickers" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from tele_sticker_maker.kakao import KakaoClient, KakaoError, parse_items, resolve_input


class Response:
    def __init__(self, url, body=b"", status=200, headers=None):
        self.url = url
        self.body = BytesIO(body)
        self.status = status
        self.headers = headers or {}

    def read(self, size=-1):
        return self.body.read(size)

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request.full_url)
        return self.responses.pop(0)


def test_resolve_legacy_slug_and_page_url_without_network():
    assert resolve_input(" hello ") == ("hello", "https://e.kakao.com/t/hello")
    assert resolve_input("https://e.kakao.com/t/hello?utm=x") == ("hello", "https://e.kakao.com/t/hello")


def test_item_url_redirect_resolves_only_approved_page():
    opener = Opener([Response("https://emoticon.kakao.com/items/abc", status=303, headers={"Location": "https://e.kakao.com/t/hello?x=1"})])

    assert resolve_input("https://emoticon.kakao.com/items/abc?query=x", opener) == ("hello", "https://e.kakao.com/t/hello")


def test_input_and_redirect_escape_are_rejected():
    with pytest.raises(KakaoError):
        resolve_input("http://e.kakao.com/t/hello")
    with pytest.raises(KakaoError, match="유효한 HTTPS"):
        resolve_input("https://[malformed")
    with pytest.raises(KakaoError):
        resolve_input("https://evil.example/t/hello")
    opener = Opener([Response("https://emoticon.kakao.com/items/abc", status=302, headers={"Location": "https://evil.example/"})])
    with pytest.raises(KakaoError, match="리디렉션"):
        resolve_input("https://emoticon.kakao.com/items/abc", opener)


def test_api_current_schema_and_legacy_fallback_are_normalized():
    current = {"contents": {"items": [{"animatedUrl": "https://item.kakaocdn.net/a.webp", "thumbnailUrl": "https://item.kakaocdn.net/a.png", "width": 10, "height": 20}]}}
    legacy = {"result": {"thumbnailUrls": ["https://item.kakaocdn.net/b.png"]}}
    assert parse_items(current)[0].animated_url.endswith("a.webp")
    assert parse_items(legacy)[0].source_url.endswith("b.png")

    opener = Opener([
        Response("https://e.kakao.com/api/items/hello", b"not json"),
        Response("https://e.kakao.com/api/v1/items/t/hello", json.dumps(legacy).encode()),
    ])
    api_url, items = KakaoClient(opener=opener).fetch_items("hello")
    assert api_url.endswith("/api/v1/items/t/hello")
    assert len(items) == 1


def test_cdn_requires_kakao_cdn_enforces_response_limit_and_rejects_escape():
    client = KakaoClient(opener=Opener([Response("https://item.kakaocdn.net/a.png", b"x" * 5)]), max_bytes=4)
    with pytest.raises(KakaoError, match="크기"):
        client.download("https://item.kakaocdn.net/a.png")
    with pytest.raises(KakaoError, match="CDN"):
        client.download("https://example.com/a.png")

    escaped = KakaoClient(opener=Opener([Response("https://item.kakaocdn.net/a.png", status=302, headers={"Location": "https://evil.example/a.png"})]))
    with pytest.raises(KakaoError, match="CDN"):
        escaped.download("https://item.kakaocdn.net/a.png")


def test_transport_errors_are_exposed_as_kakao_errors():
    class FailingOpener:
        def open(self, request, timeout):
            raise URLError("offline")

    opener = FailingOpener()
    with pytest.raises(KakaoError, match="해석"):
        resolve_input("https://emoticon.kakao.com/items/abc", opener)
    with pytest.raises(KakaoError, match="정보"):
        KakaoClient(opener=opener).fetch_items("hello")
    with pytest.raises(KakaoError, match="CDN"):
        KakaoClient(opener=opener).download("https://item.kakaocdn.net/a.png")


def test_http_exceptions_fall_back_for_api_and_become_cdn_errors():
    legacy = {"result": {"thumbnailUrls": ["https://item.kakaocdn.net/b.png"]}}

    class IncompleteResponse(Response):
        def read(self, size=-1):
            raise IncompleteRead(b"", 1)

    api_client = KakaoClient(opener=Opener([
        IncompleteResponse("https://e.kakao.com/api/items/hello"),
        Response("https://e.kakao.com/api/v1/items/t/hello", json.dumps(legacy).encode()),
    ]))
    api_url, _ = api_client.fetch_items("hello")
    assert api_url.endswith("/api/v1/items/t/hello")

    with pytest.raises(KakaoError, match="CDN"):
        KakaoClient(opener=Opener([IncompleteResponse("https://item.kakaocdn.net/a.png")])).download("https://item.kakaocdn.net/a.png")

    class BrokenOpener:
        def open(self, request, timeout):
            raise HTTPException("broken connection")

    with pytest.raises(KakaoError, match="해석"):
        resolve_input("https://emoticon.kakao.com/items/abc", BrokenOpener())


def test_malformed_primary_schema_falls_back_and_validates_item_fields():
    legacy = {"result": {"thumbnailUrls": ["https://item.kakaocdn.net/b.png"]}}
    opener = Opener([
        Response("https://e.kakao.com/api/items/hello", json.dumps({"contents": {"items": [{"animatedUrl": 1}]}}).encode()),
        Response("https://e.kakao.com/api/v1/items/t/hello", json.dumps(legacy).encode()),
    ])

    api_url, items = KakaoClient(opener=opener).fetch_items("hello")

    assert api_url.endswith("/api/v1/items/t/hello")
    assert items[0].source_url == "https://item.kakaocdn.net/b.png"
    with pytest.raises(KakaoError, match="최상위"):
        parse_items([])
    with pytest.raises(KakaoError, match="URL"):
        parse_items({"contents": {"items": [{"thumbnailUrl": ""}]}})
    with pytest.raises(KakaoError, match="양의 정수"):
        parse_items({"contents": {"items": [{"thumbnailUrl": "https://item.kakaocdn.net/a.png", "width": True}]}})
