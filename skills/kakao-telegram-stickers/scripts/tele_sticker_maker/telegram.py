"""Dependency-free, secret-safe Telegram Bot API client."""
from __future__ import annotations
import hashlib, json, math, random as random_module, re, time as time_module, uuid
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 1_048_576; MAX_RETRIES = 3; MAX_RETRY_AFTER = 60
_API_ROOT = "https://api.telegram.org"
_TOKEN = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
_BOT_USERNAME = re.compile(r"^[A-Za-z0-9_]{2,29}[Bb][Oo][Tt]$")
_TOKEN_LIKE = re.compile(r"(?:bot)?\d+:[A-Za-z0-9_-]{20,}")
_SAFE = re.compile(r"[^\w .,:;()\-<>]", re.UNICODE)

class TelegramError(RuntimeError): pass
class TelegramInputError(TelegramError): pass
class TelegramApiError(TelegramError):
    def __init__(self, method: str, code: Optional[int], description: str, retry_after: Optional[int] = None, *, retryable: Optional[bool] = None):
        self.method, self.code, self.description, self.retry_after = method, code, _sanitize(description), retry_after
        self.retryable = retryable
        super().__init__(f"Telegram {method} failed ({code if code is not None else 'network'}): {self.description}")

def _sanitize(value: object) -> str:
    normalized = str(value).replace("\n", " ").replace("\r", " ")
    redacted = _TOKEN_LIKE.sub("<redacted>", normalized)
    return _SAFE.sub("?", redacted[:300])
def _default_opener(request: Request, timeout: float) -> Any: return urlopen(request, timeout=timeout)

def generate_short_name(base: str, bot_username: str, sequence: int = 1) -> str:
    if not isinstance(base, str) or not isinstance(bot_username, str) or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1: raise TelegramInputError("팩 이름 입력이 올바르지 않습니다")
    if not _BOT_USERNAME.fullmatch(bot_username): raise TelegramInputError("bot username 형식이 올바르지 않습니다")
    clean = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_"); clean = re.sub(r"_+", "_", clean)
    if not clean or not clean[0].isalpha(): clean = "stickers" + ("_" + clean if clean else "")
    sequence_suffix = "" if sequence == 1 else "_" + str(sequence)
    suffix = "_by_" + bot_username.lower()
    base_room = 64 - len(sequence_suffix) - len(suffix)
    if base_room < 1: raise TelegramInputError("bot username이 너무 깁니다")
    if len(clean) > base_room:
        digest = hashlib.sha256(clean.encode()).hexdigest()[:8]
        if base_room > len(digest) + 1:
            clean = clean[:base_room-len(digest)-1].rstrip("_") + "_" + digest
        else:
            clean = "s" + digest[:base_room-1]
    # `clean` is already normalized. Do not normalize the final result because a
    # valid bot username may begin with `_`, which must remain in the suffix.
    return clean.rstrip("_") + sequence_suffix + suffix

@dataclass(frozen=True)
class UploadedSticker:
    file_id: str
    file_unique_id: str
    format: str
    emoji_list: Sequence[str] = ("🙂",)
    def __post_init__(self) -> None:
        emojis = self.emoji_list
        if self.format not in ("static", "video") or not all(isinstance(v,str) and v for v in (self.file_id,self.file_unique_id)):
            raise TelegramInputError("업로드된 스티커 응답 형식이 올바르지 않습니다")
        if isinstance(emojis, (str, bytes)) or not isinstance(emojis, Sequence) or not 1 <= len(emojis) <= 20 or not all(isinstance(v, str) and v for v in emojis):
            raise TelegramInputError("emoji_list는 1~20개의 비어 있지 않은 문자열이어야 합니다")
        object.__setattr__(self, "emoji_list", tuple(emojis))

@dataclass(frozen=True)
class InputSticker:
    path: Path
    emoji_list: Sequence[str]
    format: str
    def __post_init__(self) -> None:
        path = Path(self.path); emojis = self.emoji_list
        if self.format not in ("static", "video"): raise TelegramInputError("지원하지 않는 스티커 형식입니다")
        if not path.is_file(): raise TelegramInputError("스티커 파일은 존재하는 일반 파일이어야 합니다")
        extension = path.suffix.lower()
        if (self.format == "static" and extension not in (".png", ".webp")) or (self.format == "video" and extension != ".webm"): raise TelegramInputError("파일 확장자와 Telegram 스티커 형식이 일치하지 않습니다")
        if isinstance(emojis, (str, bytes)) or not isinstance(emojis, Sequence) or not 1 <= len(emojis) <= 20 or not all(isinstance(v, str) and v for v in emojis): raise TelegramInputError("emoji_list는 1~20개의 비어 있지 않은 문자열이어야 합니다")
        object.__setattr__(self, "path", path); object.__setattr__(self, "emoji_list", tuple(emojis))

