from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from marketplace.repositories.reviews import ReviewRepository
from marketplace.repositories.builders import BuilderRepository
from marketplace.plugin_router import get_current_user_required

from auth.service import get_current_user

router = APIRouter(prefix="/v1/marketplace/admin", tags=["Admin Moderation"])

review_repo = ReviewRepository()
builder_repo = BuilderRepository()

def require_admin_user(user: Dict[str, Any] = Depends(get_current_user)):
    email = user.get("email", "").lower().strip()
    if email == "shikharjadav7@gmail.com" or user.get("is_admin"):
        return user
    raise HTTPException(status_code=403, detail="Admin permissions required")

class ModerationAction(BaseModel):
    status: str = Field(..., description="visible | hidden | removed")

@router.get("/reports")
async def list_flagged_reviews(status: str = "open", limit: int = 50, admin: Dict[str, Any] = Depends(require_admin_user)):
    """List flagged reviews for moderation review."""
    return await review_repo.list_reports(status=status, limit=limit)

@router.post("/reviews/{review_id}/moderate")
async def moderate_review(review_id: str, action: ModerationAction, admin: Dict[str, Any] = Depends(require_admin_user)):
    """Hide or remove a flagged review."""
    success = await review_repo.set_review_status(review_id, action.status)
    if not success:
        raise HTTPException(status_code=404, detail="Review not found")
    return {"status": "success", "review_status": action.status}

@router.post("/builders/{builder_id}/verify")
async def verify_builder(builder_id: str, verified: bool = True, admin: Dict[str, Any] = Depends(require_admin_user)):
    """Manually grant or revoke verified badge for a builder."""
    with builder_repo.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE builders SET is_verified = %s WHERE id = %s", (verified, builder_id))
            conn.commit()
    return {"status": "success", "is_verified": verified}
