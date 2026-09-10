"""Offline tests: isolated database, no Google Sheets calls or production writes."""
import unittest
from datetime import date
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from jinja2 import Environment, FileSystemLoader
from app.database.models import SkuPerformanceSnapshot, SkuDailyPerformance, VaSalesLine, VaSalesImport, ReportUpload
from app.services import actual_performance_service as imports
from app.services.va_performance_service import purchase_rows
from app.services.va_sales_allocation import allocate_sales

D = date.fromisoformat
HEADER = 'SKU\tASIN\tUnits\tProfit_Loss\tSales\tDate\tCoG\tRoI\tMargin\tCurrent_Stock_QTY\n'
SUMMARY = HEADER + 'sku\tB000000001\t12\t24\t120\tn/a\t80\t30\t20\t0\n'
DAILY = HEADER + 'sku\tB000000001\t12\t24\t120\t2026-04-05\t80\t30\t20\t0\n'

def purchase(id=2, quantity=10, day='2026-04-01'):
    return dict(id=id, asin='B000000001', purchased_on=D(day), quantity=quantity,
        expected_price=12., expected_profit=3., title='Example', submitted=D(day),
        cost=6., rating='Risky', comments='Price could fall', va_notes='', method='OA')

def sale(row, units, day='2026-04-05'):
    return dict(source_row=row, asin='B000000001', sku='sku', date=D(day), units=units,
        sales=units*10., profit=units*2., cog=units*8.)

