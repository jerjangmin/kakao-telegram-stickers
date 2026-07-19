from __future__ import annotations
import os
import shutil
import sqlite3
import sys
import threading
from pathlib import Path
import pytest
SCRIPTS_DIR=Path(__file__).resolve().parents[1]/"skills"/"kakao-telegram-stickers"/"scripts"; sys.path.insert(0,str(SCRIPTS_DIR))
from tele_sticker_maker.cli import main
from tele_sticker_maker.config import PublishConfig
from tele_sticker_maker.models import KakaoStickerItem, ManifestV2, ItemStatus, PreparedSticker, SourceKind, TelegramFormat
from tele_sticker_maker.state import StateStore
from tele_sticker_maker.telegram import TelegramApiError, UploadedSticker, generate_short_name
from tele_sticker_maker.workflow import LEASE_TTL, StickerWorkflow, WorkflowError, _Heartbeat

TOKEN = "123456:" + "abcdefghijklmnopqrstuvwxyzABCDE"

class FakeTelegram:
    def __init__(self): self.calls=[]; self.sets={}
    def get_me(self): self.calls.append("getMe"); return {"id":1,"username":"TestBot"}
    def get_sticker_set(self,name):
        self.calls.append("getStickerSet")
        if name not in self.sets:
            from tele_sticker_maker.telegram import TelegramApiError
            raise TelegramApiError("getStickerSet",400,"Bad Request: STICKERSET_INVALID")
        stickers=[{"file_unique_id":value.file_unique_id} if isinstance(value,UploadedSticker) else value for value in self.sets[name]]
        return {"name":name,"title":name,"stickers":stickers}
    def upload_sticker_file(self, owner, sticker): self.calls.append("upload"); return UploadedSticker("file-1","unique-1",sticker.format)
    def create_new_sticker_set(self, owner, name, title, sticker): self.calls.append("create"); self.sets[name]=[sticker]; return True
    def add_sticker_to_set(self, owner, name, sticker): self.calls.append("add"); self.sets[name].append(sticker); return True

def _prepared(tmp_path, *, config=None, item_count=1):
    config=config or PublishConfig(TOKEN,1,"main","Title","slug","🙂",tmp_path)
    items=tuple(KakaoStickerItem(index,"source",None,None,f"png/sticker_{index:02}.png",f"png/sticker_{index:02}.png",None,"PNG",1,1,1,1,1,False,index,SourceKind.STATIC_PNG,ItemStatus.DOWNLOADED,f"{index:064x}") for index in range(1,item_count+1))
    manifest=ManifestV2("slug","page","api",items)
    def download(_source, output_root):
        root=Path(output_root)/"slug"; (root/"png").mkdir(parents=True)
        for item in items: (root/item.file).write_bytes(b"source")
        return manifest
    def convert(source, root):
        path=root/"telegram"/f"sticker_{source.index:02}.png"; path.parent.mkdir(exist_ok=True); path.write_bytes(b"telegram")
        return PreparedSticker(source,f"telegram/sticker_{source.index:02}.png","ignored",TelegramFormat.STATIC,None,ItemStatus.READY)
    telegram=FakeTelegram(); store=StateStore(config.data_dir/"state.sqlite")
    result=StickerWorkflow(config,store,telegram=telegram,kakao_downloader=download,converter=convert).prepare("source")
    return config,store,telegram,result,config.data_dir/"stickers"/"slug"

def test_relative_data_dir_stays_bound_after_cwd_change(tmp_path, monkeypatch):
    initial_cwd = tmp_path / "initial"; later_cwd = tmp_path / "later"
    initial_cwd.mkdir(); later_cwd.mkdir(); monkeypatch.chdir(initial_cwd)
    config = PublishConfig(TOKEN,1,"main","Title","slug","🙂",Path("relative-data"))
    assert config.data_dir == initial_cwd / "relative-data"

    config,store,telegram,result,_ = _prepared(tmp_path, config=config)
    monkeypatch.chdir(later_cwd)

    assert StickerWorkflow(config,store,telegram=telegram).publish(result.summary["jobId"],confirm=True).exit_code == 0
    assert store.list_items(result.summary["jobId"])[0]["status"] == "published"


