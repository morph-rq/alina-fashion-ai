"""Durable job queue: restart resumes work, one worker owns the queue, retries back off then stop."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import jobs
from app.database import Base
from app.models import Job


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def handlers(monkeypatch):
    calls = []
    monkeypatch.setitem(jobs.HANDLERS, "test.ok", lambda db, payload: calls.append(payload) or {"done": True})

    def failing(db, payload):
        calls.append("fail")
        raise RuntimeError("temporary")

    monkeypatch.setitem(jobs.HANDLERS, "test.fail", failing)
    return calls


def test_job_left_running_by_a_crashed_worker_is_resumed_after_restart(factory, handlers):
    with factory() as db:
        jobs.enqueue(db, "test.ok", {"n": 1})
        db.commit()
    job_id = jobs.claim(factory, worker_id="crashed-process")  # Claimed, then the process died.
    assert jobs.claim(factory, worker_id="new-process") is None  # Lease still valid: nobody else takes it.
    with factory() as db:
        db.get(Job, job_id).lease_until = jobs.now() - timedelta(seconds=1)
        db.commit()
    assert jobs.run_pending(factory, worker_id="new-process") == 1
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.state == "done" and job.attempts == 2
    assert handlers == [{"n": 1}]


def test_only_one_worker_holds_the_lease(factory):
    assert jobs.acquire_worker_lease(factory, owner="first")
    assert not jobs.acquire_worker_lease(factory, owner="second")
    assert jobs.acquire_worker_lease(factory, owner="first")  # Renewal by the owner.
    jobs.release_worker_lease(factory, owner="first")
    assert jobs.acquire_worker_lease(factory, owner="second")


def test_failures_back_off_and_stop_visibly(factory, handlers):
    with factory() as db:
        job = jobs.enqueue(db, "test.fail", {}, max_attempts=2)
        db.commit()
        job_id = job.id
    jobs.run_pending(factory)
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.state == "queued" and job.last_error == "RuntimeError: temporary"
        assert jobs._naive(job.run_after) > jobs.now()  # Backoff: not immediately due again.
        job.run_after = jobs.now()
        db.commit()
    jobs.run_pending(factory)
    with factory() as db:
        assert db.get(Job, job_id).state == "dead"


def test_dedupe_key_keeps_one_pending_job(factory, handlers):
    with factory() as db:
        first = jobs.enqueue(db, "test.ok", {"n": 1}, dedupe_key="sync:1")
        second = jobs.enqueue(db, "test.ok", {"n": 2}, dedupe_key="sync:1")
        db.commit()
        assert first.id == second.id and db.scalar(select(Job).where(Job.dedupe_key == "sync:1")).payload_json == '{"n": 1}'
    jobs.run_pending(factory)
    with factory() as db:  # After completion the same key can be queued again.
        again = jobs.enqueue(db, "test.ok", {"n": 3}, dedupe_key="sync:1")
        db.commit()
        assert again.state == "queued"
