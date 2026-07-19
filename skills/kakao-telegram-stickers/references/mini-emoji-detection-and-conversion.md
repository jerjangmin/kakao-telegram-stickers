# 카카오 미니 이모지 판별·변환 설계

## 1. 목표

카카오 이모티콘 URL 또는 slug를 `prepare`할 때 카카오 API의 명시적 타입 정보를 사용해 미니 이모지를 판별하고, 미니 이모지만 `512×512` 투명 캔버스 안의 `250×250` 콘텐츠 박스로 렌더링한다. 판정 근거와 실제 렌더 정책은 prepare 결과와 매니페스트에 남겨 publish 전에 사람이 확인할 수 있어야 한다.

## 2. 확인된 카카오 API 신호

상세 링크를 slug로 해석한 뒤 아래 API를 조회한다.

```text
https://e.kakao.com/api/items/{slug}
```

현재 응답은 다음 집합 수준 속성을 제공한다.

```json
{
  "contents": {
    "isMini": true,
    "isBig": false,
    "isSound": false,
    "items": [
      {
        "width": 180,
        "height": 180,
        "thumbnailUrl": "…",
        "animatedUrl": "…"
      }
    ]
  }
}
```

실조회 교차검증:

| 유형 | `contents.isMini` | 항목 수 | API 크기 |
|---|---:|---:|---:|
| 미니 몽몽이가 왔따 | `true` | 35 | 180×180 |
| 한교동은 휴가 가고 싶교동 | `true` | 35 | 180×180 |
| 한교동 제법 작아졌어요 | `true` | 35 | 180×180 |
| 춘식이는 승요 | `false` | 24 | 360×360 |
| 우당탕탕 마이멜로디와 쿠로미 | `false` | 24 | 360×360 |

### 판정 우선순위

1. `contents.isMini is True` → `MINI`
2. `contents.isMini is False` → `STANDARD`
3. 필드 누락·`null`·비불리언 → `UNKNOWN`

`slug`에 `mini`가 들어가는지, 제목에 `미니`가 들어가는지, 항목 수가 35개인지로 판정하지 않는다. 180×180 크기는 진단용 보조 신호일 뿐 권위 있는 판정값이 아니다.

### 일관성 경고

- `isMini=true`인데 모든 항목이 180×180이 아니면 prepare는 계속하되 `metadata_mismatch` 경고를 낸다.
- `isMini=false`인데 모든 항목이 180×180이면 같은 경고를 낸다.
- `isMini`가 `UNKNOWN`이면 `layoutMode=auto`에서는 변환하지 않고 prepare 전체를 종료 코드 `3`으로 중단한다. 조용히 일반 규칙을 적용하면 미니 이모지가 512px로 과대 확대될 수 있기 때문이다.
- API 변경이나 레거시 응답을 처리해야 할 때만 명시적 `layoutMode=mini|standard` 재정의를 허용한다. 재정의 사실과 API 관측값을 매니페스트에 모두 기록한다.

## 3. 요청 계약

### prepare request

기본값은 `auto`다. 기존 요청과 호환되도록 `layoutMode`는 생략 가능하게 한다.

```json
{
  "action": "prepare",
  "source": "<Kakao URL 또는 slug>",
  "layoutMode": "auto",
  "ownerUserId": 123,
  "pack": "default",
  "packTitle": "Kakao stickers",
  "packSlug": "kakao_stickers",
  "emoji": "🙂",
  "dataDir": "~/.tele-sticker-maker"
}
```

허용값:

- `auto`: API `contents.isMini`를 따른다.
- `mini`: API가 불명확할 때 미니 레이아웃을 강제로 선택한다.
- `standard`: API가 불명확할 때 일반 레이아웃을 강제로 선택한다.

API가 명시한 값과 수동 재정의가 충돌하면 변환은 허용하되 prepare 결과에 눈에 띄는 `manual_layout_override_conflict` 경고를 넣고, 사용자 검토 없이는 publish하지 않는다. publish는 이미 만들어진 immutable snapshot을 사용하므로 `layoutMode`를 다시 받지 않는다.

`request_runner.py`의 현재 exact-key 검증은 prepare에 한해 아래 두 key 집합을 모두 허용하도록 변경한다.

- 기존 집합
- 기존 집합 + `layoutMode`

알 수 없는 key는 계속 거부한다.

## 4. 도메인 모델

`kakao.py`가 항목 목록만 반환하지 않고 집합 메타데이터를 함께 반환하게 한다.

```python
class LayoutKind(str, Enum):
    MINI = "mini"
    STANDARD = "standard"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class KakaoSetMetadata:
    is_mini: Optional[bool]
    is_big: Optional[bool]
    is_sound: Optional[bool]

@dataclass(frozen=True)
class KakaoSetResponse:
    api_url: str
    metadata: KakaoSetMetadata
    items: tuple[RemoteSticker, ...]
```