def test_prepare_snapshot_is_immutable_and_confirm_gate_has_no_mutation(tmp_path):
    config,store,telegram,result,root=_prepared(tmp_path)
    item=store.list_items(result.summary["jobId"])[0]; snapshot=Path(item["telegram_path"]); before=snapshot.read_bytes()
    (root/"telegram"/"sticker_01.png").write_bytes(b"changed")
    assert snapshot.read_bytes()==before
    assert StickerWorkflow(config,store,telegram=telegram).publish(result.summary["jobId"],confirm=False).exit_code==7
    assert not {"create","add"}.intersection(telegram.calls)
    assert StickerWorkflow(config,store,telegram=telegram).publish(result.summary["jobId"],confirm=True).exit_code==0
    assert telegram.calls.count("create")==1

def test_tampered_snapshot_path_is_failed_without_telegram_mutation(tmp_path):
    config,store,telegram,result,_=_prepared(tmp_path); job=result.summary["jobId"]
    # The DB value points outside the per-job root; validation must reject it.
    with store.transaction(immediate=True) as db:
        db.execute("UPDATE import_items SET telegram_path=? WHERE job_id=?", (str(tmp_path/"outside.png"),job))
    (tmp_path/"outside.png").write_bytes(b"telegram")
    assert StickerWorkflow(config,store,telegram=telegram).publish(job,confirm=True).exit_code==6
    assert not {"create","add"}.intersection(telegram.calls)

def test_publish_rejects_prepared_configuration_mismatch_before_mutation(tmp_path):
    config,store,telegram,result,_=_prepared(tmp_path)
    changed=PublishConfig(config.token,config.owner_user_id,"other",config.pack_title,config.pack_slug,config.emoji,config.data_dir)
    with pytest.raises(WorkflowError): StickerWorkflow(changed,store,telegram=telegram).publish(result.summary["jobId"],confirm=True)
    assert not {"create","add"}.intersection(telegram.calls)


def test_alternate_pack_prepare_binding_can_be_reused_for_publish(tmp_path):
    config=PublishConfig(TOKEN,1,"alternate","Alternate title","alternate_pack","🔥",tmp_path)
    config,store,telegram,result,_=_prepared(tmp_path,config=config)
    binding=result.summary["binding"]
    assert binding == {"botId":1,"botUsername":"TestBot","ownerUserId":1,"packAlias":"alternate","packTitle":"Alternate title","packSlug":"alternate_pack","emoji":"🔥"}
    # The documented publish JSON copies these exact values from binding.
    documented_config=PublishConfig(config.token,binding["ownerUserId"],binding["packAlias"],binding["packTitle"],binding["packSlug"],binding["emoji"],config.data_dir)
    outcome=StickerWorkflow(documented_config,store,telegram=telegram).publish(result.summary["jobId"],confirm=True)
    assert outcome.exit_code == 0 and outcome.summary["published"] == 1

@pytest.mark.parametrize("status,reason", [(ItemStatus.SKIPPED_INVALID, "unsupported source"), (ItemStatus.FAILED, "conversion failed")])
def test_prepare_summary_reports_non_ready_item_issues(tmp_path, status, reason):
    config = PublishConfig(TOKEN, 1, "main", "Title", "slug", "🙂", tmp_path)
    item = KakaoStickerItem(1, "source", None, None, "png/sticker_01.png", "png/sticker_01.png", None, "PNG", 1, 1, 1, 1, 1, False, 1, SourceKind.STATIC_PNG, ItemStatus.DOWNLOADED, "a" * 64)
    manifest = ManifestV2("slug", "page", "api", (item,))
    def download(_source, output_root):
        root = Path(output_root) / "slug" / "png"; root.mkdir(parents=True)
        (root / "sticker_01.png").write_bytes(b"source")
        return manifest

    def convert(source, _root):
        return PreparedSticker(source, None, None, None, None, status, reason)

    result = StickerWorkflow(config, StateStore(config.data_dir / "state.sqlite"), telegram=FakeTelegram(), kakao_downloader=download, converter=convert).prepare("source")

    assert result.summary["issues"] == [{"itemIndex": 1, "status": status.value, "reason": reason}]


def test_atomic_prepared_job_inserts_items_together(tmp_path):
    store=StateStore(tmp_path/"state.sqlite")
    store.create_prepared_job_with_items(owner_user_id=1,source_url="s",resolved_url=None,kakao_slug="slug",target_alias="a",requested_emoji="🙂",summary={"jobId":"j"},job_id="j",items=[{"item_index":1,"source_sha256":"a","source_kind":"static_png","source_path":"x","status":"ready"}])
    assert store.get_job("j") is not None and len(store.list_items("j"))==1

