"""Test isolation.

Settings are read from the workspace .env when ``app.config`` is first imported, so
credentials, the development database and live AI/search endpoints are neutralised
here, before any test module imports the application.
"""
import asyncio
import os
import tempfile
from pathlib import Path

_ISOLATED_DB = Path(tempfile.mkdtemp(prefix='fashion-tests-')) / 'unused.db'
os.environ.update({
    'FASHION_DATABASE_URL': f'sqlite:///{_ISOLATED_DB.as_posix()}',
    'FASHION_APP_USERNAME': '',
    'FASHION_APP_PASSWORD': '',
    'FASHION_AI_API_KEY': '',
    'FASHION_GEMINI_API_KEY': '',
    'FASHION_GOOGLE_CSE_API_KEY': '',
    'FASHION_GOOGLE_CSE_ID': '',
    'FASHION_SEARXNG_URL': '',
    'FASHION_GOOGLE_CLIENT_ID': '',
    'FASHION_GOOGLE_CLIENT_SECRET': '',
    'FASHION_RECOVERY_PASSPHRASE': '',
    'FASHION_BACKGROUND_WORKER': '0',
    'ELEVENLABS_API_KEY': '',
})

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_backup_runtime(tmp_path, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage, 'LEGACY_BACKUPS', tmp_path/'verified-backups')
    from app import intake_archive
    monkeypatch.setattr(intake_archive, 'ARCHIVE_DIR', tmp_path/'intake-sources')
    # Secrets, recovery state, snapshots, outbox files and stored sources stay inside the test's folder.
    from app import outbox, recovery, secret_store, source_store
    monkeypatch.setattr(secret_store, 'PRIVATE', tmp_path/'private')
    monkeypatch.setattr(recovery, 'ROOT_DIR', tmp_path)
    monkeypatch.setattr(outbox, 'ROOT_DIR', tmp_path)
    monkeypatch.setattr(outbox, 'OUTBOX_DIR', tmp_path/'data'/'outbox')
    monkeypatch.setattr(outbox, 'ENCRYPTED_DIR', tmp_path/'backups'/'recovery'/'encrypted')
    monkeypatch.setattr(source_store, 'ROOT_DIR', tmp_path)
    # Generated customer/internal documents never land in the workspace's data folder.
    from app import customer_documents, selective_export
    monkeypatch.setattr(customer_documents, 'ROOT_DIR', tmp_path)
    monkeypatch.setattr(selective_export, 'ROOT_DIR', tmp_path)
    # The assistant's workbook index and dropped files stay in the test's folder too.
    from app import factory_routes, workbook_inspect
    monkeypatch.setattr(workbook_inspect, 'CACHE_DIR', tmp_path/'cache'/'inspect')
    monkeypatch.setattr(workbook_inspect, 'ROOT_DIR', tmp_path)
    monkeypatch.setattr(factory_routes, 'IMPORT_DIR', tmp_path/'data'/'imports')
