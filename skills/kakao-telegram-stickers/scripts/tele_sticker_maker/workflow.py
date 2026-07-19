"""Immutable preparation and resumable, lease-protected Telegram publishing."""
from __future__ import annotations
import hashlib, json, os, shutil, threading, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from .config import PublishConfig
from .media import _atomic_write, download_set, prepare_telegram_item, promote_set
from .models import ItemStatus, PreparedSticker
from .state import JobStateError, LeaseError, StateError, StateStore
from .telegram import InputSticker, TelegramApiError, TelegramClient, UploadedSticker, generate_short_name

PACK_CAPACITY = 120
# Telegram's maximum retry envelope is below 240 seconds; leave two full envelopes.
LEASE_TTL = 600.0
class WorkflowError(RuntimeError): pass
@dataclass(frozen=True)
class WorkflowResult: summary: dict[str, Any]; exit_code: int = 0

def _set_root(c: PublishConfig, slug: str) -> Path: return c.data_dir / "stickers" / slug
def _sha(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()
def _atomic_manifest(root: Path, manifest: Any, prepared: list[PreparedSticker]) -> None:
    found={p.source.index:p for p in prepared}; body=manifest.to_dict()
    body["items"]=[found.get(item.index,item).to_manifest_dict() for item in manifest.items]
    _atomic_write(root/"json"/"manifest.json",(json.dumps(body,ensure_ascii=False,indent=2)+"\n").encode())
def _copy_snapshot(source: Path, destination: Path) -> None:
    """Copy rather than link so later in-place conversion cannot mutate a job snapshot."""
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,destination)

class _Heartbeat:
    def __init__(self, store: StateStore, lease: Any, ttl: float) -> None:
        self.store,self.lease,self.ttl=store,lease,ttl; self.failure: Optional[BaseException]=None; self.stop=threading.Event()
        self.thread=threading.Thread(target=self._run,daemon=False)
    def start(self) -> None:
        try: self.thread.start()
        except RuntimeError as error:
            self.failure=error
            raise WorkflowError("팩 heartbeat를 시작하지 못했습니다") from None
    def renew(self) -> None:
        if self.failure: raise WorkflowError("팩 lease 갱신에 실패했습니다")
        try: self.lease=self.store.renew_pack_lease(self.lease,ttl=self.ttl)
        except (StateError,LeaseError) as error: self.failure=error; raise WorkflowError("팩 lease 갱신에 실패했습니다") from None
    def _run(self) -> None:
        while not self.stop.wait(self.ttl/3):
            try: self.lease=self.store.renew_pack_lease(self.lease,ttl=self.ttl)
            except (StateError,LeaseError) as error: self.failure=error; return
    def _release(self) -> None:
        last_error: Optional[BaseException] = None
        for attempt in range(3):
            try:
                if not self.store.release_pack_lease(self.lease):
                    last_error = LeaseError("팩 lease 소유권을 확인할 수 없습니다")
                else:
                    return
            except StateError as error:
                last_error = error
            if attempt < 2:
                self.store._sleeper(0.05 * (attempt + 1))
        self.failure = self.failure or last_error

    def close(self) -> bool:
        # StateStore calls have bounded SQLite timeouts. Wait synchronously so a
        # non-daemon renewal can never outlive process-level lease cleanup.
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join()
        self._release()
        return self.failure is None

