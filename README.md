# TeleSticker-Maker

카카오 이모티콘 원본을 보관하면서 Telegram 스티커팩 등록용 파일을 준비하는 크로스플랫폼 도구입니다. Python 코어는 독립 CLI와 Hermes 스킬이 함께 사용합니다.

> **이 비공개 작업 workspace에는 원본 자산과 기존 Git 이력이 있을 수 있으므로 직접 public으로 전환하지 마세요.** code-only public export에는 해당 자산·이력이 포함되지 않으며, [`docs/public-release.md`](docs/public-release.md)의 절차로 별도 저장소에서만 공개합니다.

## 기능

- Kakao API/CDN의 animated WebP 원본 바이트를 그대로 보관하고, 애니메이션이 없으면 PNG 원본을 보관합니다.
- 정적 이미지는 최대 변 512px PNG로, animated WebP는 투명 alpha를 보존한 VP9 WebM으로 변환·검증합니다.
- `prepare → 사용자 확인 → publish` 단계로 Telegram 팩 변경 전 결과를 검토합니다.
- SQLite로 원본 SHA-256 중복 방지, 120개 팩 rollover, 부분 실패 후 재개를 관리합니다.
- macOS, Linux, Windows에서 동일한 Python/`uv` 런타임으로 동작합니다.

## 요구 사항

- Python 3.9 이상과 [uv](https://docs.astral.sh/uv/getting-started/installation/)
- `ffmpeg`, `ffprobe`, 그리고 `libvpx-vp9` 인코더
- Telegram 등록 시에만 `TELEGRAM_BOT_TOKEN`과 본인 Telegram user ID

의존성을 확인합니다.

```bash
uv sync --locked
uv run skills/kakao-telegram-stickers/scripts/run.py doctor --json
```

`doctor` 결과의 `ok`가 `true`여야 변환을 시작할 수 있습니다. macOS는 `brew install ffmpeg`, Ubuntu/Debian은 `sudo apt-get install ffmpeg`, Windows는 `choco install ffmpeg`처럼 OS 패키지 관리자로 ffmpeg를 설치할 수 있습니다.

## 독립 다운로드 CLI

기존 `download_stickers.sh`는 계속 사용할 수 있습니다. slug(앞에 단일 하이픈이 있는 slug 포함), `https://e.kakao.com/t/<slug>`, Kakao 공유/상품 URL의 세 형식을 받고 여러 입력을 순서대로 처리합니다.

```bash
./download_stickers.sh teemos-doodle-league-of-legends \
  https://e.kakao.com/t/teemos-doodle-league-of-legends \
  'https://emoticon.kakao.com/items/<item-id>?lang=ko'

uv run skills/kakao-telegram-stickers/scripts/run.py download \
  teemos-doodle-league-of-legends --output stickers --json
```

다운로드 결과는 `stickers/<slug>/`에 저장됩니다.

- `webp/sticker_XX.webp`: CDN animated WebP 원본
- `png/sticker_XX.png`: 첫 프레임 확인용 PNG 또는 정적 원본 PNG
- `json/manifest.json`: 원본 URL, 형식, 해상도, 프레임 수, 파일 크기와 검증 정보

## Hermes 설치와 Telegram 설정

공개 code-only 저장소에서 Hermes 스킬을 검사하고 설치합니다.

```bash
hermes skills inspect jerjangmin/kakao-telegram-stickers/skills/kakao-telegram-stickers
hermes skills install jerjangmin/kakao-telegram-stickers/skills/kakao-telegram-stickers
```

상세 절차와 보안 설정은 스킬의 [`configuration.md`](skills/kakao-telegram-stickers/references/configuration.md)를 따릅니다.

1. Telegram **@BotFather**에서 봇을 만들고, 토큰은 Hermes secure prompt 또는 보호된 환경 파일의 `TELEGRAM_BOT_TOKEN`에만 저장합니다. 토큰을 채팅, 명령 인수, Git, 로그에 넣지 마세요.
2. 봇과 `/start` 대화를 시작한 본인만 owner로 설정합니다. Hermes gateway의 인증된 sender metadata를 우선 사용하고, 필요한 경우 일회용 marker로 owner ID를 확인합니다.
3. Hermes skill config에서 `owner_user_id`, `default_pack_alias`(기본 `default`), `default_pack_title`(기본 `Kakao stickers`), `default_pack_slug`(기본 `kakao_stickers`), `default_emoji`(기본 `🙂`), `data_dir`(기본 `~/.tele-sticker-maker`)를 설정합니다.

## 준비·확인·등록

Hermes에서는 스킬의 `SKILL.md`에 있는 고정 request runner와 structured `write_file`만 사용합니다. 먼저 `prepare` 결과에서 `jobId`, 대상 팩, 준비/제외/실패 항목, rollover 예정 팩을 사용자에게 한 번에 보여줍니다. 사용자가 해당 `jobId` 등록을 명시적으로 승인한 뒤에만 `publish`를 실행합니다.

`publish`는 prepare가 반환한 `binding`의 owner/팩/emoji 값을 그대로 재사용합니다. 같은 원본은 같은 팩 시리즈에 다시 넣지 않으며, 실패한 항목만 상태를 보고 재개할 수 있습니다. 자세한 JSON request와 종료 코드는 [`SKILL.md`](skills/kakao-telegram-stickers/SKILL.md), 문제 해결은 [`troubleshooting.md`](skills/kakao-telegram-stickers/references/troubleshooting.md)를 참고하세요.

## 데이터와 백업

`data_dir`에는 다음이 보관됩니다.

```text
~/.tele-sticker-maker/
├── stickers/<slug>/     # canonical 원본과 manifest
├── jobs/<job-id>/       # publish용 불변 snapshot
├── work/<job-id>/       # prepare 중 임시 workspace
└── state.sqlite         # 중복, rollover, 재개 상태
```

장치 이전이나 백업 시 `stickers/`, `jobs/`, `state.sqlite`를 함께 보관하세요. 하나의 SQLite 상태 파일을 여러 장치에서 동시에 수정하지 마세요.

## 권리·개인정보·공개성

- 본인이 소유하거나 Telegram에 등록·배포할 권한이 있는 이모티콘만 사용하세요.
- 이 도구는 Kakao 로그인, 접근 제어 우회, 비공개 콘텐츠 획득을 하지 않습니다.
- 생성된 Telegram `addstickers` 링크와 팩은 공개·공유될 수 있습니다.
- 봇 토큰은 절대 커밋하지 말고, 작업 데이터에는 원본과 Telegram 작업 기록이 포함되므로 안전하게 백업하세요.

## 테스트

```bash
uv sync --locked
uv run --python 3.9 pytest -q
uv run --python 3.12 pytest -q
uv run --no-project python scripts/export_public_tree.py ../tele-sticker-maker-public
uv run --no-project python scripts/audit_public_tree.py ../tele-sticker-maker-public
```

CI는 Ubuntu, macOS, Windows에서 Python 3.9/3.11/3.12와 ffmpeg VP9 alpha 변환, Hermes 스킬 정적 계약, code-only export/audit을 검증합니다. 실제 Kakao/Telegram 네트워크 호출과 실토큰은 CI에서 실행하지 않습니다.

## License

[MIT](LICENSE) — TeleSticker-Maker contributors.
