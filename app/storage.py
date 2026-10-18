"""Versioned verified snapshots and opt-in Google Drive backup.

Credentials use Windows DPAPI and never enter backups. Drive access is limited
to app-created files. A remote copy is successful only after checksum matching.
"""
import asyncio
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse
import zipfile
from contextlib import closing

import httpx
from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app.config import ROOT_DIR, settings
from app.database import engine

PRIVATE = ROOT_DIR / 'data' / 'private'
BACKUPS = ROOT_DIR / 'backups' / 'verified'
SCOPE = 'https://www.googleapis.com/auth/drive.file'
CALLBACK = 'http://127.0.0.1:8001/storage/google/callback'
_lock = threading.Lock()
_state_lock = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp-'+secrets.token_hex(4))
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def read_state():
    path = PRIVATE / 'storage-state.json'
    return json.loads(path.read_text('utf-8')) if path.exists() else {}


def state_update(**kwargs):
    with _state_lock:
        value = read_state()
        value.update(kwargs)
        atomic_json(PRIVATE / 'storage-state.json', value)
        return value


def protect(payload, decrypt=False):
    if os.name != 'nt':
        raise ValueError('This local Google connection uses Windows-protected credentials. Configure a platform secret store before hosting elsewhere.')
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    api.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    api.restype = ctypes.c_int
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError('Windows could not access protected Google credentials.')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def load_credentials():
    path = PRIVATE / 'google-drive.bin'
    if not path.exists():
        return {}
    return json.loads(protect(path.read_bytes(), decrypt=True))


def save_credentials(value):
    PRIVATE.mkdir(parents=True, exist_ok=True)
    target = PRIVATE / 'google-drive.bin'
    temp = target.with_suffix('.tmp')
    temp.write_bytes(protect(json.dumps(value).encode()))
    os.replace(temp, target)


