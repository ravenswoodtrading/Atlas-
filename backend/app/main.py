from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database.base import Base
from app.database.database import engine

from app.routes.dashboard import router as dashboard_router
from app.routes.products import router as products_router
from app.routes.add_product import router as add_product_router
from app.routes.analyse import router as analyse_router
from app.routes.keepa import router as keepa_router
from app.routes.scan import router as scan_router

app = FastAPI(
    title="Atlas",
    version="0.3.0"
)

Base.metadata.create_all(bind=engine)

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static"
)

app.include_router(dashboard_router)
app.include_router(products_router)
app.include_router(add_product_router)
app.include_router(analyse_router)
app.include_router(keepa_router)
app.include_router(scan_router)