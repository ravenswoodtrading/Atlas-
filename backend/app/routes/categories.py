from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.category_survey_service import CategorySurveyService

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/categories")
def categories_page(request: Request, brand: str = "", limit: int = 100):
    result = None

    if brand:
        survey = CategorySurveyService()
        result = survey.survey(brand, limit=limit)

    return templates.TemplateResponse(
        request=request,
        name="categories.html",
        context={
            "request": request,
            "brand": brand,
            "limit": limit,
            "result": result,
        }
    )
