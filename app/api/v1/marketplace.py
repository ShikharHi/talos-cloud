"""
Talos Cloud — Canonical Marketplace API (v1).

Prefix: /api/v1/marketplace
"""

from __future__ import annotations

import uuid
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domain.marketplace.errors import MarketplaceError
from app.infrastructure.security.ssrf_guard import safe_fetch_url
from app.models.accounts import Account
from app.routers.auth import get_current_session
from app.routers.relay import get_authenticated_account
from app.services.identity_service import WebSession
from app.services.marketplace.download_service import DownloadService
from app.services.marketplace.install_service import InstallService
from app.services.marketplace.listing_service import ListingService
from app.services.marketplace.moderation_service import ModerationService
from app.services.marketplace.upload_service import UploadService

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace-v1"])


async def get_optional_account(
    session: Optional[WebSession] = Depends(get_current_session),
    relay_account: Optional[Account] = Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
) -> Optional[Account]:
    if relay_account is not None:
        return relay_account
    if session and session.account_id:
        from app.repositories.account_repo import AccountRepository
        return await AccountRepository(db).get_by_id(session.account_id)
    return None


async def require_account(account: Optional[Account] = Depends(get_optional_account)) -> Account:
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required for this marketplace action.",
        )
    return account


# ── Pydantic Request / Response Schemas ─────────────────────────────────────

class ListingCreateRequest(BaseModel):
    publisher_slug: str = Field(..., min_length=2, max_length=100)
    slug: str = Field(..., min_length=2, max_length=100)
    kind: str = Field(..., description="agent | skill | mcp | tool")
    display_name: str = Field(..., min_length=1, max_length=200)
    tagline: str = Field(default="", max_length=300)
    description: str = Field(default="")
    icon_emoji: str = Field(default="📦", max_length=10)
    icon_color: str = Field(default="#a3e635", max_length=20)
    tags: List[str] = Field(default_factory=list)
    manifest_yaml: str = Field(default="")
    visibility: str = Field(default="public")


class ListingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    listing_id: uuid.UUID
    publisher_slug: str
    slug: str
    full_slug: str
    kind: str
    display_name: str
    tagline: str
    description: str
    icon_emoji: str
    icon_color: str
    tags: List[str]
    status: str
    visibility: str
    version: str
    install_count: int
    is_builtin: bool
    created_at: Any
    updated_at: Any


class VersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_id: uuid.UUID
    listing_id: uuid.UUID
    version: str
    file_size: int
    sha256: str
    manifest_json: Optional[dict[str, Any]] = None
    security_status: str
    status: str
    created_at: Any
    published_at: Optional[Any] = None


class UploadInitRequest(BaseModel):
    listing_id: uuid.UUID
    version: str = Field(..., max_length=50)
    file_size: Optional[int] = None
    sha256: Optional[str] = None


class UploadCompleteRequest(BaseModel):
    upload_id: uuid.UUID
    async_verification: bool = True


class InstallInitRequest(BaseModel):
    listing_id: Optional[uuid.UUID] = None
    publisher: Optional[str] = None
    slug: Optional[str] = None
    version: Optional[str] = None


class InstallCompleteRequest(BaseModel):
    listing_id: uuid.UUID
    install_token: str


class RemoteImportRequest(BaseModel):
    listing_id: uuid.UUID
    version: str
    source_url: str


class ReviewRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field(default="", max_length=2000)


class MarketplaceInstallPayload(BaseModel):
    listing_id: Optional[uuid.UUID] = None
    author: Optional[str] = None
    slug: Optional[str] = None
    version: Optional[str] = None


# ── Listing Routes ─────────────────────────────────────────────────────────

