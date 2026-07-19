---
name: kakao-telegram-stickers
description: "카카오 이모티콘을 Telegram 스티커팩에 준비·등록·재개할 때 사용한다."
version: 1.0.0
author: TeleSticker-Maker contributors
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  commands: [uv, ffmpeg, ffprobe]
required_environment_variables:
  - name: TELEGRAM_BOT_TOKEN
    prompt: Telegram BotFather bot token
    help: "BotFather에서 발급한 봇 토큰을 환경 변수로 설정한다. 값은 출력하거나 대화에 붙여 넣지 않는다."
metadata:
  hermes:
    tags: [kakao, emoticon, telegram, sticker, webp, webm]
    category: creative
    requires_toolsets: [terminal, file]
    config:
      - key: kakao_stickers.owner_user_id
        description: "스티커팩을 소유할 Telegram 사용자 ID"
        prompt: "BotFather 봇과 대화한 Telegram 사용자 ID를 입력하세요"
      - key: kakao_stickers.default_pack_alias
        description: "기본 스티커팩 시리즈의 로컬 별칭"
        default: default
        prompt: "기본 팩 별칭을 입력하세요"
      - key: kakao_stickers.default_pack_title
        description: "첫 번째 기본 Telegram 스티커팩 제목"
        default: "Kakao stickers"
        prompt: "기본 팩 제목을 입력하세요"
      - key: kakao_stickers.default_pack_slug
        description: "봇 이름과 조합할 기본 Telegram 스티커팩 slug"
        default: kakao_stickers
        prompt: "기본 팩 slug를 영문·숫자·밑줄로 입력하세요"
      - key: kakao_stickers.default_emoji
        description: "등록하는 스티커에 연결할 기본 emoji"
        default: "🙂"
        prompt: "기본 emoji를 입력하세요"
      - key: kakao_stickers.data_dir
        description: "원본, 파생본, 작업 기록과 SQLite 상태를 보관할 디렉터리"
        default: "~/.tele-sticker-maker"
        prompt: "스티커 작업 데이터 디렉터리를 입력하세요"
---

# Kakao → Telegram stickers

카카오 이모티콘 원본을 보관하고 Telegram 봇이 관리하는 스티커팩에 안전하게 등록한다. animated WebP는 원본을 유지하고 투명 VP9 WebM 파생본으로 등록한다.

## 사용할 때와 사용하지 않을 때

- **사용:** 카카오 이모티콘 URL/slug 다운로드, Telegram 스티커팩 변환·등록, 준비한 등록 작업의 재개를 요청할 때.
- **사용하지 않음:** Telegram 사용자 계정(MTProto) 로그인, 다른 봇이 소유한 팩 수정, 카카오 이모티콘과 무관한 이미지 편집·생성 요청일 때.

## 필수 실행 규칙

1. `uv`, `ffmpeg`, `ffprobe`를 확인하고 `TELEGRAM_BOT_TOKEN`은 Hermes secure prompt 또는 안전한 환경 파일에만 저장한다.
2. user 입력과 `[Skill config]` 값은 셸 명령에 직접 넣지 않는다. **terminal/cat/heredoc를 사용하지 말고 Hermes `write_file` structured tool**로 세션 request JSON을 작성한다.
3. request 파일은 항상 **[Skill config] header에 표시된 Hermes home** 아래 `${HERMES_HOME}/data/kakao-telegram-stickers/requests/request-${HERMES_SESSION_ID}.json`에 쓴다. `HERMES_SESSION_ID`는 Hermes가 생성한 system token만 사용하며 사용자 입력으로 대체하지 않는다. `assets/request-template.json`은 구조 참고용으로만 읽는다.
4. 실행은 아래 고정 명령 한 가지로만 한다. 경로와 session ID는 Hermes가 제공하는 system-generated 값이며 사용자 입력을 포함하지 않는다.

```bash
uv run --script "${HERMES_SKILL_DIR}/scripts/request_runner.py" --request-id "${HERMES_SESSION_ID}"
```

runner는 허용된 Hermes data root 안의 request만 검증하고 실행 뒤 삭제하며, 비어 있는 `requests/` 디렉터리도 정리한다. 설치된 스킬 asset에는 동적 request를 쓰지 않는다. `scripts/run.py`는 runner가 호출하는 standalone runtime이며 직접 실행 명령으로 제시하지 않는다.

## JSON request 형식

`write_file`로 작성하는 JSON의 모든 key는 아래 action 형식과 정확히 일치해야 한다. JSON 값에는 사용자 입력과 주입된 설정 값을 넣어도 되지만 셸에 보간하지 않는다.

### doctor

```json
{"action":"doctor"}
```

### download-only

```json
{"action":"download","inputs":["<Kakao URL 또는 slug>"],"output":"<kakao_stickers.data_dir>/stickers"}
```

### prepare

```json
{"action":"prepare","source":"<Kakao URL 또는 slug>","ownerUserId":<kakao_stickers.owner_user_id>,"pack":"<kakao_stickers.default_pack_alias>","packTitle":"<kakao_stickers.default_pack_title>","packSlug":"<kakao_stickers.default_pack_slug>","emoji":"<kakao_stickers.default_emoji>","dataDir":"<kakao_stickers.data_dir>"}
```

