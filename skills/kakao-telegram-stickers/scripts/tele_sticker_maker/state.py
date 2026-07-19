"""Durable SQLite state for resumable Telegram sticker publishing."""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence, Union

SCHEMA_VERSION = 3


class StateError(RuntimeError): pass
class StateCorruptionError(StateError): pass
class JobStateError(StateError): pass
class LeaseError(StateError): pass


@dataclass(frozen=True)
class PackLease:
    owner_user_id: int
    pack_alias: str
    token: str
    expires_at: float


# Each statement is intentionally executed inside an explicit transaction. Do not use
# executescript here: sqlite3 executescript commits implicitly before running SQL.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS packs (owner_user_id INTEGER NOT NULL, alias TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 1, telegram_name TEXT NOT NULL, title TEXT NOT NULL, last_known_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(owner_user_id,alias,sequence), UNIQUE(owner_user_id,telegram_name))",
    "CREATE TABLE IF NOT EXISTS imports (job_id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, source_url TEXT NOT NULL, resolved_url TEXT, kakao_slug TEXT NOT NULL, target_alias TEXT NOT NULL, requested_emoji TEXT NOT NULL, status TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, confirmed_at TEXT, completed_at TEXT)",
    "CREATE TABLE IF NOT EXISTS import_items (job_id TEXT NOT NULL, item_index INTEGER NOT NULL, source_sha256 TEXT NOT NULL, source_kind TEXT NOT NULL, source_path TEXT NOT NULL, telegram_path TEXT, telegram_sha256 TEXT, telegram_format TEXT, duration_ms INTEGER, status TEXT NOT NULL, pack_name TEXT, telegram_file_id TEXT, error TEXT, PRIMARY KEY(job_id,item_index), FOREIGN KEY(job_id) REFERENCES imports(job_id) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS registrations (owner_user_id INTEGER NOT NULL, pack_alias TEXT NOT NULL, pack_name TEXT NOT NULL, source_sha256 TEXT NOT NULL, telegram_sha256 TEXT, telegram_file_id TEXT, registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(owner_user_id,pack_alias,source_sha256))",
    "CREATE TABLE IF NOT EXISTS publish_leases (owner_user_id INTEGER NOT NULL, pack_alias TEXT NOT NULL, lease_owner TEXT NOT NULL, expires_at REAL NOT NULL, PRIMARY KEY(owner_user_id,pack_alias))",
    "CREATE INDEX IF NOT EXISTS import_items_resume ON import_items(job_id,status)",
)