def test_outside_and_symlink_snapshot_paths_never_reach_telegram(tmp_path):
    config, store, telegram, result, _ = _prepared(tmp_path); job=result.summary["jobId"]
    outside=tmp_path/"outside.png"; outside.write_bytes(b"telegram")
    with store.transaction(immediate=True) as db: db.execute("UPDATE import_items SET source_path=?,telegram_path=? WHERE job_id=?", (str(outside),str(outside),job))
    assert StickerWorkflow(config,store,telegram=telegram).publish(job,confirm=True).exit_code==6
    assert not {"create","add"}.intersection(telegram.calls)
    # A symlink inside the expected root is rejected too, not merely resolved.
    config, store, telegram, result, _ = _prepared(tmp_path/"symlink"); job=result.summary["jobId"]
    item=store.list_items(job)[0]; link=Path(item["telegram_path"]).with_name("link.png")
    os.symlink(Path(item["telegram_path"]),link)
    with store.transaction(immediate=True) as db: db.execute("UPDATE import_items SET telegram_path=? WHERE job_id=?", (str(link),job))
    assert StickerWorkflow(config,store,telegram=telegram).publish(job,confirm=True).exit_code==6
    assert not {"create","add"}.intersection(telegram.calls)


@pytest.mark.parametrize("column", ["source_path", "telegram_path"])
def test_snapshot_hash_tamper_is_failed_without_mutation(tmp_path,column):
    config,store,telegram,result,_=_prepared(tmp_path); job=result.summary["jobId"]; item=store.list_items(job)[0]
    Path(item[column]).write_bytes(b"tampered")
    assert StickerWorkflow(config,store,telegram=telegram).publish(job,confirm=True).exit_code==6
    assert store.list_items(job)[0]["status"]=="failed" and not {"create","add"}.intersection(telegram.calls)


def test_two_prepared_jobs_deduplicate_after_series_lease(tmp_path):
    config,store,telegram,first,_=_prepared(tmp_path)
    assert StickerWorkflow(config,store,telegram=telegram).publish(first.summary["jobId"],confirm=True).exit_code==0
    # Reconstruct a second already-prepared snapshot to exercise the race check after
    # lease acquisition (a normal second prepare would skip it earlier).
    duplicate="duplicate-job"; shutil.copytree(tmp_path/"jobs"/first.summary["jobId"],tmp_path/"jobs"/duplicate)
    summary=store.get_job(first.summary["jobId"])["summary_json"]
    import json
    row=store.list_items(first.summary["jobId"])[0]
    copied={k:row[k] for k in ("item_index","source_sha256","source_kind","source_path","telegram_path","telegram_sha256","telegram_format","duration_ms","status","error")}
    copied["source_path"]=copied["source_path"].replace(first.summary["jobId"],duplicate)
    copied["telegram_path"]=copied["telegram_path"].replace(first.summary["jobId"],duplicate)
    copied["status"]="ready"
    store.create_prepared_job_with_items(owner_user_id=1,source_url="s",resolved_url=None,kakao_slug="slug",target_alias="main",requested_emoji="🙂",summary=json.loads(summary),job_id=duplicate,items=[copied])
    before=telegram.calls.count("add")
    result=StickerWorkflow(config,store,telegram=telegram).publish(duplicate,confirm=True)
    assert result.summary["duplicates"]==1 and result.summary["published"]==0 and telegram.calls.count("add")==before


def test_atomic_publish_commit_duplicate_race_preserves_pack_count(tmp_path):
    store=StateStore(tmp_path/"state.sqlite")
    lease=store.acquire_pack_lease(1,"a")
    common=dict(lease=lease,job_id="j",item_index=1,owner_user_id=1,pack_alias="a",pack_name="p_by_bot",source_sha256="a",telegram_sha256="b",sequence=1,title="P",remote_count=8)
    store.create_prepared_job_with_items(owner_user_id=1,source_url="s",resolved_url=None,kakao_slug="s",target_alias="a",requested_emoji="x",summary={},job_id="j",items=[{"item_index":1,"source_sha256":"a","source_kind":"x","source_path":"x","status":"ready"}])
    assert store.commit_published_item(**common)
    store.create_prepared_job_with_items(owner_user_id=1,source_url="s",resolved_url=None,kakao_slug="s",target_alias="a",requested_emoji="x",summary={},job_id="j2",items=[{"item_index":1,"source_sha256":"a","source_kind":"x","source_path":"x","status":"ready"}])
    assert not store.commit_published_item(**dict(common,job_id="j2",remote_count=99))
    assert store.list_items("j2")[0]["status"]=="skipped_duplicate" and store.latest_pack(1,"a")["last_known_count"]==8


