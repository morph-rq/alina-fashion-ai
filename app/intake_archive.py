"""Keep original AI source files inside the backed-up local data directory."""
import hashlib
import json
import re
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from app.config import ROOT_DIR

ARCHIVE_DIR = ROOT_DIR/'data'/'intake-sources'


def bundle_path(token):
    if not re.fullmatch(r'[a-f0-9]{32}',token): raise ValueError('Invalid source archive reference.')
    path = ARCHIVE_DIR/token
    if not (path/'manifest.json').is_file(): raise ValueError('Source archive is missing. Analyze your attachment again.')
    return path


def archive_sources(message,uploads):
    token=secrets.token_hex(16)
    directory=ARCHIVE_DIR/token
    directory.mkdir(parents=True)
    entries=[]
    for index,(name,mime,content) in enumerate(uploads):
        safe_name=Path(name.replace('\\','/')).name
        filename=f'{index:02d}-'+re.sub(r'[^\w. -]','_',safe_name)[:150]
        (directory/filename).write_bytes(content)
        entries.append(dict(original_name=name,filename=filename,mime=mime,size=len(content),sha256=hashlib.sha256(content).hexdigest()))
    (directory/'manifest.json').write_text(json.dumps(dict(message=message,files=entries),ensure_ascii=False),encoding='utf-8')
    return token


def install_intake_sources(app,templates,context):
    @app.get('/intake-sources/{token}')
    def page(token: str,request: Request):
        try:
            data=json.loads((bundle_path(token)/'manifest.json').read_text(encoding='utf-8'))
        except ValueError as exc: raise HTTPException(404,str(exc)) from None
        return templates.TemplateResponse(request,'intake_sources.html',context(request,token=token,archive=data))

    @app.get('/intake-sources/{token}/{index}')
    def download(token: str,index: int):
        try:
            directory=bundle_path(token)
            entries=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))['files']
            if index < 0 or index >= len(entries): raise ValueError('Source attachment not found.')
            entry=entries[index]
        except ValueError as exc: raise HTTPException(404,str(exc)) from None
        return FileResponse(directory/entry['filename'],filename=entry['filename'],media_type='application/octet-stream')
