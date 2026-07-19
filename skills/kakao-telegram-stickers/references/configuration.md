# 설정 및 설치

## 필수 도구

이 스킬은 `uv`, `ffmpeg`, `ffprobe`가 필요하다. macOS에서는 `brew install uv ffmpeg`, Linux에서는 배포판 패키지 관리자로 `ffmpeg`를 설치한 뒤 [uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 따른다. Windows에서는 `winget install astral-sh.uv`와 PATH에 있는 `ffmpeg`/`ffprobe`를 사용한다.

모든 실행은 SKILL.md의 고정 request runner 명령으로 한다. dynamic 값이 든 명령을 셸에 작성하지 않는다.

## Telegram 봇 토큰

1. Telegram의 **@BotFather**에서 `/newbot`으로 봇을 만들고 토큰을 발급받는다.
2. Hermes `required_environment_variables` secure prompt가 보이면 그 입력란에만 토큰을 저장한다.
3. secure prompt를 사용할 수 없으면 `${HERMES_HOME:-$HOME/.hermes}/.env`를 편집기로 열어 `TELEGRAM_BOT_TOKEN=<token>`을 저장하고, Unix 계열에서는 `chmod 600 "${HERMES_HOME:-$HOME/.hermes}/.env"`를 적용한 뒤 gateway를 재시작한다.

토큰은 채팅, 명령줄 인수, 출력, 로그에 넣지 않는다.

## 소유자 ID

Hermes gateway의 인증된 Telegram 발신자 metadata를 먼저 사용한다. fallback이 필요하면 gateway를 중지하고, 소유자가 봇에 전송한 일회용 무작위 marker를 request JSON에 넣는다.

```json
{"action":"owner-id","marker":"<one-time random marker>"}
```

Hermes `write_file` structured tool로 **[Skill config] header에 표시된 Hermes home** 아래 `${HERMES_HOME}/data/kakao-telegram-stickers/requests/request-${HERMES_SESSION_ID}.json`에 작성한 뒤 SKILL.md의 고정 runner 명령을 사용한다. `HERMES_SESSION_ID`는 Hermes가 만든 system token만 사용하며 설치된 스킬 asset에는 동적 request를 쓰지 않는다. helper는 정확히 일치하는 marker 후보가 한 명일 때만 ID·username·first name JSON을 반환한다. 출력 값을 소유자가 수동 확인한 후 `owner_user_id`에 저장하고 gateway를 다시 시작한다. 원본 `getUpdates` 응답을 출력하거나 마지막 업데이트를 임의 선택하지 않는다.

## Hermes `config.yaml`

Hermes는 스킬의 논리 키를 `skills.config` 아래에 저장하고, 실행 시 `[Skill config]` 블록으로 값을 주입한다.

```yaml
skills:
  config:
    kakao_stickers:
      owner_user_id: 123456789
      default_pack_alias: default
      default_pack_title: Kakao stickers
      default_pack_slug: kakao_stickers
      default_emoji: "🙂"
      data_dir: ~/.tele-sticker-maker
```

`owner_user_id`에는 기본값이 없고, 팩을 바꿀 때는 alias, title, slug를 함께 바꾼다.

## 데이터 보관과 백업

`data_dir`의 `stickers/<slug>/`에는 canonical 원본·manifest, `jobs/<jobId>/`에는 publish용 불변 snapshot, `state.sqlite`에는 중복 방지·rollover·재개 상태가 있다. `work/<jobId>/`는 prepare 동안만 쓰는 격리 workspace이며 완료 후 정리된다. 백업 시 `stickers/`, `jobs/`, `state.sqlite`를 함께 보관하고, 하나의 `state.sqlite`를 여러 장치에서 동시에 수정하지 않는다.
