# 문제 해결

모든 동적 값은 Hermes `write_file` structured tool로 **[Skill config] header에 표시된 Hermes home** 아래 `${HERMES_HOME}/data/kakao-telegram-stickers/requests/request-${HERMES_SESSION_ID}.json`에 JSON으로 작성하고, SKILL.md의 고정 request runner 명령만 실행한다. `HERMES_SESSION_ID`는 Hermes가 생성한 system token만 사용하며 설치된 스킬 asset에는 동적 request를 쓰지 않는다. 셸 보간, `cat`, heredoc를 사용하지 않는다.

## 종료 코드와 대응

| 코드 | 의미 | 대응 |
| --- | --- | --- |
| 0 | 성공 | JSON 결과를 확인한다. |
| 1 | `doctor` 의존성 미충족만 해당 | `uv`, `ffmpeg`, `ffprobe`, VP9 encoder를 설치한다. |
| 2 | 설정 또는 request 오류 | JSON action/key/type, owner ID, 팩 값, emoji, dataDir을 확인한다. |
| 3 | 다운로드·상태 저장·준비·owner ID 조회 오류 | 네트워크와 dataDir 권한을 확인한다. owner ID fallback은 marker가 정확히 한 명에게만 일치하는지 확인한다. |
| 4 | 미디어 변환 오류 | 해당 항목의 원본 형식과 변환 제약을 확인한다. |
| 5 | Telegram API 오류 | 봇 토큰, owner ID, 봇 username, 연결 상태를 확인한다. |
| 6 | 일부 항목 실패 | `issues`를 검토한다. 성공 항목은 다시 등록하지 않는다. |
| 7 | 확인 누락·상태 오류·lease 충돌 | 정확한 job ID와 명시적 승인을 확인하고 다른 publish가 끝난 뒤 재시도한다. |

## 현재 상태를 검토한 뒤 재개

`status` request를 작성해 실행한다.

```json
{"action":"status","jobId":"<jobId>","dataDir":"<kakao_stickers.data_dir>"}
```

출력의 `prepareSummary.binding`(최상위 `binding`에도 같은 값), `items` 전체 상태, `counts`, `issues`의 모든 reason, `pending`을 사용자에게 보여준다. 같은 `jobId`를 다시 publish할지 명시적으로 승인받은 뒤에만 `confirm: true` publish request를 작성한다. publish의 `ownerUserId`, `pack`, `packTitle`, `packSlug`, `emoji`는 현재 기본 설정이 아니라 이 `binding` 값을 그대로 사용한다. 최신 작업을 추측하지 않는다.

## 자주 발생하는 상황

- **토큰 누락:** secure prompt 또는 안전한 환경 파일을 확인한다. 토큰 값을 출력·전송하지 않는다.
- **owner ID 불일치:** 인증된 sender metadata를 먼저 확인한다. fallback marker는 gateway를 중지한 뒤 한 명만 전송하도록 하고 결과의 username/ID를 수동 확인한다.
- **팩이 가득 참:** 120개면 다음 시퀀스 팩으로 자동 rollover된다. prepare의 `packsAfterPublish`와 publish의 `packLinks`를 확인한다.
- **동일 slug prepare:** 각 prepare는 격리 workspace에서 완료된 세트만 canonical `stickers/<slug>/`로 원자적으로 승격한다. publish는 각 job snapshot을 사용한다.
- **publish가 부분 실패하거나 재개 필요:** 결과의 `packLinks` 전체와 `published`, `duplicates`, `excluded`, `failed`, `resumable`을 확인한다. 종료 코드 `6`, `3`, `5`, `7`이면 먼저 `status`를 다시 조회해 `issues`의 모든 reason과 다음 행동(원인 해결, 네트워크·토큰 확인, 승인, lease 해제 대기)을 사용자에게 안내한 뒤 재개한다.