class TelegramClient:
    def __init__(self, token: str, *, opener: Optional[Callable[..., Any]] = None, timeout: float = 15.0, sleep: Callable[[float], None] = time_module.sleep, random: Callable[[], float] = random_module.random, max_retries: int = MAX_RETRIES) -> None:
        stripped = token.strip() if isinstance(token, str) else ""
        if not isinstance(token, str) or "\n" in token or "\r" in token or re.search(r"\s", stripped) or not _TOKEN.fullmatch(stripped): raise TelegramInputError("Telegram bot token 형식이 올바르지 않습니다")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise TelegramInputError("timeout은 유한한 양수여야 합니다")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise TelegramInputError("max_retries는 0 이상의 정수여야 합니다")
        # Keep only the normalized secret, never an untrimmed caller-provided copy.
        self._token, self._opener, self._timeout = stripped, opener or _default_opener, float(timeout)
        self._sleep, self._random, self._max_retries = sleep, random, max_retries
    def __repr__(self) -> str: return "TelegramClient(token=<redacted>)"
    __str__ = __repr__
    def _url(self, method: str) -> str: return f"{_API_ROOT}/bot{self._token}/{method}"
    @staticmethod
    def _read(response: Any) -> bytes:
        chunks: list[bytes] = []; total = 0
        while True:
            piece = response.read(min(65536, MAX_RESPONSE_BYTES-total+1))
            if not piece: return b"".join(chunks)
            total += len(piece)
            if total > MAX_RESPONSE_BYTES: raise TelegramError("Telegram 응답이 허용 크기를 초과했습니다")
            chunks.append(piece)
    def _call(self, method: str, *, fields: Optional[Mapping[str,str]] = None, files: Optional[Mapping[str,Path]] = None, retry: bool = True) -> Any:
        body, content_type = _multipart(fields or {}, files or {}) if files else (json.dumps(dict(fields or {}), ensure_ascii=False, separators=(",",":")).encode(), "application/json")
        max_retries = self._max_retries if retry else 0
        for attempt in range(max_retries + 1):
            request = Request(self._url(method), data=body, headers={"Content-Type":content_type,"Accept":"application/json"}, method="POST")
            try:
                response = self._opener(request, self._timeout)
                try: payload = _response_payload(self._read(response), method)
                finally:
                    close = getattr(response,"close",None)
                    if close: close()
                if payload["ok"]: return payload["result"]
                error = _api_error(method,payload)
            except HTTPError as exc:
                payload: Optional[dict[str, Any]] = None
                try:
                    payload = _response_payload(self._read(exc), method)
                except Exception:
                    # The HTTPError URL includes the bot token. Do not retain or
                    # chain it into the public error when its body is unusable.
                    error = TelegramApiError(method, None, "HTTP 오류 응답 schema가 올바르지 않습니다", retryable=False)
                else:
                    if payload["ok"]:
                        error = TelegramApiError(method, None, "HTTP 오류 응답 schema가 올바르지 않습니다", retryable=False)
                    else:
                        try:
                            error = _api_error(method, payload, exc.code)
                        except Exception:
                            error = TelegramApiError(method, None, "HTTP 오류 응답 schema가 올바르지 않습니다", retryable=False)
                finally:
                    try:
                        exc.close()
                    except Exception:
                        pass
            except (URLError,OSError,HTTPException,ValueError,TypeError): error = TelegramApiError(method,None,"네트워크 요청에 실패했습니다")
            retryable = error.retryable if error.retryable is not None else (error.code is None or error.code == 429 or (isinstance(error.code,int) and 500 <= error.code <= 599))
            if not retryable or attempt == max_retries: raise error from None
            delay = min(MAX_RETRY_AFTER,error.retry_after) if error.code == 429 and error.retry_after is not None else min(MAX_RETRY_AFTER, 2**attempt + self._random())
            self._sleep(delay)
        raise AssertionError("unreachable")
    def get_me(self) -> dict[str,Any]:
        result = self._call("getMe")
        if not isinstance(result,dict) or not isinstance(result.get("username"),str) or not result["username"]: raise TelegramApiError("getMe",None,"응답 result 형식이 올바르지 않습니다")
        return result
    def get_sticker_set(self,name: str) -> dict[str,Any]:
        _text(name,"팩 이름"); result = self._call("getStickerSet",fields={"name":name})
        if not isinstance(result,dict) or not all(isinstance(result.get(key),str) and result[key] for key in ("name","title")) or not isinstance(result.get("stickers"),list): raise TelegramApiError("getStickerSet",None,"응답 result 형식이 올바르지 않습니다")
        return result
    def upload_sticker_file(self,user_id:int,sticker:InputSticker)->UploadedSticker:
        _owner(user_id); result=self._call("uploadStickerFile",fields={"user_id":str(user_id),"sticker_format":sticker.format},files={"sticker":sticker.path})
        if not isinstance(result,dict) or not all(isinstance(result.get(k),str) and result[k] for k in ("file_id","file_unique_id")):
            raise TelegramApiError("uploadStickerFile",None,"응답 result 형식이 올바르지 않습니다")
        return UploadedSticker(result["file_id"],result["file_unique_id"],sticker.format,sticker.emoji_list)
    def create_new_sticker_set(self,user_id:int,name:str,title:str,sticker:InputSticker|UploadedSticker)->bool:
        _owner(user_id); _text(name,"팩 이름"); _text(title,"팩 제목"); result=self._call("createNewStickerSet",fields=_create_sticker_fields(user_id,name,title,sticker),files={"sticker_file":sticker.path} if isinstance(sticker,InputSticker) else None,retry=False)
        if result is not True: raise TelegramApiError("createNewStickerSet",None,"응답 result 형식이 올바르지 않습니다")
        return True
    def add_sticker_to_set(self,user_id:int,name:str,sticker:InputSticker|UploadedSticker)->bool:
        _owner(user_id); _text(name,"팩 이름"); result=self._call("addStickerToSet",fields=_add_sticker_fields(user_id,name,sticker),files={"sticker_file":sticker.path} if isinstance(sticker,InputSticker) else None,retry=False)
        if result is not True: raise TelegramApiError("addStickerToSet",None,"응답 result 형식이 올바르지 않습니다")
        return True

