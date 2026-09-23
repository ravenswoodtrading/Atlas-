"""Free price sweep: turning getItemOffers answers into a verdict, the population it covers, the rolling batch,
never-overwrite-with-a-failed-call, hit tracking and the accuracy overview. Isolated in-memory SQLite and a fake
SP-API client -- no live database, no network, no Keepa."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import ListingRestriction, PriceSweepResult, ProductRecord, ReplenA2AItem, WatchedProduct
from app.services import keepa_watch_list_service as kw
from app.services import price_sweep_service as ps
from app.services.keepa_watch_list_service import TARGET_ROI_PCT, _stored_rate_roi
from app.services.price_sweep_service import (
    STATUS_BELOW, STATUS_CLOSE, STATUS_LEAD, STATUS_NO_EU, STATUS_NO_MODEL, STATUS_NO_UK, PriceSweepService, assess,
    model_for,
)

NOW = datetime.now(timezone.utc).replace(tzinfo=None)
IDENTITY = lambda price, market: price          # EU prices already "in GBP" for the pure tests


def flat_model(rate=0.15):
    return _stored_rate_roi(0.2, rate, 3.5, 0.20)


def offer(price, status="Success"):
    return {"status": status, "price": price, "offer_count": 5}


def record(asin="B0AAAAAAAA", price=32.0, cost=17.4, rate=0.15, **kw_):
    model = flat_model(rate)
    fields = dict(asin=asin, title=f"Product {asin}", brand="Brand", category_name="", buy_box_now=price,
                  buy_box_90d=price * 1.1, best_source_marketplace="DE", best_source_cost_gbp=cost, fba_fee=3.5,
                  referral_rate_used=rate, uk_vat_rate_used=0.2, eu_vat_rate_used=0.20,
                  roi=round(model(price, cost), 2) if cost > 0 else 0.0, roi_90d=0.0, monthly_sales=50,
                  sales_drops_30d=0, recommendation="IGNORE", scanned_at=NOW - timedelta(days=2))
    fields.update(kw_)
    return ProductRecord(**fields)


class AssessTests(unittest.TestCase):
    fn = staticmethod(flat_model())

    def eu(self, **prices):
        return {m: (offer(p) if p is not None else offer(None)) for m, p in prices.items()}

    def test_a_lead_a_close_and_a_below(self):
        eu = self.eu(DE=17.4, FR=None, ES=None, IT=None)
        self.assertEqual(assess(self.fn, offer(40.0), eu, IDENTITY)["status"], STATUS_LEAD)
        self.assertEqual(assess(self.fn, offer(25.0), eu, IDENTITY)["status"], STATUS_BELOW)
        close = assess(self.fn, offer(32.0), eu, IDENTITY)
        self.assertEqual(close["status"], STATUS_CLOSE)
        self.assertTrue(ps.CLOSE_ROI_PCT <= close["est_roi"] < TARGET_ROI_PCT)

    def test_it_uses_the_cheapest_market_and_reports_it(self):
        out = assess(self.fn, offer(40.0), self.eu(DE=19.0, FR=17.4, ES=None, IT=22.0), IDENTITY)
        self.assertEqual((out["best_market"], out["best_cost_gbp"]), ("FR", 17.4))
        self.assertEqual(out["eu_prices_gbp"], {"DE": 19.0, "FR": 17.4, "IT": 22.0})   # the market with no price is left out

    def test_a_uk_rise_or_an_eu_fall_alone_can_make_the_lead(self):
        """The reason for this instead of one-way Keepa alerts: both directions land in one number."""
        base = assess(self.fn, offer(32.0), self.eu(DE=22.0), IDENTITY)
        self.assertLess(base["est_roi"], TARGET_ROI_PCT)
        uk_rose = assess(self.fn, offer(45.0), self.eu(DE=22.0), IDENTITY)
        eu_fell = assess(self.fn, offer(32.0), self.eu(DE=16.0), IDENTITY)
        both_a_bit = assess(self.fn, offer(38.0), self.eu(DE=20.0), IDENTITY)   # neither move is enough alone (16% / 4%)
        self.assertLess(assess(self.fn, offer(38.0), self.eu(DE=22.0), IDENTITY)["est_roi"], TARGET_ROI_PCT)
        self.assertLess(assess(self.fn, offer(32.0), self.eu(DE=20.0), IDENTITY)["est_roi"], TARGET_ROI_PCT)
        self.assertEqual(uk_rose["status"], STATUS_LEAD)
        self.assertEqual(eu_fell["status"], STATUS_LEAD)
        self.assertEqual(both_a_bit["status"], STATUS_LEAD)

    def test_currency_conversion_is_applied_per_market(self):
        seen = []
        def to_gbp(price, market):
            seen.append((price, market))
            return round(price * 0.85, 2)
        out = assess(self.fn, offer(40.0), {"DE": offer(20.0), "FR": offer(None), "ES": offer(None), "IT": offer(None)}, to_gbp)
        self.assertEqual(seen, [(20.0, "DE")])                       # only markets that have a price are converted
        self.assertEqual(out["best_cost_gbp"], 17.0)

    def test_no_uk_price_is_a_real_answer_not_a_failure(self):
        out = assess(self.fn, offer(None), {}, IDENTITY)
        self.assertEqual((out["failed"], out["status"]), (False, STATUS_NO_UK))
        self.assertFalse(assess(self.fn, offer(40.0, status="Inactive"), self.eu(DE=17.0), IDENTITY)["failed"])
        self.assertEqual(assess(self.fn, offer(40.0, status="Inactive"), self.eu(DE=17.0), IDENTITY)["status"], STATUS_NO_UK)

    def test_no_eu_price_anywhere_is_a_real_answer(self):
        out = assess(self.fn, offer(40.0), self.eu(DE=None, FR=None, ES=None, IT=None), IDENTITY)
        self.assertEqual((out["failed"], out["status"]), (False, STATUS_NO_EU))

    def test_a_failed_call_is_never_read_as_no_offer(self):
        self.assertEqual(assess(self.fn, None, {}, IDENTITY), {"failed": True})
        eu_all_failed = {m: None for m in ps.EU_MARKETS}
        self.assertEqual(assess(self.fn, offer(40.0), eu_all_failed, IDENTITY), {"failed": True})
        # every market answered "no price" except one whose call failed: still unknown, not "no source"
        eu_mixed = {"DE": offer(None), "FR": None, "ES": offer(None), "IT": offer(None)}
        self.assertEqual(assess(self.fn, offer(40.0), eu_mixed, IDENTITY), {"failed": True})

    def test_a_failed_market_can_only_understate_the_roi(self):
        """DE has a price; FR's call failed (it might have been cheaper). The answer uses DE and never invents a lead."""
        full = assess(self.fn, offer(30.0), self.eu(DE=20.0, FR=None), IDENTITY)
        partial = assess(self.fn, offer(30.0), {"DE": offer(20.0), "FR": None}, IDENTITY)
        self.assertFalse(partial["failed"])
        self.assertEqual(partial["est_roi"], full["est_roi"])


