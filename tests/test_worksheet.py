from datetime import date
from decimal import Decimal as D
from pathlib import Path
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.config import settings
from app.main import app
from app.models import (Brand, Customer, Distributor, Order, OrderItem, PurchaseGroup,
                        Season, SellerCreditAccount, SellerCreditEntry)
from app.worksheet import (account_balance, get_account, group_amounts, split_cost,
                           transfer_credit, worksheet_data)
from app import google_api, recovery, secret_store


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'app_username', '')
    monkeypatch.setattr(settings, 'app_password', '')
    engine = create_engine('sqlite:///'+str(tmp_path/'ledger.db'), connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        season, brand, customer, distributor = Season(name='SS26'), Brand(name='Atelier'), Customer(code='AA'), Distributor(name='Distributor A')
        db.add_all([season,brand,customer,distributor]);db.flush()
        order=Order(season_id=season.id,brand_id=brand.id)
        first=PurchaseGroup(name='White group',season_id=season.id,distributor_id=distributor.id,seller='Seller One',special_cost=D('10'))
        second=PurchaseGroup(name='Replacement',season_id=season.id,distributor_id=distributor.id,seller='Seller One')
        db.add_all([order,first,second]);db.flush()
        for group,state,price in [(first,'pending','100'),(first,'pending','200'),(second,'process','300')]:
            db.add(OrderItem(order_id=order.id,customer_id=customer.id,purchase_group_id=group.id,
                            quantity=1,wholesale_unit_price=D(price),markup_percentage=D(0),worksheet_status=state))
        db.commit()
        yield db,first,second,customer
    engine.dispose()


def seed_credit(db, group, amount='110'):
    item=group.items[0]
    account=get_account(db,group,item)
    db.add(SellerCreditEntry(account_id=account.id,amount=D(amount),kind='prepayment',stage='shipment',date=date.today(),reference='BANK-1',operation_key='seed'))
    db.commit();db.expire_all()
    return db.get(SellerCreditAccount,account.id)


def test_50_plus_5_is_one_shipment_advance(ledger):
    db,first,_,_=ledger
    amounts=group_amounts(first)
    assert amounts.initial==D('150')
    assert amounts.shipment==D('165')
    assert amounts.transport==D('15')
    assert amounts.total==D('325')  # 300 products + 15 transport + 10 special
    assert worksheet_data(db)['held']==0  # Required is not paid.


@pytest.mark.parametrize('method',['value','quantity','equal'])
def test_group_cost_allocations_conserve_every_cent(ledger,method):
    db,first,_,_=ledger
    for total in ['0.01','0.05','10','19.99','203.87']:
        shares=split_cost(D(total),first.items,method)
        assert sum(shares.values())==D(total)
        assert all(v>=0 for v in shares.values())


def test_white_product_credit_is_not_lost_and_transfer_is_balanced(ledger):
    db,first,second,customer=ledger
    source=seed_credit(db,first)
    transfer_credit(db,source,second,D('80'),'Replacement product','transfer-test')
    db.commit();db.expire_all()
    target=db.scalar(select(SellerCreditAccount).where(SellerCreditAccount.group_id==second.id))
    assert account_balance(source)==D('30')
    assert account_balance(target)==D('80')
    assert target.customer_id==customer.id
    assert worksheet_data(db)['held']==D('110')
    assert worksheet_data(db)['available']==D('30')
    assert source.item.worksheet_status=='pending'


def test_credit_cannot_be_spent_twice_or_cross_seller(ledger):
    db,first,second,_=ledger
    source=seed_credit(db,first)
    with pytest.raises(ValueError,match='exceeds'):
        transfer_credit(db,source,second,D('111'),'Too much','too-much')
    second.seller='Different seller'
    with pytest.raises(ValueError,match='same seller'):
        transfer_credit(db,source,second,D('10'),'Wrong seller','wrong-seller')


def test_credit_never_changes_customer_ownership(ledger):
    db,first,second,_=ledger
    source=seed_credit(db,first)
    other=Customer(code='BB');db.add(other);db.flush()
    second.items[0].customer_id=other.id;db.commit()
    with pytest.raises(ValueError,match='no products for this customer'):
        transfer_credit(db,source,second,D('10'),'Wrong customer','wrong-customer')


def test_cancelled_rows_remain_in_workbook(ledger):
    db,first,_,_=ledger
    first.items[0].status='Cancelled';db.commit()
    data=worksheet_data(db)
    assert data['total_items']==3
    assert sum(data['counts'].values())==3


def test_new_pages_render_all_items_and_filter_customer(ledger):
    db,first,_,_=ledger
    app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(app)
        for path in ['/','/orders','/worksheet','/groups','/credits','/worksheet/items/1','/storage']:
            response=client.get(path)
            assert response.status_code==200,(path,response.text[:300])
        assert client.get('/worksheet').text.count('class="product-row"')==3
        assert client.get('/worksheet?state=pending').text.count('class="product-row"')==2
        assert client.get('/worksheet?customer=9999').text.count('class="product-row"')==0
        assert client.get('/worksheet?q=zzzz').text.count('class="product-row"')==0
    finally:
        app.dependency_overrides.clear()


def test_posted_payments_are_idempotent_and_reject_nan(ledger):
    db,first,_,_=ledger
    app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(app)
        body=dict(group_id=first.id,amount='110',stage='shipment',payment_date=str(date.today()),reference='BANK-POST',token='one-submit')
        db.rollback()
        assert client.post('/credits/prepay',data=body,follow_redirects=False).status_code==303
        db.rollback()
        assert client.post('/credits/prepay',data=body,follow_redirects=False).status_code==303
        db.rollback()
        assert len(db.scalars(select(SellerCreditEntry)).all())==1
        db.rollback()
        assert client.post('/credits/prepay',data=body|{'amount':'NaN','token':'different'},follow_redirects=False).status_code==422
    finally:
        db.rollback();app.dependency_overrides.clear()


def test_group_costs_reach_customer_statement_once(ledger):
    from app.services import customer_statement,dashboard_data
    db,first,_,customer=ledger
    statement=customer_statement(db,customer.id,'SS26')
    assert statement.group_special_costs==D('10')
    assert statement.allocated_costs==D('10')
    assert dashboard_data(db,'SS26').expenses_total==D('10')


def test_verified_snapshot_contains_data_not_credentials(ledger,tmp_path):
    db,*_=ledger
    root=tmp_path/'workspace';(root/'data'/'imports').mkdir(parents=True)
    (root/'data'/'private').mkdir()
    (root/'data'/'private'/'token.txt').write_text('secret')
    (root/'data'/'imports'/'source.txt').write_text('source document')
    snapshot=recovery.create_snapshot(db,reason='test',root=root)
    manifest=recovery.verify_snapshot(snapshot.path)
    assert 'data/fashion_orders.db' in manifest['files']
    assert 'data/imports/source.txt' in manifest['files']
    assert not any('private' in name for name in manifest['files'])
    assert manifest['table_counts']['order_items']==3


def test_secret_store_round_trip_excludes_plaintext(tmp_path,monkeypatch):
    monkeypatch.setattr(secret_store,'PRIVATE',tmp_path)
    credentials={'client_id':'test-client','refresh_token':'private-secret'}
    secret_store.save('google-test',credentials)
    assert b'private-secret' not in (tmp_path/'secrets'/'google-test.bin').read_bytes()
    assert secret_store.load('google-test')==credentials


def test_cross_origin_write_is_rejected(ledger):
    response=TestClient(app).post('/credits/prepay',headers={'Origin':'https://unrelated.example'},data={})
    assert response.status_code==403


def test_export_retains_colors_and_new_ledger_sheets(ledger):
    from io import BytesIO
    import openpyxl
    from app.excel_export import build_business_workbook
    db,*_=ledger
    workbook=openpyxl.load_workbook(BytesIO(build_business_workbook(db)),data_only=False)
    assert {'Shipment groups','Seller accounts','Seller ledger'} <= set(workbook.sheetnames)
    assert workbook['Products']['Z5'].value=='Not completed'
    assert workbook['Products']['A5'].fill.fgColor.rgb.endswith('FFFFFF')


def test_concurrent_transfers_cannot_overdraw(ledger):
    from concurrent.futures import ThreadPoolExecutor
    from app.worksheet import lock_ledger
    db,first,second,_=ledger
    source=seed_credit(db,first)
    source_id,target_id=source.id,second.id
    engine=db.get_bind()
    db.rollback()
    def attempt(index):
        with Session(engine) as session:
            lock_ledger(session)
            account=session.get(SellerCreditAccount,source_id)
            target=session.get(PurchaseGroup,target_id)
            try:
                transfer_credit(session,account,target,D('80'),'Concurrent test',f'concurrent-{index}')
                session.commit()
                return 'ok'
            except ValueError:
                session.rollback()
                return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes=list(executor.map(attempt,[1,2]))
    assert sorted(outcomes)==['blocked','ok']
    db.expire_all()
    assert account_balance(db.get(SellerCreditAccount,source_id))==D('30')


@pytest.mark.parametrize('matches',[True,False])
def test_drive_requires_matching_checksum_before_success(tmp_path,monkeypatch,matches):
    import hashlib
    import httpx
    path=tmp_path/'archive.afra';path.write_bytes(b'verified archive fixture')
    monkeypatch.setattr(secret_store,'PRIVATE',tmp_path/'private')
    monkeypatch.setattr(settings,'google_client_id','client.apps.googleusercontent.com')
    monkeypatch.setattr(settings,'google_client_secret','secret')
    secret_store.save('google-t',{'refresh_token':'refresh'})
    checksum=hashlib.md5(path.read_bytes()).hexdigest()
    def handler(request):
        if request.url.host=='oauth2.googleapis.com':
            return httpx.Response(200,json={'access_token':'access','expires_in':3600})
        if request.method=='POST':return httpx.Response(200,headers={'Location':'https://www.googleapis.com/upload/session'})
        if request.method=='PUT':return httpx.Response(200,json={'id':'remote-1','md5Checksum':checksum if matches else 'incorrect'})
        raise AssertionError(str(request.url))
    session=google_api.GoogleSession('google-t',transport=httpx.MockTransport(handler),sleep=lambda _s:None)
    drive=google_api.DriveClient(session)
    if matches:
        assert drive.upload('folder-1','archive.afra',path,'application/octet-stream','abc')['id']=='remote-1'
    else:
        with pytest.raises(google_api.GoogleError,match='checksum'):
            drive.upload('folder-1','archive.afra',path,'application/octet-stream','abc')


def test_invalid_google_callback_is_rejected(ledger):
    db,*_=ledger
    app.dependency_overrides[get_db]=lambda:(yield db)
    try:
        response=TestClient(app).get('/connections/google/callback?state=forged&code=fake',follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code==303 and 'could+not+be+verified' in response.headers['location']
    from app.models import Connection
    assert db.scalar(select(Connection))is None


def test_custom_group_payment_percentages_reach_statements(ledger):
    from app.services import customer_statement,order_summary
    db,first,_,customer=ledger
    first.initial_percentage=D('.6');first.shipment_percentage=D('.4');db.commit()
    assert group_amounts(first).initial==D('180')
    assert group_amounts(first).shipment==D('135')
    assert customer_statement(db,customer.id,'SS26').initial_prepayment==D('330')
    assert order_summary(first.items[0].order).amounts.required_prepayment==D('330')