def _text(value:object,label:str)->None:
    if not isinstance(value,str) or not value.strip(): raise TelegramInputError(f"{label}은 비어 있지 않은 문자열이어야 합니다")
def _owner(value:object)->None:
    if not isinstance(value,int) or isinstance(value,bool): raise TelegramInputError("owner user id는 정수여야 합니다")
def _sticker_json(sticker: InputSticker|UploadedSticker) -> str:
    if isinstance(sticker, UploadedSticker): return json.dumps({"sticker":sticker.file_id,"format":sticker.format,"emoji_list":list(sticker.emoji_list)}, ensure_ascii=False, separators=(",",":"))
    return json.dumps({"sticker":"attach://sticker_file","format":sticker.format,"emoji_list":list(sticker.emoji_list)}, ensure_ascii=False, separators=(",",":"))
def _create_sticker_fields(user_id:int,name:str,title:str,sticker:InputSticker|UploadedSticker)->dict[str,str]:
    return {"user_id":str(user_id),"name":name,"title":title,"stickers":"[" + _sticker_json(sticker) + "]"}
def _add_sticker_fields(user_id:int,name:str,sticker:InputSticker|UploadedSticker)->dict[str,str]:
    return {"user_id":str(user_id),"name":name,"sticker":_sticker_json(sticker)}
def _multipart(fields:Mapping[str,str],files:Mapping[str,Path])->tuple[bytes,str]:
    boundary="----tele-sticker-"+uuid.uuid4().hex; chunks:list[bytes]=[]
    for key,value in fields.items(): chunks += [f"--{boundary}\r\n".encode(),f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),str(value).encode(),b"\r\n"]
    for key,path in files.items():
        path=Path(path); filename=re.sub(r'[\r\n"\\]',"_",path.name); mime="video/webm" if path.suffix.lower()==".webm" else ("image/webp" if path.suffix.lower()==".webp" else "image/png")
        chunks += [f"--{boundary}\r\n".encode(),f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode(),f"Content-Type: {mime}\r\n\r\n".encode(),path.read_bytes(),b"\r\n"]
    chunks.append(f"--{boundary}--\r\n".encode()); return b"".join(chunks),f"multipart/form-data; boundary={boundary}"
def _response_payload(raw:bytes,method:str)->dict[str,Any]:
    try: value=json.loads(raw.decode())
    except (UnicodeDecodeError,json.JSONDecodeError) as error: raise TelegramApiError(method,None,"JSON 응답 형식이 올바르지 않습니다") from error
    if not isinstance(value,dict) or not isinstance(value.get("ok"),bool): raise TelegramApiError(method,None,"응답 schema가 올바르지 않습니다")
    if value["ok"]:
        if "result" not in value: raise TelegramApiError(method,None,"응답 result가 없습니다")
    elif not isinstance(value.get("error_code"),int) or isinstance(value["error_code"],bool) or not isinstance(value.get("description"),str): raise TelegramApiError(method,None,"오류 응답 schema가 올바르지 않습니다")
    return value
def _api_error(method:str,payload:dict[str,Any],fallback:Optional[int]=None)->TelegramApiError:
    code=payload.get("error_code",fallback); parameters=payload.get("parameters"); retry=parameters.get("retry_after") if isinstance(parameters,dict) else None
    retry=retry if isinstance(retry,int) and not isinstance(retry,bool) and retry >= 0 else None
    return TelegramApiError(method,code if isinstance(code,int) and not isinstance(code,bool) else fallback,payload["description"],retry)
