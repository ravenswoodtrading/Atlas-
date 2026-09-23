"""Digital Services Fee in FeeEngine (2026-09-21): 2% of (referral + FBA fee) comes off profit everywhere profit/ROI/max
cost is worked out; the prep fee is still counted; storage and inbound shipping are deliberately not modelled.
Pure arithmetic -- no database, no network.

The worked example is B0DL6FD23C (Hot Wheels Track Creator) with Atlas's own stored inputs: cost 25.68 (Italy; netted by UK VAT 20%
since Tamara reclaims UK VAT only on EU FBA), UK price 43.51, FBA 3.59, referral 6.53. Originally: profit 4.64, ROI 18.07%. SAS showed 10.01% for it (it also
charges storage 0.97 and inbound 0.33 -- which Tamara chose not to model -- plus VAT, FBA and prep differences)."""
import unittest

from app.config.fees import DIGITAL_SERVICES_FEE_RATE, EU_VAT_RATE_BY_MARKETPLACE, PREP_FEE_GBP, UK_VAT_STANDARD_RATE
from app.models.product import Product
from app.services.fee_engine import FeeEngine

CATEGORY = "Toys & Games"


def hot_wheels(**kw):
    fields = dict(asin="B0DL6FD23C", title="Hot Wheels", brand="Mattel", category="x", buy_box_now=43.51,
                  buy_box_90d=46.23, buy_box_max_90d=52.00, best_source_marketplace="IT", best_source_cost_gbp=25.68,
                  fba_fee=3.59)
    fields.update(kw)
    return Product(**fields)


class EuVatIsUkVatTests(unittest.TestCase):
    """Tamara (2026-09-21): "We are charged UK VAT so will reclaim this and this only on Amazon FBA EU" -- every EU source
    cost is netted by the UK 20%, not the local rate."""

    def test_every_eu_marketplace_uses_the_uk_rate(self):
        self.assertEqual(EU_VAT_RATE_BY_MARKETPLACE, {"DE": UK_VAT_STANDARD_RATE, "FR": UK_VAT_STANDARD_RATE,
                                                     "IT": UK_VAT_STANDARD_RATE, "ES": UK_VAT_STANDARD_RATE})
        self.assertEqual(UK_VAT_STANDARD_RATE, 0.20)

    def test_the_same_cost_gives_the_same_profit_whichever_eu_site_it_came_from(self):
        results = {m: FeeEngine.calculate(hot_wheels(best_source_marketplace=m), CATEGORY) for m in ("DE", "FR", "IT", "ES", "??")}
        self.assertEqual(len({(r.profit, r.roi, r.eu_vat_rate_used) for r in results.values()}), 1)
        self.assertEqual(results["IT"].eu_vat_rate_used, 0.20)

    def test_italian_cost_is_netted_by_20_not_22(self):
        r = FeeEngine.calculate(hot_wheels(best_source_marketplace="IT"), CATEGORY)
        by_hand_at_20 = 43.51 / 1.2 - 3.59 - 6.53 - 0.45 - 0.20 - 25.68 / 1.20
        by_hand_at_22 = 43.51 / 1.2 - 3.59 - 6.53 - 0.45 - 0.20 - 25.68 / 1.22
        self.assertAlmostEqual(r.profit, by_hand_at_20, delta=0.01)
        self.assertGreater(abs(r.profit - by_hand_at_22), 0.3)                   # 35p apart: the tests can tell them apart

    def test_a_german_cost_now_nets_more_vat_off_than_it_used_to(self):
        de = FeeEngine.calculate(hot_wheels(best_source_marketplace="DE"), CATEGORY)
        at_19 = 43.51 / 1.2 - 3.59 - 6.53 - 0.45 - 0.20 - 25.68 / 1.19
        self.assertGreater(de.profit, at_19 - 0.005 + 0.0)


