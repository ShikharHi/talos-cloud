from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field

from auth.repository import auth_repo
from marketplace.repositories.plugins import PluginRepository
from marketplace.repositories.ratings import RatingRepository
from marketplace.repositories.reviews import ReviewRepository
from marketplace.repositories.builders import BuilderRepository
from marketplace.services.ratings import ratings_service
from marketplace.plugin_router import get_current_user_required

router = APIRouter(prefix="/v1/marketplace", tags=["User Actions"])

plugin_repo = PluginRepository()
rating_repo = RatingRepository()
review_repo = ReviewRepository()
builder_repo = BuilderRepository()

class RatingRequest(BaseModel):
    stars: int = Field(..., ge=1, le=5)
    version_used: Optional[str] = None

class ReviewRequest(BaseModel):
    body: str = Field(..., min_length=2, max_length=2000)
    stars: Optional[int] = Field(None, ge=1, le=5)
    version_used: Optional[str] = None

class ReplyRequest(BaseModel):
    body: str = Field(..., min_length=2, max_length=2000)

class ReportRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)

@router.put("/plugins/{plugin_id}/rating")
async def rate_plugin(plugin_id: str, payload: RatingRequest, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Rate a plugin (1-5 stars). Requires verified install download and blocks self-rating."""
    user_id = user["id"]

    # Check plugin existence and prevent self-rating
    plugin = await plugin_repo.get_plugin_by_id(plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    if plugin.get("builder_id") == user_id or plugin.get("publisher_id") == user_id:
        raise HTTPException(status_code=400, detail="Builders cannot rate their own plugins")

    # Verify download installation gate
    verified = await plugin_repo.check_user_install_verified(user_id, plugin_id)
    if not verified:
        raise HTTPException(status_code=403, detail="You must install and download this plugin before rating it")

    # Upsert rating atomically
    rating = await rating_repo.upsert_rating(user_id, plugin_id, payload.stars, payload.version_used)

    # Recompute Bayesian scores
    await ratings_service.recompute_plugin_and_builder(plugin_id)
    return {"status": "success", "rating": rating}

@router.delete("/plugins/{plugin_id}/rating")
async def delete_rating(plugin_id: str, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Remove user's rating."""
    user_id = user["id"]
    removed = await rating_repo.delete_rating(user_id, plugin_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Rating not found")
    await ratings_service.recompute_plugin_and_builder(plugin_id)
    return {"status": "success"}

@router.post("/plugins/{plugin_id}/reviews")
async def post_review(plugin_id: str, payload: ReviewRequest, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Submit a written review. Requires verified install download."""
    user_id = user["id"]

    plugin = await plugin_repo.get_plugin_by_id(plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    if plugin.get("builder_id") == user_id or plugin.get("publisher_id") == user_id:
        raise HTTPException(status_code=400, detail="Builders cannot review their own plugins")

    verified = await plugin_repo.check_user_install_verified(user_id, plugin_id)
    if not verified:
        raise HTTPException(status_code=403, detail="You must install and download this plugin before reviewing it")

    rating_id = None
    if payload.stars:
        rating = await rating_repo.upsert_rating(user_id, plugin_id, payload.stars, payload.version_used)
        rating_id = rating["id"]
        await ratings_service.recompute_plugin_and_builder(plugin_id)
    else:
        existing_rating = await rating_repo.get_user_rating(user_id, plugin_id)
        if existing_rating:
            rating_id = existing_rating["id"]

    review = await review_repo.create_review(
        user_id=user_id,
        plugin_id=plugin_id,
        body=payload.body,
        rating_id=rating_id
    )
    return {"status": "success", "review": review}

@router.post("/reviews/{review_id}/reply")
async def reply_to_review(review_id: str, payload: ReplyRequest, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Reply to a review. Strictly restricted to the authoring builder."""
    user_id = user["id"]
    try:
        reply = await review_repo.create_review(
            user_id=user_id,
            plugin_id="",
            body=payload.body,
            parent_id=review_id
        )
        return {"status": "success", "reply": reply}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

@router.post("/reviews/{review_id}/helpful")
async def vote_helpful(review_id: str, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Toggle a helpful vote on a review."""
    user_id = user["id"]
    res = await review_repo.vote_helpful(user_id, review_id)
    return res

@router.post("/reviews/{review_id}/report")
async def report_review(review_id: str, payload: ReportRequest, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Report an abusive or spam review for moderator review."""
    user_id = user["id"]
    res = await review_repo.report_review(user_id, review_id, payload.reason)
    return {"status": "reported", "report_id": res["id"]}
