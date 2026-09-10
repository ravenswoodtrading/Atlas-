import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from va_price_drop_audit import EPOCH, price_on_day, classification


def minute(value):
    return int((datetime.fromisoformat(value).replace(tzinfo=timezone.utc)-EPOCH).total_seconds()/60)


class HistoricalBuyBox(unittest.TestCase):
    def test_threshold_is_strict(self):
        self.assertEqual(classification('£21.00', Decimal('20')), 'No')
        self.assertEqual(classification('£21.01', Decimal('20')), 'Yes')
        self.assertEqual(classification('19', Decimal('20')), 'No')

    def test_no_buy_box_does_not_reuse_previous_available_price(self):
        series = [minute('2026-07-08T12:00'), 2000, 0,
                  minute('2026-07-09T15:00'), -1, -1]
        self.assertIsNone(price_on_day(series, date(2026, 7, 9)))
        self.assertEqual(classification('25', None), 'Unknown')

    def test_shipping_and_uk_midnight(self):
        series = [minute('2026-07-08T12:00'), 2000, 200,
                  minute('2026-07-09T23:00'), 3000, 0]
        self.assertEqual(price_on_day(series, date(2026, 7, 9)), Decimal('22'))

    def test_missing_history_or_target(self):
        self.assertIsNone(price_on_day([minute('2026-07-10T12:00'), 2000, 0], date(2026,7,9)))
        self.assertEqual(classification('', Decimal('20')), 'Unknown')
        self.assertEqual(classification('NaN', Decimal('20')), 'Unknown')


if __name__ == '__main__':
    unittest.main()