class StateStore:
    """Connection-per-operation state repository safe across threads and processes."""
    def __init__(self, path: Union[Path, str], *, timeout: float = 5.0, clock: Callable[[], float] = time.time, sleeper: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic):
        self.path, self.timeout, self._clock = Path(path), timeout, clock
        self._sleeper, self._monotonic = sleeper, monotonic
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise StateError("상태 데이터 디렉터리를 만들 수 없습니다") from None
        self._initialize()

    def __repr__(self) -> str: return f"StateStore(path={self.path!s})"

    def _connect(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            # SQLite PRAGMA assignment cannot bind placeholders; value is a local int.
            db.execute("PRAGMA busy_timeout = {}".format(int(self.timeout * 1000)))
            return db
        except sqlite3.DatabaseError as error:
            raise StateCorruptionError("상태 데이터베이스를 열 수 없습니다") from error

    def _initialize(self) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            # v0 databases may not yet have the version table. Migrations are ordered
            # so later versions can be appended without changing initialization logic.
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            if not exists:
                db.execute(_SCHEMA[0]); db.execute("INSERT INTO schema_version(id,version) VALUES(1,0)")
            else:
                columns = [row["name"] for row in db.execute("PRAGMA table_info(schema_version)")]
                if columns != ["id", "version"]:
                    raise StateCorruptionError("상태 버전 테이블 형식이 올바르지 않습니다")
                rows = db.execute("SELECT id,version FROM schema_version").fetchall()
                if len(rows) != 1 or rows[0]["id"] != 1 or not isinstance(rows[0]["version"], int):
                    raise StateCorruptionError("상태 버전 레코드가 올바르지 않습니다")
            version = db.execute("SELECT version FROM schema_version WHERE id=1").fetchone()["version"]
            if version > SCHEMA_VERSION or version < 0: raise StateCorruptionError("지원하지 않는 상태 데이터베이스 버전입니다")
            while version < SCHEMA_VERSION:
                self._migrate(db, version)
                version += 1
                db.execute("UPDATE schema_version SET version=? WHERE id=1", (version,))
            db.commit()
        except sqlite3.DatabaseError as error:
            db.rollback()
            if self._is_locked(error):
                raise StateError("상태 데이터베이스 초기화가 잠겨 있습니다") from error
            raise StateCorruptionError("상태 데이터베이스 스키마를 읽을 수 없습니다") from error
        except BaseException:
            db.rollback(); raise
        finally: db.close()
        self._enable_wal()

    @staticmethod
    def _is_locked(error: sqlite3.DatabaseError) -> bool:
        return "locked" in str(error).lower() or "busy" in str(error).lower()

    def _enable_wal(self) -> None:
        """Enable WAL once the schema transaction has committed, retrying locks."""
        deadline = self._monotonic() + self.timeout
        while True:
            db: Optional[sqlite3.Connection] = None
            try:
                db = self._connect()
                current = db.execute("PRAGMA journal_mode").fetchone()[0]
                if str(current).lower() != "wal":
                    current = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(current).lower() != "wal":
                    raise StateError("WAL journal mode를 활성화하지 못했습니다")
                return
            except sqlite3.DatabaseError as error:
                if not self._is_locked(error):
                    raise StateCorruptionError("상태 데이터베이스 WAL 설정에 실패했습니다") from error
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise StateError("상태 데이터베이스 WAL 설정이 잠겨 있습니다") from error
                self._sleeper(min(0.01, remaining))
            finally:
                if db is not None:
                    db.close()

    @staticmethod
    def _migrate(db: sqlite3.Connection, version: int) -> None:
        if version == 0:
            for statement in _SCHEMA[1:]: db.execute(statement)
        elif version == 1:
            db.execute("ALTER TABLE import_items ADD COLUMN attempt_operation TEXT")
            db.execute("ALTER TABLE import_items ADD COLUMN attempt_count_before INTEGER")
        elif version == 2:
            db.execute("ALTER TABLE import_items ADD COLUMN uploaded_file_id TEXT")
            db.execute("ALTER TABLE import_items ADD COLUMN uploaded_file_unique_id TEXT")
        else: raise StateCorruptionError("알 수 없는 상태 데이터베이스 migration입니다")

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield db; db.commit()
        except sqlite3.DatabaseError as error:
            db.rollback(); raise StateError("상태 데이터베이스 작업에 실패했습니다") from error
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]: return dict(row) if row else None

    def create_job(self, *, owner_user_id: int, source_url: str, kakao_slug: str, target_alias: str, requested_emoji: str, resolved_url: Optional[str] = None, summary: Optional[dict[str, Any]] = None, job_id: Optional[str] = None) -> str:
        if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool): raise StateError("owner_user_id는 정수여야 합니다")
        job_id = job_id or uuid.uuid4().hex
        with self.transaction(immediate=True) as db:
            db.execute("INSERT INTO imports(job_id,owner_user_id,source_url,resolved_url,kakao_slug,target_alias,requested_emoji,status,summary_json) VALUES(?,?,?,?,?,?,?,?,?)", (job_id,owner_user_id,source_url,resolved_url,kakao_slug,target_alias,requested_emoji,"prepared",json.dumps(summary or {}, ensure_ascii=False, sort_keys=True)))
        return job_id

    def create_prepared_job_with_items(self, *, owner_user_id: int, source_url: str, kakao_slug: str, target_alias: str, requested_emoji: str, resolved_url: Optional[str], summary: dict[str, Any], job_id: str, items: Sequence[dict[str, Any]], pack_reservations: Sequence[dict[str, Any]] = ()) -> str:
        """Atomically reserve pack identities and persist an immutable prepared job."""
        if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool) or owner_user_id <= 0:
            raise StateError("owner_user_id는 양의 정수여야 합니다")
        with self.transaction(immediate=True) as db:
            for reservation in pack_reservations:
                sequence,name=int(reservation["sequence"]),reservation["telegram_name"]
                existing=db.execute("SELECT telegram_name FROM packs WHERE owner_user_id=? AND alias=? AND sequence=?", (owner_user_id,target_alias,sequence)).fetchone()
                if existing is not None and existing["telegram_name"] != name: raise StateError("pack alias sequence의 Telegram 이름이 이미 예약되어 있습니다")
                if existing is None: db.execute("INSERT INTO packs(owner_user_id,alias,sequence,telegram_name,title,last_known_count,status) VALUES(?,?,?,?,?,?,?)", (owner_user_id,target_alias,sequence,name,reservation["title"],int(reservation.get("last_known_count",0)),"active"))
            db.execute("INSERT INTO imports(job_id,owner_user_id,source_url,resolved_url,kakao_slug,target_alias,requested_emoji,status,summary_json) VALUES(?,?,?,?,?,?,?,?,?)", (job_id,owner_user_id,source_url,resolved_url,kakao_slug,target_alias,requested_emoji,"prepared",json.dumps(summary, ensure_ascii=False, sort_keys=True)))
            for item in items:
                db.execute("INSERT INTO import_items(job_id,item_index,source_sha256,source_kind,source_path,telegram_path,telegram_sha256,telegram_format,duration_ms,status,pack_name,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (job_id,item["item_index"],item["source_sha256"],item["source_kind"],item["source_path"],item.get("telegram_path"),item.get("telegram_sha256"),item.get("telegram_format"),item.get("duration_ms"),item["status"],item.get("pack_name"),item.get("error")))
        return job_id

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self.transaction() as db: return self._row(db.execute("SELECT * FROM imports WHERE job_id=?", (job_id,)).fetchone())

    def update_job(self, job_id: str, *, status: Optional[str] = None, summary: Optional[dict[str, Any]] = None, confirmed: bool = False, completed: bool = False) -> None:
        parts: list[str] = []; values: list[Any] = []
        if status is not None: parts.append("status=?"); values.append(status)
        if summary is not None: parts.append("summary_json=?"); values.append(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        if confirmed: parts.append("confirmed_at=CURRENT_TIMESTAMP")
        if completed: parts.append("completed_at=CURRENT_TIMESTAMP")
        if not parts: return
        with self.transaction(immediate=True) as db:
            if db.execute("UPDATE imports SET " + ",".join(parts) + " WHERE job_id=?", tuple(values + [job_id])).rowcount != 1: raise JobStateError("작업을 찾을 수 없습니다")

    def begin_publish(self, job_id: str) -> None:
        with self.transaction(immediate=True) as db:
            if db.execute("UPDATE imports SET status='publishing',confirmed_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='prepared'", (job_id,)).rowcount != 1:
                row = db.execute("SELECT status FROM imports WHERE job_id=?", (job_id,)).fetchone()
                raise JobStateError("작업이 이미 등록 중입니다" if row and row["status"] == "publishing" else "prepared 상태의 작업만 등록할 수 있습니다")

    def resume_job(self, job_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            job = db.execute("SELECT status FROM imports WHERE job_id=?", (job_id,)).fetchone()
            if not job: raise JobStateError("작업을 찾을 수 없습니다")
            if job["status"] not in ("prepared", "publishing"): raise JobStateError("재개할 수 없는 작업 상태입니다")
            return [dict(row) for row in db.execute("SELECT * FROM import_items WHERE job_id=? AND status!='published' ORDER BY item_index", (job_id,))]

    @staticmethod
    def _lease_ttl(ttl: float) -> float:
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise LeaseError("lease TTL은 유한한 양수여야 합니다")
        return float(ttl)

    def acquire_pack_lease(self, owner_user_id: int, pack_alias: str, *, ttl: float = 60.0, lease_owner: Optional[str] = None) -> PackLease:
        ttl = self._lease_ttl(ttl)
        owner = lease_owner or secrets.token_urlsafe(24)
        with self.transaction(immediate=True) as db:
            # BEGIN IMMEDIATE may wait for another publisher. Measure TTL only after
            # that lock has been acquired, using one consistent timestamp.
            now = self._clock(); expiry = now + ttl
            row = db.execute("SELECT lease_owner,expires_at FROM publish_leases WHERE owner_user_id=? AND pack_alias=?", (owner_user_id,pack_alias)).fetchone()
            if row is None:
                db.execute("INSERT INTO publish_leases(owner_user_id,pack_alias,lease_owner,expires_at) VALUES(?,?,?,?)", (owner_user_id,pack_alias,owner,expiry))
            elif row["expires_at"] <= now:
                if db.execute("UPDATE publish_leases SET lease_owner=?,expires_at=? WHERE owner_user_id=? AND pack_alias=? AND expires_at<=?", (owner,expiry,owner_user_id,pack_alias,now)).rowcount != 1: raise LeaseError("팩 등록 lease를 획득하지 못했습니다")
            elif row["lease_owner"] == owner:
                db.execute("UPDATE publish_leases SET expires_at=? WHERE owner_user_id=? AND pack_alias=? AND lease_owner=?", (expiry,owner_user_id,pack_alias,owner))
            else: raise LeaseError("같은 팩 별칭이 다른 등록 작업으로 잠겨 있습니다")
        return PackLease(owner_user_id, pack_alias, owner, expiry)

    def renew_pack_lease(self, lease: PackLease, *, ttl: float = 60.0) -> PackLease:
        ttl = self._lease_ttl(ttl)
        with self.transaction(immediate=True) as db:
            now = self._clock(); expiry = now + ttl
            if db.execute("UPDATE publish_leases SET expires_at=? WHERE owner_user_id=? AND pack_alias=? AND lease_owner=? AND expires_at>?", (expiry,lease.owner_user_id,lease.pack_alias,lease.token,now)).rowcount != 1: raise LeaseError("유효하지 않거나 만료된 pack lease입니다")
        return PackLease(lease.owner_user_id,lease.pack_alias,lease.token,expiry)

    def release_pack_lease(self, lease: PackLease) -> bool:
        with self.transaction(immediate=True) as db: return db.execute("DELETE FROM publish_leases WHERE owner_user_id=? AND pack_alias=? AND lease_owner=?", (lease.owner_user_id,lease.pack_alias,lease.token)).rowcount == 1

    def add_item(self, job_id: str, *, item_index: int, source_sha256: str, source_kind: str, source_path: str, status: str, telegram_path: Optional[str] = None, telegram_sha256: Optional[str] = None, telegram_format: Optional[str] = None, duration_ms: Optional[int] = None, error: Optional[str] = None) -> None:
        # Each item write commits independently, so a later publish failure can resume.
        with self.transaction(immediate=True) as db: db.execute("INSERT INTO import_items(job_id,item_index,source_sha256,source_kind,source_path,telegram_path,telegram_sha256,telegram_format,duration_ms,status,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job_id,item_index,source_sha256,source_kind,source_path,telegram_path,telegram_sha256,telegram_format,duration_ms,status,error))

    def update_item_status(self, job_id: str, item_index: int, status: str, *, pack_name: Optional[str] = None, telegram_file_id: Optional[str] = None, error: Optional[str] = None) -> None:
        with self.transaction(immediate=True) as db:
            if db.execute("UPDATE import_items SET status=?,pack_name=COALESCE(?,pack_name),telegram_file_id=COALESCE(?,telegram_file_id),error=? WHERE job_id=? AND item_index=?", (status,pack_name,telegram_file_id,error,job_id,item_index)).rowcount != 1: raise JobStateError("작업 항목을 찾을 수 없습니다")

    @staticmethod
    def _require_live_lease(db: sqlite3.Connection, lease: PackLease, now: float) -> None:
        if not db.execute("SELECT 1 FROM publish_leases WHERE owner_user_id=? AND pack_alias=? AND lease_owner=? AND expires_at>?", (lease.owner_user_id,lease.pack_alias,lease.token,now)).fetchone():
            raise LeaseError("만료되었거나 소유권을 잃은 pack lease입니다")

    def update_item_status_fenced(self, *, lease: PackLease, job_id: str, item_index: int, status: str, expected_status: str, pack_name: Optional[str] = None, error: Optional[str] = None) -> None:
        with self.transaction(immediate=True) as db:
            self._require_live_lease(db,lease,self._clock())
            if db.execute("UPDATE import_items SET status=?,pack_name=COALESCE(?,pack_name),error=? WHERE job_id=? AND item_index=? AND status=?", (status,pack_name,error,job_id,item_index,expected_status)).rowcount != 1:
                raise JobStateError("작업 항목 상태가 이미 변경되었습니다")

    def persist_uploaded_sticker(self, *, lease: PackLease, job_id: str, item_index: int, file_id: str, file_unique_id: str) -> None:
        with self.transaction(immediate=True) as db:
            self._require_live_lease(db,lease,self._clock())
            if db.execute("UPDATE import_items SET uploaded_file_id=?,uploaded_file_unique_id=? WHERE job_id=? AND item_index=? AND status='ready'", (file_id,file_unique_id,job_id,item_index)).rowcount != 1: raise JobStateError("ready 상태 작업 항목을 찾을 수 없습니다")

    def mark_item_attempt(self, job_id: str, item_index: int, pack_name: str, operation: str, count_before: int) -> None:
        if operation not in ("create", "add"): raise StateError("알 수 없는 Telegram 등록 작업입니다")
        with self.transaction(immediate=True) as db:
            if db.execute("UPDATE import_items SET status='publishing_item',pack_name=?,attempt_operation=?,attempt_count_before=?,error=NULL WHERE job_id=? AND item_index=? AND status='ready'", (pack_name,operation,count_before,job_id,item_index)).rowcount != 1: raise JobStateError("ready 상태 작업 항목을 찾을 수 없습니다")

    def mark_item_attempt_fenced(self, *, lease: PackLease, job_id: str, item_index: int, pack_name: str, operation: str, count_before: int) -> None:
        if operation not in ("create", "add"): raise StateError("알 수 없는 Telegram 등록 작업입니다")
        with self.transaction(immediate=True) as db:
            self._require_live_lease(db,lease,self._clock())
            if db.execute("UPDATE import_items SET status='publishing_item',pack_name=?,attempt_operation=?,attempt_count_before=?,error=NULL WHERE job_id=? AND item_index=? AND status='ready'", (pack_name,operation,count_before,job_id,item_index)).rowcount != 1:
                raise JobStateError("ready 상태 작업 항목을 찾을 수 없습니다")

    def reset_item_attempt(self, job_id: str, item_index: int) -> None:
        with self.transaction(immediate=True) as db:
            db.execute("UPDATE import_items SET status='ready',attempt_operation=NULL,attempt_count_before=NULL WHERE job_id=? AND item_index=? AND status='publishing_item'", (job_id,item_index))

    def reset_item_attempt_fenced(self, *, lease: PackLease, job_id: str, item_index: int) -> None:
        with self.transaction(immediate=True) as db:
            self._require_live_lease(db,lease,self._clock())
            if db.execute("UPDATE import_items SET status='ready',attempt_operation=NULL,attempt_count_before=NULL WHERE job_id=? AND item_index=? AND status='publishing_item'", (job_id,item_index)).rowcount != 1:
                raise JobStateError("publishing_item 상태 작업 항목을 찾을 수 없습니다")

    def get_registration(self, owner_user_id: int, pack_alias: str, source_sha256: str) -> Optional[dict[str, Any]]:
        with self.transaction() as db: return self._row(db.execute("SELECT * FROM registrations WHERE owner_user_id=? AND pack_alias=? AND source_sha256=?", (owner_user_id,pack_alias,source_sha256)).fetchone())

    def commit_published_item(self, *, lease: PackLease, job_id: str, item_index: int, owner_user_id: int, pack_alias: str, pack_name: str, source_sha256: str, telegram_sha256: Optional[str], sequence: int, title: str, remote_count: int) -> bool:
        """Fenced, atomic registration/item/pack commit after a remote success."""
        with self.transaction(immediate=True) as db:
            now = self._clock()
            valid = db.execute("SELECT 1 FROM publish_leases WHERE owner_user_id=? AND pack_alias=? AND lease_owner=? AND expires_at>?", (owner_user_id,pack_alias,lease.token,now)).fetchone()
            if not valid: raise LeaseError("만료되었거나 소유권을 잃은 pack lease입니다")
            inserted = db.execute("INSERT OR IGNORE INTO registrations(owner_user_id,pack_alias,pack_name,source_sha256,telegram_sha256) VALUES(?,?,?,?,?)", (owner_user_id,pack_alias,pack_name,source_sha256,telegram_sha256)).rowcount == 1
            if not inserted:
                registered = db.execute("SELECT pack_name FROM registrations WHERE owner_user_id=? AND pack_alias=? AND source_sha256=?", (owner_user_id,pack_alias,source_sha256)).fetchone()
                db.execute("UPDATE import_items SET status='skipped_duplicate',pack_name=?,attempt_operation=NULL,attempt_count_before=NULL,error=NULL WHERE job_id=? AND item_index=?", (registered["pack_name"],job_id,item_index))
                return False
            if db.execute("UPDATE import_items SET status='published',pack_name=?,attempt_operation=NULL,attempt_count_before=NULL,error=NULL WHERE job_id=? AND item_index=?", (pack_name,job_id,item_index)).rowcount != 1: raise JobStateError("작업 항목을 찾을 수 없습니다")
            existing_pack=db.execute("SELECT telegram_name FROM packs WHERE owner_user_id=? AND alias=? AND sequence=?", (owner_user_id,pack_alias,sequence)).fetchone()
            if existing_pack is not None and existing_pack["telegram_name"] != pack_name: raise StateError("pack alias sequence의 Telegram 이름이 이미 예약되어 있습니다")
            if existing_pack is None:
                db.execute("INSERT INTO packs(owner_user_id,alias,sequence,telegram_name,title,last_known_count,status) VALUES(?,?,?,?,?,?,?)", (owner_user_id,pack_alias,sequence,pack_name,title,remote_count,"active"))
            else:
                db.execute("UPDATE packs SET title=?,last_known_count=?,status='active',updated_at=CURRENT_TIMESTAMP WHERE owner_user_id=? AND alias=? AND sequence=?", (title,remote_count,owner_user_id,pack_alias,sequence))
            return True

    def commit_duplicate_item(self, *, lease: PackLease, job_id: str, item_index: int, owner_user_id: int, pack_alias: str, pack_name: str, source_sha256: str, telegram_sha256: Optional[str]) -> None:
        with self.transaction(immediate=True) as db:
            now=self._clock()
            if not db.execute("SELECT 1 FROM publish_leases WHERE owner_user_id=? AND pack_alias=? AND lease_owner=? AND expires_at>?", (owner_user_id,pack_alias,lease.token,now)).fetchone(): raise LeaseError("만료되었거나 소유권을 잃은 pack lease입니다")
            db.execute("INSERT OR IGNORE INTO registrations(owner_user_id,pack_alias,pack_name,source_sha256,telegram_sha256) VALUES(?,?,?,?,?)", (owner_user_id,pack_alias,pack_name,source_sha256,telegram_sha256))
            if db.execute("UPDATE import_items SET status='skipped_duplicate',pack_name=?,attempt_operation=NULL,attempt_count_before=NULL,error=NULL WHERE job_id=? AND item_index=?", (pack_name,job_id,item_index)).rowcount != 1: raise JobStateError("작업 항목을 찾을 수 없습니다")

    @staticmethod
    def _upsert_pack_row(db: sqlite3.Connection, *, owner_user_id: int, alias: str, sequence: int, telegram_name: str, title: str, last_known_count: int, status: str) -> None:
        existing=db.execute("SELECT telegram_name FROM packs WHERE owner_user_id=? AND alias=? AND sequence=?", (owner_user_id,alias,sequence)).fetchone()
        if existing is not None and existing["telegram_name"] != telegram_name:
            raise StateError("pack alias sequence의 Telegram 이름이 이미 예약되어 있습니다")
        if existing is None:
            db.execute("INSERT INTO packs(owner_user_id,alias,sequence,telegram_name,title,last_known_count,status) VALUES(?,?,?,?,?,?,?)", (owner_user_id,alias,sequence,telegram_name,title,last_known_count,status))
        else:
            db.execute("UPDATE packs SET title=?,last_known_count=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE owner_user_id=? AND alias=? AND sequence=?", (title,last_known_count,status,owner_user_id,alias,sequence))

    def upsert_pack(self, *, owner_user_id: int, alias: str, sequence: int, telegram_name: str, title: str, last_known_count: int = 0, status: str = "active") -> None:
        """Reserve an alias sequence identity; its Telegram name is immutable."""
        with self.transaction(immediate=True) as db:
            self._upsert_pack_row(db,owner_user_id=owner_user_id,alias=alias,sequence=sequence,telegram_name=telegram_name,title=title,last_known_count=last_known_count,status=status)

    def upsert_pack_fenced(self, *, lease: PackLease, sequence: int, telegram_name: str, title: str, last_known_count: int = 0, status: str = "active") -> None:
        """Update publish-time pack state only while the caller owns a live lease."""
        with self.transaction(immediate=True) as db:
            now=self._clock()
            if not db.execute("SELECT 1 FROM publish_leases WHERE owner_user_id=? AND pack_alias=? AND lease_owner=? AND expires_at>?", (lease.owner_user_id,lease.pack_alias,lease.token,now)).fetchone():
                raise LeaseError("만료되었거나 소유권을 잃은 pack lease입니다")
            self._upsert_pack_row(db,owner_user_id=lease.owner_user_id,alias=lease.pack_alias,sequence=sequence,telegram_name=telegram_name,title=title,last_known_count=last_known_count,status=status)

    def latest_pack(self, owner_user_id: int, alias: str) -> Optional[dict[str, Any]]:
        with self.transaction() as db: return self._row(db.execute("SELECT * FROM packs WHERE owner_user_id=? AND alias=? ORDER BY sequence DESC LIMIT 1", (owner_user_id,alias)).fetchone())

    def list_packs(self, owner_user_id: int, alias: Optional[str] = None) -> list[dict[str, Any]]:
        with self.transaction() as db:
            query = "SELECT * FROM packs WHERE owner_user_id=?"
            values: tuple[Any, ...] = (owner_user_id,)
            if alias is not None:
                query += " AND alias=?"; values += (alias,)
            return [dict(row) for row in db.execute(query + " ORDER BY alias, sequence", values)]

    def list_items(self, job_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM import_items WHERE job_id=? ORDER BY item_index", (job_id,))]

    def count_pack(self, owner_user_id: int, alias: str) -> int:
        record = self.latest_pack(owner_user_id,alias); return int(record["last_known_count"]) if record else 0

    def record_registration(self, *, owner_user_id: int, pack_alias: str, pack_name: str, source_sha256: str, telegram_sha256: Optional[str] = None, telegram_file_id: Optional[str] = None) -> bool:
        with self.transaction(immediate=True) as db: return db.execute("INSERT OR IGNORE INTO registrations(owner_user_id,pack_alias,pack_name,source_sha256,telegram_sha256,telegram_file_id) VALUES(?,?,?,?,?,?)", (owner_user_id,pack_alias,pack_name,source_sha256,telegram_sha256,telegram_file_id)).rowcount == 1
    def registration_exists(self, owner_user_id: int, pack_alias: str, source_sha256: str) -> bool:
        with self.transaction() as db: return db.execute("SELECT 1 FROM registrations WHERE owner_user_id=? AND pack_alias=? AND source_sha256=?", (owner_user_id,pack_alias,source_sha256)).fetchone() is not None
    def list_registrations(self, owner_user_id: int, pack_alias: Optional[str] = None) -> Sequence[dict[str, Any]]:
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM registrations WHERE owner_user_id=? ORDER BY registered_at", (owner_user_id,)) if pack_alias is None else db.execute("SELECT * FROM registrations WHERE owner_user_id=? AND pack_alias=? ORDER BY registered_at", (owner_user_id,pack_alias))
            return [dict(row) for row in rows]

StateRepository = StateStore
SQLiteState = StateStore
