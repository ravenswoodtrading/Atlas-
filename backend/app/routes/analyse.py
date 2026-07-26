from dataclasses import asdict

from fastapi import APIRouter

from app.models.product import Product
from app.services.opportunity_engine import OpportunityEngine

router = APIRouter()


@router.get("/analyse/{asin}")
def analyse(asin: str):

    product = Product(
        asin=asin,
        title="Test Product",
        brand="Atlas",
        category="Testing",

        buy_box_now=20,
        buy_box_90d=21,

        offers_now=12,
        offers_90d=18,

        sales_rank_now=1200,
        sales_rank_90d=1800,

        sales_drops_30d=35,

        uk_cost=10,
        fr_cost=8,
        de_cost=9,
        it_cost=11,
        es_cost=10,

        profit=12,
        roi=40,
    )

    result = OpportunityEngine.analyse(product)

    return asdict(result)