`prepare`는 다운로드·변환·원본/파생본/작업 기록 저장과 Telegram 조회만 수행한다. **publish 전에는 Telegram 팩을 생성하거나 변경하지 않는다.** 다른 팩 시리즈를 선택하면 `pack`, `packTitle`, `packSlug`를 모두 함께 바꾼다.

## 확인 후 publish

prepare JSON을 받은 직후, publish를 실행하지 말고 **하나의 검토 메시지**에 다음을 모두 표시한다.

- `slug`, `targetPack`, `packsAfterPublish`
- `discovered`, `readyStatic`, `readyVideo`, `duplicates`, `excluded`, `failed`
- `issues`의 모든 `itemIndex`, `status`, `reason`
- 생성될 Telegram 팩 링크가 공개 접근될 수 있으며, 사용·배포 권한이 있는 이모티콘만 등록해야 한다는 경고

그 메시지에서 정확한 `jobId`를 명시하고 등록 승인을 한 번 요청한다. 최신 작업을 추측하거나 다른 작업 ID를 대신 사용하지 않는다. 사용자가 명시적으로 승인하기 전에는 publish request를 작성하거나 실행하지 않는다.

명시적 승인을 받은 뒤에만 아래 구조의 request JSON을 `write_file`로 작성하고 고정 runner 명령을 실행한다.

```json
{"action":"publish","jobId":"<prepare 결과의 jobId>","confirm":true,"ownerUserId":<prepare JSON binding.ownerUserId>,"pack":"<prepare JSON binding.packAlias>","packTitle":"<prepare JSON binding.packTitle>","packSlug":"<prepare JSON binding.packSlug>","emoji":"<prepare JSON binding.emoji>","dataDir":"<prepare에 사용한 dataDir>"}
```

`publish`의 `ownerUserId`, `pack`, `packTitle`, `packSlug`, `emoji`는 현재 기본 설정값으로 다시 만들지 않는다. 반드시 해당 prepare JSON의 `binding.ownerUserId`, `binding.packAlias`, `binding.packTitle`, `binding.packSlug`, `binding.emoji`를 그대로 복사한다.

## 상태·재개·팩 목록·owner ID

재개 전에는 현재 상태 request를 실행하고, 반환된 `prepareSummary.binding`(최상위 `binding`에도 같은 값), `items`의 전체 상태와 `counts`, `issues`의 모든 reason, `pending`을 검토 메시지에 표시한 뒤 다시 명시적 승인을 받는다. 재개 publish도 이 `binding` 값을 그대로 사용한다.

```json
{"action":"status","jobId":"<jobId>","dataDir":"<kakao_stickers.data_dir>"}
```

```json
{"action":"packs","ownerUserId":<kakao_stickers.owner_user_id>,"dataDir":"<kakao_stickers.data_dir>"}
```

owner ID fallback은 gateway를 중지하고 소유자가 전송한 일회용 marker가 정확히 한 명에게만 일치할 때 사용한다. marker도 request JSON에만 넣는다. 조회 실패는 민감 정보 없는 종료 코드 `3`으로 반환된다.

```json
{"action":"owner-id","marker":"<one-time random marker>"}
```

## JSON 결과와 종료 코드

runner가 CLI JSON 결과를 그대로 반환한다. `0`은 성공, `1`은 doctor 의존성 미충족, `2`는 설정·request 오류, `3`은 저장소·준비·상태·owner ID 조회 오류, `4`는 미디어 오류, `5`는 Telegram API 오류, `6`은 일부 항목 실패, `7`은 확인 필요·작업 상태·lease 충돌을 뜻한다.

## publish 결과 응답

publish 뒤 사용자에게 `packLinks` **전체**, `published`, `duplicates`, `excluded`, `failed`, `resumable`을 모두 보여준다. 종료 코드가 `6`, `3`, `5`, `7`이면 즉시 같은 job의 `status`를 조회해 `issues`의 모든 `reason`과 다음 행동(각 reason의 해결, 네트워크·토큰 확인, 명시적 승인, 또는 lease 해제 대기 후 재개)을 함께 안내한다.

## Supporting files

Hermes GitHub installer가 런타임 파일을 함께 설치하도록 다음 상대경로를 유지한다.

- `scripts/run.py`
- `scripts/request_runner.py`
- `scripts/get_owner_id.py`
- `scripts/tele_sticker_maker/__init__.py`
- `scripts/tele_sticker_maker/cli.py`
- `scripts/tele_sticker_maker/config.py`
- `scripts/tele_sticker_maker/kakao.py`
- `scripts/tele_sticker_maker/media.py`
- `scripts/tele_sticker_maker/models.py`
- `scripts/tele_sticker_maker/state.py`
- `scripts/tele_sticker_maker/telegram.py`
- `scripts/tele_sticker_maker/webp.py`
- `scripts/tele_sticker_maker/workflow.py`
- `assets/request-template.json`
- `references/configuration.md`
- `references/troubleshooting.md`
