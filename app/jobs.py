"""A small durable job queue for SQLite.

* The ``jobs`` table is the only record of pending work; nothing lives only in memory.
* Claiming happens inside ``BEGIN IMMEDIATE`` so two workers cannot take the same job.
* A claimed job holds a lease; if the process stops, the lease expires and the
  job is claimed again after restart.
* Failures retry with exponential backoff (capped) until ``max_attempts``; a
  ``PermanentJobError`` stops retries at once. Dead jobs stay visible.
* ``WorkerLease`` gives one process the right to run the background loop, so
  starting a second app instance does not create a second worker.

Handlers must be idempotent: a job can run again after a crash between its work
and its completion record.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import random
import secrets
import socket
from typing import Callable

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Job, WorkerLease

log = logging.getLogger("alina.jobs")
HANDLERS: dict[str, Callable[[Session, dict], dict | None]] = {}
PERIODIC: list[Callable[[Session], None]] = []
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
LEASE_SECONDS = 300
MAX_BACKOFF_SECONDS = 3600


class PermanentJobError(Exception):
    """The job cannot succeed by retrying (for example, revoked consent)."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(value: datetime | None) -> datetime | None:
    # SQLite returns stored UTC timestamps without tzinfo.
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def handler(kind: str):
    def register(function):
        HANDLERS[kind] = function
        return function
    return register


def periodic(function):
    """Register a scheduler that may enqueue due work (called every loop)."""
    PERIODIC.append(function)
    return function


def enqueue(db: Session, kind: str, payload: dict | None = None, *, dedupe_key: str | None = None,
            delay_seconds: float = 0, max_attempts: int = 6) -> Job:
    """Add a job (flushes). With a dedupe key, at most one pending job exists per key."""
    if dedupe_key:
        existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing is not None:
            if existing.state in {"queued", "running"}:
                return existing
            existing.state, existing.attempts, existing.last_error = "queued", 0, None
            existing.payload_json = json.dumps(payload or {}, default=str)
            existing.run_after = now() + timedelta(seconds=delay_seconds)
            existing.claimed_by = existing.lease_until = None
            db.flush()
            return existing
    job = Job(kind=kind, payload_json=json.dumps(payload or {}, default=str), dedupe_key=dedupe_key,
              run_after=now() + timedelta(seconds=delay_seconds), max_attempts=max_attempts)
    db.add(job)
    db.flush()
    return job


def claim(session_factory: sessionmaker, worker_id: str = WORKER_ID, kinds: set[str] | None = None) -> int | None:
    with session_factory() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        current = now()
        query = select(Job).where(or_(
            (Job.state == "queued") & (Job.run_after <= current),
            (Job.state == "running") & (Job.lease_until < current),
        )).order_by(Job.run_after, Job.id).limit(1)
        if kinds:
            query = query.where(Job.kind.in_(kinds))
        job = db.scalar(query)
        if job is None:
            db.rollback()
            return None
        job.state, job.claimed_by = "running", worker_id
        job.lease_until = current + timedelta(seconds=LEASE_SECONDS)
        job.attempts += 1
        db.commit()
        return job.id


def _safe_error(exc: BaseException) -> str:
    # Exception text from our handlers is written for operators; never include response bodies or URLs with tokens.
    message = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {message}"[:500]


def run_job(session_factory: sessionmaker, job_id: int, worker_id: str = WORKER_ID) -> str:
    with session_factory() as db:
        job = db.get(Job, job_id)
        kind, payload = job.kind, json.loads(job.payload_json or "{}")
    function = HANDLERS.get(kind)
    outcome, result, error, permanent = "done", None, None, False
    try:
        if function is None:
            raise PermanentJobError(f"No handler for job kind {kind}")
        with session_factory() as db:
            result = function(db, payload)
            db.commit()
    except PermanentJobError as exc:
        outcome, error, permanent = "dead", _safe_error(exc), True
    except Exception as exc:  # noqa: BLE001 - every failure must be recorded, not lost.
        outcome, error = "failed", _safe_error(exc)
        log.warning("Job %s (%s) failed: %s", job_id, kind, error)
    with session_factory() as db:
        job = db.get(Job, job_id)
        if job.claimed_by != worker_id:
            return "lost-lease"  # Another worker reclaimed it after our lease expired.
        job.claimed_by, job.lease_until = None, None
        if outcome == "done":
            job.state, job.last_error = "done", None
            job.result_json = json.dumps(result or {}, default=str)
        elif permanent or job.attempts >= job.max_attempts:
            job.state, job.last_error = "dead", error
        else:
            backoff = min(30 * 2 ** (job.attempts - 1), MAX_BACKOFF_SECONDS) * random.uniform(0.8, 1.2)
            job.state, job.last_error = "queued", error
            job.run_after = now() + timedelta(seconds=backoff)
        db.commit()
        return job.state


def run_pending(session_factory: sessionmaker, *, limit: int = 100, kinds: set[str] | None = None,
                worker_id: str = WORKER_ID) -> int:
    """Run due jobs until none remain (used by the worker loop and by tests)."""
    count = 0
    while count < limit:
        job_id = claim(session_factory, worker_id, kinds)
        if job_id is None:
            break
        run_job(session_factory, job_id, worker_id)
        count += 1
    return count


def acquire_worker_lease(session_factory: sessionmaker, name: str = "background", owner: str = WORKER_ID,
                         seconds: int = 90) -> bool:
    with session_factory() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        lease = db.get(WorkerLease, name)
        current = now()
        if lease is not None and lease.owner != owner and _naive(lease.lease_until) > current:
            db.rollback()
            return False
        if lease is None:
            lease = WorkerLease(name=name, owner=owner, lease_until=current)
            db.add(lease)
        lease.owner, lease.lease_until = owner, current + timedelta(seconds=seconds)
        db.commit()
        return True


def release_worker_lease(session_factory: sessionmaker, name: str = "background", owner: str = WORKER_ID) -> None:
    with session_factory() as db:
        lease = db.get(WorkerLease, name)
        if lease is not None and lease.owner == owner:
            db.delete(lease)
            db.commit()


def run_schedulers(session_factory: sessionmaker) -> None:
    for scheduler in PERIODIC:
        try:
            with session_factory() as db:
                scheduler(db)
                db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Scheduler %s failed: %s", scheduler.__name__, _safe_error(exc))


async def worker_loop(session_factory: sessionmaker, poll_seconds: float = 5.0) -> None:
    """Background loop for the app process. Only the lease holder does work."""
    try:
        while True:
            if await asyncio.to_thread(acquire_worker_lease, session_factory):
                await asyncio.to_thread(run_schedulers, session_factory)
                await asyncio.to_thread(run_pending, session_factory)
            await asyncio.sleep(poll_seconds)
    finally:
        await asyncio.to_thread(release_worker_lease, session_factory)


def queue_summary(db: Session) -> dict:
    from sqlalchemy import func
    rows = db.execute(select(Job.kind, Job.state, func.count()).group_by(Job.kind, Job.state)).all()
    summary: dict[str, dict[str, int]] = {}
    for kind, state, count in rows:
        summary.setdefault(kind, {})[state] = count
    return summary
