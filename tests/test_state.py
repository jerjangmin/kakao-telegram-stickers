from __future__ import annotations
import math
import sqlite3, sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import pytest
SCRIPTS_DIR=Path(__file__).resolve().parents[1]/"skills"/"kakao-telegram-stickers"/"scripts"; sys.path.insert(0,str(SCRIPTS_DIR))
from tele_sticker_maker.state import JobStateError, LeaseError, StateError, StateStore

def test_state_directory_creation_error_is_sanitized(tmp_path):
    data_dir = tmp_path / "existing-file"
    data_dir.write_text("not a directory")

    with pytest.raises(StateError, match="상태 데이터 디렉터리") as error:
        StateStore(data_dir / "state.sqlite")

    assert str(data_dir) not in str(error.value)


def test_state_directory_permission_error_is_sanitized(tmp_path, monkeypatch):
    database = tmp_path / "blocked" / "state.sqlite"
    mkdir = Path.mkdir

    def deny(path, *args, **kwargs):
        if path == database.parent:
            raise PermissionError("permission denied")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny)
    with pytest.raises(StateError, match="상태 데이터 디렉터리") as error:
        StateStore(database)

    assert "permission denied" not in str(error.value)
    assert str(database.parent) not in str(error.value)


def test_jobs_items_and_resume_are_durable(tmp_path):
    store=StateStore(tmp_path/"state.sqlite"); job=store.create_job(owner_user_id=1,source_url="source",kakao_slug="slug",target_alias="default",requested_emoji="🙂")
    store.add_item(job,item_index=1,source_sha256="a"*64,source_kind="static_png",source_path="a.png",status="ready"); store.begin_publish(job)
    assert [x["item_index"] for x in store.resume_job(job)]==[1]
    store.update_item_status(job,1,"published",pack_name="pack"); assert store.resume_job(job)==[]
    with pytest.raises(JobStateError): store.begin_publish(job)
    assert StateStore(tmp_path/"state.sqlite").get_job(job)["job_id"]==job

def test_lease_requires_owner_token_and_expires(tmp_path):
    now=[100.0]; store=StateStore(tmp_path/"state.sqlite",clock=lambda:now[0])
    first=store.acquire_pack_lease(1,"default",ttl=10,lease_owner="first")
    with pytest.raises(LeaseError): store.acquire_pack_lease(1,"default",ttl=10,lease_owner="second")
    renewed=store.renew_pack_lease(first,ttl=20); assert renewed.expires_at==120
    assert not store.release_pack_lease(type(first)(1,"default","wrong",120))
    now[0]=121; second=store.acquire_pack_lease(1,"default",ttl=10,lease_owner="second")
    with pytest.raises(LeaseError): store.renew_pack_lease(renewed)
    assert store.release_pack_lease(second)

def test_registration_alias_and_latest_pack_count(tmp_path):
    store=StateStore(tmp_path/"state.sqlite")
    store.upsert_pack(owner_user_id=1,alias="one",sequence=1,telegram_name="one_by_bot",title="One",last_known_count=119)
    store.upsert_pack(owner_user_id=1,alias="one",sequence=2,telegram_name="two_by_bot",title="Two",last_known_count=1)
    assert store.count_pack(1,"one")==1 and store.latest_pack(1,"one")["sequence"]==2
    assert store.record_registration(owner_user_id=1,pack_alias="one",pack_name="one_by_bot",source_sha256="a")
    assert not store.record_registration(owner_user_id=1,pack_alias="one",pack_name="one_by_bot",source_sha256="a")
    assert store.record_registration(owner_user_id=1,pack_alias="two",pack_name="two_by_bot",source_sha256="a")

def test_v0_migration_and_concurrent_initialization(tmp_path):
    path=tmp_path/"state.sqlite"; db=sqlite3.connect(path); db.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)"); db.execute("INSERT INTO schema_version VALUES(1,0)"); db.commit(); db.close()
    StateStore(path); assert sqlite3.connect(path).execute("SELECT version FROM schema_version").fetchone()[0]==3
    path=tmp_path/"new.sqlite"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(StateStore, path) for _ in range(2)]
        stores = [future.result() for future in futures]
    assert len(stores) == 2

