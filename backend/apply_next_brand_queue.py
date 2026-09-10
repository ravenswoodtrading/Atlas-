"""Apply approved brand additions after Codex runtime is available."""
from app.services.scan_queue_service import ScanQueueService
from app.services.product_repository import ProductRepository
from app.database.database import SessionLocal
from app.database.models import ScanQueueItem, ProductRecord

APPROVED = ('knipex', 'stanley', 'play-doh')
WATCH_CANDIDATES = ('huawei', 'garmin')

def main():
    gated = ProductRepository.get_whole_gated_brand_names()
    with SessionLocal() as db:
        existing = {(r.brand.strip().lower(), r.category_ids or '') for r in db.query(ScanQueueItem).all()}
        for brand in APPROVED:
            if brand not in gated and (brand, '') not in existing:
                ScanQueueService.add_item(brand)
        watch_rows = db.query(ProductRecord).filter(
            ProductRecord.brand.in_(WATCH_CANDIDATES),
            ProductRecord.category_name.ilike('%watch%'),
        ).all()
        category_ids = set()
        for row in watch_rows:
            # Saved report_json may carry Keepa's root category; only use
            # an exact saved value, never guess a category ID.
            import json
            try:
                payload = json.loads(row.report_json or '{}')
            except (TypeError, ValueError):
                payload = {}
            for key in ('root_category_id', 'rootCategory', 'category_id'):
                if payload.get(key):
                    category_ids.add(str(payload[key]))
        if len(category_ids) == 1:
            category = next(iter(category_ids))
            for brand in WATCH_CANDIDATES:
                if brand not in gated and (brand, category) not in existing:
                    ScanQueueService.add_item(brand, [category])
        elif len(category_ids) > 1:
            print('WATCH_CATEGORY_REVIEW_REQUIRED', sorted(category_ids))
        else:
            print('WATCH_CATEGORY_REVIEW_REQUIRED no exact saved watch category ID')
    print('Approved brands prepared:', ', '.join(APPROVED))

if __name__ == '__main__':
    main()