class ModelForTests(unittest.TestCase):
    def test_no_record_no_model(self):
        self.assertIsNone(model_for(None))

    def test_a_record_that_reproduces_its_own_roi_gets_a_model(self):
        r = record()
        self.assertAlmostEqual(model_for(r)(32.0, 17.4), r.roi, delta=0.5)

    def test_a_record_whose_fee_data_cannot_reproduce_its_roi_is_refused(self):
        r = record(category_name="", referral_rate_used=0.0)
        r.roi = 30.0                                                  # the default fee model gives ~19% for this record
        self.assertIsNone(model_for(r))

    def test_a_record_with_no_eu_cost_uses_its_rates_unvalidated(self):
        r = record(cost=0.0)
        self.assertIsNotNone(model_for(r))
        self.assertIsNone(model_for(record(cost=0.0, category_name="", referral_rate_used=0.0)))


class FakeClient:
    """getItemOffers stand-in: answers[(asin, market)] -> dict | None (a failed call); anything unlisted has no price."""
    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def get_item_offers(self, asin, market, item_condition="New"):
        self.calls.append((asin, market))
        return self.answers.get((asin, market), offer(None))


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        for module in (ps, kw):
            patcher = patch.object(module, "SessionLocal", self.sessions)
            patcher.start()
            self.addCleanup(patcher.stop)
        currency = patch.object(ps.CurrencyService, "to_gbp", staticmethod(lambda amount, currency: amount))
        currency.start()
        self.addCleanup(currency.stop)
        self.addCleanup(self.engine.dispose)

    def add(self, *objs):
        with self.sessions() as db:
            db.add_all(objs)
            db.commit()

    def watch(self, rec, restricted=False):
        objs = [rec, WatchedProduct(asin=rec.asin, title=rec.title)]
        if restricted:
            objs.append(ListingRestriction(asin=rec.asin, marketplace="UK", restricted=True))
        self.add(*objs)

    def replen(self, asin, **fields):
        self.add(ReplenA2AItem(asin=asin, title=f"Replen {asin}", **fields))

    def row(self, asin):
        with self.sessions() as db:
            r = db.get(PriceSweepResult, asin)
            if r is not None:
                db.expunge(r)
            return r

    def sweep(self, client, **kw_):
        return PriceSweepService.run_batch(client=client, now=kw_.pop("now", NOW), **kw_)


