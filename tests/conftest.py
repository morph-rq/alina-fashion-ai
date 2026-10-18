"""Keep background storage work out of the user's local backup history."""
import asyncio
import pytest


@pytest.fixture(autouse=True)
def isolate_backup_runtime(tmp_path, monkeypatch):
    from app import storage, main
    async def test_worker():
        await asyncio.Event().wait()
    monkeypatch.setattr(main, 'backup_worker', test_worker)
    monkeypatch.setattr(storage, 'PRIVATE', tmp_path/'storage-private')
    monkeypatch.setattr(storage, 'BACKUPS', tmp_path/'verified-backups')
    from app import intake_archive
    monkeypatch.setattr(intake_archive, 'ARCHIVE_DIR', tmp_path/'intake-sources')
