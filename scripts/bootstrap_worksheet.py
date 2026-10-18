"""Import the supplied SS26 workbook into the isolated local app only."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func
from app.config import ROOT_DIR
from app.database import SessionLocal, init_db, engine
from app.importer import import_workbook, SPECS
from app.models import OrderItem, ImportBatch
from app.storage import create_snapshot, mark_dirty
from app.worksheet import worksheet_data

assert ROOT_DIR.name == 'alina-fashion-local', 'This script is only for the isolated local app.'
assert Path(engine.url.database).resolve().is_relative_to(ROOT_DIR.resolve()), 'Database must be inside local workspace.'
source = Path('C:/Users/user/Downloads/Отчет_SS26 AA.xlsx')
original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
before = create_snapshot()
init_db()
destination = ROOT_DIR / 'data' / 'imports' / 'ss26-original' / source.name
destination.parent.mkdir(parents=True,exist_ok=True)
if not destination.exists():
    shutil.copy2(source,destination)
assert hashlib.sha256(destination.read_bytes()).hexdigest() == original_hash
with SessionLocal() as db:
    summary = import_workbook(db,destination)
    db.commit()
    data = worksheet_data(db)
    summary.update(counts=dict(data['counts']), units=data['units'], records=data['total_items'],
                   groups=len(data['groups']), retained_reference_rows=[(r.item.source_sheet,r.item.source_row)
                   for r in data['rows'] if (r.item.source_sheet=='THE ROW pre' and r.item.source_row in [13,14,15])
                   or (r.item.source_sheet=='DIOR CRUIZ SSv 26' and r.item.source_row==5)])
mark_dirty()
after = create_snapshot()
assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
summary.update(before_backup=before.name, after_backup=after.name, source_unchanged=True)
(ROOT_DIR/'tmp'/'worksheet-import-audit.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