def test_lease_ttl_must_be_finite_positive_and_not_bool(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    for ttl in (True, False, 0, -1, math.nan, math.inf, -math.inf):
        with pytest.raises(LeaseError):
            store.acquire_pack_lease(1, "default", ttl=ttl)
    lease = store.acquire_pack_lease(1, "default", ttl=1.0)
    for ttl in (True, 0, math.nan, math.inf):
        with pytest.raises(LeaseError):
            store.renew_pack_lease(lease, ttl=ttl)

def test_lease_ttl_starts_after_immediate_transaction_lock(tmp_path):
    now, clock_calls = [0.0], []
    def clock():
        clock_calls.append(now[0]); return now[0]
    store = StateStore(tmp_path / "state.sqlite", clock=clock)
    original_transaction = store.transaction
    @contextmanager
    def delayed_transaction(*args, **kwargs):
        with original_transaction(*args, **kwargs) as db:
            # This runs after BEGIN IMMEDIATE has succeeded, modelling time spent
            # waiting on a competing publisher before the transaction yielded.
            now[0] += 100
            yield db
    store.transaction = delayed_transaction
    lease = store.acquire_pack_lease(1, "default", ttl=1_000, lease_owner="first")
    assert clock_calls == [100] and lease.expires_at == 1_100
    clock_calls.clear()
    renewed = store.renew_pack_lease(lease, ttl=10)
    assert clock_calls == [200] and renewed.expires_at == 210

class _JournalCursor:
    def __init__(self, value): self.value = value
    def fetchone(self): return (self.value,)

class _JournalDb:
    def __init__(self, attempts): self.attempts, self.closed = attempts, False
    def execute(self, statement):
        if statement == "PRAGMA journal_mode": return _JournalCursor("delete")
        if statement == "PRAGMA journal_mode = WAL":
            self.attempts.append(1)
            if len(self.attempts) == 1: raise sqlite3.OperationalError("database is locked")
            return _JournalCursor("wal")
        raise AssertionError(statement)
    def close(self): self.closed = True

def test_wal_enable_retries_lock_with_injected_clock_and_sleeper():
    store = object.__new__(StateStore); attempts = []; sleeps = []
    store.timeout, store._monotonic, store._sleeper = 1.0, lambda: 0.0, sleeps.append
    store._connect = lambda: _JournalDb(attempts)
    store._enable_wal()
    assert len(attempts) == 2 and sleeps == [0.01]

def test_upsert_pack_fenced_rejects_expired_takeover_lease_without_overwriting_count(tmp_path):
    now = [0.0]; store = StateStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    stale = store.acquire_pack_lease(1, "default", ttl=10, lease_owner="stale")
    store.upsert_pack_fenced(lease=stale, sequence=1, telegram_name="one_by_bot", title="One", last_known_count=1)
    now[0] = 10
    current = store.acquire_pack_lease(1, "default", ttl=10, lease_owner="current")
    store.upsert_pack_fenced(lease=current, sequence=1, telegram_name="one_by_bot", title="One", last_known_count=2)
    with pytest.raises(LeaseError):
        store.upsert_pack_fenced(lease=stale, sequence=1, telegram_name="one_by_bot", title="One", last_known_count=1)
    assert store.count_pack(1, "default") == 2
    store.upsert_pack_fenced(lease=current, sequence=1, telegram_name="one_by_bot", title="One", last_known_count=3)
    assert store.count_pack(1, "default") == 3


def test_stale_lease_cannot_replace_takeover_committed_item(tmp_path):
    now = [0.0]; store = StateStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    store.create_prepared_job_with_items(owner_user_id=1, source_url="s", resolved_url=None, kakao_slug="s", target_alias="default", requested_emoji="🙂", summary={}, job_id="job", items=[{"item_index":1,"source_sha256":"a","source_kind":"static_png","source_path":"source.png","status":"ready"}])
    stale = store.acquire_pack_lease(1, "default", ttl=10, lease_owner="stale")
    now[0] = 10
    current = store.acquire_pack_lease(1, "default", ttl=10, lease_owner="current")
    assert store.commit_published_item(lease=current, job_id="job", item_index=1, owner_user_id=1, pack_alias="default", pack_name="pack_by_bot", source_sha256="a", telegram_sha256="b", sequence=1, title="Pack", remote_count=1)

    with pytest.raises((LeaseError, JobStateError)):
        store.update_item_status_fenced(lease=stale, job_id="job", item_index=1, status="failed", expected_status="publishing_item", error="stale")
    with pytest.raises((LeaseError, JobStateError)):
        store.reset_item_attempt_fenced(lease=stale, job_id="job", item_index=1)
    with pytest.raises((LeaseError, JobStateError)):
        store.mark_item_attempt_fenced(lease=stale, job_id="job", item_index=1, pack_name="pack_by_bot", operation="add", count_before=1)

    assert store.list_items("job")[0]["status"] == "published"

@pytest.mark.parametrize("workers", (2, 4, 12))
def test_concurrent_first_initialization_stress(tmp_path, workers):
    for iteration in range(100):
        path = tmp_path / f"state-{workers}-{iteration}.sqlite"
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(StateStore, path) for _ in range(workers)]
            stores = [future.result() for future in futures]
        assert len(stores) == workers
        db = sqlite3.connect(path)
        try:
            assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            db.close()
