"""Validation for user-submitted review decisions, before any writes."""
from fastapi import HTTPException

from app.services.review_queue_service import REVIEW_REASON_CATEGORIES


def validate_rejection_reason(reason: str, category: str, *, require_category: bool = False):
    reason, category = reason.strip(), category.strip()
    if category and category not in REVIEW_REASON_CATEGORIES:
        raise HTTPException(422, "Please choose a valid rejection reason.")
    if not category and (require_category or not reason):
        raise HTTPException(422, "Please provide a reason before rejecting this item.")
    if category == "OTHER" and not reason:
        raise HTTPException(422, 'Please add a note explaining "Other".')
    return reason, category
