"""
Talos Cloud — Canonical Marketplace API (v1).

Prefix: /api/v1/marketplace
"""

from __future__ import annotations

import uuid
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
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
    pricing_type: str = Field(default="free", description="free | paid")
    price_credits: int = Field(default=0, ge=0)
    version_policy: str = Field(default="all_minor_patch")


class ListingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    listing_id: uuid.UUID
    publisher_slug: str
    author_username: str
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
    download_count: int = 0
    purchase_count: int = 0
    pricing_type: str = "free"
    price_credits: int = 0
    verified: bool = False
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
    # Public marketplace search must never expose drafts or unverified uploads.
    status_str = "approved"
    service = ListingService(db)
    items, _ = await service.search(
        q=q, kind=kind, tag=tag, publisher=publisher, status=status_str, page=page, page_size=page_size
    )
    return items


@router.get("/listings/{listing_id}", response_model=ListingResponse)
async def get_listing(listing_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    service = ListingService(db)
    try:
        listing = await service.get_by_id(listing_id)
        if listing.status != "approved":
            raise HTTPException(status_code=404, detail="Marketplace listing not found.")
        return listing
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
            pricing_type=payload.pricing_type,
            price_credits=payload.price_credits,
            version_policy=payload.version_policy,
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


@router.post("/uploads/file")
async def upload_package_file(
    response: Response,
    listing_id: uuid.UUID = Form(...),
    version: str = Form(...),
    file: UploadFile = File(...),
    async_verification: bool = Form(True),
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    """Streams an authenticated package upload through Cloud into Tigris staging."""
    import hashlib

    from app.config import get_settings
    from app.domain.marketplace.upload import UploadStatus
    from app.models.marketplace import PackageUpload
    from app.services.marketplace.upload_service import UploadService

    extension = (file.filename or "").lower().rsplit(".", 1)[-1]
    if extension not in ("zip", "skill"):
        raise HTTPException(status_code=400, detail="Upload a .zip or .skill archive.")

    max_size = get_settings().storage_max_package_size_bytes
    hasher = hashlib.sha256()
    file_size = 0
    while chunk := await file.read(1024 * 1024):
        file_size += len(chunk)
        if file_size > max_size:
            raise HTTPException(status_code=413, detail=f"Package exceeds the {max_size}-byte upload limit.")
        hasher.update(chunk)
    if file_size == 0:
        raise HTTPException(status_code=400, detail="Uploaded package is empty.")
    await file.seek(0)

    service = UploadService(db)
    try:
        initialized = await service.init_upload(
            account=account,
            listing_id=listing_id,
            version=version,
            file_size=file_size,
            sha256=hasher.hexdigest(),
        )
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=e.message)

    upload_id = uuid.UUID(initialized["upload_id"])
    try:
        await service.storage.upload_stream(
            initialized["staging_key"], file.file, content_type="application/zip"
        )
        result = await service.complete_upload(
            account=account,
            upload_id=upload_id,
            async_verification=async_verification,
        )
    except Exception as exc:
        upload = await service.upload_repo.get_by_id(upload_id)
        if upload and upload.status in (UploadStatus.PENDING.value, UploadStatus.UPLOADING.value):
            upload.status = UploadStatus.FAILED.value
            upload.failure_reason = "Cloud storage upload failed."
            await db.flush()
        logger.exception("Could not store marketplace upload %s", upload_id)
        raise HTTPException(status_code=502, detail="Could not store the package in Cloud storage.") from exc

    if result.get("status") == "verifying":
        response.status_code = status.HTTP_202_ACCEPTED
    return result


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


# ── Economy & Creator Routes ───────────────────────────────────────────────

class PurchaseRequest(BaseModel):
    listing_id: uuid.UUID
    idempotency_key: Optional[str] = None


@router.post("/items/{listing_id}/purchase")
@router.post("/purchase")
async def purchase_listing(
    payload: Optional[PurchaseRequest] = None,
    listing_id: Optional[uuid.UUID] = None,
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    from app.services.marketplace.purchase_service import PurchaseService
    target_id = listing_id or (payload.listing_id if payload else None)
    if not target_id:
        raise HTTPException(status_code=400, detail="listing_id is required.")

    service = PurchaseService(db)
    try:
        return await service.purchase_listing(
            buyer=account,
            listing_id=target_id,
            idempotency_key=payload.idempotency_key if payload else None,
        )
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/entitlements")
async def list_user_entitlements(
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    from app.services.marketplace.purchase_service import PurchaseService
    service = PurchaseService(db)
    items = await service.list_entitlements(account.account_id)
    return [
        {
            "entitlement_id": str(e.entitlement_id),
            "listing_id": str(e.listing_id),
            "slug": e.listing.slug if e.listing else "",
            "display_name": e.listing.display_name if e.listing else "",
            "kind": e.listing.kind if e.listing else "",
            "purchase_id": e.purchase_id,
            "price_paid_credits": e.price_paid_credits,
            "status": e.status,
            "acquired_at": e.acquired_at.isoformat() if e.acquired_at else None,
        }
        for e in items
    ]


@router.get("/creators/{publisher_slug}")
async def get_creator_public_profile(
    publisher_slug: str,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import func, select
    from app.models.marketplace import MarketplaceListing, CreatorEarning
    clean_pub = publisher_slug.lower().strip()

    # Find author account
    acc_stmt = select(Account).where(
        (func.lower(Account.publisher_slug) == clean_pub) |
        (func.lower(Account.email).like(f"{clean_pub}@%"))
    )
    acc_res = await db.execute(acc_stmt)
    author = acc_res.scalar_one_or_none()

    # Aggregate public publisher stats
    stats_stmt = select(
        func.count(MarketplaceListing.listing_id).label("published_items"),
        func.coalesce(func.sum(MarketplaceListing.install_count), 0).label("total_installs"),
        func.coalesce(func.sum(MarketplaceListing.download_count), 0).label("total_downloads"),
        func.coalesce(func.sum(MarketplaceListing.purchase_count), 0).label("total_purchases"),
    ).where(
        func.lower(MarketplaceListing.publisher_slug) == clean_pub,
        MarketplaceListing.status == "approved",
    )
    stats_res = await db.execute(stats_stmt)
    stats_row = stats_res.first()

    # Fetch published items
    items_stmt = select(MarketplaceListing).where(
        func.lower(MarketplaceListing.publisher_slug) == clean_pub,
        MarketplaceListing.status == "approved",
    ).order_by(MarketplaceListing.install_count.desc())
    items_res = await db.execute(items_stmt)
    items = items_res.scalars().all()

    return {
        "publisher_slug": clean_pub,
        "display_name": author.display_name if author else clean_pub,
        "avatar_url": author.avatar_url if author else None,
        "bio": author.bio if author else "",
        "verified_publisher": getattr(author, "verified_publisher", False) if author else False,
        "stats": {
            "published_items": stats_row.published_items if stats_row else 0,
            "total_installs": stats_row.total_installs if stats_row else 0,
            "total_downloads": stats_row.total_downloads if stats_row else 0,
            "total_purchases": stats_row.total_purchases if stats_row else 0,
        },
        "items": [
            {
                "listing_id": str(i.listing_id),
                "slug": i.slug,
                "display_name": i.display_name,
                "kind": i.kind,
                "tagline": i.tagline,
                "pricing_type": i.pricing_type,
                "price_credits": i.price_credits,
                "version": i.version,
                "install_count": i.install_count,
            }
            for i in items
        ]
    }


@router.get("/creator/dashboard")
async def get_creator_dashboard(
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import func, select
    from app.models.marketplace import CreatorEarning, MarketplaceListing

    # 1. Total creator earnings breakdown (Credits only, no real money)
    earn_stmt = select(
        func.coalesce(func.sum(CreatorEarning.creator_share_credits), 0).label("total_earned"),
        func.coalesce(
            func.sum(CreatorEarning.creator_share_credits).filter(CreatorEarning.status == "available"), 0
        ).label("available_earnings"),
        func.coalesce(
            func.sum(CreatorEarning.creator_share_credits).filter(CreatorEarning.status == "pending"), 0
        ).label("pending_earnings"),
    ).where(CreatorEarning.creator_id == account.account_id)
    earn_res = await db.execute(earn_stmt)
    earn_row = earn_res.first()

    # 2. Creator listings performance
    list_stmt = select(MarketplaceListing).where(
        MarketplaceListing.author_account_id == account.account_id
    ).order_by(MarketplaceListing.created_at.desc())
    list_res = await db.execute(list_stmt)
    listings = list_res.scalars().all()

    # 3. Recent earning transactions
    tx_stmt = select(CreatorEarning).where(
        CreatorEarning.creator_id == account.account_id
    ).order_by(CreatorEarning.created_at.desc()).limit(20)
    tx_res = await db.execute(tx_stmt)
    transactions = tx_res.scalars().all()

    return {
        "overview": {
            "published_items": len(listings),
            "total_credits_earned": earn_row.total_earned if earn_row else 0,
            "available_earnings": earn_row.available_earnings if earn_row else 0,
            "pending_earnings": earn_row.pending_earnings if earn_row else 0,
            "currency": "Talos Credits",
            "payouts_status": "real_money_disabled_development_mode",
        },
        "items": [
            {
                "listing_id": str(l.listing_id),
                "slug": l.slug,
                "display_name": l.display_name,
                "kind": l.kind,
                "pricing_type": l.pricing_type,
                "price_credits": l.price_credits,
                "install_count": l.install_count,
                "download_count": l.download_count,
                "purchase_count": l.purchase_count,
                "status": l.status,
            }
            for l in listings
        ],
        "recent_earnings": [
            {
                "earning_id": str(t.earning_id),
                "listing_id": str(t.listing_id),
                "gross_credits": t.gross_credits,
                "creator_share_credits": t.creator_share_credits,
                "platform_share_credits": t.platform_share_credits,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in transactions
        ],
    }

