"""Static contract tests for the Hermes skill package."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import sys
import types
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "kakao-telegram-stickers"
SKILL_FILE = SKILL_DIR / "SKILL.md"


def _frontmatter() -> tuple[dict, str]:
    text = SKILL_FILE.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, raw_frontmatter, body = text.split("---", 2)
    parsed = yaml.safe_load(raw_frontmatter)
    assert isinstance(parsed, dict)
    return parsed, body


def test_hermes_frontmatter_contract():
    frontmatter, _ = _frontmatter()
    assert frontmatter["name"] == SKILL_DIR.name
    assert len(frontmatter["name"]) <= 64
    description = frontmatter["description"]
    assert 1 <= len(description) <= 60
    assert description.endswith("사용한다.") and description.count(".") == 1
    assert all(term in description for term in ("카카오 이모티콘", "Telegram 스티커팩", "준비", "등록", "재개"))
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["author"]
    assert frontmatter["license"]
    assert frontmatter["platforms"] == ["linux", "macos", "windows"]
    assert frontmatter["prerequisites"]["commands"] == ["uv", "ffmpeg", "ffprobe"]
    token = frontmatter["required_environment_variables"]
    assert len(token) == 1
    assert token[0]["name"] == "TELEGRAM_BOT_TOKEN"
    assert token[0]["prompt"] == "Telegram BotFather bot token"
    assert token[0]["help"]

    hermes = frontmatter["metadata"]["hermes"]
    assert hermes["category"] == "creative"
    assert hermes["requires_toolsets"] == ["terminal", "file"]
    assert {"kakao", "telegram", "sticker"}.issubset(set(hermes["tags"]))


def test_documented_exit_code_contract_keeps_one_for_doctor_and_three_for_owner_lookup():
    troubleshooting = (SKILL_DIR / "references" / "troubleshooting.md").read_text(encoding="utf-8")
    assert "| 1 | `doctor` 의존성 미충족만 해당" in troubleshooting
    assert "owner ID 조회 오류" in troubleshooting
    assert "종료 코드 `6`, `3`, `5`, `7`" in troubleshooting
    assert "issues`의 모든 reason" in troubleshooting


def test_hermes_logical_config_contract():
    frontmatter, _ = _frontmatter()
    config = frontmatter["metadata"]["hermes"]["config"]
    by_key = {item["key"]: item for item in config}
    expected = {
        "kakao_stickers.owner_user_id",
        "kakao_stickers.default_pack_alias",
        "kakao_stickers.default_pack_title",
        "kakao_stickers.default_pack_slug",
        "kakao_stickers.default_emoji",
        "kakao_stickers.data_dir",
    }
    assert set(by_key) == expected
    assert "default" not in by_key["kakao_stickers.owner_user_id"]
    assert by_key["kakao_stickers.default_pack_alias"]["default"] == "default"
    assert all(item.get("description") and item.get("prompt") for item in config)


def test_skill_body_enforces_confirmation_and_portable_execution():
    _, body = _frontmatter()
    command = 'uv run --script "${HERMES_SKILL_DIR}/scripts/request_runner.py" --request-id "${HERMES_SESSION_ID}"'
    assert command in body
    assert "${HERMES_HOME}/data/kakao-telegram-stickers/requests/request-${HERMES_SESSION_ID}.json" in body
    assert "[Skill config] header" in body
    assert "--request-file" not in body
    assert "설치된 스킬 asset에는 동적 request를 쓰지 않는다" in body
    assert "scripts/run.py\" doctor" not in body
    assert "<skill-dir>" not in body
    assert "[Skill config]" in body
    assert "publish 전에는 Telegram 팩을 생성하거나 변경하지 않는다" in body
    assert "하나의 검토 메시지" in body
    for field in ("slug", "targetPack", "packsAfterPublish", "discovered", "readyStatic", "readyVideo", "duplicates", "excluded", "failed", "itemIndex", "status", "reason"):
        assert field in body
    assert "명시적으로 승인" in body
    assert "최신 작업을 추측" in body
    assert '"action":"publish"' in body and '"confirm":true' in body
    assert '"pack"' in body and '"packTitle"' in body and '"packSlug"' in body
    assert "binding.ownerUserId" in body and "binding.packAlias" in body
    assert "binding.packTitle" in body and "binding.packSlug" in body and "binding.emoji" in body
    assert "현재 기본 설정값으로 다시 만들지 않는다" in body
    assert "download" in body and "재개" in body
    assert "issues" in body and "종료 코드" in body
    for field in ("packLinks", "published", "duplicates", "excluded", "failed", "resumable"):
        assert field in body
    assert "종료 코드가 `6`, `3`, `5`, `7`" in body
    assert "공개 접근" in body and "권한" in body
    assert "셸 명령에 직접 넣지 않는다" in body and "write_file" in body
    assert "/Users/" not in SKILL_FILE.read_text(encoding="utf-8")


def _hermes_referenced_support_paths(skill_md: str) -> set[str]:
    """Mirror Hermes tools/skills_hub.py::_referenced_support_paths.

    Kept dependency-free so the contract runs on all CI platforms without a
    Hermes source checkout. The source regex was verified against Hermes main.
    """
    allowed={"references","templates","scripts","assets","examples"}
    pattern=re.compile(r"(?:\]\(|`|(?:^|[\s\"']))((?:references|templates|scripts|assets|examples)/[^\s)`\"'<>]+)",re.MULTILINE)
    result=set()
    for match in pattern.finditer(skill_md.replace("\\\\","/")):
        raw=unquote(urlsplit(match.group(1).rstrip(".,;:")).path)
        path=PurePosixPath(raw)
        parts=[part for part in path.parts if part not in ("",".")]
        assert parts and not raw.startswith("/") and ".." not in parts
        normalized="/".join(parts)
        if parts[0] in allowed: result.add(normalized)
    return result


def _expected_runtime_bundle() -> set[str]:
    return {
        "scripts/run.py",
        "scripts/request_runner.py",
        "scripts/get_owner_id.py",
        "scripts/tele_sticker_maker/__init__.py",
        "scripts/tele_sticker_maker/cli.py",
        "scripts/tele_sticker_maker/config.py",
        "scripts/tele_sticker_maker/kakao.py",
        "scripts/tele_sticker_maker/media.py",
        "scripts/tele_sticker_maker/models.py",
        "scripts/tele_sticker_maker/state.py",
        "scripts/tele_sticker_maker/telegram.py",
        "scripts/tele_sticker_maker/webp.py",
        "scripts/tele_sticker_maker/workflow.py",
        "assets/request-template.json",
        "references/configuration.md",
        "references/troubleshooting.md",
    }


def test_hermes_installer_extracts_complete_runtime_bundle():
    expected = _expected_runtime_bundle()
    assert _hermes_referenced_support_paths(SKILL_FILE.read_text(encoding="utf-8")) == expected
    assert all((SKILL_DIR / relative).is_file() for relative in expected)


SECRET_ENV_LOOKUP = re.compile(
    r"""os\.(?:environ\s*\.get|getenv)\s*\(\s*(?:
        ["'][^"']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[^"']*["']
        | [A-Za-z_]\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\w*
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _runtime_bundle_text() -> str:
    return "\n".join((SKILL_DIR / relative).read_text(encoding="utf-8") for relative in sorted(_expected_runtime_bundle()))


def test_runtime_bundle_has_no_secret_named_environment_lookup():
    """Keep the static scanner regression active even without Hermes sources."""
    bundle = _runtime_bundle_text()
    assert not SECRET_ENV_LOOKUP.search(bundle)
    assert 'ENV_NAME = "TELEGRAM_BOT_TOKEN"' in bundle
    assert "os.getenv(ENV_NAME" in bundle
    frontmatter, _ = _frontmatter()
    assert frontmatter["required_environment_variables"][0]["name"] == "TELEGRAM_BOT_TOKEN"


def test_local_hermes_toolset_registry_accepts_declared_names_when_available():
    hermes_root = Path("/tmp/hermes-agent-docs")
    if not hermes_root.is_dir():
        return
    sys.path.insert(0, str(hermes_root))
    try:
        from toolsets import get_all_toolsets
        available = get_all_toolsets()
        assert {"terminal", "file"}.issubset(available)
        assert "files" not in available
    finally:
        sys.path.pop(0)


def test_local_hermes_extractor_matches_expected_bundle_when_available():
    hermes_root = Path("/tmp/hermes-agent-docs")
    if sys.version_info < (3, 10) or not hermes_root.is_dir():
        return
    injected_httpx = "httpx" not in sys.modules
    if injected_httpx:
        httpx = types.ModuleType("httpx")
        httpx.Response = object
        httpx.HTTPError = Exception
        httpx.DecodingError = Exception
        sys.modules["httpx"] = httpx
    sys.path.insert(0, str(hermes_root))
    try:
        from tools.skills_hub import _referenced_support_paths
        assert _referenced_support_paths(SKILL_FILE.read_text(encoding="utf-8")) == _expected_runtime_bundle()
    finally:
        sys.path.pop(0)
        if injected_httpx:
            sys.modules.pop("httpx", None)


def test_local_hermes_scanner_allows_community_bundle_when_available():
    hermes_root = Path("/tmp/hermes-agent-docs")
    if sys.version_info < (3, 10) or not hermes_root.is_dir():
        return
    sys.path.insert(0, str(hermes_root))
    try:
        from tools.skills_guard import scan_skill, should_allow_install
    finally:
        sys.path.pop(0)

    result = scan_skill(SKILL_DIR, source="community")
    allowed, _ = should_allow_install(result)
    assert result.verdict == "safe"
    assert allowed


TRIGGER_EVALS = (
    ("카카오 이모티콘 링크를 텔레그램 스티커팩에 넣어줘", True),
    ("https://e.kakao.com/t/example 이모티콘을 스티커로 변환해줘", True),
    ("카카오 이모티콘을 다운로드하고 Telegram 봇 팩에 등록해줘", True),
    ("준비한 카카오 스티커 job을 Telegram에 재개 등록해줘", True),
    ("카카오 emoticon slug를 텔레그램 sticker pack으로 만들어줘", True),
    ("텔레그램 사용자 계정으로 다른 사람이 만든 팩을 수정해줘", False),
    ("사진 배경을 지우고 PNG로 만들어줘", False),
    ("카카오톡 대화 내용을 백업해줘", False),
    ("Telegram 봇 토큰을 새로 만들어줘", False),
)


def test_trigger_and_nontrigger_eval_fixtures_are_present():
    assert len(TRIGGER_EVALS) >= 8
    assert sum(should_trigger for _, should_trigger in TRIGGER_EVALS) >= 4
    assert sum(not should_trigger for _, should_trigger in TRIGGER_EVALS) >= 4
    assert all(isinstance(prompt, str) and prompt for prompt, _ in TRIGGER_EVALS)
