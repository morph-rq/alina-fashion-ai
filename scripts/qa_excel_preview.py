"""Synthetic local UI fixture only. Never points at the user's order database."""
import asyncio
import os
import sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
os.environ['FASHION_DATABASE_URL']='sqlite:///'+(root/'tmp'/'ui-excel-fixture.db').as_posix()
os.environ['FASHION_APP_USERNAME']=' '
os.environ['FASHION_APP_NAME']='ALINA · synthetic UI check'

from decimal import Decimal
from sqlalchemy import select
from app import main,ai_intake,intake_archive,excel_exchange
from app.database import Base,engine,SessionLocal
from app.models import Season,Brand,BrandSeasonRule,Customer,Order,OrderItem,PurchaseGroup

Base.metadata.create_all(engine)
with SessionLocal() as db:
    if not db.scalar(select(Season)):
        s=Season(name='SS26');b=Brand(name='Sample Atelier');c=Customer(code='SC',name='Sample Customer')
        db.add_all([s,b,c]);db.flush()
        rule=BrandSeasonRule(season_id=s.id,brand_id=b.id,markup_percentage=Decimal('.35'))
        o=Order(season_id=s.id,brand_id=b.id);g=PurchaseGroup(name='Sample shipment group',season_id=s.id)
        db.add_all([rule,o,g]);db.flush()
        for state,name in [('process','Cotton blouse'),('done','Evening dress'),('pending','Wool coat')]:
            db.add(OrderItem(order_id=o.id,customer_id=c.id,purchase_group_id=g.id,name=name,article='SAMPLE-'+state,color='Black',size='M',quantity=1,wholesale_unit_price=Decimal('200'),markup_percentage=Decimal('.35'),worksheet_status=state,status='Ordered'))
        db.commit()

async def idle_worker(): await asyncio.Event().wait()
main.backup_worker=idle_worker
intake_archive.ARCHIVE_DIR=root/'tmp'/'qa-intake-sources'
excel_exchange.EXCHANGE_DIR=root/'tmp'/'qa-excel-exchange'
ai_intake.analyze_submission=lambda *_:ai_intake.IntakeBundle(
    orders=[ai_intake.IntakeFacts(customer_name='Sample Customer',brand='Sample Atelier',product_name='Silk blouse',article='TEST-101',color='Ivory',explicit_size='M',quantity=2,stated_price='200',price_type='wholesale'),
            ai_intake.IntakeFacts(customer_name='Sample Customer',brand='Sample Atelier',product_name='Wool coat',quantity=1,missing_or_ambiguous=['Confirm exact size','Confirm price and whether it is wholesale or final'])],
    source_summary='Synthetic UI fixture: two sample products, with one row requiring confirmation.')

if __name__=='__main__':
    import uvicorn
    uvicorn.run(main.app,host='127.0.0.1',port=8001)