def test_heartbeat_stuck_thread_keeps_lease_and_ttl_is_safe():
    entered=threading.Event(); unblock=threading.Event()
    released=[]
    class SlowStore:
        _sleeper=staticmethod(lambda _: None)
        def renew_pack_lease(self, lease, *, ttl): entered.set(); unblock.wait(); return lease
        def release_pack_lease(self, lease): released.append(lease); return True
    heartbeat=_Heartbeat(SlowStore(),object(),0.01)
    heartbeat.start(); entered.wait(1)
    threading.Timer(0.02, unblock.set).start()
    assert LEASE_TTL>=600 and heartbeat.close() is True and released


def test_name_occupied_without_readable_pack_stays_ready_and_is_resumable(tmp_path):
    config,store,telegram,result,_=_prepared(tmp_path); job=result.summary["jobId"]
    def occupied(*_): raise TelegramApiError("createNewStickerSet",400,"NAME_OCCUPIED")
    telegram.create_new_sticker_set=occupied
    outcome=StickerWorkflow(config,store,telegram=telegram).publish(job,confirm=True)
    assert outcome.exit_code==3 and outcome.summary["resumable"] and store.list_items(job)[0]["status"]=="ready"


def test_duplicate_remote_response_preserves_observed_count(tmp_path):
    config,store,telegram,result,_=_prepared(tmp_path); job=result.summary["jobId"]
    pack=result.summary["targetPack"]; telegram.sets[pack]=[{"file_unique_id":"unique-1"}]+[object() for _ in range(4)]
    def duplicate(*_): raise TelegramApiError("addStickerToSet",400,"STICKER_DUPLICATE")
    telegram.add_sticker_to_set=duplicate
    assert StickerWorkflow(config,store,telegram=telegram).publish(job,confirm=True).exit_code==0
    assert store.latest_pack(1,"main")["last_known_count"]==5


def test_capacity_plan_skips_full_second_sequence(tmp_path):
    config,store,telegram,result,_=_prepared(tmp_path)
    one=generate_short_name("slug","TestBot",1); two=generate_short_name("slug","TestBot",2)
    telegram.sets[one]=[object() for _ in range(119)]; telegram.sets[two]=[object() for _ in range(120)]
    planned=StickerWorkflow(config,store,telegram=telegram)._plan_pack_names("TestBot",1,119,2)
    assert planned==[one,generate_short_name("slug","TestBot",3)]


def test_status_and_packs_corrupt_db_return_clean_exit(tmp_path, capsys):
    db=tmp_path/"state.sqlite"; db.write_bytes(b"not sqlite")
    assert main(["status","--job-id","x","--data-dir",str(tmp_path)])==3
    assert "Traceback" not in capsys.readouterr().err
    assert main(["packs","--owner-user-id","1","--data-dir",str(tmp_path)])==3
    assert "Traceback" not in capsys.readouterr().err


def test_heartbeat_start_failure_releases_lease_and_warns(tmp_path, monkeypatch):
    config, store, telegram, prepared, _ = _prepared(tmp_path)
    releases = []
    release = store.release_pack_lease

    def record_release(lease):
        releases.append(lease)
        return release(lease)

    def fail_start(_thread):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(store, "release_pack_lease", record_release)
    monkeypatch.setattr(threading.Thread, "start", fail_start)

    result = StickerWorkflow(config, store, telegram=telegram).publish(prepared.summary["jobId"], confirm=True)

    assert result.exit_code == 7
    assert result.summary["leaseWarning"] == "팩 lease 정리에 실패했습니다"
    assert releases
    lease = store.acquire_pack_lease(config.owner_user_id, config.pack_alias)
    assert store.release_pack_lease(lease)


@pytest.mark.parametrize("failure", ["upload", "identity_precheck"])
def test_upload_or_identity_precheck_500_keeps_item_ready_without_intent(tmp_path, monkeypatch, failure):
    config, store, telegram, prepared, _ = _prepared(tmp_path)
    workflow = StickerWorkflow(config, store, telegram=telegram)
    if failure == "upload":
        def fail_upload(*_):
            raise TelegramApiError("uploadStickerFile", 500, "server error")
        telegram.upload_sticker_file = fail_upload
    else:
        def fail_identity_precheck(*_):
            raise TelegramApiError("getStickerSet", 500, "server error")
        monkeypatch.setattr(workflow, "_pack_has_unique_id", fail_identity_precheck)

    result = workflow.publish(prepared.summary["jobId"], confirm=True)
    item = store.list_items(prepared.summary["jobId"])[0]

    assert result.exit_code == 5 and result.summary["resumable"]
    assert item["status"] == "ready"
    assert item["attempt_operation"] is None and item["pack_name"] is None


