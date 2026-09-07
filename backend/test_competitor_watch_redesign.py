"""
Tests for the Competitor Watch redesign (Opportunities / Source Finder /
Competitors), 2026-09-03.

Run with `python test_competitor_watch_redesign.py` (same plain-script
style as the other test_*.py files here, no pytest).

Every DB-writing test here uses a dedicated, obviously-fake ASIN/seller
ID and cleans up in a try/finally, verified against exact row counts at
the end so nothing is left behind in a real Atlas database.

NO live Keepa/SerpApi/Brave calls anywhere in this file --
OaSourceDiscoveryService.run_batch is monkeypatched to a stub for the
Source Finder route tests (contract only: was it called, with what
args, was it NOT called on a GET), never actually invoked.
"""
from datetime import datetime, timedelta, timezone

from app.database.database import SessionLocal
from app.database.models import ProductRecord, SellerNewListing, TrackedSeller
from app.services.seller_watch_service import SellerWatchService
from app.services.review_queue_service import ReviewQueueService, QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_NEEDS_ATTENTION
from app.routes import competitors as competitors_route
import app.services.oa_source_discovery_service as oa_source_discovery_service
from fastapi.testclient import TestClient
from app.main import app

TEST_ASIN_PREFIX = "B0CWTEST"
TEST_SELLER_IDS = ["CWTESTSELLER1", "CWTESTSELLER2"]


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cleanup(asins):
    db = SessionLocal()
    try:
        db.query(ProductRecord).filter(ProductRecord.asin.in_(asins)).delete(synchronize_session=False)
        db.query(SellerNewListing).filter(SellerNewListing.asin.in_(asins)).delete(synchronize_session=False)
        db.query(TrackedSeller).filter(TrackedSeller.seller_id.in_(TEST_SELLER_IDS)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def snapshot_counts():
    db = SessionLocal()
    try:
        return (
            db.query(ProductRecord).count(),
            db.query(SellerNewListing).count(),
            db.query(TrackedSeller).count(),
        )
    finally:
        db.close()


before_counts = snapshot_counts()
created_asins = set()


def make_seller(seller_id, nickname):
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id=seller_id, nickname=nickname, active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        return seller.id
    finally:
        db.close()


def make_record(asin, recommendation="BUY", profit=10.0, roi=50.0, buy_box_now=100.0,
                 best_source_marketplace="DE", best_source_cost_gbp=50.0, monthly_sales=10,
                 sales_drops_30d=5, category_name="", fba_fee=5.0, ean=""):
    db = SessionLocal()
    try:
        record = ProductRecord(
            asin=asin, title=f"Test Competitor Product {asin}", brand="TestBrand", category="",
            category_name=category_name, best_source_marketplace=best_source_marketplace,
            best_source_cost_gbp=best_source_cost_gbp, buy_box_now=buy_box_now, fba_fee=fba_fee,
            profit=profit, roi=roi, recommendation=recommendation, score=80,
            monthly_sales=monthly_sales, sales_drops_30d=sales_drops_30d, ean=ean,
            scanned_at=utcnow(), review=None,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        created_asins.add(asin)
        return record.id
    finally:
        db.close()


def make_listing(seller_id, asin, record_id, sourcing_tag, currently_buyable, reasoning_json="",
                  detected_at=None):
    db = SessionLocal()
    try:
        listing = SellerNewListing(
            tracked_seller_id=seller_id, asin=asin, product_record_id=record_id,
            sourcing_tag=sourcing_tag, currently_buyable=currently_buyable,
            sourcing_reasoning_json=reasoning_json, detected_at=detected_at or utcnow(),
        )
        db.add(listing)
        db.commit()
        db.refresh(listing)
        created_asins.add(asin)
        return listing.id
    finally:
        db.close()


client = TestClient(app)

try:
    seller1 = make_seller(TEST_SELLER_IDS[0], "CW Test Seller One")
    seller2 = make_seller(TEST_SELLER_IDS[1], "CW Test Seller Two")

    # =====================================================================
    # 1 -- current BUY -> BUY_NOW bucket (list_notable_buyable)
    # =====================================================================
    asin = f"{TEST_ASIN_PREFIX}A"
    record_id = make_record(asin, recommendation="BUY", profit=20.0, roi=60.0, monthly_sales=15, sales_drops_30d=8)
    make_listing(seller1, asin, record_id, "EU A2A", currently_buyable=True,
                 reasoning_json='{"marketplace": "DE", "guessed_buy_price_gbp": 40.0, "guessed_buy_date": "2026-08-25"}')

    buy_now = SellerWatchService.list_notable_buyable(limit=500)
    assert asin in {e["listing"].asin for e in buy_now}, "currently-buyable + notable competitor find must reach list_notable_buyable"
    print("test 1: current BUY (currently_buyable + notable) -> BUY_NOW bucket: ok")

    # =====================================================================
    # 2 -- historical A2A, no current BUY -> NEEDS_ATTENTION bucket
    # =====================================================================
    asin2 = f"{TEST_ASIN_PREFIX}B"
    record_id2 = make_record(asin2, recommendation="IGNORE", profit=0.0, roi=0.0)
    make_listing(seller1, asin2, record_id2, "EU A2A", currently_buyable=False,
                 reasoning_json='{"marketplace": "FR", "guessed_buy_price_gbp": 15.0, "guessed_buy_date": "2026-08-20"}')

    needs_attention = SellerWatchService.list_historical_a2a_not_buyable(limit=500)
    assert asin2 in {e["listing"].asin for e in needs_attention}, "historical A2A, not buyable -> must reach list_historical_a2a_not_buyable"
    print("test 2: historical A2A evidence, no current buy -> NEEDS_ATTENTION bucket: ok")

    # Also verify current price genuinely does NOT erase the historical
    # evidence -- the raw reasoning is still readable off the listing.
    db = SessionLocal()
    try:
        listing2 = db.query(SellerNewListing).filter(SellerNewListing.asin == asin2).first()
        assert '"marketplace": "FR"' in listing2.sourcing_reasoning_json
    finally:
        db.close()
    print("test 2b: historical evidence (marketplace/price/date) survives on the listing even though not currently buyable: ok")

    # =====================================================================
    # 3 -- OA worth investigating: breakeven > 0 required, not the raw tag count
    # =====================================================================
    asin3 = f"{TEST_ASIN_PREFIX}C"
    # profit/roi/monthly_sales/sales_drops_30d explicitly zeroed (not
    # make_record's default "clean BUY" values) -- this scenario's real
    # intent is "no confirmed economics, OA tag only", which only
    # actually held under the OLD is_notable()-excludes-IGNORE-by-name
    # behaviour; Opportunity Engine 2.0's lens reads the real numbers
    # regardless of recommendation label, so the fixture must express
    # the scenario numerically now, not just via the label.
    record_id3 = make_record(
        asin3, recommendation="IGNORE", buy_box_now=150.0, category_name="", fba_fee=6.0,
        profit=0.0, roi=0.0, monthly_sales=0, sales_drops_30d=0,
    )
    make_listing(seller2, asin3, record_id3, "OA / unclear", currently_buyable=False,
                 reasoning_json='{"note": "No recent EU margin or UK dip in the last 30 days"}')

    # An OA row where Amazon's own fees already exceed the sale price --
    # must NOT count as "worth investigating".
    asin4 = f"{TEST_ASIN_PREFIX}D"
    record_id4 = make_record(asin4, recommendation="IGNORE", buy_box_now=0.50, category_name="", fba_fee=6.0)
    make_listing(seller2, asin4, record_id4, "OA / unclear", currently_buyable=False)

    oa_worth = SellerWatchService.list_oa_worth_investigating(limit=500)
    oa_worth_asins = {e["listing"].asin for e in oa_worth}
    assert asin3 in oa_worth_asins, "genuine breakeven room -> must count as OA worth investigating"
    assert asin4 not in oa_worth_asins, "fees exceeding sale price -> must NOT count as worth investigating"
    entry3 = next(e for e in oa_worth if e["listing"].asin == asin3)
    assert entry3["oa_price_guide"]["breakeven"] > 0
    assert entry3["oa_price_guide"]["target"] > 0
    print("test 3: OA worth-investigating uses the real price-guide breakeven, not the raw OA/unclear tag: ok")

    # =====================================================================
    # 4 -- Opportunities feed builder: counts + view/source filters
    # =====================================================================
    ctx = competitors_route.build_opportunities_context()
    all_asins = {e["listing"].asin for e in ctx["items"]}
    assert asin in all_asins and ctx["counts"]["buy_now"] >= 1
    assert asin2 in all_asins and ctx["counts"]["needs_attention"] >= 1
    assert asin3 in all_asins and ctx["counts"]["oa_investigate"] >= 1
    print("test 4: build_opportunities_context pool + counts include all three real buckets: ok")

    ctx_buy_only = competitors_route.build_opportunities_context(view="buy_now")
    assert all(e["opp_view"] == "buy_now" for e in ctx_buy_only["items"])
    assert asin in {e["listing"].asin for e in ctx_buy_only["items"]}
    assert asin2 not in {e["listing"].asin for e in ctx_buy_only["items"]}
    print("test 4b: view=buy_now filters the pool to exactly that bucket: ok")

    ctx_source = competitors_route.build_opportunities_context(source="oa")
    assert all(e["listing"].sourcing_tag == "OA / unclear" for e in ctx_source["items"])
    print("test 4c: source=oa filters to exactly that sourcing tag: ok")

    ctx_seller = competitors_route.build_opportunities_context(seller_id=seller1)
    assert all(e["listing"].tracked_seller_id == seller1 for e in ctx_seller["items"])
    assert asin in {e["listing"].asin for e in ctx_seller["items"]}
    assert asin3 not in {e["listing"].asin for e in ctx_seller["items"]}
    print("test 4d: seller_id filters to that competitor only (VIEW DISCOVERIES target): ok")

    ctx_search = competitors_route.build_opportunities_context(q=asin2)
    assert {e["listing"].asin for e in ctx_search["items"]} == {asin2}
    print("test 4e: search filters by ASIN: ok")

    # =====================================================================
    # 5 -- A2A historical vs current source shown SEPARATELY (drawer data)
    # =====================================================================
    item = ReviewQueueService.get_queue_item(asin2)
    assert item is not None
    competitor_source = next(s for s in item["source_items"] if s["source"] == "competitor")
    assert competitor_source["reasoning"]["marketplace"] == "FR"  # historical
    assert item.get("best_source_marketplace") != "FR" or not competitor_source["currently_buyable"]
    assert QUEUE_PRIORITY_NEEDS_ATTENTION in item["views"]
    print("test 5: drawer's underlying data keeps historical source (FR) separate from current buyability state: ok")

    # =====================================================================
    # 6 -- Google URL variants
    # =====================================================================
    urls_with_ean = competitors_route._google_search_url_variants("Widget Pro", "5012345678900", "B0TESTASIN")
    assert urls_with_ean["title"] and "Widget" in urls_with_ean["title"]
    assert urls_with_ean["title_ean"] and "5012345678900" in urls_with_ean["title_ean"]
    assert urls_with_ean["asin_title"] and "B0TESTASIN" in urls_with_ean["asin_title"]

    urls_no_ean = competitors_route._google_search_url_variants("Widget Pro", "", "B0TESTASIN")
    assert urls_no_ean["title_ean"] is None, "no EAN on record -> title_ean variant must not be fabricated"
    print("test 6: Google URL variants (title / title+EAN / ASIN+title), EAN variant omitted when absent: ok")

    # =====================================================================
    # 7 -- single-ASIN Source Finder route: contract only, no live call
    # =====================================================================
    calls = []

    def fake_run_batch(limit=20, since_days=None, test_mode=False, test_asins=None, raw_products=None):
        calls.append({"limit": limit, "test_mode": test_mode, "test_asins": test_asins})
        return {"run_id": 999999, "asins_targeted": len(test_asins or [])}

    original_run_batch = oa_source_discovery_service.OaSourceDiscoveryService.run_batch
    oa_source_discovery_service.OaSourceDiscoveryService.run_batch = staticmethod(fake_run_batch)
    competitors_route.OaSourceDiscoveryService.run_batch = staticmethod(fake_run_batch)
    try:
        # GET (preview + preload) must NEVER call run_batch.
        r_get = client.get(f"/competitors?tab=source_finder&asin={asin3}")
        assert r_get.status_code == 200
        assert calls == [], f"GET /competitors?tab=source_finder must never run a search automatically, got calls={calls}"
        print("test 7a: opening Source Finder (GET, even pre-loaded) does NOT run a source search: ok")

        # POST is the only thing that runs it, and it's scoped to ONE ASIN.
        r_post = client.post("/competitors/find-source", data={"asin": asin3}, follow_redirects=False)
        assert r_post.status_code == 303
        assert len(calls) == 1, calls
        assert calls[0]["test_asins"] == [asin3]
        assert calls[0]["limit"] == 1
        assert calls[0]["test_mode"] is False, "must be a REAL run (is_test=False), not silently marked test-only"
        print("test 7b: POST /competitors/find-source calls the EXISTING run_batch, scoped to exactly one ASIN, test_mode=False: ok")
    finally:
        oa_source_discovery_service.OaSourceDiscoveryService.run_batch = original_run_batch
        competitors_route.OaSourceDiscoveryService.run_batch = original_run_batch

    # =====================================================================
    # 8/9 -- competitor 7d/30d + A2A/OA/Unknown breakdown
    # =====================================================================
    breakdown = SellerWatchService.get_seller_breakdown()
    b1 = breakdown[seller1]
    assert b1["eu_a2a"] == 2, b1  # asin (buyable) + asin2 (historical) both EU A2A
    assert b1["total"] == 2
    assert b1["last_7d"] == 2
    assert b1["last_30d"] == 2
    assert b1["buy_opportunities"] == 1, "only the currently_buyable=True listing counts"

    b2 = breakdown[seller2]
    assert b2["oa"] == 2, b2
    assert b2["buy_opportunities"] == 0
    print("test 8/9: per-seller tag breakdown + 7d/30d + BUY-opportunity counts, from real aggregation: ok")

    # An old detection (outside both windows) must NOT count toward 7d/30d.
    asin_old = f"{TEST_ASIN_PREFIX}E"
    record_id_old = make_record(asin_old, recommendation="IGNORE")
    make_listing(seller1, asin_old, record_id_old, "Wholesale (likely)", currently_buyable=False,
                 detected_at=utcnow() - timedelta(days=45))
    breakdown2 = SellerWatchService.get_seller_breakdown()
    assert breakdown2[seller1]["last_30d"] == 2, "a 45-day-old detection must not count toward last_30d"
    assert breakdown2[seller1]["wholesale"] == 1
    assert breakdown2[seller1]["total"] == 3
    print("test 8b: a detection older than the window is excluded from last_7d/last_30d, still counted in total: ok")

    # =====================================================================
    # 10 -- marketplace pattern aggregation (Atlas inference)
    # =====================================================================
    asin5 = f"{TEST_ASIN_PREFIX}F"
    record_id5 = make_record(asin5, recommendation="IGNORE")
    make_listing(seller1, asin5, record_id5, "EU A2A", currently_buyable=False,
                 reasoning_json='{"marketplace": "DE"}')

    pattern = SellerWatchService.get_seller_marketplace_pattern(seller1)
    assert pattern is not None
    assert pattern["marketplace"] == "DE", pattern  # 2 of 3 EU A2A entries (asin, asin5) are DE vs 1 FR (asin2)
    assert pattern["of_total"] == 3
    print("test 10: marketplace-pattern aggregation picks the genuinely most common marketplace: ok")

    no_evidence_pattern = SellerWatchService.get_seller_marketplace_pattern(seller2)
    assert no_evidence_pattern is None, "a seller with zero EU A2A detections must get None, never a guessed pattern"
    print("test 10b: no EU A2A evidence -> None, never fabricated: ok")

    # =====================================================================
    # 11 -- OA retailer patterns: empty (honest) when Source Finder hasn't run
    # =====================================================================
    patterns = SellerWatchService.get_seller_oa_retailer_patterns(seller2)
    assert patterns == [], "no OaSourceCandidate rows exist for these test ASINs -- must be an empty list, not fabricated"
    print("test 11: OA retailer patterns are honestly empty when Source Finder hasn't investigated this seller's OA finds: ok")

    # =====================================================================
    # 12 -- recent-discoveries count is a real 7-day tally
    # =====================================================================
    recent = SellerWatchService.count_recent_detections(days=7)
    assert recent >= 5, f"expected at least the 5 recent test detections just created, got {recent}"
    print("test 12: count_recent_detections is a real tally (not a placeholder): ok")

    # =====================================================================
    # 13 -- rendering: opportunities / source finder / competitors / drawer
    # =====================================================================
    for url in [
        "/competitors", "/competitors?tab=opportunities", "/competitors?tab=opportunities&view=buy_now",
        # strong_consider added 2026-09-04 (Buy Now/Strong Consider split, same
        # day as the navigation cleanup this test list was touched for) --
        "/competitors?tab=opportunities&view=strong_consider",
        "/competitors?tab=opportunities&view=needs_attention", "/competitors?tab=opportunities&view=oa_investigate",
        "/competitors?tab=opportunities&source=oa", "/competitors?tab=source_finder",
        f"/competitors?tab=source_finder&asin={asin3}", f"/competitors?tab=competitors&seller_id={seller1}",
        # /competitors-legacy removed 2026-09-04 (navigation cleanup) --
        # confirmed zero remaining dependencies before deletion, see that
        # commit's own message.
    ]:
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
    print("test 13: every redesigned route renders 200 via TestClient (read-only, no live jobs): ok")

    r_drawer_buy = client.get(f"/competitors/opportunity/{asin}")
    assert r_drawer_buy.status_code == 200 and "BUY NOW" in r_drawer_buy.text.upper() or "Buy" in r_drawer_buy.text
    r_drawer_oa = client.get(f"/competitors/opportunity/{asin3}")
    assert r_drawer_oa.status_code == 200 and "Find Source" in r_drawer_oa.text
    print("test 13b: detail drawer renders for both a BUY_NOW-eligible item and an OA item, with the right next actions: ok")

    # =====================================================================
    # 14 -- existing Review Queue behaviour unchanged
    # =====================================================================
    r_rq = client.get("/review-queue")
    assert r_rq.status_code == 200
    r_home = client.get("/")
    assert r_home.status_code == 200
    print("test 14: Home and Review Queue still render unchanged: ok")

finally:
    cleanup(created_asins)

after_counts = snapshot_counts()
assert after_counts == before_counts, (
    f"row counts must be identical after cleanup -- before={before_counts}, after={after_counts}"
)
print(f"\ncleanup verified -- {len(created_asins)} test ASINs created and fully removed, row counts unchanged")

print("\nALL PASS")