class AllocationTests(unittest.TestCase):
    def test_cap_fifo_and_exclude_earlier_sales(self):
        result = allocate_sales([purchase(), purchase(3, 5, '2026-04-03')],
            [sale(2, 20, '2026-03-31'), sale(3, 12), sale(4, 8, '2026-04-06')], D('2026-03-01'), D('2026-04-30'))
        self.assertEqual([b['sold'] for b in result], [10, 5])
        self.assertEqual([b['actual_profit'] for b in result], [20, 10])
        self.assertEqual(result[0]['sell_through_days'], 4)
        self.assertEqual(result[1]['allocations'][0]['allocated'], 2)
        self.assertEqual(result[1]['allocations'][1]['allocated'], 3)
        self.assertEqual(result[0]['expected_sold_profit'], 30)
    def test_coverage_and_returns_are_not_zero_results(self):
        for sales, start in [([sale(2, 5)], '2026-04-02'), ([sale(2, -1)], '2026-04-01')]:
            b = allocate_sales([purchase()], sales, D(start), D('2026-04-30'))[0]
            self.assertEqual(b['status'], 'Needs review')
            self.assertEqual(b['sold'], 0)
    def test_partial_batch_and_missing_expectation(self):
        p = purchase(); p['expected_price'] = None
        b = allocate_sales([p], [sale(2, 4)], D('2026-04-01'), D('2026-04-30'))[0]
        self.assertEqual(b['remaining'], 6)
        self.assertIsNone(b['sell_through'])
        self.assertIsNone(b['price_gap'])
        self.assertEqual(b['elapsed_days'], 29)
    def test_undated_batch_does_not_erase_dated_history(self):
        p = purchase(); p['purchased_on'] = None
        batches = allocate_sales([p, purchase(3)], [sale(2, 20)], D('2026-04-01'), D('2026-04-30'))
        self.assertTrue(batches[0]['issue'])
        self.assertEqual(batches[0]['sold'], 0)
        self.assertFalse(batches[1]['issue'])
        self.assertEqual(batches[1]['sold'], 10)
    def test_sheet_fields_and_ambiguous_purchase(self):
        lead = {'ASIN':'B000000001', 'Date':'01 Apr 26', 'Purchased Qty':'10', 'Sale Price':'Â£12', 'Client Notes':'Risky', 'Expected Profit':'3'}
        buy = {'ASIN':'B000000001', 'Date Ordered':'02 Apr 26'}
        b = purchase_rows([lead], [buy])[0]
        self.assertEqual(b['purchased_on'], D('2026-04-02'))
        self.assertEqual(b['comments'], 'Risky')
        self.assertFalse(purchase_rows([lead], [buy, buy])[0]['issue'])
        other = dict(buy, **{'Date Ordered': '03 Apr 26'})
        self.assertTrue(purchase_rows([lead], [buy, other])[0]['issue'])

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        for model in (SkuPerformanceSnapshot, SkuDailyPerformance, VaSalesLine, VaSalesImport, ReportUpload):
            model.__table__.create(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.mock = patch.object(imports, 'SessionLocal', self.sessions); self.mock.start()
        imports.import_reports(SUMMARY, DAILY, '2026-04-01', '2026-04-30')
    def tearDown(self):
        self.mock.stop(); self.engine.dispose()
    def assert_preserved(self):
        with self.sessions() as db:
            self.assertEqual(db.query(SkuPerformanceSnapshot).one().units, 12)
            self.assertEqual(db.query(VaSalesLine).one().units, 12)
    def test_repeat_upload_does_not_duplicate(self):
        imports.import_reports(SUMMARY, DAILY, '2026-04-01', '2026-04-30')
        self.assert_preserved()
    def test_invalid_daily_preserves_both(self):
        for invalid in ['', DAILY.replace('2026-04-05','bad-date'), DAILY.replace('\t24\t','\tnan\t')]:
            with self.assertRaises(ValueError):
                imports.import_reports(SUMMARY.replace('\t12\t','\t8\t'), invalid, '2026-04-01', '2026-04-30')
            self.assert_preserved()
    def test_database_failure_rolls_back_deletes(self):
        with self.assertRaises(IntegrityError):
            imports.import_reports(SUMMARY + SUMMARY.splitlines()[1]+'\n', DAILY, '2026-04-01', '2026-04-30')
        self.assert_preserved()
    def test_dates_and_csv(self):
        with self.assertRaises(ValueError):
            imports.import_reports(SUMMARY, DAILY, '2026-04-10', '2026-04-30')
        imports.import_reports(SUMMARY.replace('\t',','), DAILY.replace('\t',','), '2026-04-01','2026-04-30')
        self.assert_preserved()

class TemplateTests(unittest.TestCase):
    def test_render_details_and_escape_product_text(self):
        env = Environment(loader=FileSystemLoader('app/templates'), autoescape=True)
        p = purchase(); p['title'] = '<script>bad()</script>'
        rows = allocate_sales([p], [sale(2, 4)], D('2026-04-01'), D('2026-04-30'))
        html = env.get_template('va_performance.html').render(errors=[], uploaded=None,
            **__import__('app.services.va_actuals_insights', fromlist=['report_view']).report_view(rows, [], __import__('app.services.va_actuals_insights', fromlist=['date_selection']).date_selection(), D('2026-04-30')))
        self.assertIn('Allocated daily sales lines', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>bad()', html)




class InsightTests(unittest.TestCase):
    def test_financial_or_thresholds_and_30_day_boundary(self):
        cases = [(25, 200, 100, 30, 'Met target'), (14, 100, 200, 30, 'Met target'),
                 (13.99, 100, 100, 30, 'Below financial target'),
                 (25, 200, 100, 31, 'Missed sell-through target')]
        from datetime import timedelta
        for profit, revenue, cog, days, target in cases:
            s = sale(2, 10); s.update(profit=profit, sales=revenue, cog=cog, date=D('2026-04-01')+timedelta(days=days))
            b = allocate_sales([purchase()], [s], D('2026-04-01'), D('2026-06-30'))[0]
            self.assertEqual(b['target_result'], target)
        s = sale(2,10); s.update(profit=10, cog=None)
        self.assertEqual(allocate_sales([purchase()], [s], D('2026-04-01'), D('2026-06-30'))[0]['target_result'], 'Insufficient data')
    def test_dates_and_allocation_before_filtering(self):
        from app.services.va_actuals_insights import date_selection, report_view
        self.assertEqual(date_selection('quarter', quarter='2026-Q2')['upper'], D('2026-06-30'))
        self.assertEqual(date_selection('month', month='2028-02')['upper'], D('2028-02-29'))
        with self.assertRaises(ValueError): date_selection('custom', start='2026-06-01', end='2026-04-01')
        batches = allocate_sales([purchase(), purchase(3,10,'2026-05-01')], [sale(2,12,'2026-05-05')], D('2026-04-01'), D('2026-06-30'))
        view = report_view(batches, [], date_selection('month', month='2026-05'), D('2026-06-30'))
        self.assertEqual(view['totals']['sold'], 2)
        self.assertEqual(view['totals']['purchases'], 1)
    def test_top_problem_and_replen_evidence(self):
        from app.services.va_actuals_insights import date_selection, report_view
        p = purchase(); p['expected_price'] = 9
        batches = allocate_sales([p], [sale(2,10)], D('2026-04-01'), D('2026-06-30'))
        buys = [dict(asin=p['asin'], date=D(day), quantity=qty, row=row, sku='sku')
                for day,qty,row in [('2026-04-01',10,2),('2026-05-01',20,3),('2026-07-01',50,4)]]
        view = report_view(batches, buys, date_selection(), D('2026-06-30'))
        self.assertEqual(len(view['insights']['top']), 1)
        self.assertEqual(view['insights']['replens'][0]['extra_units'],20)
        batches[0]['price_gap'] = -2
        self.assertTrue(report_view(batches,buys,date_selection(),D('2026-06-30'))['insights']['problems'])
    def test_migration_preserves_existing_rows_and_is_repeatable(self):
        from sqlalchemy import text, inspect
        from app.database.reporting_schema import migrate_reporting_schema
        engine = create_engine('sqlite://')
        with engine.begin() as db:
            db.execute(text('CREATE TABLE va_sales_lines (id INTEGER PRIMARY KEY, units INTEGER)'))
            db.execute(text('INSERT INTO va_sales_lines VALUES (1, 10)'))
        migrate_reporting_schema(engine); migrate_reporting_schema(engine)
        with engine.connect() as db:
            self.assertEqual(db.execute(text('SELECT units, cog FROM va_sales_lines')).one(), (10,None))
        engine.dispose()

class UploadPageTests(unittest.TestCase):
    def test_upload_metadata_and_failed_replacement(self):
        engine = create_engine('sqlite://')
        for model in (SkuPerformanceSnapshot,SkuDailyPerformance,VaSalesLine,VaSalesImport,ReportUpload):
            model.__table__.create(engine)
        with patch.object(imports,'SessionLocal',sessionmaker(bind=engine)):
            imports.import_reports(SUMMARY, DAILY,'2026-04-01','2026-04-30','summary.txt','daily.txt')
            before = imports.import_status()['uploads']
            with self.assertRaises(ValueError):
                imports.import_reports(SUMMARY,'bad','2026-04-01','2026-04-30','new.txt','bad.txt')
            self.assertEqual(imports.import_status()['uploads'],before)
            env=Environment(loader=FileSystemLoader('app/templates'),autoescape=True)
            html=env.get_template('report_uploads.html').render(status=imports.import_status())
            self.assertIn('summary.txt',html)
            self.assertIn('2026-04-30',html)
            self.assertIn('name="period_start"',html)
        engine.dispose()
    def test_http_routes_and_validation(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes import actual_performance, va_performance
        from app.services.va_actuals_insights import date_selection,report_view
        app=FastAPI(); app.include_router(actual_performance.router); app.include_router(va_performance.router)
        def context(period='all',month='',quarter='',start='',end=''):
            return dict(report_view([],[],date_selection(period,month,quarter,start,end),D('2026-04-30')),
                errors=[],uploaded=None,period_start=D('2026-04-01'),period_end=D('2026-04-30'))
        with TestClient(app) as client, patch.object(va_performance,'performance_report',side_effect=context), patch.object(imports,'import_status',return_value=dict(ready=False,summary_rows=0,daily_rows=0,uploads={})):
            self.assertEqual(client.get('/reports/uploads').status_code,200)
            response=client.get('/reports/va/performance?period=quarter&quarter=2026-Q2')
            self.assertEqual(response.status_code,200)
            self.assertIn('2026-Q2',response.text)
            self.assertNotIn('type="file"',response.text)
            self.assertEqual(client.get('/reports/va/performance?period=quarter&quarter=2026-Q5').status_code,400)
            self.assertEqual(client.post('/reports/uploads').status_code,422)

if __name__ == '__main__':
    unittest.main()