@pytest.mark.parametrize("failure", ["upload", "identity_precheck"])
def test_pre_mutation_400_fails_only_its_item_then_continues_without_retry(tmp_path, monkeypatch, failure):
    config, store, telegram, prepared, _ = _prepared(tmp_path, item_count=2)
    workflow = StickerWorkflow(config, store, telegram=telegram)
    calls = 0

    if failure == "upload":
        original_upload = telegram.upload_sticker_file

        def fail_first_upload(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TelegramApiError("uploadStickerFile", 400, "invalid sticker")
            return original_upload(*args)

        telegram.upload_sticker_file = fail_first_upload
    else:
        original_identity_check = workflow._pack_has_unique_id

        def fail_first_identity_check(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TelegramApiError("getStickerSet", 400, "invalid identity probe")
            return original_identity_check(*args)

        monkeypatch.setattr(workflow, "_pack_has_unique_id", fail_first_identity_check)

    result = workflow.publish(prepared.summary["jobId"], confirm=True)
    items = store.list_items(prepared.summary["jobId"])

    assert result.exit_code == 6
    assert [item["status"] for item in items] == ["failed", "published"]
    assert calls == 2
    assert telegram.calls.count("create") == 1


def test_same_slug_prepare_uses_isolated_workspace_and_promotes_only_complete_set(tmp_path):
    config = PublishConfig(TOKEN, 1, "main", "Title", "slug", "🙂", tmp_path)
    store = StateStore(config.data_dir / "state.sqlite")
    telegram = FakeTelegram()
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def downloader(source, output_root):
        raw = source.encode("ascii")
        root = Path(output_root) / "slug" / "png"; root.mkdir(parents=True)
        (root / "sticker_01.png").write_bytes(raw)
        item = KakaoStickerItem(1, source, None, None, "png/sticker_01.png", "png/sticker_01.png", None, "PNG", 1, 1, 1, 1, 1, False, len(raw), SourceKind.STATIC_PNG, ItemStatus.DOWNLOADED, __import__("hashlib").sha256(raw).hexdigest())
        return ManifestV2("slug", "page", "api", (item,))

    def converter(item, root):
        assert barrier.wait(timeout=2) in (0, 1)
        raw = (root / item.file).read_bytes()
        relative = "telegram/sticker_01.png"; destination = root / relative; destination.parent.mkdir(exist_ok=True); destination.write_bytes(b"telegram-" + raw)
        return PreparedSticker(item, relative, __import__("hashlib").sha256(destination.read_bytes()).hexdigest(), TelegramFormat.STATIC, None, ItemStatus.READY)

    def prepare(source):
        try:
            results[source] = StickerWorkflow(config, store, telegram=telegram, kakao_downloader=downloader, converter=converter).prepare(source)
        except Exception as error:  # pragma: no cover - assertions below report failures
            errors.append(error)

    first = threading.Thread(target=prepare, args=("one",)); second = threading.Thread(target=prepare, args=("two",))
    first.start(); second.start(); first.join(timeout=5); second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive() and errors == []
    assert set(results) == {"one", "two"}
    for source, result in results.items():
        item = store.list_items(result.summary["jobId"])[0]
        assert Path(item["source_path"]).read_bytes() == source.encode("ascii")
        assert Path(item["telegram_path"]).read_bytes() == b"telegram-" + source.encode("ascii")
    canonical = config.data_dir / "stickers" / "slug"
    assert (canonical / "json" / "manifest.json").is_file()
    assert (canonical / "png" / "sticker_01.png").read_bytes() in (b"one", b"two")
    assert not (config.data_dir / "work").exists()


def test_completed_job_with_three_release_failures_warns_and_is_not_resumable(tmp_path, monkeypatch):
    config, store, telegram, prepared, _ = _prepared(tmp_path)
    releases = []

    def fail_release(lease):
        releases.append(lease)
        return False

    monkeypatch.setattr(store, "release_pack_lease", fail_release)

    result = StickerWorkflow(config, store, telegram=telegram).publish(prepared.summary["jobId"], confirm=True)

    assert store.get_job(prepared.summary["jobId"])["status"] == "completed"
    assert result.exit_code == 7 and result.summary["resumable"] is False
    assert result.summary["leaseWarning"] == "팩 lease 정리에 실패했습니다"
    assert len(releases) == 3