@router.get("/listings", response_model=List[ListingResponse])
async def search_listings(
    q: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    publisher: Optional[str] = Query(None),
    status: Optional[str] = Query("approved"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    status_str = status if isinstance(status, str) else "approved"
    service = ListingService(db)
    items, _ = await service.search(
        q=q, kind=kind, tag=tag, publisher=publisher, status=status_str, page=page, page_size=page_size
    )
    return items


@router.get("/listings/{listing_id}", response_model=ListingResponse)
async def get_listing(listing_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    service = ListingService(db)
    try:
        return await service.get_by_id(listing_id)
    except MarketplaceError as e:
        raise HTTPException(status_code=404, detail=e.message)


@router.post("/listings", response_model=ListingResponse, status_code=status.HTTP_201_CREATED)
async def create_listing(
    payload: ListingCreateRequest,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = ListingService(db)
    try:
        return await service.create_listing(
            account=account,
            publisher_slug=payload.publisher_slug,
            slug=payload.slug,
            kind=payload.kind,
            display_name=payload.display_name,
            tagline=payload.tagline,
            description=payload.description,
            icon_emoji=payload.icon_emoji,
            icon_color=payload.icon_color,
            tags=payload.tags,
            manifest_yaml=payload.manifest_yaml,
            visibility=payload.visibility,
        )
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.post("/listings/{listing_id}/unpublish")
async def unpublish_listing(
    listing_id: uuid.UUID,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = ListingService(db)
    try:
        await service.unpublish(account, listing_id)
        return {"ok": True, "status": "tombstoned"}
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)


# ── Upload & Publishing Routes ──────────────────────────────────────────────

@router.post("/uploads/init")
async def init_upload(
    payload: UploadInitRequest,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = UploadService(db)
    try:
        return await service.init_upload(
            account=account,
            listing_id=payload.listing_id,
            version=payload.version,
            file_size=payload.file_size,
            sha256=payload.sha256,
        )
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.post("/uploads/complete")
@router.post("/upload/complete")
async def complete_upload(
    payload: UploadCompleteRequest,
    response: Response,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = UploadService(db)
    try:
        res = await service.complete_upload(
            account=account,
            upload_id=payload.upload_id,
            async_verification=payload.async_verification,
        )
        if res.get("status") == "verifying":
            response.status_code = status.HTTP_202_ACCEPTED
        return res
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.get("/uploads/{upload_id}")
async def get_upload_status(
    upload_id: uuid.UUID,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = UploadService(db)
    try:
        return await service.get_upload_status(account, upload_id)
    except MarketplaceError as e:
        raise HTTPException(status_code=404, detail=e.message)


# ── SSRF-Protected Remote Source Ingestion ──────────────────────────────────

@router.post("/source/import", status_code=status.HTTP_202_ACCEPTED)
async def import_remote_source(
    payload: RemoteImportRequest,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    """Safely downloads package archive from approved remote HTTPS source (e.g. GitHub)."""
    service = UploadService(db)
    init_res = await service.init_upload(
        account=account,
        listing_id=payload.listing_id,
        version=payload.version,
    )

    upload_id = uuid.UUID(init_res["upload_id"])

    # Download safely using SSRF guard
    try:
        archive_bytes = await safe_fetch_url(payload.source_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch remote source: {exc}")

    # Upload directly to Tigris staging key
    staging_key = init_res["staging_key"]
    await service.storage._storage.upload(staging_key, archive_bytes, content_type="application/zip")

    # Trigger complete
    return await service.complete_upload(account=account, upload_id=upload_id, async_verification=True)


# ── Installation Routes (Two-Phase) ─────────────────────────────────────────

@router.post("/installs/init")
async def init_install(
    payload: InstallInitRequest,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = InstallService(db)
    try:
        return await service.init_install(
            account=account,
            listing_id=payload.listing_id,
            publisher=payload.publisher,
            slug=payload.slug,
            requested_version=payload.version,
        )
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.post("/installs/complete")
async def complete_install(
    payload: InstallCompleteRequest,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = InstallService(db)
    try:
        return await service.complete_install(
            account=account,
            listing_id=payload.listing_id,
            install_token=payload.install_token,
        )
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.delete("/installs/{listing_id}")
async def uninstall_listing(
    listing_id: uuid.UUID,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = InstallService(db)
    return await service.uninstall(account, listing_id=listing_id)


@router.get("/installs")
async def list_installs(
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = InstallService(db)
    items = await service.list_user_installs(account)
    return [
        {
            "install_id": str(i.install_id),
            "listing_id": str(i.listing_id),
            "version": i.installed_version,
            "status": i.status,
            "installed_at": i.installed_at.isoformat() if i.installed_at else None,
            "listing": {
                "display_name": i.listing.display_name if i.listing else "",
                "slug": i.listing.slug if i.listing else "",
                "publisher_slug": i.listing.publisher_slug if i.listing else "",
                "kind": i.listing.kind if i.listing else "",
            },
        }
        for i in items
    ]


# ── Download Route (Zero GET side-effects) ──────────────────────────────────

@router.get("/download/{publisher}/{slug}")
async def get_download_url(
    publisher: str,
    slug: str,
    version: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    service = DownloadService(db)
    try:
        url = await service.get_download_url(publisher, slug, version=version)
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    except MarketplaceError as e:
        raise HTTPException(status_code=404, detail=e.message)


# ── Reviews ────────────────────────────────────────────────────────────────

@router.get("/listings/{listing_id}/reviews")
async def list_reviews(
    listing_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    from app.repositories.marketplace.review_repo import ReviewRepository
    repo = ReviewRepository(db)
    items, total = await repo.list_by_listing(listing_id, page=page, page_size=page_size)
    return {
        "items": [
            {
                "review_id": str(r.review_id),
                "account_id": str(r.account_id),
                "rating": r.rating,
                "comment": r.comment,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in items
        ],
        "total": total,
    }


@router.post("/listings/{listing_id}/reviews")
async def create_or_update_review(
    listing_id: uuid.UUID,
    payload: ReviewRequest,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    from app.repositories.marketplace.review_repo import ReviewRepository
    repo = ReviewRepository(db)
    review = await repo.upsert_review(listing_id, account.account_id, payload.rating, payload.comment)
    return {"ok": True, "review_id": str(review.review_id), "rating": review.rating}


# ── Admin Moderation ───────────────────────────────────────────────────────

@router.post("/admin/{listing_id}/approve")
async def admin_approve(
    listing_id: uuid.UUID,
    reason: Optional[str] = None,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = ModerationService(db)
    try:
        listing = await service.approve(account, listing_id, reason=reason)
        return {"ok": True, "status": listing.status}
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.post("/admin/{listing_id}/reject")
async def admin_reject(
    listing_id: uuid.UUID,
    reason: Optional[str] = None,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = ModerationService(db)
    try:
        listing = await service.reject(account, listing_id, reason=reason)
        return {"ok": True, "status": listing.status}
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)


# ── Compatibility Aliases ──────────────────────────────────────────────────

@router.get("/search", response_model=List[ListingResponse])
async def search_alias(
    q: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    author: Optional[str] = Query(None),
    status: Optional[str] = Query("approved"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    status_str = status if isinstance(status, str) else "approved"
    service = ListingService(db)
    items, _ = await service.search(
        q=q, kind=kind, tag=tag, publisher=author, status=status_str, page=page, page_size=page_size
    )
    return items


@router.get("/items/{author}/{slug}", response_model=ListingResponse)
async def get_item_alias(
    author: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    service = ListingService(db)
    try:
        return await service.get_by_slug(author, slug)
    except MarketplaceError as e:
        raise HTTPException(status_code=404, detail=e.message)


@router.post("/installs")
async def compat_install_listing(
    payload: MarketplaceInstallPayload,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    service = InstallService(db)
    try:
        res = await service.init_install(
            account=account,
            listing_id=payload.listing_id,
            publisher=payload.author,
            slug=payload.slug,
            requested_version=payload.version,
        )
        if "install_token" in res:
            await service.complete_install(
                account=account,
                listing_id=uuid.UUID(res["listing_id"]),
                install_token=res["install_token"],
            )
        return {
            "ok": True,
            "listing_id": res["listing_id"],
            "version": res.get("version", "1.0.0"),
            "status": "active",
            "download_url": res.get("download_url"),
            "sha256": res.get("sha256"),
        }
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)
