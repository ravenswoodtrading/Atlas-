"""Local UI review with synthetic data only; no production database or sheets."""
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import va_performance, actual_performance
from app.services.va_actuals_insights import date_selection, report_view
from app.services.va_sales_allocation import allocate_sales
from test_va_actuals import purchase, sale, D

p1=purchase(); p1.update(title='Demo — Fast seller',expected_price=9)
p2=purchase(3); p2.update(asin='B000000002',title='Demo — Slow seller')
s1=sale(2,10)
s2=sale(3,4); s2.update(asin=p2['asin'],profit=-4)
batches=allocate_sales([p1,p2],[s1,s2],D('2026-04-01'),D('2026-06-30'))
buys=[dict(row=2,asin=p1['asin'],date=D('2026-04-01'),quantity=10,sku='demo'),dict(row=3,asin=p1['asin'],date=D('2026-05-01'),quantity=20,sku='demo-replen')]
def report(period='all',month='',quarter='',start='',end=''):
    return dict(report_view(batches,buys,date_selection(period,month,quarter,start,end),D('2026-06-30')),
        errors=['PREVIEW: synthetic example data, not your business results.'],period_start=D('2026-04-01'),period_end=D('2026-06-30'),uploaded=datetime(2026,7,1,12))
va_performance.performance_report=report
actual_performance.performance.import_status=lambda: dict(ready=False,summary_rows=0,daily_rows=0,uploads={})
app=FastAPI()
app.include_router(va_performance.router)
# Only read-only preview pages. Uploads cannot mutate production data.
app.add_api_route('/reports/uploads',actual_performance.report_uploads,methods=['GET'])
app.mount('/static',StaticFiles(directory='app/static'),name='static')
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8134)