class PopulationTests(_Db):
    def pop(self):
        with self.sessions() as db:
            return {e["asin"]: e for e in PriceSweepService.population(db)}

    def test_watch_and_replen_are_merged_per_asin(self):
        self.watch(record("B0BOTHSIDES"))
        self.replen("B0BOTHSIDES")
        self.replen("B0REPLENONLY")
        pop = self.pop()
        self.assertEqual(pop["B0BOTHSIDES"]["scopes"], ["Watch", "Replen"])
        self.assertEqual(pop["B0REPLENONLY"]["scopes"], ["Replen"])

    def test_a_replen_item_uses_its_latest_scan_record_and_none_when_never_scanned(self):
        self.add(record("B0SCANNED0", scanned_at=NOW - timedelta(days=9)), record("B0SCANNED0", price=33.0, scanned_at=NOW - timedelta(days=1)))
        self.replen("B0SCANNED0")
        self.replen("B0NEVERSCAN")
        pop = self.pop()
        self.assertEqual(pop["B0SCANNED0"]["record"].buy_box_now, 33.0)
        self.assertIsNone(pop["B0NEVERSCAN"]["record"])

    def test_who_is_left_out(self):
        self.watch(record("B0NOSALES0", monthly_sales=0, sales_drops_30d=0))
        self.watch(record("B0GATEDREC", recommendation="GATED"))
        self.watch(record("B0RESTRICT"), restricted=True)
        self.watch(record("B0PLUGITEM", title="Shark Cordless Vacuum Cleaner"))
        self.watch(record("B0KEEPME00"))
        self.replen("B0IGNORED0", ignored=True)
        self.replen("B0GATEDREP", gated=True)
        self.add(ListingRestriction(asin="B0RESTREPL", marketplace="UK", restricted=True))
        self.replen("B0RESTREPL")
        self.assertEqual(set(self.pop()), {"B0KEEPME00"})


