from pathlib import Path

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.database import get_db
from app.models import AllocationReview, ImportReview, OrderItem
from app.worksheet import audit


def source_items(db,review):
    # Match source filename as well as sheet/row; never assign across seasons by row alone.
    source = review.source_file if isinstance(review,AllocationReview) else review.import_batch.filename
    name = Path(source.replace('\\','/')).name.casefold()
    candidates = db.scalars(select(OrderItem).where(OrderItem.source_sheet == review.sheet,OrderItem.source_row == review.row)).all() if review.row else []
    return [i for i in candidates if Path((i.source_file or '').replace('\\','/')).name.casefold() == name]


def install_source_reviews(app,templates,context):
    def get_review(db,kind,identity):
        model = {'import':ImportReview,'allocation':AllocationReview}.get(kind)
        review = db.get(model,identity) if model else None
        if review is None: raise HTTPException(404,'Source review not found.')
        return review

    @app.get('/source-reviews')
    def listing(request: Request,kind: str='import',db=Depends(get_db)):
        kind = 'allocation' if kind == 'allocation' else 'import'
        model = AllocationReview if kind == 'allocation' else ImportReview
        reviews = db.scalars(select(model).where(model.status == 'Open').order_by(model.id)).all()
        return templates.TemplateResponse(request,'source_reviews.html',context(request,kind=kind,reviews=reviews))

    @app.get('/source-reviews/{kind}/{identity}')
    def detail(kind: str,identity: int,request: Request,db=Depends(get_db)):
        review = get_review(db,kind,identity)
        return templates.TemplateResponse(request,'source_reviews.html',context(request,kind=kind,review=review,items=source_items(db,review)))

    @app.post('/source-reviews/{kind}/{identity}/resolve')
    def resolve(kind: str,identity: int,resolution: str=Form(...),db=Depends(get_db)):
        review = get_review(db,kind,identity)
        if len(resolution.strip()) < 12 or len(resolution) > 5000:
            raise HTTPException(422,'Explain what you verified or corrected (12–5000 characters).')
        if review.status != 'Open':
            return RedirectResponse(f'/source-reviews/{kind}/{identity}',303)
        items = source_items(db,review)
        if kind == 'allocation':
            if not items or any(i.customer_id is None or i.allocation_status != 'Allocated' for i in items):
                raise HTTPException(422,'Allocate every matching source product to a confirmed customer first. If there are no exact matches, keep this note open and review the source importer.')
            ids = {i.customer_id for i in items}
            review.resolved_customer_id = next(iter(ids)) if len(ids)==1 else None
        review.status = 'Resolved'
        audit(db,type(review).__name__,review.id,f'Source review resolved by user: {resolution.strip()}')
        db.commit()
        return RedirectResponse(f'/source-reviews/{kind}/{identity}',303)