class StickerWorkflow:
    def __init__(self, config: PublishConfig, store: StateStore, *, telegram: Optional[Any]=None, kakao_downloader: Callable[...,Any]=download_set, converter: Callable[...,PreparedSticker]=prepare_telegram_item) -> None:
        self.config,self.store=config,store; self.telegram=telegram or TelegramClient(config.token); self._download,self._convert=kakao_downloader,converter

    def prepare(self, source: str) -> WorkflowResult:
        job_id=uuid.uuid4().hex; staging=self.config.data_dir/"jobs"/(job_id+".staging"); final=self.config.data_dir/"jobs"/job_id; work_dir=self.config.data_dir/"work"/job_id
        try:
            me=self.telegram.get_me(); workspace_root=work_dir/"stickers"; manifest=self._download(source,workspace_root); root=workspace_root/manifest.slug
            latest=self.store.latest_pack(self.config.owner_user_id,self.config.pack_alias)
            if latest and latest["telegram_name"] != generate_short_name(self.config.pack_slug,me["username"],int(latest["sequence"])):
                raise WorkflowError("기존 pack alias의 Telegram 이름이 현재 slug/bot 설정과 일치하지 않습니다")
            target,count,sequence=self._available_pack(me["username"],reserve=False)
            prepared=[]; records=[]
            for item in manifest.items:
                registration = self.store.get_registration(self.config.owner_user_id,self.config.pack_alias,item.source_sha256) if item.source_sha256 else None
                if registration:
                    prepared_item=PreparedSticker(item,None,None,None,None,ItemStatus.SKIPPED_DUPLICATE)
                else: prepared_item=self._convert(item,root)
                prepared.append(prepared_item)
                source_dst=staging/"items"/("source_"+Path(item.file).name); _copy_snapshot(root/item.file,source_dst)
                source_final=final/"items"/source_dst.name; telegram_path=telegram_sha=None
                if prepared_item.status is ItemStatus.READY and prepared_item.telegram_path:
                    tg_src=root/prepared_item.telegram_path; tg_dst=staging/"items"/("telegram_"+Path(prepared_item.telegram_path).name)
                    _copy_snapshot(tg_src,tg_dst); telegram_path=str(final/"items"/tg_dst.name); telegram_sha=_sha(tg_dst)
                records.append({"item_index":item.index,"source_sha256":_sha(source_dst),"source_kind":item.source_kind.value,"source_path":str(source_final),"telegram_path":telegram_path,"telegram_sha256":telegram_sha,"telegram_format":prepared_item.telegram_format.value if prepared_item.telegram_format else None,"duration_ms":prepared_item.duration_ms,"status":prepared_item.status.value,"pack_name":registration["pack_name"] if registration else None,"error":prepared_item.error})
            _atomic_manifest(root,manifest,prepared)
            promote_set(root, self.config.data_dir / "stickers")
            ready=[p for p in prepared if p.status is ItemStatus.READY]
            issues = [
                {
                    "itemIndex": prepared_item.source.index,
                    "status": prepared_item.status.value,
                    "reason": prepared_item.error or "같은 원본이 이 팩 시리즈에 이미 등록되어 있습니다",
                }
                for prepared_item in prepared
                if prepared_item.status is not ItemStatus.READY
            ]
            summary={"jobId":job_id,"slug":manifest.slug,"targetPack":target,"discovered":len(prepared),"readyStatic":sum(p.telegram_format and p.telegram_format.value=="static" for p in ready),"readyVideo":sum(p.telegram_format and p.telegram_format.value=="video" for p in ready),"duplicates":sum(p.status is ItemStatus.SKIPPED_DUPLICATE for p in prepared),"excluded":sum(p.status is ItemStatus.SKIPPED_INVALID for p in prepared),"failed":sum(p.status is ItemStatus.FAILED for p in prepared),"issues":issues,"packsAfterPublish":self._plan_pack_names(me["username"],sequence,count,len(ready)),"requiresConfirmation":True,"binding":{"botId":me.get("id"),"botUsername":me["username"],"ownerUserId":self.config.owner_user_id,"packAlias":self.config.pack_alias,"packTitle":self.config.pack_title,"packSlug":self.config.pack_slug,"emoji":self.config.emoji}}
            staging.parent.mkdir(parents=True,exist_ok=True); os.replace(staging,final)
            self.store.create_prepared_job_with_items(owner_user_id=self.config.owner_user_id,source_url=source,kakao_slug=manifest.slug,target_alias=self.config.pack_alias,requested_emoji=self.config.emoji,resolved_url=manifest.source_page,summary=summary,job_id=job_id,items=records,pack_reservations=[{"sequence":sequence,"telegram_name":target,"title":self._title(sequence),"last_known_count":count or 0}])
            return WorkflowResult(summary)
        except (OSError,StateError) as error:
            shutil.rmtree(staging,ignore_errors=True); shutil.rmtree(final,ignore_errors=True)
            raise WorkflowError("준비 작업을 안전하게 저장하지 못했습니다") from None
        except BaseException:
            shutil.rmtree(staging,ignore_errors=True); shutil.rmtree(final,ignore_errors=True)
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            try:
                work_dir.parent.rmdir()
            except OSError:
                pass

    def publish(self, job_id: str, *, confirm: bool) -> WorkflowResult:
        if not confirm: return WorkflowResult({"jobId":job_id,"requiresConfirmation":True},7)
        job=self.store.get_job(job_id)
        if not job: raise JobStateError("작업을 찾을 수 없습니다")
        if job["status"]=="completed": raise JobStateError("완료된 작업은 다시 등록할 수 없습니다")
        summary=json.loads(job["summary_json"]); self._verify_binding(summary)
        if job["status"]=="prepared": self.store.begin_publish(job_id)
        elif job["status"]!="publishing": raise JobStateError("등록할 수 없는 작업 상태입니다")
        try: lease=self.store.acquire_pack_lease(self.config.owner_user_id,self.config.pack_alias,ttl=LEASE_TTL)
        except LeaseError: return self._result(job_id,resumable=True,exit_code=7)
        except StateError: return self._result(job_id,resumable=True,exit_code=3)
        beat=_Heartbeat(self.store,lease,LEASE_TTL)
        try:
            beat.start()
            beat.renew(); me=self.telegram.get_me(); self._check_bot(summary,me)
            for item in self.store.resume_job(job_id):
                if item["status"] == "publishing_item":
                    if not self._reconcile_attempt(job_id,item,beat): return self._result(job_id,resumable=True,exit_code=3)
                    continue
                if item["status"]!="ready": continue
                # Recheck after acquiring the series lease: another completed job may
                # have registered this source while this job was waiting.
                existing = self.store.get_registration(self.config.owner_user_id, self.config.pack_alias, item["source_sha256"])
                if existing:
                    self.store.update_item_status_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"],status="skipped_duplicate",expected_status="ready",pack_name=existing["pack_name"]); continue
                if not self._valid_snapshot(job_id, item):
                    self.store.update_item_status_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"],status="failed",expected_status="ready",error="준비된 파일 검증에 실패했습니다"); continue
                beat.renew(); pack,count,sequence=self._available_pack(me["username"],beat)
                try:
                    source_sticker=InputSticker(Path(item["telegram_path"]),(job["requested_emoji"],),item["telegram_format"])
                    if item.get("uploaded_file_id") and item.get("uploaded_file_unique_id"):
                        sticker=UploadedSticker(item["uploaded_file_id"],item["uploaded_file_unique_id"],item["telegram_format"],(job["requested_emoji"],))
                    else:
                        beat.renew(); sticker=self.telegram.upload_sticker_file(self.config.owner_user_id,source_sticker)
                        self.store.persist_uploaded_sticker(lease=beat.lease,job_id=job_id,item_index=item["item_index"],file_id=sticker.file_id,file_unique_id=sticker.file_unique_id)
                        item=next(row for row in self.store.list_items(job_id) if row["item_index"]==item["item_index"])
                    if self._pack_has_unique_id(pack,sticker.file_unique_id,beat):
                        self.store.commit_duplicate_item(lease=beat.lease,job_id=job_id,item_index=item["item_index"],owner_user_id=self.config.owner_user_id,pack_alias=self.config.pack_alias,pack_name=pack,source_sha256=item["source_sha256"],telegram_sha256=item["telegram_sha256"]); continue
                    operation = "create" if count is None else "add"
                    self.store.mark_item_attempt_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"],pack_name=pack,operation=operation,count_before=-1 if count is None else count)
                    beat.renew()
                    if count is None: self.telegram.create_new_sticker_set(self.config.owner_user_id,pack,self._title(sequence),sticker)
                    else: self.telegram.add_sticker_to_set(self.config.owner_user_id,pack,sticker)
                    self._mark_published(job_id,item,pack,count or 0,sequence,lease=beat.lease)
                except TelegramApiError as error:
                    attempted=next(row for row in self.store.list_items(job_id) if row["item_index"] == item["item_index"])
                    # Upload and pre-mutation probes happen while the item is still
                    # ready. Their deterministic 400s are per-item failures, not
                    # mutation reconciliation signals.
                    if attempted["status"] == "ready":
                        if error.code == 400:
                            self.store.update_item_status_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"],status="failed",expected_status="ready",error=error.description)
                            continue
                        return self._result(job_id,resumable=True,exit_code=5)
                    if self._is_exact_duplicate(error):
                        if self._pack_has_unique_id(pack,sticker.file_unique_id,beat):
                            self.store.commit_duplicate_item(lease=beat.lease,job_id=job_id,item_index=item["item_index"],owner_user_id=self.config.owner_user_id,pack_alias=self.config.pack_alias,pack_name=pack,source_sha256=item["source_sha256"],telegram_sha256=item["telegram_sha256"])
                        else: return self._result(job_id,resumable=True,exit_code=5)
                    elif count is None and error.code == 400 and "NAME_OCCUPIED" in error.description.upper():
                        # The short name already exists. Adopt it only after checking
                        # the exact uploaded sticker identity, never a count heuristic.
                        remote = self._remote_pack(pack,beat)
                        if remote is None:
                            self.store.reset_item_attempt_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"])
                            return self._result(job_id,resumable=True,exit_code=3)
                        observed=len(remote["stickers"])
                        if self._has_unique_id(remote,sticker.file_unique_id):
                            self.store.commit_duplicate_item(lease=beat.lease,job_id=job_id,item_index=item["item_index"],owner_user_id=self.config.owner_user_id,pack_alias=self.config.pack_alias,pack_name=pack,source_sha256=item["source_sha256"],telegram_sha256=item["telegram_sha256"])
                            continue
                        self.store.reset_item_attempt_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"])
                        self.store.mark_item_attempt_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"],pack_name=pack,operation="add",count_before=observed)
                        try:
                            beat.renew(); self.telegram.add_sticker_to_set(self.config.owner_user_id,pack,sticker)
                            self._mark_published(job_id,item,pack,observed,sequence,lease=beat.lease)
                        except TelegramApiError as add_error:
                            attempted=next(row for row in self.store.list_items(job_id) if row["item_index"] == item["item_index"])
                            if self._is_exact_duplicate(add_error):
                                if self._pack_has_unique_id(pack,sticker.file_unique_id,beat):
                                    self.store.commit_duplicate_item(lease=beat.lease,job_id=job_id,item_index=item["item_index"],owner_user_id=self.config.owner_user_id,pack_alias=self.config.pack_alias,pack_name=pack,source_sha256=item["source_sha256"],telegram_sha256=item["telegram_sha256"])
                                else: return self._result(job_id,resumable=True,exit_code=5)
                            elif add_error.code == 400:
                                self.store.update_item_status_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"],status="failed",expected_status="publishing_item",error=add_error.description)
                            elif not self._reconcile_attempt(job_id,attempted,beat): return self._result(job_id,resumable=True,exit_code=3)
                    elif error.code == 400:
                        self.store.update_item_status_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"],status="failed",expected_status="publishing_item",error=error.description)
                    else:
                        attempted=next(row for row in self.store.list_items(job_id) if row["item_index"] == item["item_index"])
                        if attempted["status"] != "publishing_item":
                            return self._result(job_id,resumable=True,exit_code=5)
                        if not self._reconcile_attempt(job_id,attempted,beat): return self._result(job_id,resumable=True,exit_code=3)
            pending=any(i["status"] in ("ready","publishing_item") for i in self.store.resume_job(job_id))
            if not pending: self.store.update_job(job_id,status="completed",completed=True)
            return self._result(job_id,resumable=pending,exit_code=3 if pending else (6 if self._counts(job_id)["failed"] else 0))
        except TelegramApiError: return self._result(job_id,resumable=True,exit_code=5)
        except (JobStateError,LeaseError): return self._result(job_id,resumable=True,exit_code=7)
        except (WorkflowError,StateError): return self._result(job_id,resumable=True,exit_code=3)
        finally:
            if not beat.close():
                current=self.store.get_job(job_id)
                result=self._result(job_id,resumable=not current or current["status"] != "completed",exit_code=7)
                result.summary["leaseWarning"]="팩 lease 정리에 실패했습니다"
                return result

    def _verify_binding(self, summary: dict[str,Any]) -> None:
        bind=summary.get("binding",{})
        expected={"ownerUserId":self.config.owner_user_id,"packAlias":self.config.pack_alias,"packTitle":self.config.pack_title,"packSlug":self.config.pack_slug,"emoji":self.config.emoji}
        if any(bind.get(k)!=v for k,v in expected.items()): raise WorkflowError("준비된 작업 설정이 현재 설정과 일치하지 않습니다")
    def _check_bot(self, summary:dict[str,Any], me:dict[str,Any]) -> None:
        bind=summary["binding"]
        if me.get("id")!=bind.get("botId") or me.get("username")!=bind.get("botUsername"): raise WorkflowError("준비한 봇과 현재 봇이 일치하지 않습니다")
    def _valid_snapshot(self, job_id: str, item:dict[str,Any])->bool:
        """Trust only regular, non-symlink files beneath this job's immutable root."""
        try:
            root=(self.config.data_dir/"jobs"/job_id/"items").resolve(strict=True)
            for key, digest in (("source_path",item["source_sha256"]),("telegram_path",item["telegram_sha256"])):
                raw=Path(item[key])
                if raw.is_symlink(): return False
                path=raw.resolve(strict=True)
                path.relative_to(root)
                if not path.is_file() or _sha(path)!=digest: return False
            return True
        except (OSError,ValueError,TypeError): return False
    def _reconcile_attempt(self, job_id: str, item: dict[str, Any], beat: _Heartbeat) -> bool:
        """Resolve a persisted mutation intent by exact Telegram file identity."""
        pack, operation = item.get("pack_name"), item.get("attempt_operation")
        unique_id = item.get("uploaded_file_unique_id")
        if not pack or operation not in ("create", "add") or not isinstance(unique_id, str) or not unique_id: return False
        remote = self._remote_pack(pack,beat)
        sequence = next((int(p["sequence"]) for p in self.store.list_packs(self.config.owner_user_id,self.config.pack_alias) if p["telegram_name"] == pack), 1)
        if remote is not None and self._has_unique_id(remote,unique_id):
            self._mark_published(job_id,item,pack,len(remote["stickers"]),sequence,increment=False,lease=beat.lease); return True
        self.store.reset_item_attempt_fenced(lease=beat.lease,job_id=job_id,item_index=item["item_index"])
        return True

    @staticmethod
    def _has_unique_id(remote: dict[str, Any], unique_id: str) -> bool:
        return any(isinstance(sticker,dict) and sticker.get("file_unique_id") == unique_id for sticker in remote.get("stickers",()))

    def _remote_pack(self,name:str,beat:Optional[_Heartbeat]=None)->Optional[dict[str,Any]]:
        if beat: beat.renew()
        try:return self.telegram.get_sticker_set(name)
        except TelegramApiError as error:
            if error.code==400 and "STICKERSET_INVALID" in error.description.upper(): return None
            raise

    def _pack_has_unique_id(self, name: str, unique_id: str, beat: _Heartbeat) -> bool:
        remote=self._remote_pack(name,beat)
        return remote is not None and self._has_unique_id(remote,unique_id)

    def _remote_count(self,name:str,beat:Optional[_Heartbeat]=None)->Optional[int]:
        remote=self._remote_pack(name,beat)
        return len(remote["stickers"]) if remote is not None else None
    def _available_pack(self,username:str,beat:Optional[_Heartbeat]=None,reserve: bool=True)->tuple[str,Optional[int],int]:
        latest=self.store.latest_pack(self.config.owner_user_id,self.config.pack_alias); seq=int(latest["sequence"]) if latest else 1
        while True:
            name=generate_short_name(self.config.pack_slug,username,seq); count=self._remote_count(name,beat)
            if count is None or count<PACK_CAPACITY:
                if reserve:
                    if beat:
                        self.store.upsert_pack_fenced(lease=beat.lease,sequence=seq,telegram_name=name,title=self._title(seq),last_known_count=count or 0)
                    else:
                        self.store.upsert_pack(owner_user_id=self.config.owner_user_id,alias=self.config.pack_alias,sequence=seq,telegram_name=name,title=self._title(seq),last_known_count=count or 0)
                return name,count,seq
            seq+=1
    def _mark_published(self,job,item,pack,count,seq,*,increment: bool = True,lease: Any)->None:
        # A remote duplicate/reconciliation must preserve its observed remote count.
        self.store.commit_published_item(lease=lease,job_id=job,item_index=item["item_index"],owner_user_id=self.config.owner_user_id,pack_alias=self.config.pack_alias,pack_name=pack,source_sha256=item["source_sha256"],telegram_sha256=item["telegram_sha256"],sequence=seq,title=self._title(seq),remote_count=count + (1 if increment else 0))
    def _title(self,seq:int)->str:
        suffix="" if seq==1 else f" ({seq})"; return self.config.pack_title[:64-len(suffix)].rstrip()+suffix
    def _plan_pack_names(self, username: str, sequence: int, current: Optional[int], ready: int) -> list[str]:
        """Query each future sequence so the plan reflects existing full packs."""
        names: list[str] = []; count = current
        while ready:
            name = generate_short_name(self.config.pack_slug, username, sequence)
            if count is None: count = self._remote_count(name)
            used = count or 0
            if used >= PACK_CAPACITY:
                sequence += 1; count = None; continue
            names.append(name)
            consumed = min(ready, PACK_CAPACITY - used)
            ready -= consumed; sequence += 1; count = None
        return names
    @staticmethod
    def _is_exact_duplicate(error:TelegramApiError)->bool:return error.code==400 and "STICKER" in error.description.upper() and "DUPLICATE" in error.description.upper()
    @staticmethod
    def _ambiguous_create(error:TelegramApiError)->bool:return error.code is None or error.code==429 or error.code>=500 or "NAME_OCCUPIED" in error.description.upper()
    def _counts(self,job:str)->dict[str,int]:
        items=self.store.list_items(job); return {"published":sum(i["status"]=="published" for i in items),"duplicates":sum(i["status"]=="skipped_duplicate" for i in items),"excluded":sum(i["status"]=="skipped_invalid" for i in items),"failed":sum(i["status"]=="failed" for i in items)}
    def _result(self,job:str,*,resumable:bool,exit_code:int)->WorkflowResult:
        counts=self._counts(job); packs=sorted({i["pack_name"] for i in self.store.list_items(job) if i["status"] in ("published","skipped_duplicate") and i["pack_name"]})
        return WorkflowResult({"jobId":job,"packLinks":["https://t.me/addstickers/"+p for p in packs],**counts,"resumable":resumable},exit_code)