class BatchTests(_Db):
    def live(self, asin, uk, de=None):
        answers = {(asin, "UK"): offer(uk)}
        if de is not None:
            answers[(asin, "DE")] = offer(de)
        return answers

    def test_a_lead_is_saved_with_the_evidence_and_flagged_as_a_hit(self):
        self.watch(record("B0LEADNOW0"))                                  # stored: ~19% at UK 32 / EU 17.4
        result = self.sweep(FakeClient(self.live("B0LEADNOW0", uk=40.0, de=17.4)))
        r = self.row("B0LEADNOW0")
        self.assertEqual((r.status, r.best_market, r.best_cost_gbp, r.uk_price), (STATUS_LEAD, "DE", 17.4, 40.0))
        self.assertGreaterEqual(r.est_roi, TARGET_ROI_PCT)
        self.assertLess(r.ref_roi, TARGET_ROI_PCT)                       # Keepa's last scan said it wasn't
        self.assertEqual(r.hit_since, NOW)
        self.assertEqual((result["checked"], result["leads"], result["new_hits"]), (1, 1, 1))

    def test_a_lead_keepa_already_knew_about_is_not_a_new_hit(self):
        self.watch(record("B0KNOWNLEAD", price=40.0, cost=17.4))         # stored ROI is already above the bar
        self.sweep(FakeClient(self.live("B0KNOWNLEAD", uk=40.0, de=17.4)))
        r = self.row("B0KNOWNLEAD")
        self.assertEqual(r.status, STATUS_LEAD)
        self.assertIsNone(r.hit_since)

    def test_hit_since_sticks_while_it_stays_a_lead_and_clears_when_it_stops(self):
        self.watch(record("B0STICKY000"))
        client = FakeClient(self.live("B0STICKY000", uk=40.0, de=17.4))
        self.sweep(client, now=NOW - timedelta(hours=5))
        self.sweep(client, now=NOW)
        self.assertEqual(self.row("B0STICKY000").hit_since, NOW - timedelta(hours=5))
        self.sweep(FakeClient(self.live("B0STICKY000", uk=25.0, de=17.4)), now=NOW + timedelta(hours=1))
        r = self.row("B0STICKY000")
        self.assertEqual(r.status, STATUS_BELOW)
        self.assertIsNone(r.hit_since)

    def test_a_failed_call_never_overwrites_the_earlier_answer(self):
        self.watch(record("B0FAILSAFE0"))
        self.sweep(FakeClient(self.live("B0FAILSAFE0", uk=40.0, de=17.4)), now=NOW - timedelta(hours=5))
        before = self.row("B0FAILSAFE0")
        result = self.sweep(FakeClient({("B0FAILSAFE0", "UK"): None}), now=NOW)
        after = self.row("B0FAILSAFE0")
        self.assertEqual(result["failed"], 1)
        self.assertEqual((after.status, after.est_roi, after.checked_at), (before.status, before.est_roi, before.checked_at))

    def test_no_eu_calls_when_there_is_no_uk_price_to_compare_against(self):
        self.watch(record("B0NOUKPRICE"))
        client = FakeClient({("B0NOUKPRICE", "UK"): offer(None)})
        self.sweep(client)
        self.assertEqual(client.calls, [("B0NOUKPRICE", "UK")])
        self.assertEqual(self.row("B0NOUKPRICE").status, STATUS_NO_UK)

    def test_all_four_eu_markets_are_asked_when_there_is_a_uk_price(self):
        self.watch(record("B0ALLMARKET"))
        client = FakeClient(self.live("B0ALLMARKET", uk=32.0, de=17.4))
        self.sweep(client)
        self.assertEqual(client.calls, [("B0ALLMARKET", m) for m in ("UK", "DE", "FR", "ES", "IT")])

    def test_an_unscanned_replen_item_is_no_model_and_costs_no_calls(self):
        self.replen("B0NEVERSCAN")
        client = FakeClient()
        result = self.sweep(client)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.row("B0NEVERSCAN").status, STATUS_NO_MODEL)
        self.assertEqual(result["no_model"], 1)

    def test_the_batch_takes_never_checked_first_then_the_oldest(self):
        for asin in ("B0OLDEST000", "B0NEWER0000", "B0NEVERCHEK"):
            self.watch(record(asin))
        self.add(PriceSweepResult(asin="B0OLDEST000", status=STATUS_BELOW, checked_at=NOW - timedelta(hours=30)),
                 PriceSweepResult(asin="B0NEWER0000", status=STATUS_BELOW, checked_at=NOW - timedelta(hours=2)))
        client = FakeClient()
        self.sweep(client, limit=2)
        self.assertEqual([a for a, m in client.calls if m == "UK"], ["B0NEVERCHEK", "B0OLDEST000"])

    def test_products_with_no_fee_data_do_not_use_up_the_batch(self):
        for asin in ("B0NOMODEL01", "B0NOMODEL02", "B0NOMODEL03"):
            self.replen(asin)                                             # never scanned: no fee data, costs no calls
        for hours, asin in ((30, "B0WATCHED01"), (20, "B0WATCHED02"), (10, "B0WATCHED03")):
            self.watch(record(asin))
            self.add(PriceSweepResult(asin=asin, status=STATUS_BELOW, checked_at=NOW - timedelta(hours=hours)))
        client = FakeClient()                                              # never-checked (the 3 no-model ones) sort first
        result = self.sweep(client, limit=2)
        self.assertEqual({a for a, m in client.calls}, {"B0WATCHED01", "B0WATCHED02"})   # a full 2 real checks despite 3 free ones first
        self.assertEqual((result["no_model"], result["checked"]), (3, 2))

    def test_results_for_products_that_left_the_population_are_removed(self):
        self.add(PriceSweepResult(asin="B0GONEAWAY", status=STATUS_LEAD, checked_at=NOW, hit_since=NOW))
        self.watch(record("B0STILLHERE"))
        self.sweep(FakeClient())
        self.assertIsNone(self.row("B0GONEAWAY"))
        self.assertIsNotNone(self.row("B0STILLHERE"))

    def test_a_spent_time_budget_stops_without_touching_anything(self):
        self.watch(record("B0NOTIME000"))
        client = FakeClient()
        result = self.sweep(client, budget_seconds=0)
        self.assertTrue(result["out_of_time"])
        self.assertEqual((client.calls, self.row("B0NOTIME000")), ([], None))

    def test_unconfigured_sp_api_skips_cleanly(self):
        with patch.object(ps, "get_sp_api_client", return_value=None):
            result = PriceSweepService.run_batch()
        self.assertIn("skipped", result)
        self.assertIn("Skipped", PriceSweepService.summarise(result))

    def test_the_replen_item_scope_is_recorded_beside_the_watch_scope(self):
        self.watch(record("B0BOTHSIDES"))
        self.replen("B0BOTHSIDES")
        self.sweep(FakeClient())
        self.assertEqual(self.row("B0BOTHSIDES").scopes, "Watch, Replen")