변환 정책은 데이터로 표현한다.

```python
@dataclass(frozen=True)
class RenderLayout:
    kind: LayoutKind
    canvas_width: int
    canvas_height: int
    content_max_width: int
    content_max_height: int
    anchor: str

STANDARD_LAYOUT = RenderLayout(LayoutKind.STANDARD, 512, 512, 512, 512, "center")
MINI_LAYOUT = RenderLayout(LayoutKind.MINI, 512, 512, 250, 250, "center")
```

일반 이모티콘은 현재와 같은 “긴 변 512” 결과를 유지해 회귀를 막는다. 다만 내부 렌더 helper는 공용으로 사용한다.

## 5. 미니 이모지 렌더 알고리즘

### 핵심 규칙

- 출력 캔버스: 투명 RGBA `512×512`
- 안쪽 콘텐츠 박스: `250×250`
- 리사이즈: 원본 프레임의 전체 캔버스를 종횡비 유지한 채 안쪽 박스에 contain
- 정렬: 정중앙
- crop 금지
- alpha bounding box 기반 재크롭 금지
- 모든 프레임에 같은 레이아웃 정책 적용

정사각형 180×180 원본은 250×250으로 확대되고, `(512-250)/2 = 131`이므로 사방 131px 투명 여백이 생긴다.

```python
def fit_inside(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    scale = min(max_width / width, max_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def render_frame(frame: Image.Image, layout: RenderLayout) -> Image.Image:
    rgba = frame.convert("RGBA")
    width, height = fit_inside(
        rgba.width,
        rgba.height,
        layout.content_max_width,
        layout.content_max_height,
    )
    resized = rgba.resize((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (layout.canvas_width, layout.canvas_height), (0, 0, 0, 0))
    x = (layout.canvas_width - width) // 2
    y = (layout.canvas_height - height) // 2
    canvas.alpha_composite(resized, (x, y))
    return canvas
```

원본 프레임별 alpha bounding box로 캐릭터만 잘라내면 프레임마다 위치·크기가 흔들리고 카카오가 의도한 동작 여백이 사라질 수 있으므로 사용하지 않는다.

### 정적 PNG

`make_static_png(source, destination, layout)`에 레이아웃을 전달한다. 미니는 항상 512×512 투명 PNG가 되며 Telegram의 512KB 제한을 기존처럼 검증한다.

### animated WebP → WebM

`make_animated_webm(source, destination, layout, ...)`에서 각 WebP 프레임을 `render_frame`으로 처리한 뒤 기존 타임라인 샘플링과 VP9 인코딩을 그대로 사용한다.

유지할 Telegram 검증:

- WebM / VP9
- `yuva420p` 및 alpha metadata
- 오디오 없음
- 최대 30fps
- 최대 3초
- 최대 256KB
- 출력 512×512
- 완전 투명 영상 거부

현재 `validate_telegram_video`의 “긴 변 512” 검증은 통과하지만, 미니 레이아웃에서는 별도로 정확한 `width == 512 and height == 512`도 확인한다.

## 6. 판정·정책 전달 흐름

```text
URL/slug
  → resolve_input()
  → /api/items/{slug}
  → parse set metadata (`contents.isMini`)
  → resolve_layout(auto|mini|standard)
  → ManifestV3에 detection 기록
  → prepare_telegram_item(..., layout)
  → 정적 PNG 또는 VP9 WebM
  → prepare summary에 layout 표시
  → 사용자 검토
  → 명시적 승인 뒤 publish
```

권장 함수 경계:

```python
def resolve_layout(metadata: KakaoSetMetadata, requested: str) -> LayoutDecision: ...
def prepare_telegram_item(item, set_root, *, layout: RenderLayout, ffmpeg="ffmpeg", ffprobe="ffprobe"): ...
def make_static_png(source, destination, *, layout: RenderLayout): ...
def make_animated_webm(source, destination, *, layout: RenderLayout, ffmpeg="ffmpeg", ffprobe="ffprobe"): ...
```

## 7. 매니페스트와 prepare 결과

집합 수준 판정이므로 각 항목에 중복 저장하지 않고 최상위에 기록한다. 기존 schemaVersion 2를 3으로 올린다.

```json
{
  "schemaVersion": 3,
  "slug": "mini-mongmong-is-here",
  "sourceTraits": {
    "isMini": true,
    "isBig": false,
    "isSound": false
  },
  "layout": {
    "requestedMode": "auto",
    "kind": "mini",
    "decisionSource": "contents.isMini",
    "canvas": [512, 512],
    "contentBox": [250, 250],
    "anchor": "center",
    "warnings": []
  }
}
```

prepare summary에도 다음을 포함한다.

