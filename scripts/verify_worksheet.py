"""Verify the local workspace and remove only empty, test-created snapshots."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import ROOT_DIR
from app.database import SessionLocal,engine
from app.models import ImportBatch
from app.worksheet import worksheet_data
from app.services import dashboard_data
from app.storage import BACKUPS,create_snapshot,verify_snapshot,state_update,read_state
from sqlalchemy import select

assert ROOT_DIR.name=='alina-fashion-local'
assert Path(engine.url.database).resolve().is_relative_to(ROOT_DIR.resolve())
audit=json.loads((ROOT_DIR/'tmp'/'worksheet-import-audit.json').read_text('utf-8'))
preserve={audit['before_backup'],audit['after_backup']}
removed=0
for candidate in BACKUPS.glob('fashion-*.zip'):
    assert candidate.resolve().parent == BACKUPS.resolve()
    if candidate.name in preserve or candidate.name < audit['after_backup']:
        continue
    with zipfile.ZipFile(candidate) as archive:
        payload=archive.read('data/fashion_orders.db')
    connection=sqlite3.connect(':memory:')
    try:
        connection.deserialize(payload)
        product_count=connection.execute('SELECT COUNT(*) FROM order_items').fetchone()[0]
        season_count=connection.execute('SELECT COUNT(*) FROM seasons').fetchone()[0]
    finally:
        connection.close()
    if product_count==0 and season_count==0:
        candidate.unlink()  # Only empty regression-database snapshots created in this task.
        removed+=1
with SessionLocal() as db:
    data=worksheet_data(db)
    total=dashboard_data(db,'SS26').totals.final
    original=Path('C:/Users/user/Downloads/Отчет_SS26 AA.xlsx')
    digest=hashlib.sha256(original.read_bytes()).hexdigest()
    source=db.scalar(select(ImportBatch).where(ImportBatch.sha256==digest))
    assert source and data['total_items']==143 and str(total)=='114599.25'
    assert len({r.item.id for r in data['rows']})==143
    assert sum(data['counts'].values())==143
    assert all(r.item.purchase_group_id for r in data['rows'])
    assert not data['accounts'], 'Never leave simulated seller money in the real preview.'
    result={'records':data['total_items'],'units':data['units'],'status_counts':dict(data['counts']),
            'groups':len(data['groups']),'source_order_total':str(total),'source_unchanged':True,
            'test_only_snapshots_removed':removed,'seller_credit_not_inferred':True}
backup=create_snapshot()
manifest=verify_snapshot(backup)
state_update(backed_up_dirty_at=read_state().get('dirty_at',0))
result.update(verified_backup=backup.name,backup_files=len(manifest['files']))
(ROOT_DIR/'tmp'/'worksheet-verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