def file_hash(path, algorithm='sha256'):
    digest = hashlib.new(algorithm)
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_snapshot(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        if archive.testzip():
            raise ValueError('Backup archive CRC verification failed.')
        for name, expected in manifest['files'].items():
            if name.startswith(('/', '\\')) or '..' in Path(name).parts or ':' in name:
                raise ValueError('Unsafe file path in backup.')
            actual = hashlib.sha256(archive.read(name)).hexdigest()
            if actual != expected:
                raise ValueError('Backup file checksum mismatch.')
        if set(archive.namelist()) != set(manifest['files']) | {'manifest.json'}:
            raise ValueError('Unlisted files found in backup.')
        return manifest


def create_snapshot():
    if engine.dialect.name != 'sqlite':
        raise ValueError('Verified snapshot backup currently requires SQLite.')
    BACKUPS.mkdir(parents=True, exist_ok=True)
    filename = 'fashion-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+secrets.token_hex(3)+'.zip'
    destination = BACKUPS / filename
    pending = destination.with_suffix('.partial')
    files = {}
    try:
        with tempfile.TemporaryDirectory(prefix='snapshot-', dir=BACKUPS) as scratch:
            db_copy = Path(scratch)/'fashion_orders.db'
            raw = engine.raw_connection()
            try:
                with closing(sqlite3.connect(db_copy)) as output:
                    raw.driver_connection.backup(output)
                    if output.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Database integrity check failed.')
            finally:
                raw.close()
            with zipfile.ZipFile(pending, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(db_copy, 'data/fashion_orders.db')
                files['data/fashion_orders.db'] = file_hash(db_copy)
                for folder in ('data', 'outputs', 'output'):
                    root = ROOT_DIR / folder
                    if not root.exists():
                        continue
                    for file in root.rglob('*'):
                        if not file.is_file() or file.is_symlink():
                            continue
                        resolved = file.resolve()
                        if not resolved.is_relative_to(ROOT_DIR.resolve()):
                            continue
                        relative = file.relative_to(ROOT_DIR).as_posix()
                        if relative.startswith(('data/private/', 'data/staged-images/')) or file.suffix in {'.db','.db-wal','.db-shm'} or file.name.endswith(('-wal','-shm')):
                            continue
                        payload = file.read_bytes()
                        archive.writestr(relative, payload)
                        files[relative] = hashlib.sha256(payload).hexdigest()
                manifest = {'version':1, 'created_at':now(), 'files':files,
                            'includes':'Database, customers, products, payments, credit ledger, imports, images, library, reports.',
                            'excludes':'Passwords, OAuth tokens, application source code and unconfirmed staging uploads.'}
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        verify_snapshot(pending)
        os.replace(pending, destination)
        state_update(last_local_at=now(), last_local_file=filename, last_local_sha256=file_hash(destination),
                     last_local_size=destination.stat().st_size, local_error=None)
        return destination
    finally:
        pending.unlink(missing_ok=True)


def access_token(client, credentials):
    response = client.post('https://oauth2.googleapis.com/token', data={
        'client_id':credentials['client_id'], 'client_secret':credentials['client_secret'],
        'refresh_token':credentials['refresh_token'], 'grant_type':'refresh_token'})
    response.raise_for_status()
    return response.json()['access_token']


def upload_snapshot(path):
    credentials = load_credentials()
    if not credentials.get('refresh_token'):
        raise ValueError('Google Drive is not connected. Complete Google sign-in first.')
    checksum = file_hash(path, 'md5')
    sha256 = file_hash(path)
    with httpx.Client(timeout=45, follow_redirects=False) as client:
        headers = {'Authorization':'Bearer '+access_token(client, credentials)}
        folder = credentials.get('folder_id')
        if not folder:
            response = client.post('https://www.googleapis.com/drive/v3/files', headers=headers,
                json={'name':'ALINA Fashion Backups', 'mimeType':'application/vnd.google-apps.folder'}, params={'fields':'id'})
            response.raise_for_status()
            folder = response.json()['id']
            credentials['folder_id'] = folder
            save_credentials(credentials)
        # Idempotency after an uncertain upload: look for this content before creating another copy.
        result = client.get('https://www.googleapis.com/drive/v3/files', headers=headers, params={
            'q':f"'{folder}' in parents and trashed = false and appProperties has {{ key='sha256' and value='{sha256}' }}",
            'fields':'files(id,md5Checksum,name)', 'pageSize':100})
        result.raise_for_status()
        remote = next((f for f in result.json().get('files',[]) if f.get('md5Checksum') == checksum), None)
        if remote is None:
            response = client.post('https://www.googleapis.com/upload/drive/v3/files',
                params={'uploadType':'resumable','fields':'id,md5Checksum,name'}, headers=headers | {
                    'X-Upload-Content-Type':'application/zip', 'X-Upload-Content-Length':str(path.stat().st_size)},
                json={'name':path.name, 'parents':[folder], 'appProperties':{'sha256':sha256}})
            response.raise_for_status()
            location = response.headers['Location']
            parsed = urlparse(location)
            if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com':
                raise ValueError('Google returned an unexpected upload endpoint.')
            size = path.stat().st_size
            offset = 0
            with path.open('rb') as stream:
                while offset < size:
                    chunk = stream.read(8*1024*1024)
                    uploaded = client.put(location, headers=headers | {'Content-Type':'application/zip',
                        'Content-Range':f'bytes {offset}-{offset+len(chunk)-1}/{size}'}, content=chunk)
                    if uploaded.status_code == 308:
                        offset += len(chunk)
                        continue
                    uploaded.raise_for_status()
                    remote = uploaded.json()
                    offset += len(chunk)
            if not remote or remote.get('md5Checksum') != checksum:
                raise ValueError('Google Drive checksum did not match the local backup.')
        state_update(last_drive_at=now(), last_drive_file=path.name, last_drive_id=remote['id'],
                     drive_folder=folder, drive_error=None)


def mark_dirty():
    state_update(dirty_at=time.time())


def backup_cycle(force=False):
    if not _lock.acquire(blocking=False):
        return
    try:
        state = read_state()
        dirty = state.get('dirty_at',0)
        filename = state.get('last_local_file')
        path = BACKUPS/filename if filename else None
        if force or dirty > state.get('backed_up_dirty_at',0) or not path or not path.exists():
            try:
                path = create_snapshot()
                state_update(backed_up_dirty_at=dirty)
            except Exception as exc:
                state_update(local_error=f'Local backup failed ({type(exc).__name__}). Records remain in the database; retry backup.')
                return
        try:
            credentials = load_credentials()
            state = read_state()
            if credentials.get('refresh_token') and state.get('last_drive_file') != path.name:
                upload_snapshot(path)
        except Exception as exc:
            # Avoid putting HTTP response bodies, secrets or upload URLs into logs/UI.
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            state_update(drive_error=f'Drive backup failed ({status or type(exc).__name__}). Local backup is safe; automatic retry while app runs.')
    finally:
        _lock.release()


async def backup_worker():
    while True:
        await asyncio.to_thread(backup_cycle)
        await asyncio.sleep(30)


def install_storage(app, templates, context):
    @app.middleware('http')
    async def track_saved_changes(request: Request, call_next):
        # Reject browser cross-origin writes, including forms submitted from another website.
        if request.method in {'POST','PUT','PATCH','DELETE'}:
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail':'Cross-origin writes are not allowed.'}, status_code=403)
        response = await call_next(request)
        if request.method in {'POST','PUT','PATCH','DELETE'} and response.status_code < 400 and not request.url.path.startswith('/storage'):
            mark_dirty()
        return response

    @app.get('/storage')
    def storage_page(request: Request):
        try:
            credentials = load_credentials()
        except ValueError:
            credentials = {}
        return templates.TemplateResponse(request, 'storage.html', context(request, state=read_state(),
            configured=bool(credentials.get('client_id')), connected=bool(credentials.get('refresh_token')),
            backups=sorted(BACKUPS.glob('*.zip'), reverse=True)[:30], callback=CALLBACK))

    @app.post('/storage/backup')
    def backup_now():
        backup_cycle(force=True)
        return RedirectResponse('/storage',303)

    @app.get('/storage/download/{filename}')
    def download_backup(filename: str):
        if Path(filename).name != filename or not filename.startswith('fashion-') or not filename.endswith('.zip'):
            raise HTTPException(404,'Backup not found.')
        path = BACKUPS / filename
        if not path.is_file():
            raise HTTPException(404,'Backup not found.')
        verify_snapshot(path)
        return FileResponse(path, filename=filename, media_type='application/zip')

    @app.post('/storage/google/configure')
    async def configure_google(file: UploadFile = File(...)):
        payload = await file.read(65537)
        try:
            if len(payload)>65536:
                raise ValueError()
            config = json.loads(payload)['web']
            if CALLBACK not in config.get('redirect_uris',[]) or not config.get('client_id','').endswith('.apps.googleusercontent.com') or not config.get('client_secret'):
                raise ValueError()
            existing = load_credentials()
            if existing.get('refresh_token'):
                raise HTTPException(422,'Disconnect Google Drive before replacing its application credentials.')
            save_credentials({'client_id':config['client_id'], 'client_secret':config['client_secret']})
        except (KeyError,ValueError,TypeError):
            raise HTTPException(422,'Upload a Google OAuth web-client JSON with the exact callback shown on this page. Credentials are protected with Windows encryption.')
        return RedirectResponse('/storage',303)

    @app.post('/storage/google/connect')
    def google_connect():
        credentials = load_credentials()
        if not credentials.get('client_id'):
            raise HTTPException(422,'Configure the Google OAuth application first.')
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        atomic_json(PRIVATE/'oauth-state.json', {'hash':hashlib.sha256(state.encode()).hexdigest(),
                    'verifier':verifier, 'expires':time.time()+600})
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        response = RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({
            'client_id':credentials['client_id'], 'redirect_uri':CALLBACK, 'scope':SCOPE,
            'response_type':'code', 'access_type':'offline', 'prompt':'consent', 'state':state,
            'code_challenge':challenge, 'code_challenge_method':'S256'}),303)
        response.set_cookie('fashion_oauth_state',state,httponly=True,samesite='lax',max_age=600,path='/storage/google')
        return response

    @app.get('/storage/google/callback')
    def google_callback(request: Request, state: str = '', code: str = '', error: str = ''):
        pending_file = PRIVATE/'oauth-state.json'
        pending = json.loads(pending_file.read_text()) if pending_file.exists() else {}
        cookie = request.cookies.get('fashion_oauth_state','')
        if not state or not secrets.compare_digest(cookie,state) or pending.get('expires',0)<time.time() or not secrets.compare_digest(pending.get('hash',''),hashlib.sha256(state.encode()).hexdigest()):
            raise HTTPException(400,'Google sign-in expired or could not be verified. Start again from Storage.')
        pending_file.unlink(missing_ok=True)
        if error or not code:
            return RedirectResponse('/storage?connection=cancelled',303)
        credentials = load_credentials()
        with httpx.Client(timeout=30) as client:
            response = client.post('https://oauth2.googleapis.com/token', data={
                'code':code,'client_id':credentials['client_id'],'client_secret':credentials['client_secret'],
                'redirect_uri':CALLBACK,'grant_type':'authorization_code','code_verifier':pending['verifier']})
        if response.status_code != 200 or not response.json().get('refresh_token'):
            raise HTTPException(400,'Google did not authorize offline backups. Reconnect and grant the requested file permission.')
        credentials['refresh_token'] = response.json()['refresh_token']
        save_credentials(credentials)
        state_update(drive_error=None, last_drive_file=None)
        mark_dirty()
        result = RedirectResponse('/storage',303)
        result.delete_cookie('fashion_oauth_state',path='/storage/google')
        return result

    @app.post('/storage/google/disconnect')
    def disconnect_google():
        with _lock:
            credentials = load_credentials()
            token = credentials.pop('refresh_token',None)
            credentials.pop('folder_id',None)
            save_credentials(credentials)
            revoked = False
            if token:
                try:
                    with httpx.Client(timeout=15) as client:
                        revoked = client.post('https://oauth2.googleapis.com/revoke',data={'token':token}).status_code==200
                except httpx.HTTPError:
                    pass
            state_update(drive_error=None if revoked else 'Disconnected locally. You can also revoke access in your Google account settings.')
        return RedirectResponse('/storage',303)
