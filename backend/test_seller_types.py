"""Tracked-seller types (2026-09-20): EU A2A / OA / OA-Wholesale / OA-Wholesale + A2A, an evidence-based
suggestion that counts plug/electrical items (which can never be EU A2A), the "some sellers do both"
overlap, the additive migration, and the Competitors pages. Isolated in-memory SQLite -- no live writes."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import ProductRecord, SellerNewListing, TrackedSeller
from app.services import seller_watch_service as sws
from app.services.seller_watch_service import (
    SELLER_TYPES, SUGGEST_EU_A2A_SHARE, SUGGEST_MIN_TAGGED, SUGGEST_PLUG_SHARE, SellerWatchService,
    migrate_seller_schema, seller_type_matches, suggest_seller_type,
)

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def sig(tagged=100, eu=0, wholesale=0, oa=0, plug=0, titled=100, uk=0):
    """Signals for a seller with `tagged` classified detections, given as counts."""
    return dict(tagged=tagged, eu=eu, uk=uk, wholesale=wholesale, oa=oa, titled=titled, plug=plug)


class SuggestTests(unittest.TestCase):
    def kind(self, **kw):
        return suggest_seller_type(sig(**kw))[0]

    def test_not_enough_data_makes_no_suggestion_and_says_why(self):
        kind, reason = suggest_seller_type(sig(tagged=SUGGEST_MIN_TAGGED - 1, eu=15))
        self.assertIsNone(kind)
        self.assertIn("Not enough data", reason)
        self.assertEqual(self.kind(tagged=SUGGEST_MIN_TAGGED, eu=15, titled=20), "eu_a2a")     # exactly enough

    def test_mostly_eu_a2a(self):
        self.assertEqual(self.kind(eu=55, wholesale=25, oa=10), "eu_a2a")
        self.assertEqual(self.kind(eu=int(SUGGEST_EU_A2A_SHARE * 100), wholesale=30, oa=10), "eu_a2a")   # boundary is inclusive
        self.assertNotEqual(self.kind(eu=int(SUGGEST_EU_A2A_SHARE * 100) - 1, wholesale=40, oa=10), "eu_a2a")

    def test_a_real_eu_a2a_share_alongside_lots_of_oa_or_wholesale_is_the_mixed_group(self):
        self.assertEqual(self.kind(eu=32, wholesale=56, oa=3), "mixed")           # D&F Shop-like
        self.assertEqual(self.kind(eu=22, wholesale=57, oa=10), "mixed")          # Lesta-like

    def test_mostly_wholesale_and_oa_with_little_eu_a2a(self):
        self.assertEqual(self.kind(eu=14, wholesale=70, oa=6), "oa_wholesale")    # Griffin-like
        self.assertEqual(self.kind(eu=13, wholesale=77, oa=6), "oa_wholesale")    # JAF-like

    def test_oa_when_oa_clearly_outweighs_wholesale(self):
        self.assertEqual(self.kind(eu=15, wholesale=29, oa=49), "oa")             # (VAT) Unprofitable-like

    def test_a_seller_full_of_plug_items_is_oa_because_those_can_never_be_eu_a2a(self):
        # A third of the range is plug/electrical and almost none of it is EU A2A.
        self.assertEqual(self.kind(eu=5, wholesale=20, oa=40, plug=35), "oa")
        self.assertEqual(self.kind(eu=5, wholesale=40, oa=20, plug=35), "oa_wholesale")

    def test_plug_items_beat_the_price_tags_a_mostly_eu_a2a_seller_with_a_plug_range_does_both(self):
        # 50% tagged EU A2A would normally be "EU A2A", but 35% of the range can't be -- they do both.
        kind, reason = suggest_seller_type(sig(eu=50, wholesale=30, oa=10, plug=35))
        self.assertEqual(kind, "mixed")
        self.assertIn("they do both", reason)
        self.assertIn("can't be EU A2A", reason)

    def test_plug_share_boundary_is_inclusive(self):
        at = int(SUGGEST_PLUG_SHARE * 100)
        self.assertEqual(self.kind(eu=5, wholesale=50, oa=10, plug=at), "oa_wholesale")
        self.assertEqual(self.kind(eu=60, wholesale=10, oa=5, plug=at - 1), "eu_a2a")

    def test_no_titles_means_no_plug_evidence_and_no_crash(self):
        self.assertEqual(self.kind(eu=60, wholesale=10, oa=5, plug=0, titled=0), "eu_a2a")

    def test_reason_states_the_numbers(self):
        _, reason = suggest_seller_type(sig(eu=42, wholesale=27, oa=13, plug=9, tagged=100, titled=95))
        for fragment in ("42% EU A2A", "40% OA/Wholesale", "9% plug/electrical", "100 classified"):
            self.assertIn(fragment, reason)

    def test_real_sellers_land_where_their_data_says(self):
        """Approximate real detection counts, 2026-09-20 (tagged counts and plug-titled counts)."""
        cases = [
            ("Beat it Distribution", sig(tagged=50, eu=33, wholesale=10, oa=1, plug=4, titled=53), "eu_a2a"),
            ("Tinciad PW", sig(tagged=158, eu=84, wholesale=55, oa=13, plug=7, titled=168), "eu_a2a"),
            ("Uk Deals Direct", sig(tagged=212, eu=117, wholesale=57, oa=21, plug=5, titled=225), "eu_a2a"),
            ("D&F Shop", sig(tagged=213, eu=68, wholesale=120, oa=6, plug=9, titled=224), "mixed"),
            ("Trusted distribution", sig(tagged=141, eu=43, wholesale=58, oa=15, plug=5, titled=157), "mixed"),
            ("Griffin Goodies", sig(tagged=453, eu=63, wholesale=317, oa=27, plug=14, titled=462), "oa_wholesale"),
            ("JAF", sig(tagged=172, eu=22, wholesale=132, oa=10, plug=31, titled=210), "oa_wholesale"),
            ("(VAT) Unprofitable", sig(tagged=68, eu=10, wholesale=20, oa=33, plug=7, titled=69), "oa"),
            ("Elite Lux (too little data)", sig(tagged=11, oa=7, plug=5, titled=11), None),
        ]
        for name, signals, expected in cases:
            self.assertEqual(suggest_seller_type(signals)[0], expected, name)


class TypeMatchTests(unittest.TestCase):
    def test_groups_overlap_because_some_sellers_do_both(self):
        self.assertTrue(seller_type_matches("mixed", "does_a2a"))
        self.assertTrue(seller_type_matches("mixed", "does_oa"))
        self.assertTrue(seller_type_matches("eu_a2a", "does_a2a"))
        self.assertFalse(seller_type_matches("eu_a2a", "does_oa"))
        self.assertTrue(seller_type_matches("oa", "does_oa"))
        self.assertTrue(seller_type_matches("oa_wholesale", "does_oa"))
        self.assertFalse(seller_type_matches("oa", "does_a2a"))
        self.assertFalse(seller_type_matches("oa_wholesale", "does_a2a"))

    def test_exact_types_unsorted_and_everything(self):
        self.assertTrue(seller_type_matches("oa", "oa"))
        self.assertFalse(seller_type_matches("mixed", "oa"))            # exact match is exact
        self.assertTrue(seller_type_matches("", "unsorted"))
        self.assertFalse(seller_type_matches("oa", "unsorted"))
        for t in ("", "eu_a2a", "mixed", "oa", "oa_wholesale"):
            self.assertTrue(seller_type_matches(t, ""))                 # no filter -> every seller
        self.assertFalse(seller_type_matches("oa", "garbage"))


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        patcher = patch.object(sws, "SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def seller(self, name, seller_type="", active=True):
        with self.sessions() as db:
            row = TrackedSeller(seller_id=f"A{name.upper()}", nickname=name, active=active, seller_type=seller_type)
            db.add(row)
            db.commit()
            return row.id

    def detect(self, seller_id, tag, title=None, dismissed=False, asin=None):
        with self.sessions() as db:
            record_id = None
            if title is not None:
                record = ProductRecord(asin=asin or f"B{db.query(SellerNewListing).count():09d}", title=title)
                db.add(record)
                db.flush()
                record_id = record.id
            db.add(SellerNewListing(tracked_seller_id=seller_id, asin=asin or f"B{db.query(SellerNewListing).count():09d}",
                                    detected_at=NOW, product_record_id=record_id, sourcing_tag=tag, dismissed=dismissed))
            db.commit()

    def type_of(self, seller_id):
        with self.sessions() as db:
            return db.get(TrackedSeller, seller_id).seller_type


class SignalsTests(_Db):
    def test_counts_tags_titles_and_plug_titles_per_seller(self):
        a = self.seller("Alpha")
        self.detect(a, "EU A2A", "Bialetti Moka Express 6 Cup Stovetop Coffee Maker")
        self.detect(a, "EU A2A", "Bissell Cordless Vacuum Cleaner 2 in 1")
        self.detect(a, "Wholesale (likely)", "Fischer Wall Plugs 100 Pack")
        self.detect(a, "OA / unclear", "Generic Ironing Board Cover")
        self.detect(a, "UK A2A", "Lego Classic Brick Box")
        self.detect(a, None, "Some untagged thing")
        signals = SellerWatchService.get_seller_type_signals()[a]
        self.assertEqual((signals["eu"], signals["uk"], signals["wholesale"], signals["oa"]), (2, 1, 1, 1))
        self.assertEqual(signals["tagged"], 5)                  # the untagged one isn't classified
        self.assertEqual(signals["titled"], 6)
        self.assertEqual(signals["plug"], 1)                    # the vacuum; the Moka pot is plug-free

    def test_dismissed_detections_and_missing_product_records_are_handled(self):
        a = self.seller("Alpha")
        self.detect(a, "EU A2A", "Cordless Vacuum Cleaner", dismissed=True)      # dismissed: ignored entirely
        self.detect(a, "OA / unclear", None)                                       # no product record: tagged, no title
        signals = SellerWatchService.get_seller_type_signals()[a]
        self.assertEqual((signals["tagged"], signals["titled"], signals["plug"]), (1, 0, 0))

    def test_a_seller_with_no_detections_still_gets_a_suggestion_entry_saying_not_enough_data(self):
        a = self.seller("Quiet")
        suggestion = SellerWatchService.get_seller_type_suggestions()[a]
        self.assertIsNone(suggestion["type"])
        self.assertIn("Not enough data", suggestion["reason"])


class ServiceTests(_Db):
    def test_set_type_accepts_the_four_types_clears_with_blank_and_rejects_junk(self):
        a = self.seller("Alpha")
        for key, _ in SELLER_TYPES:
            self.assertTrue(SellerWatchService.set_seller_type(a, key))
            self.assertEqual(self.type_of(a), key)
        self.assertTrue(SellerWatchService.set_seller_type(a, ""))
        self.assertEqual(self.type_of(a), "")
        SellerWatchService.set_seller_type(a, "oa")
        self.assertFalse(SellerWatchService.set_seller_type(a, "banana"))
        self.assertEqual(self.type_of(a), "oa")                                     # unchanged by the bad value
        self.assertFalse(SellerWatchService.set_seller_type(99999, "oa"))

    def make_evidence(self, seller_id, eu, wholesale, oa):
        for tag, n in (("EU A2A", eu), ("Wholesale (likely)", wholesale), ("OA / unclear", oa)):
            for _ in range(n):
                self.detect(seller_id, tag, "Plain product")

    def test_apply_suggestions_sorts_only_unsorted_sellers_and_never_overrides_a_choice(self):
        eu_seller = self.seller("EuSeller")
        self.make_evidence(eu_seller, eu=30, wholesale=8, oa=2)                     # -> eu_a2a
        chosen = self.seller("Chosen", seller_type="oa")
        self.make_evidence(chosen, eu=30, wholesale=8, oa=2)                        # evidence says eu_a2a, Tamara said oa
        thin = self.seller("Thin")
        self.make_evidence(thin, eu=3, wholesale=1, oa=0)                           # too little data
        result = SellerWatchService.apply_suggested_types()
        self.assertEqual(result, {"applied": 1, "skipped_no_data": 1, "already_sorted": 1})
        self.assertEqual(self.type_of(eu_seller), "eu_a2a")
        self.assertEqual(self.type_of(chosen), "oa")                                # untouched
        self.assertEqual(self.type_of(thin), "")
        self.assertEqual(SellerWatchService.apply_suggested_types()["applied"], 0)  # idempotent

    def test_counts_per_type_with_zero_for_empty_ones(self):
        self.seller("A", "eu_a2a")
        self.seller("B", "mixed")
        self.seller("C", "mixed")
        self.seller("D")
        counts = SellerWatchService.get_seller_type_counts()
        self.assertEqual(counts, {"eu_a2a": 1, "mixed": 2, "oa_wholesale": 0, "oa": 0, "": 1})


class MigrationTests(unittest.TestCase):
    def legacy_engine(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        with engine.begin() as c:
            c.execute(text("CREATE TABLE tracked_sellers (id INTEGER PRIMARY KEY, seller_id VARCHAR, nickname VARCHAR, "
                           "active BOOLEAN, last_checked_at DATETIME, last_asin_snapshot VARCHAR, created_at DATETIME)"))
            c.execute(text("INSERT INTO tracked_sellers (id, seller_id, nickname, active, last_asin_snapshot) "
                           "VALUES (1, 'A1', 'Old seller', 1, '')"))
        self.addCleanup(engine.dispose)
        return engine

    def test_adds_the_column_and_existing_sellers_read_as_not_sorted_not_null(self):
        engine = self.legacy_engine()
        migrate_seller_schema(engine)
        self.assertIn("seller_type", {c["name"] for c in inspect(engine).get_columns("tracked_sellers")})
        with engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT seller_type FROM tracked_sellers")).scalar(), "")

    def test_is_idempotent_and_a_missing_table_is_not_an_error(self):
        engine = self.legacy_engine()
        migrate_seller_schema(engine)
        migrate_seller_schema(engine)                                               # second run: no-op
        empty = create_engine("sqlite://")
        migrate_seller_schema(empty)                                                # no tracked_sellers table yet
        empty.dispose()

    def test_old_code_inserting_without_the_column_still_works_thanks_to_the_default(self):
        """The live server runs old code until restarted; its INSERTs never mention seller_type."""
        engine = self.legacy_engine()
        migrate_seller_schema(engine)
        with engine.begin() as c:
            c.execute(text("INSERT INTO tracked_sellers (id, seller_id, nickname, active) VALUES (2, 'A2', 'New', 1)"))
            self.assertEqual(c.execute(text("SELECT seller_type FROM tracked_sellers WHERE id=2")).scalar(), "")


class PagesTests(_Db):
    def setUp(self):
        super().setUp()
        self.eu = self.seller("Beat it", "eu_a2a")
        self.mixed = self.seller("D&F Shop", "mixed")
        self.oa = self.seller("Oslos", "oa")
        self.wh = self.seller("Griffin", "oa_wholesale")
        self.unsorted = self.seller("Fresh")
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.text

    def names_on_sellers_page(self, url):
        html = self.get(url)
        return {n for n in ("Beat it", "D&amp;F Shop", "Oslos", "Griffin", "Fresh") if f">{n}</td>" in html}

    def test_sellers_page_lists_everyone_with_a_type_selector_per_row(self):
        html = self.get("/competitors/sellers")
        self.assertEqual(html.count('action="/competitors/set-type"'), 5)
        self.assertEqual(self.names_on_sellers_page("/competitors/sellers"), {"Beat it", "D&amp;F Shop", "Oslos", "Griffin", "Fresh"})

    def test_headline_chips_overlap_because_a_mixed_seller_does_both(self):
        html = self.get("/competitors/sellers")
        self.assertRegex(html, r"Does EU A2A\s*<span[^>]*>2</span>")            # EU A2A + mixed
        self.assertRegex(html, r"Does OA / Wholesale\s*<span[^>]*>3</span>")    # OA + OA/Wholesale + mixed
        self.assertRegex(html, r"Not sorted\s*<span[^>]*>1</span>")

    def test_filtering_by_the_overlapping_views(self):
        self.assertEqual(self.names_on_sellers_page("/competitors/sellers?stype=does_a2a"), {"Beat it", "D&amp;F Shop"})
        self.assertEqual(self.names_on_sellers_page("/competitors/sellers?stype=does_oa"), {"D&amp;F Shop", "Oslos", "Griffin"})
        self.assertEqual(self.names_on_sellers_page("/competitors/sellers?stype=mixed"), {"D&amp;F Shop"})
        self.assertEqual(self.names_on_sellers_page("/competitors/sellers?stype=unsorted"), {"Fresh"})
        self.assertIn("No tracked sellers in this group", self.get("/competitors/sellers?stype=garbage"))

    def test_setting_a_type_from_the_page_saves_it_and_returns_to_the_same_view(self):
        r = self.client.post("/competitors/set-type", follow_redirects=False,
                             data={"tracked_seller_id": self.unsorted, "seller_type": "mixed",
                                   "return_to": "/competitors/sellers?stype=unsorted"})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/competitors/sellers?stype=unsorted"))
        self.assertEqual(self.type_of(self.unsorted), "mixed")

    def test_a_junk_type_is_ignored_and_an_offsite_return_is_refused(self):
        self.client.post("/competitors/set-type", follow_redirects=False,
                         data={"tracked_seller_id": self.eu, "seller_type": "banana"})
        self.assertEqual(self.type_of(self.eu), "eu_a2a")
        for evil in ("https://evil.example/x", "//evil.example", "/competitors//evil.example", "/admin"):
            r = self.client.post("/competitors/set-type", follow_redirects=False,
                                 data={"tracked_seller_id": self.eu, "seller_type": "eu_a2a", "return_to": evil})
            self.assertEqual(r.headers["location"], "/competitors/sellers", evil)

    def test_apply_suggestions_button_reports_what_it_did_and_appears_only_when_useful(self):
        self.assertNotIn("not-yet-sorted from the evidence", self.get("/competitors/sellers"))   # nothing suggestable
        for tag, n in (("EU A2A", 30), ("Wholesale (likely)", 8), ("OA / unclear", 2)):
            for _ in range(n):
                self.detect(self.unsorted, tag, "Plain product")
        self.assertIn("Sort 1 not-yet-sorted from the evidence", self.get("/competitors/sellers"))
        r = self.client.post("/competitors/apply-suggested-types", follow_redirects=False, data={})
        self.assertEqual(r.status_code, 303)
        self.assertIn("check_result=Sorted%201%20seller", r.headers["location"])
        self.assertEqual(self.type_of(self.unsorted), "eu_a2a")

    def test_competitors_tab_shows_a_type_column_and_filters(self):
        html = self.get("/competitors?tab=competitors")
        self.assertIn(">Type</th>", html)
        self.assertIn("OA / Wholesale + A2A", html)
        filtered = self.get("/competitors?tab=competitors&stype=does_a2a")
        self.assertIn("Beat it", filtered)
        self.assertIn("D&amp;F Shop", filtered)
        self.assertNotIn("Oslos</td>", filtered)


class OpportunityFilterTests(_Db):
    def entry(self, seller_id, asin):
        listing = SimpleNamespace(asin=asin, tracked_seller_id=seller_id, detected_at=NOW, sourcing_reasoning_json=None,
                                  sourcing_tag="EU A2A")
        return {"listing": listing, "record": None}

    def build(self, **kw):
        from app.routes import competitors as routes
        entries = [self.entry(self.eu, "B0EUEUEUEU"), self.entry(self.mixed, "B0MIXMIXMI"),
                   self.entry(self.oa, "B0OAOAOAOA"), self.entry(self.unsorted, "B0UNSUNSUN")]
        with patch.object(routes.SellerWatchService, "list_notable_buyable", return_value=entries), \
             patch.object(routes.SellerWatchService, "list_historical_a2a_not_buyable", return_value=[]), \
             patch.object(routes.SellerWatchService, "list_oa_worth_investigating", return_value=[]), \
             patch.object(routes.SellerWatchService, "count_recent_detections", return_value=0), \
             patch.object(routes.ProductRepository, "get_watched_asins", return_value=set()):
            context = routes.build_opportunities_context(**kw)
        return sorted(e["listing"].asin for e in context["items"]), context

    def setUp(self):
        super().setUp()
        self.eu = self.seller("Beat it", "eu_a2a")
        self.mixed = self.seller("D&F", "mixed")
        self.oa = self.seller("Oslos", "oa")
        self.unsorted = self.seller("Fresh")

    def test_detections_can_be_filtered_by_how_the_seller_sources(self):
        everything, _ = self.build()
        self.assertEqual(len(everything), 4)
        self.assertEqual(self.build(stype="does_a2a")[0], ["B0EUEUEUEU", "B0MIXMIXMI"])
        self.assertEqual(self.build(stype="does_oa")[0], ["B0MIXMIXMI", "B0OAOAOAOA"])      # the mixed seller is in both
        self.assertEqual(self.build(stype="eu_a2a")[0], ["B0EUEUEUEU"])
        self.assertEqual(self.build(stype="unsorted")[0], ["B0UNSUNSUN"])

    def test_it_combines_with_the_other_filters_and_exposes_the_choices_to_the_page(self):
        items, context = self.build(stype="does_a2a", q="mix")
        self.assertEqual(items, ["B0MIXMIXMI"])
        self.assertEqual(context["stype"], "does_a2a")
        self.assertIn(("does_oa", "Does OA / Wholesale"), context["seller_type_filters"])
        self.assertIn(("unsorted", "Not sorted"), context["seller_type_filters"])


if __name__ == "__main__":
    unittest.main()
