"""Apply the owner's approved brand additions and gating declarations."""
from app.services.scan_queue_service import ScanQueueService
from app.services.product_repository import ProductRepository
from app.database.database import SessionLocal
from app.database.models import ScanQueueItem

for brand in ('sandisk', 'bosch', 'hp'):
    if brand not in ProductRepository.get_whole_gated_brand_names():
        ProductRepository.add_gated_brand(brand=brand, reason='Owner confirmed account is gated on this brand.')
with SessionLocal() as db:
    before = {r.brand.strip().lower() for r in db.query(ScanQueueItem).all()}
for brand in ('bialetti', 'razer'):
    if brand in ProductRepository.get_whole_gated_brand_names():
        raise RuntimeError(f'{brand} is gated; not adding')
    if brand not in before:
        ScanQueueService.add_item(brand)
with SessionLocal() as db:
    items = db.query(ScanQueueItem).all()
    names = {r.brand.strip().lower() for r in items}
    assert {'bialetti','razer'} <= names
    assert not names.intersection({'sandisk','bosch','hp'})
    print('Verified additions: Bialetti, Razer; gated: SanDisk, Bosch, HP')
    print('Queue entries:',len(items),'Distinct brands:',len(names))