class OverviewTests(_Db):
    def test_accuracy_compares_the_free_check_with_keepas_last_scan(self):
        self.watch(record("B0AGREES000"))                                # live prices identical to the stored ones
        self.watch(record("B0DISAGREES"))                                # live UK far above what Keepa saw
        self.watch(record("B0OLDREF000", scanned_at=NOW - timedelta(days=20)))   # too old to be a fair yardstick
        client = FakeClient({("B0AGREES000", "UK"): offer(32.0), ("B0AGREES000", "DE"): offer(17.4),
                             ("B0DISAGREES", "UK"): offer(40.0), ("B0DISAGREES", "DE"): offer(17.4),
                             ("B0OLDREF000", "UK"): offer(32.0), ("B0OLDREF000", "DE"): offer(17.4)})
        self.sweep(client)
        o = PriceSweepService.overview(now=NOW)
        self.assertEqual(o["accuracy"]["n"], 2)
        self.assertEqual(o["accuracy"]["within_close"], 1)
        self.assertEqual(o["accuracy"]["lead_agreement"], 1)             # one call on "is it a lead" matched, one didn't
        self.assertEqual([d["asin"] for d in o["disagreements"]][0], "B0DISAGREES")
        self.assertEqual([h["asin"] for h in o["hits"]], ["B0DISAGREES"])
        self.assertAlmostEqual(o["hits"][0]["uk_move_pct"], 25.0, delta=0.01)       # UK 40 live vs 32 at Keepa's scan
        self.assertEqual(o["by_status"], {STATUS_CLOSE: 2, STATUS_LEAD: 1})

    def test_a_hit_built_on_a_huge_uk_jump_is_set_aside_as_a_probable_spike(self):
        self.watch(record("B0REALMOVE0"))                                # UK 32 -> 40 (+25%): worth a look
        self.watch(record("B0SPIKEMOVE"))                                # UK 32 -> 85 (+166%): Amazon out of stock, inflated 3P price
        self.sweep(FakeClient({("B0REALMOVE0", "UK"): offer(40.0), ("B0REALMOVE0", "DE"): offer(17.4),
                               ("B0SPIKEMOVE", "UK"): offer(85.0), ("B0SPIKEMOVE", "DE"): offer(17.4)}))
        o = PriceSweepService.overview(now=NOW)
        self.assertEqual([h["asin"] for h in o["hits_worth_a_look"]], ["B0REALMOVE0"])
        self.assertEqual([h["asin"] for h in o["hits_probably_spikes"]], ["B0SPIKEMOVE"])
        self.assertEqual(len(o["hits"]), 2)

    def test_coverage_counts_only_products_with_usable_fee_data(self):
        self.watch(record("B0ANSWERED0"))
        self.watch(record("B0NOEUPRICE"))
        self.replen("B0NEVERSCAN")
        self.sweep(FakeClient({("B0ANSWERED0", "UK"): offer(32.0), ("B0ANSWERED0", "DE"): offer(17.4),
                               ("B0NOEUPRICE", "UK"): offer(32.0)}))
        o = PriceSweepService.overview(now=NOW)
        self.assertEqual(o["coverage"], {"answered": 1, "modelled": 2})
        self.assertEqual((o["population"], o["checked_ever"], o["never_checked"]), (3, 3, 0))
        self.assertEqual(o["by_status"][STATUS_NO_EU], 1)

    def test_an_empty_database_is_fine(self):
        o = PriceSweepService.overview(now=NOW)
        self.assertEqual((o["population"], o["hits"], o["disagreements"]), (0, [], []))
        self.assertIsNone(o["accuracy"]["mean_abs_delta"])


if __name__ == "__main__":
    unittest.main()
