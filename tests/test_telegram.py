from __future__ import annotations
import io
import sys
from pathlib import Path
from urllib.error import HTTPError
import pytest
SCRIPTS_DIR=Path(__file__).resolve().parents[1]/"skills"/"kakao-telegram-stickers"/"scripts"; sys.path.insert(0,str(SCRIPTS_DIR))
import tele_sticker_maker.telegram as telegram
from tele_sticker_maker.telegram import InputSticker, TelegramApiError, TelegramClient, TelegramInputError, generate_short_name
TOKEN = "123456:" + "abcdefghijklmnopqrstuvwxyzABCDE"
class Response:
    def __init__(self,value): self.value=value; self.closed=False
    def read(self,_=-1): value,self.value=self.value,b""; return value
    def close(self): self.closed=True

def test_get_me_and_token_redaction():
    seen=[]
    def opener(request,timeout): seen.append(request.full_url); return Response(b'{"ok":true,"result":{"username":"TestBot"}}')
    client=TelegramClient(TOKEN,opener=opener); assert client.get_me()["username"]=="TestBot"
    assert TOKEN not in repr(client) and TOKEN not in str(TelegramApiError("getMe",400,"bot"+TOKEN))
    assert TelegramClient(" "+TOKEN+" ",opener=opener).get_me()["username"]=="TestBot"
    with pytest.raises(TelegramInputError): TelegramClient(TOKEN+"\n")

def test_default_opener_uses_keyword_timeout(monkeypatch):
    calls=[]
    def fake_urlopen(request, *, timeout):
        calls.append(timeout); return Response(b'{"ok":true,"result":{"username":"TestBot"}}')
    monkeypatch.setattr(telegram,"urlopen",fake_urlopen)
    assert TelegramClient(TOKEN,timeout=7).get_me()["username"]=="TestBot"
    assert calls==[7]

def test_multipart_static_and_video_payload(tmp_path):
    png,webm=tmp_path/"one.png",tmp_path/"two.webm"; png.write_bytes(b"png"); webm.write_bytes(b"webm"); requests=[]
    def opener(request,timeout): requests.append(request); return Response(b'{"ok":true,"result":true}')
    client=TelegramClient(TOKEN,opener=opener)
    assert client.create_new_sticker_set(7,"one_by_bot","One",InputSticker(png,("🙂",),"static"))
    assert client.add_sticker_to_set(7,"one_by_bot",InputSticker(webm,["🙂"],"video"))
    create_body, add_body = requests[0].data, requests[1].data
    assert b'name="stickers"' in create_body and b'[{"sticker":"attach://sticker_file","format":"static","emoji_list":["\xf0\x9f\x99\x82"]}]' in create_body
    assert b'name="sticker"' in add_body and b'{"sticker":"attach://sticker_file","format":"video","emoji_list":["\xf0\x9f\x99\x82"]}' in add_body
    assert b'name="sticker"' not in create_body and b'name="stickers"' not in add_body
    assert b"two.webm" in add_body
    with pytest.raises(TelegramInputError): InputSticker(png,"🙂","static")

def test_429_retries_caps_delay_and_400_does_not():
    calls=[]; sleeps=[]
    def opener(request,timeout):
        calls.append(1)
        return Response(b'{"ok":false,"error_code":429,"description":"slow","parameters":{"retry_after":999}}') if len(calls)==1 else Response(b'{"ok":true,"result":{"username":"TestBot"}}')
    assert TelegramClient(TOKEN,opener=opener,sleep=sleeps.append,random=lambda:0).get_me()["username"]=="TestBot"; assert sleeps==[60]
    with pytest.raises(TelegramApiError): TelegramClient(TOKEN,opener=lambda *_:Response(b'{"ok":false,"error_code":400,"description":"bad"}')).get_me()

def test_short_name_constraints_and_schema_validation():
    name=generate_short_name("123 long __ title "*10,"Example_Bot",2)
    assert len(name)<=64 and name.startswith("stickers") and "__" not in name and name.endswith("_2_by_example_bot")
    assert generate_short_name("a","a_bot").endswith("_by_a_bot")
    assert generate_short_name("a", "1testbot").endswith("_by_1testbot")
    assert generate_short_name("a", "_testbot").endswith("_by__testbot")
    for sequence in (2, 3):
        long_name = generate_short_name("long-title-" * 30, "Example_Bot", sequence)
        assert len(long_name) <= 64 and long_name.endswith(f"_{sequence}_by_example_bot")
    with pytest.raises(TelegramInputError): generate_short_name("a","invalid-name")
    with pytest.raises(TelegramInputError): generate_short_name("a","a"*30+"bot")
    with pytest.raises(TelegramApiError): TelegramClient(TOKEN,opener=lambda *_:Response(b'{"ok":false,"description":4}')).get_me()

