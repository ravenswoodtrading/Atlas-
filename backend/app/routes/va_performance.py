from fastapi import APIRouter, Request, HTTPException
from fastapi.templating import Jinja2Templates
from app.services.va_performance_service import performance_report

router = APIRouter()
templates = Jinja2Templates(directory='app/templates')

@router.get('/reports/va/performance')
def page(request: Request, period: str = 'all', month: str = '', quarter: str = '',
         start_date: str = '', end_date: str = ''):
    try:
        context = performance_report(period, month, quarter, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return templates.TemplateResponse(request=request, name='va_performance.html', context=context)
