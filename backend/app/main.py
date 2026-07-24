from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.app.routes.dashboard import router as dashboard_router
from backend.app.routes.products import router as products_router
from backend.app.database.base import Base
from backend.app.database.database import engine
from backend.app.models.product import Product

app = FastAPI(
    title="Atlas",
    version="0.3.0"
)

Base.metadata.create_all(bind=engine)

app.mount(
    "/static",
    StaticFiles(directory="backend/app/static"),
    name="static"
)

app.include_router(dashboard_router)
app.include_router(products_router)