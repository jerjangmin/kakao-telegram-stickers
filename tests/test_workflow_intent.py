from __future__ import annotations
import sys
from pathlib import Path
SCRIPTS_DIR=Path(__file__).resolve().parents[1]/"skills"/"kakao-telegram-stickers"/"scripts"; sys.path.insert(0,str(SCRIPTS_DIR))
from tele_sticker_maker.telegram import TelegramApiError
from tele_sticker_maker.workflow import StickerWorkflow
from test_workflow import _prepared


def test_add_response_loss_reconciles_count_without_resend(tmp_path):
    config, store, telegram, prepared, _ = _prepared(tmp_path); pack = prepared.summary["targetPack"]
    telegram.sets[pack] = [object()]
    calls = []
    def lost_add(_owner, _name, sticker):
        calls.append(1); telegram.sets[pack].append(sticker)
        raise TelegramApiError("addStickerToSet", None, "network lost")
    telegram.add_sticker_to_set = lost_add
    result = StickerWorkflow(config, store, telegram=telegram).publish(prepared.summary["jobId"], confirm=True)
    assert result.exit_code == 0 and len(calls) == 1
    assert store.list_items(prepared.summary["jobId"])[0]["status"] == "published"
    assert store.latest_pack(1, "main")["last_known_count"] == 2


def test_create_response_loss_reconciles_without_followup_add(tmp_path):
    config, store, telegram, prepared, _ = _prepared(tmp_path); calls = []
    def lost_create(_owner, name, _title, sticker):
        calls.append("create"); telegram.sets[name] = [sticker]
        raise TelegramApiError("createNewStickerSet", None, "network lost")
    telegram.create_new_sticker_set = lost_create
    def unexpected_add(*_): calls.append("add"); raise AssertionError("create reconciliation must not add")
    telegram.add_sticker_to_set = unexpected_add
    result = StickerWorkflow(config, store, telegram=telegram).publish(prepared.summary["jobId"], confirm=True)
    assert result.exit_code == 0 and calls == ["create"]
    assert store.list_items(prepared.summary["jobId"])[0]["status"] == "published"
