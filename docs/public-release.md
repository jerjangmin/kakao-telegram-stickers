# Code-only 공개 배포 절차

> **현재 비공개 자산 저장소를 public으로 전환하거나 이 Git 이력을 복사하지 마세요.** 현재 이력에는 추적 파일 2,033개(원본 이모티콘·archive 포함)가 있습니다. 삭제 커밋으로 숨겨도 Git object/history에서 복구될 수 있습니다.

공개 저장소에는 코드, 문서, synthetic 테스트만 넣습니다. 실제 Kakao 원본, manifest, 작업 데이터, SQLite, 토큰, 기존 history는 절대 넣지 않습니다.

CI는 `jerjangmin/TeleSticker-Maker`만 비공개 자산 source workspace로 취급하며 `.code-only-release` marker가 **없어야** 합니다. 새 public repository는 이 이름을 사용하지 않고 exporter가 생성한 marker를 반드시 포함해야 합니다. marker가 누락되거나 source workspace에 추가되면 CI가 실패합니다.

## 1. 깨끗한 export 만들기

비공개 작업 저장소에서, 작업 저장소 바깥의 비어 있는 경로로 export합니다.

```bash
uv run --no-project python scripts/export_public_tree.py ../tele-sticker-maker-public
uv run --no-project python scripts/audit_public_tree.py ../tele-sticker-maker-public
```

exporter는 명시적 allowlist만 복사하며 대상이 비어 있지 않으면 삭제하지 않고 실패합니다. `README.md`, `LICENSE`, CLI/runtime, skill, tests, CI, 이 문서와 export/audit 스크립트 및 `.code-only-release` marker만 포함합니다.

## 2. 별도 Git 이력 만들기

새 디렉터리에서만 Git을 초기화합니다. 기존 private repository의 `.git`를 복사하거나 remote를 재사용하지 않습니다.

```bash
cd ../tele-sticker-maker-public
git init -b main
git add .code-only-release README.md LICENSE download_stickers.sh pyproject.toml uv.lock skills tests .github docs scripts
git commit -m "Initial code-only public release"
```

기존 저장소에서 공개해야 할 코드가 추가되면 private history를 merge/cherry-pick하지 말고, 이 export를 다시 만들어 새 clean commit으로 반영합니다. orphan 방식은 별도 clone/worktree에서만 선택적으로 사용합니다. 기존 `main`을 orphan으로 바꾸지 말고 `git switch --orphan public-main`으로 새 branch를 만든 뒤 export 내용만 commit하고, **새 public remote에만** `git push <new-public-remote> public-main:main`을 실행합니다. private remote·old refs·기존 `.git` objects는 절대 public remote에 push하지 마세요. 별도 새 export repository가 더 안전합니다.

## 3. 공개 직전 감사

새 저장소에서 다음을 모두 수행합니다.

```bash
uv run --no-project python scripts/audit_public_tree.py .
git rev-list --objects --all
rg -n -i 'telegram_bot_token\s*[:=]|[0-9]{6,12}:[A-Za-z0-9_-]{20,}' . --glob '!.git/**'
uv sync --locked
uv run --python 3.12 pytest -q
```

`audit_public_tree.py`는 code-only marker, allowlist 밖 파일, 허용되지 않은 파일 형식, symlink, 중첩 `.git`, media/state 확장자와 magic byte, `.env`, Telegram·AWS·GitHub·PEM·일반 named credential 패턴, 2 MiB 초과 파일, `stickers`/`archived`/`jobs`/`work`/`dist` 디렉터리를 검사합니다. Git이 있으면 모든 reachable history object path와 2 MiB 이하 blob 내용도 검사합니다. 감사 결과가 `{"ok": true}`가 아니면 공개하지 마세요.

## 4. 새 GitHub repository를 private staging으로 만들기

`main`의 clean commit과 local audit이 성공한 뒤에만, export directory에서 **새 repository를 기본 private 상태로** 만듭니다. 기존 private 자산 repository의 remote, branch, tag, release artifact를 재사용하지 않습니다.

```bash
gh repo create <owner>/<repo> --private --source=. --remote=origin --push
gh repo edit <owner>/<repo> --delete-branch-on-merge
```

push 뒤 GitHub의 private staging에서 파일 트리와 commit history를 다시 확인하고, GitHub secret scanning 결과도 확인합니다. `git rev-list --objects --all`과 `uv run --no-project python scripts/audit_public_tree.py .`가 clean이어야 합니다.

## 5. public 전환 후 Hermes smoke test

private staging 검토가 모두 끝난 경우에만 public으로 전환합니다. 그 뒤 즉시 실제 public remote를 대상으로 Hermes installer smoke test를 실행합니다.

```bash
gh repo edit <owner>/<repo> --visibility public --accept-visibility-change-consequences
hermes skills inspect <owner>/<repo>/skills/kakao-telegram-stickers
hermes skills install <owner>/<repo>/skills/kakao-telegram-stickers
```

실제 bot token, Kakao 다운로드, Telegram publish는 이 smoke test에서 실행하지 않습니다. inspect/install 또는 공개 후 검사에 실패하면 public 노출을 중단하고 즉시 다시 private으로 전환한 뒤 수정합니다.

```bash
gh repo edit <owner>/<repo> --visibility private --accept-visibility-change-consequences
```