```json
{
  "layout": {
    "kind": "mini",
    "decisionSource": "contents.isMini",
    "canvas": [512, 512],
    "contentBox": [250, 250],
    "manualOverride": false,
    "warnings": []
  }
}
```

스킬의 publish 전 검토 메시지에는 반드시 다음 한 줄을 추가한다.

```text
레이아웃: 미니 이모지 자동 판별 · 512×512 투명 캔버스 · 콘텐츠 최대 250×250 · 중앙 정렬
```

## 8. 변경 파일

- Modify: `scripts/tele_sticker_maker/kakao.py`
  - `contents.isMini/isBig/isSound` 파싱
  - set response 모델 반환
- Modify: `scripts/tele_sticker_maker/models.py`
  - `LayoutKind`, `KakaoSetMetadata`, `RenderLayout`, `LayoutDecision`, ManifestV3
- Modify: `scripts/tele_sticker_maker/media.py`
  - downloader에서 set metadata와 layout 보존
  - converter에 layout 전달
- Modify: `scripts/tele_sticker_maker/webp.py`
  - `fit_inside`, `render_frame`
  - static/video 레이아웃 지원
- Modify: `scripts/tele_sticker_maker/workflow.py`
  - requested mode 해석
  - prepare summary/binding에 layout 감사정보 저장
- Modify: `scripts/tele_sticker_maker/cli.py`
  - `--layout-mode auto|mini|standard`
- Modify: `scripts/request_runner.py`
  - prepare request의 선택적 `layoutMode` 검증
- Modify: `assets/request-template.json`
- Modify: `SKILL.md`
- Create in source repository: `tests/test_kakao_layout.py`
- Create in source repository: `tests/test_mini_render.py`
- Modify in source repository: request/CLI/workflow integration tests

설치된 스킬에는 테스트가 포함돼 있지 않으므로 실제 구현은 원본 저장소의 test suite에서 먼저 진행하고, 통과한 산출물을 설치 경로에 반영해야 한다.

## 9. TDD 구현 순서

### Task 1: API 메타데이터 파싱

1. `isMini=true`, `false`, 누락, 잘못된 타입 fixture를 만든다.
2. 실패 테스트를 실행한다.
3. `KakaoSetMetadata`와 parser를 구현한다.
4. 테스트를 통과시킨다.

### Task 2: 레이아웃 결정

1. auto true→mini, auto false→standard, auto unknown→오류 테스트를 쓴다.
2. 수동 override와 conflict warning 테스트를 쓴다.
3. `resolve_layout`을 구현한다.

### Task 3: 정적 미니 렌더

1. 불투명 180×180 fixture를 만든다.
2. 출력이 512×512인지 확인한다.
3. alpha bbox가 `(131, 131, 381, 381)`인지 확인한다.
4. 원본 파일 hash가 바뀌지 않는지 확인한다.

### Task 4: 애니메이션 미니 렌더

1. 위치가 다른 3프레임 animated WebP fixture를 만든다.
2. 모든 출력 프레임 캔버스가 512×512인지 확인한다.
3. 원본 전체 캔버스가 각 프레임에서 250×250 안에 유지되는지 확인한다.
4. alpha, fps, duration, byte limit 검증을 통과시킨다.

### Task 5: 요청·CLI·워크플로 통합

1. 기존 prepare JSON이 그대로 통과하는 회귀 테스트를 쓴다.
2. `layoutMode=auto|mini|standard` 허용 테스트를 쓴다.
3. 알 수 없는 값과 알 수 없는 key 거부 테스트를 쓴다.
4. prepare summary와 binding의 layout 기록을 검증한다.

### Task 6: live smoke

네트워크 의존 테스트는 자동 테스트에 넣지 않고 수동 smoke로 둔다.

- 미니 slug: `mini-mongmong-is-here`
  - 기대: `isMini=true`, `kind=mini`, `contentBox=[250,250]`
- 일반 slug: `victory-fairy-choonsik-lotte-giants`
  - 기대: `isMini=false`, `kind=standard`, 일반 크기 유지

두 작업 모두 `prepare`까지만 실행하고 Telegram 팩이 생성·변경되지 않았는지 확인한다.

## 10. 완료 기준

- 미니 판정은 slug/제목 휴리스틱이 아니라 `contents.isMini`를 사용한다.
- API가 불명확한 auto 판정은 조용히 일반 처리하지 않는다.
- 미니 정적·영상 결과가 512×512 투명 캔버스와 250×250 콘텐츠 박스를 사용한다.
- crop 없이 원본 비율·프레임 위치·alpha·타이밍을 보존한다.
- 일반 이모티콘 결과에 시각·크기 회귀가 없다.
- prepare summary에 판정 근거와 레이아웃이 표시된다.
- publish는 기존처럼 명시적 승인 전 절대 실행되지 않는다.
- 전체 테스트, Python compile, doctor, 미니/일반 live prepare smoke가 통과한다.
