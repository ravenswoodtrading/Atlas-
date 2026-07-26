from dataclasses import dataclass

from app.models.product import Product

from app.services.trend_engine import TrendEngine, TrendAnalysis
from app.services.scoring_engine import ScoringEngine
from app.services.confidence_engine import ConfidenceEngine


@dataclass
class OpportunityReport:
    asin: str
    title: str
    brand: str

    score: int
    confidence: int

    recommendation: str

    trend: TrendAnalysis


class OpportunityEngine:

    @staticmethod
    def analyse(product: Product) -> OpportunityReport:

        # Analyse trends
        trend = TrendEngine.calculate(product)

        # Calculate score
        score = ScoringEngine.score(product, trend)

        # Calculate confidence
        confidence = ConfidenceEngine.score(product, trend)

        # Recommendation
        if score >= 85 and confidence >= 80:
            recommendation = "BUY"

        elif score >= 65 and confidence >= 60:
            recommendation = "REVIEW"

        else:
            recommendation = "IGNORE"

        return OpportunityReport(
            asin=product.asin,
            title=product.title,
            brand=product.brand,
            score=score,
            confidence=confidence,
            recommendation=recommendation,
            trend=trend,
        )