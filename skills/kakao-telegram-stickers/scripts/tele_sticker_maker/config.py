"""Validated runtime configuration for the prepare/publish commands."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ConfigError(ValueError):
    pass


ENV_NAME = "TELEGRAM_BOT_TOKEN"


def default_data_dir() -> Path:
    hermes_home = os.environ.get("HERMES_HOME")
    return (Path(hermes_home).expanduser() / "data" / "kakao-telegram-stickers") if hermes_home else Path.home() / ".tele-sticker-maker"


@dataclass(frozen=True)
class PublishConfig:
    token: str
    owner_user_id: int
    pack_alias: str
    pack_title: str
    pack_slug: str
    emoji: str
    data_dir: Path
    layout_mode: str = "auto"

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser().resolve())
        if self.layout_mode not in {"auto", "mini", "standard"}:
            raise ConfigError("layout_mode는 auto, mini, standard 중 하나여야 합니다")

    @classmethod
    def from_values(cls, *, owner_user_id: object, pack: object, pack_title: object,
                    pack_slug: object, emoji: object, data_dir: Optional[object] = None,
                    token: Optional[str] = None, layout_mode: object = "auto") -> "PublishConfig":
        actual_token = token if token is not None else os.getenv(ENV_NAME, "")
        if not isinstance(actual_token, str) or not actual_token.strip():
            raise ConfigError("TELEGRAM_BOT_TOKEN이 필요합니다")
        if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool) or owner_user_id <= 0:
            raise ConfigError("owner_user_id는 양의 정수여야 합니다")
        values = {"pack": pack, "pack_title": pack_title, "pack_slug": pack_slug, "emoji": emoji}
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise ConfigError("팩 별칭, 제목, slug, emoji는 비어 있을 수 없습니다")
        if not isinstance(layout_mode, str) or layout_mode not in {"auto", "mini", "standard"}:
            raise ConfigError("layout_mode는 auto, mini, standard 중 하나여야 합니다")
        return cls(actual_token.strip(), owner_user_id, str(pack).strip(), str(pack_title).strip(),
                   str(pack_slug).strip(), str(emoji).strip(), Path(data_dir).expanduser() if data_dir else default_data_dir(), layout_mode)