class DigitalServicesFeeTests(unittest.TestCase):
    def test_the_fee_is_two_percent_of_referral_plus_fba(self):
        self.assertEqual(DIGITAL_SERVICES_FEE_RATE, 0.02)
        self.assertEqual(FeeEngine._digital_services_fee(6.54, 3.69), 0.20)        # SAS's own line for this product
        self.assertEqual(FeeEngine._digital_services_fee(6.53, 3.59), 0.20)
        self.assertEqual(FeeEngine._digital_services_fee(15.0, 5.0), 0.40)
        self.assertEqual(FeeEngine._digital_services_fee(0.0, 0.0), 0.0)

    def test_calculate_takes_it_off_profit_and_reports_it(self):
        r = FeeEngine.calculate(hot_wheels(), CATEGORY)
        self.assertEqual((r.referral_fee, r.fba_fee), (6.53, 3.59))
        self.assertEqual(r.digital_services_fee, 0.20)
        # 43.51/1.2 - 3.59 - 6.53 - 0.45 prep - 0.20 DSF - 25.68/1.20 = 4.09 (4.64 before the fee and the VAT rule)
        self.assertEqual(r.profit, 4.09)
        self.assertEqual(r.roi, round(4.09 / 25.68 * 100, 2))
        self.assertAlmostEqual(r.roi, 15.93, places=2)

    def test_the_90_day_and_peak_figures_carry_their_own_fee_at_their_own_price(self):
        p = hot_wheels()
        r = FeeEngine.calculate(p, CATEGORY)
        for price, profit in ((p.buy_box_90d, r.profit_90d), (p.buy_box_max_90d, r.profit_peak)):
            referral = FeeEngine._referral_fee(price, CATEGORY.lower())
            dsf = FeeEngine._digital_services_fee(referral, 3.59)
            expected = round(price / 1.2 - 3.59 - referral - PREP_FEE_GBP - dsf - 25.68 / 1.20, 2)
            self.assertEqual(profit, expected, price)

    def test_profit_is_exactly_the_old_profit_minus_the_fee_across_prices_and_costs(self):
        for price in (12.0, 19.99, 30.0, 43.51, 80.0, 150.0):
            for cost in (5.0, 12.5, 25.68, 60.0):
                new = FeeEngine.roi_at_price(price, cost, CATEGORY, 3.59, 0.22)
                old = FeeEngine.roi_at_price(price, cost, CATEGORY, 3.59, 0.22, include_dsf=False)
                referral = FeeEngine._referral_fee(price, CATEGORY.lower())
                dsf = FeeEngine._digital_services_fee(referral, 3.59)
                self.assertAlmostEqual(old - new, dsf / cost * 100, delta=0.02, msg=(price, cost))
                self.assertLess(new, old)

    def test_the_old_formula_is_still_available_for_checking_old_records(self):
        self.assertEqual(FeeEngine.roi_at_price(43.51, 25.68, CATEGORY, 3.59, 0.22, include_dsf=False), 18.07)

    def test_prep_is_still_counted(self):
        r = FeeEngine.calculate(hot_wheels(), CATEGORY)
        self.assertEqual(r.prep_fee, PREP_FEE_GBP)
        self.assertEqual(PREP_FEE_GBP, 0.45)                                     # net of VAT; 0.54 with it
        with unittest.mock.patch("app.services.fee_engine.PREP_FEE_GBP", 1.45):
            dearer = FeeEngine.calculate(hot_wheels(), CATEGORY)
        self.assertAlmostEqual(r.profit - dearer.profit, 1.00, places=2)         # every extra £ of prep is a £ off profit

    def test_storage_and_inbound_shipping_are_not_modelled(self):
        """Tamara's decision (2026-09-21): only the digital services fee is added. Profit must not depend on anything else."""
        r = FeeEngine.calculate(hot_wheels(), CATEGORY)
        by_hand = 43.51 / 1.2 - 3.59 - 6.53 - 0.45 - 0.20 - 25.68 / 1.20
        self.assertAlmostEqual(r.profit, by_hand, delta=0.01)

    def test_max_source_cost_leaves_room_for_the_fee(self):
        """The highest cost that still hits a target must really hit it once the fee is taken off (OA maths: UK VAT on cost)."""
        for price, target in ((43.51, 25.0), (30.0, 25.0), (80.0, 30.0)):
            cost = FeeEngine.max_source_cost(price, CATEGORY, 3.59, target)
            referral = FeeEngine._referral_fee(price, CATEGORY.lower())
            dsf = FeeEngine._digital_services_fee(referral, 3.59)
            profit = price / 1.2 - 3.59 - referral - PREP_FEE_GBP - dsf - cost / 1.2
            self.assertAlmostEqual(profit / cost * 100, target, delta=0.1, msg=(price, target))
        # ... and it is lower than it would have been without the fee
        with_fee = FeeEngine.max_source_cost(43.51, CATEGORY, 3.59, 25.0)
        without = (43.51 / 1.2 - 3.59 - 6.53 - PREP_FEE_GBP) / (0.25 + 1 / 1.2)
        self.assertLess(with_fee, round(without, 2))

    def test_the_margin_and_profit_ceilings_also_leave_room_for_the_fee(self):
        price, fba = 43.51, 3.59
        referral = FeeEngine._referral_fee(price, CATEGORY.lower())
        dsf = FeeEngine._digital_services_fee(referral, fba)
        for cost, fn, target in ((FeeEngine.max_source_cost_for_profit(price, CATEGORY, fba, 2.0), "profit", 2.0),
                                 (FeeEngine.max_source_cost_for_margin(price, CATEGORY, fba, 13.0), "margin", price * 0.13)):
            profit = price / 1.2 - fba - referral - PREP_FEE_GBP - dsf - cost / 1.2
            self.assertAlmostEqual(profit, target, delta=0.02, msg=fn)

    def test_a_missing_fba_fee_uses_the_default_and_its_fee_is_charged_on_that(self):
        with_default = FeeEngine.calculate(hot_wheels(fba_fee=0.0), CATEGORY)
        self.assertEqual(with_default.digital_services_fee, FeeEngine._digital_services_fee(
            FeeEngine._referral_fee(43.51, CATEGORY.lower()), FeeEngine.DEFAULT_FBA_FEE))

    def test_no_cost_means_no_profit_calculation_at_all(self):
        r = FeeEngine.calculate(hot_wheels(best_source_cost_gbp=0.0), CATEGORY)
        self.assertEqual((r.profit, r.roi), (0.0, 0.0))


import unittest.mock  # noqa: E402  (used by test_prep_is_still_counted)

if __name__ == "__main__":
    unittest.main()
