"""Write protection middleware and the retired storage URLs.

Backups moved to ``app.recovery`` (consistent snapshots), ``app.recovery_crypto``
(portable encryption) and ``app.outbox`` (verified Drive uploads of encrypted
archives only). Google connections moved to ``/connections``. The previous
storage implementation uploaded unencrypted snapshot ZIPs to Drive and was
removed; ZIPs it created in ``backups/verified`` remain downloadable here.
"""
import hashlib
import json
from pathlib import Path
import zipfile

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.config import ROOT_DIR

LEGACY_BACKUPS = ROOT_DIR / 'backups' / 'verified'


def verify_legacy_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        if archive.testzip():
            raise ValueError('Backup archive CRC verification failed.')
        for name, expected in manifest['files'].items():
            if name.startswith(('/', '\\')) or '..' in Path(name).parts or ':' in name:
                raise ValueError('Unsafe file path in backup.')
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError('Backup file checksum mismatch.')
        return manifest


def install_storage(app, templates, context):
    @app.middleware('http')
    async def reject_cross_origin_writes(request: Request, call_next):
        # Reject browser cross-origin writes, including forms submitted from another website.
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Cross-origin writes are not allowed.'}, status_code=403)
        return await call_next(request)

    @app.get('/storage')
    def storage_page():
        return RedirectResponse('/recovery', 303)

    @app.post('/storage/backup')
    def backup_now():
        return RedirectResponse('/recovery', 303)

    @app.api_route('/storage/google/{rest:path}', methods=['GET', 'POST'])
    def google_moved(rest: str):
        return RedirectResponse('/connections', 303)

    @app.get('/storage/download/{filename}')
    def download_legacy_backup(filename: str):
        if Path(filename).name != filename or not filename.startswith('fashion-') or not filename.endswith('.zip'):
            raise HTTPException(404, 'Backup not found.')
        path = LEGACY_BACKUPS / filename
        if not path.is_file():
            raise HTTPException(404, 'Backup not found.')
        verify_legacy_zip(path)
        return FileResponse(path, filename=filename, media_type='application/zip')