def _assert_safe_http_error(exc_info, calls):
    error = exc_info.value
    assert len(calls) == 1 and error.__cause__ is None and error.__context__ is None and error.__suppress_context__
    current = error
    while current is not None:
        assert TOKEN not in str(current) and not isinstance(current, HTTPError)
        current = current.__cause__ or current.__context__

def test_sanitize_redacts_before_truncating_and_malformed_http_error_does_not_retry():
    description = "x" * 295 + " bot" + TOKEN + "/getMe"
    error = TelegramApiError("getMe", 400, description)
    # The redaction marker may be clipped at the 300-character display limit,
    # but no suffix of the raw token may survive that boundary.
    assert TOKEN not in str(error)
    calls = []
    def opener(*_):
        calls.append(1)
        raise HTTPError("https://api.telegram.org/bot" + TOKEN + "/getMe", 400, "bad", {}, io.BytesIO(b'{"ok":false,"description":4}'))
    with pytest.raises(TelegramApiError) as exc_info:
        TelegramClient(TOKEN, opener=opener, max_retries=3).get_me()
    _assert_safe_http_error(exc_info, calls)

def test_http_error_with_success_body_is_schema_error_without_retry():
    calls = []
    def opener(*_):
        calls.append(1)
        raise HTTPError("https://api.telegram.org/bot" + TOKEN + "/getMe", 500, "bad", {}, io.BytesIO(b'{"ok":true,"result":{"username":"TestBot"}}'))
    with pytest.raises(TelegramApiError) as exc_info:
        TelegramClient(TOKEN, opener=opener, max_retries=3).get_me()
    _assert_safe_http_error(exc_info, calls)

def test_client_timeout_and_retry_input_validation():
    for timeout in (True, False, 0, -1, float("nan"), float("inf"), -float("inf")):
        with pytest.raises(TelegramInputError): TelegramClient(TOKEN, timeout=timeout)
    for retries in (True, False, -1, 1.5, "1"):
        with pytest.raises(TelegramInputError): TelegramClient(TOKEN, max_retries=retries)
    assert TelegramClient(TOKEN, timeout=1, max_retries=0)._timeout == 1.0

def test_non_idempotent_sticker_registration_does_not_retry_response_loss_but_safe_calls_do(tmp_path):
    sticker_path = tmp_path / "sticker.png"; sticker_path.write_bytes(b"png")
    sticker = InputSticker(sticker_path, ("🙂",), "static")
    calls = {}
    def opener(request, timeout):
        method = request.full_url.rsplit("/", 1)[-1]
        calls[method] = calls.get(method, 0) + 1
        if method in ("createNewStickerSet", "addStickerToSet"):
            raise telegram.URLError("response lost")
        if calls[method] == 1:
            raise telegram.URLError("response lost")
        if method == "getMe":
            return Response(b'{"ok":true,"result":{"username":"TestBot"}}')
        return Response(b'{"ok":true,"result":{"file_id":"file","file_unique_id":"unique"}}')
    client = TelegramClient(TOKEN, opener=opener, sleep=lambda _: None, random=lambda: 0, max_retries=3)
    with pytest.raises(TelegramApiError) as create_error:
        client.create_new_sticker_set(1, "one_by_bot", "One", sticker)
    with pytest.raises(TelegramApiError) as add_error:
        client.add_sticker_to_set(1, "one_by_bot", sticker)
    assert create_error.value.method == "createNewStickerSet" and add_error.value.method == "addStickerToSet"
    assert calls == {"createNewStickerSet": 1, "addStickerToSet": 1}
    assert client.get_me()["username"] == "TestBot"
    assert client.upload_sticker_file(1, sticker).file_id == "file"
    assert calls == {"createNewStickerSet": 1, "addStickerToSet": 1, "getMe": 2, "uploadStickerFile": 2}
