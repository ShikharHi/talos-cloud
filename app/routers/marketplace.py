import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
)
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion, PackageUpload, UserInstall
from app.routers.auth import get_current_session
from app.routers.relay import get_authenticated_account
from app.services.identity_service import WebSession
from app.storage import (
    StorageError,
    StorageImmutabilityError,
    StorageNotFoundError,
    StorageValidationError,
    UploadState,
    VersionState,
    asset_object_key,
    get_storage_service,
    package_object_key,
    sanitize_identifier,
    temp_asset_upload_object_key,
    temp_upload_object_key,
)

logger = logging.getLogger("talos.marketplace")
router = APIRouter(prefix="/marketplace", tags=["marketplace"])


class UploadInitRequest(BaseModel):
    file_size: Optional[int] = None
    sha256: Optional[str] = None
    display_name: Optional[str] = None
    tagline: Optional[str] = None


class UploadInitResponse(BaseModel):
    upload_id: str
    object_key: str
    upload_url: str
    expires_in: int


class UploadCompleteRequest(BaseModel):
    upload_id: str
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    icon_emoji: Optional[str] = None
    icon_color: Optional[str] = None
    tags: Optional[List[str]] = None


class PackageVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_id: uuid.UUID
    listing_id: uuid.UUID
    version: str
    storage_key: str
    bucket: str
    file_size: int
    sha256: str
    mime_type: str
    status: str
    created_at: datetime


class DownloadUrlResponse(BaseModel):
    download_url: str
    expires_in: int


class AssetUploadInitRequest(BaseModel):
    filename: str = "icon.png"
    content_type: str = "image/png"


class AssetUploadCompleteRequest(BaseModel):
    upload_id: str
    filename: str = "icon.png"


class ListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    listing_id: uuid.UUID
    author_account_id: uuid.UUID
    author_username: str
    kind: str
    slug: str
    full_slug: str
    display_name: str
    tagline: str
    icon_emoji: str
    icon_color: str
    tags: List[str] = Field(default_factory=list)
    status: str
    version: str
    install_count: int
    is_builtin: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, (set, tuple)):
            return [str(x) for x in v]
        return []


class ListingDetailOut(ListingOut):
    manifest_yaml: str


@router.get("/listings", response_model=List[ListingOut])
async def get_listings(
    kind: Optional[str] = None,
    tag: Optional[str] = None,
    q: Optional[str] = None,
    author: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketplaceListing).where(MarketplaceListing.status == "approved")
    if kind:
        stmt = stmt.where(MarketplaceListing.kind == kind)
    if author:
        stmt = stmt.where(MarketplaceListing.author_username == author)
    if tag:
        stmt = stmt.where(MarketplaceListing.tags.contains([tag]))
    if q:
        query_clean = q.strip()
        search_pattern = f"%{query_clean}%"
        is_postgres = False
        try:
            bind = db.get_bind()
            is_postgres = bool(bind and getattr(bind.dialect, "name", "") == "postgresql")
        except Exception:
            is_postgres = False

        if is_postgres:
            stmt = stmt.where(
                or_(
                    func.to_tsvector("english", MarketplaceListing.display_name + " " + func.coalesce(MarketplaceListing.tagline, "")).op("@@")(
                        func.plainto_tsquery("english", query_clean)
                    ),
                    MarketplaceListing.display_name.ilike(search_pattern),
                    MarketplaceListing.tagline.ilike(search_pattern),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    MarketplaceListing.display_name.ilike(search_pattern),
                    MarketplaceListing.tagline.ilike(search_pattern),
                )
            )
    
    stmt = stmt.order_by(MarketplaceListing.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/listings/{author}/{slug}", response_model=ListingDetailOut)
async def get_listing(
    author: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.author_username == author,
        MarketplaceListing.slug == slug,
        MarketplaceListing.status == "approved",
    )
    result = await db.execute(stmt)
    listing = result.scalars().first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    return listing


async def get_optional_account(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Account:
    auth_header = request.headers.get("Authorization") or ""
    raw_token = ""
    if auth_header.startswith("Bearer "):
        raw_token = auth_header.removeprefix("Bearer ").strip()
    if not raw_token:
        raw_token = request.cookies.get("talos_session") or ""

    if raw_token:
        # 1. Try web session JWT
        try:
            from app.services.identity_service import verify_web_session
            ws = verify_web_session(raw_token)
            if ws and ws.account_id:
                res = await db.execute(select(Account).where(Account.account_id == ws.account_id))
                acc = res.scalars().first()
                if acc:
                    return acc
        except Exception:
            pass

        # 2. Try device token
        try:
            from app.services.auth_service import get_account_for_token
            acc = await get_account_for_token(db, raw_token)
            if acc:
                return acc
        except Exception:
            pass

    # 3. Dev / anonymous fallback
    res = await db.execute(select(Account).where(Account.role == "admin"))
    acc = res.scalars().first()
    if not acc:
        res = await db.execute(select(Account))
        acc = res.scalars().first()
    if not acc:
        acc = Account(
            email="system@talos.ai",
            role="admin",
            subscription_tier="admin",
        )
        db.add(acc)
        await db.flush()
    return acc


def _download_github_subfolder_zip(github_url: str, kind: str = "skill"):
    import urllib.request, io, zipfile, re
    
    u = github_url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
        
    owner, repo, branch, subpath = None, None, None, ""
    
    m_tree = re.search(r'github\.com/([^/]+)/([^/]+)/(tree|blob)/([^/]+)/?(.*)', u)
    if m_tree:
        owner = m_tree.group(1)
        repo = m_tree.group(2)
        branch = m_tree.group(4)
        subpath = m_tree.group(5)
        if subpath.endswith('/SKILL.md') or subpath.endswith('/skill.md') or subpath.endswith('/agent.yaml') or subpath.endswith('/tool.yaml'):
            subpath = subpath.rsplit('/', 1)[0]
    else:
        m_repo = re.search(r'github\.com/([^/]+)/([^/\.]+)', u)
        if m_repo:
            owner = m_repo.group(1)
            repo = m_repo.group(2)
    
    if not owner or not repo:
        return None, None
        
    branches_to_try = [branch] if branch else ["main", "master"]
    zip_bytes = None
    
    for b in branches_to_try:
        for zip_url in (
            f"https://api.github.com/repos/{owner}/{repo}/zipball/{b}",
            f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{b}",
            f"https://github.com/{owner}/{repo}/archive/refs/heads/{b}.zip",
        ):
            try:
                req_obj = urllib.request.Request(zip_url, headers={"User-Agent": "TalosCloud-Marketplace"})
                with urllib.request.urlopen(req_obj, timeout=15) as resp:
                    if resp.status == 200:
                        zip_bytes = resp.read()
                        break
            except Exception:
                continue
        if zip_bytes:
            break
            
    if not zip_bytes:
        return None, None
        
    try:
        src_zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        namelist = src_zf.namelist()
        root_prefix = namelist[0].split("/")[0] if namelist else ""
        subpath_clean = subpath.strip("/")
        
        out_buf = io.BytesIO()
        manifest_text = None
        
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out_zf:
            for member in src_zf.infolist():
                if member.is_dir():
                    continue
                rel_path = member.filename[len(root_prefix):].lstrip("/")
                if subpath_clean:
                    if not rel_path.startswith(subpath_clean + "/") and rel_path != subpath_clean:
                        continue
                    dest_rel_path = rel_path[len(subpath_clean):].lstrip("/")
                else:
                    dest_rel_path = rel_path
                    
                if not dest_rel_path:
                    continue
                    
                content = src_zf.read(member)
                out_zf.writestr(dest_rel_path, content)
                
                if dest_rel_path.lower() in ("skill.md", "skill.yaml", "skill.yml", "agent.yaml", "connector.yaml"):
                    manifest_text = content.decode("utf-8", errors="ignore")
                    
        return out_buf.getvalue(), manifest_text
    except Exception:
        return None, None


@router.post("/listings", response_model=ListingOut)
async def create_listing(
    request: Request,
    display_name: str = Form(...),
    slug: str = Form(...),
    manifest_yaml: Optional[str] = Form(None),
    tagline: Optional[str] = Form("Skill package"),
    kind: str = Form("skill"),
    icon_emoji: Optional[str] = Form("⚡"),
    icon_color: Optional[str] = Form("#eab308"),
    tags: Optional[str] = Form("skill"),
    version: Optional[str] = Form("1.0.0"),
    package_zip: Optional[UploadFile] = File(None),
    package_file: Optional[UploadFile] = File(None),
    github_url: Optional[str] = Form(None),
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.slug == slug
    )
    existing = (await db.execute(stmt)).scalars().first()

    upload = package_file or package_zip
    zip_bytes = None
    manifest_text = manifest_yaml or ""

    if github_url and upload is None:
        gh_zip, gh_manifest = _download_github_subfolder_zip(github_url, kind)
        if gh_zip and len(gh_zip) > 10:
            zip_bytes = gh_zip
            if not manifest_text and gh_manifest:
                manifest_text = gh_manifest

    if upload is not None:
        file_bytes = await upload.read()
        filename = upload.filename or "upload.zip"
        if filename.endswith(".zip") or filename.endswith(".agent"):
            zip_bytes = file_bytes
            if not manifest_text:
                try:
                    import io, zipfile, yaml
                    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                        mf = next((f for f in zf.namelist() if f.endswith("agent.yaml") or f.endswith("agent.yml") or f.endswith("agent.json") or f.endswith("SKILL.md") or f.endswith(".yaml") or f.endswith(".yml")), None)
                        if mf:
                            manifest_text = zf.read(mf).decode("utf-8", errors="ignore")
                except Exception:
                    pass
        else:
            manifest_text = file_bytes.decode("utf-8", errors="ignore")
            import io, zipfile
            buf = io.BytesIO()
            filename_in_zip = "SKILL.md" if kind == "skill" else ("agent.yaml" if kind == "agent" else f"{kind}.yaml")
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(filename_in_zip, manifest_text)
            zip_bytes = buf.getvalue()

    # Pre-publish validation for autonomous agents
    if kind == "agent" and zip_bytes:
        try:
            import io, zipfile, re
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                nl = zf.namelist()
                code_parts = []
                for f in nl:
                    if f.endswith(".py") or f.endswith(".js") or f.endswith(".ts"):
                        code_parts.append(zf.read(f).decode("utf-8", errors="ignore"))
                combined_code = "\n\n".join(code_parts)

                if combined_code.strip():
                    uses_openai = bool(
                        re.search(r"from\s+openai\s+import", combined_code) or
                        re.search(r"import\s+openai", combined_code) or
                        re.search(r"OpenAI\(", combined_code) or
                        re.search(r"AsyncOpenAI\(", combined_code) or
                        re.search(r"require\([\"']openai[\"']\)", combined_code) or
                        re.search(r"from\s+[\"']openai[\"']", combined_code) or
                        re.search(r"ChatOpenAI", combined_code) or
                        re.search(r"client:\s*openai", manifest_text) or
                        "remote" in manifest_text
                    )
                    if not uses_openai:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Defect: Agent must use the 'openai' client library for LLM interaction (e.g. 'from openai import OpenAI' or 'import openai')."
                        )

                    missing = []
                    if not (re.search(r"LLM_API_KEY", combined_code) or "LLM_API_KEY" in manifest_text or re.search(r"OPENAI_API_KEY|API_KEY", combined_code)):
                        missing.append("LLM_API_KEY")
                    if not (re.search(r"LLM_MODEL", combined_code) or "LLM_MODEL" in manifest_text or re.search(r"MODEL_NAME|MODEL", combined_code)):
                        missing.append("LLM_MODEL")
                    if not (re.search(r"LLM_URL", combined_code) or "LLM_URL" in manifest_text or re.search(r"BASE_URL|OPENAI_BASE_URL", combined_code)):
                        missing.append("LLM_URL")

                    if missing:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Defect: Agent code must reference standard LLM variables: {', '.join(missing)} via os.getenv() so Talos can inject the relay LLM."
                        )

                    if re.search(r"sk-[a-zA-Z0-9_-]{20,}", combined_code):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Defect: Security alert: Found hardcoded API key (sk-...). Remove hardcoded keys and use the 'LLM_API_KEY' variable instead."
                        )
        except HTTPException:
            raise
        except Exception:
            pass
    
    if zip_bytes is None:
        if not manifest_text:
            manifest_text = f"""---
name: {display_name}
description: {tagline or "Skill package"}
---

# Instructions
- Step by step guidance for {display_name}.
"""
        import io, zipfile
        buf = io.BytesIO()
        filename = "SKILL.md" if kind == "skill" else f"{kind}.yaml"
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(filename, manifest_text)
        zip_bytes = buf.getvalue()

    tag_list = [t.strip() for t in (tags or "skill").split(",") if t.strip()]
    author_username = account.email.split("@")[0] if account.email else "talos"

    # Extract description directly from SKILL.md / manifest_text
    extracted_tagline = ""
    if manifest_text:
        try:
            import yaml
            if manifest_text.startswith("---"):
                parts = manifest_text.split("---", 2)
                if len(parts) >= 3:
                    fm = yaml.safe_load(parts[1]) or {}
                    extracted_tagline = str(fm.get("description") or fm.get("tagline") or "").strip()
            elif "description:" in manifest_text:
                fm = yaml.safe_load(manifest_text) or {}
                extracted_tagline = str(fm.get("description") or fm.get("tagline") or "").strip()
        except Exception:
            pass

    final_tagline = extracted_tagline or tagline or "Skill package"

    if existing:
        existing.display_name = display_name
        existing.tagline = final_tagline
        existing.icon_emoji = icon_emoji or existing.icon_emoji
        existing.icon_color = icon_color or existing.icon_color
        existing.manifest_yaml = manifest_text
        existing.package_zip = zip_bytes
        existing.tags = tag_list
        existing.status = "approved"
        await db.commit()
        await db.refresh(existing)
        return existing

    new_listing = MarketplaceListing(
        author_account_id=account.account_id,
        author_username=author_username,
        kind=kind,
        slug=slug,
        display_name=display_name,
        tagline=final_tagline,
        icon_emoji=icon_emoji or "⚡",
        icon_color=icon_color or "#eab308",
        tags=tag_list,
        manifest_yaml=manifest_text,
        package_zip=zip_bytes,
        status="approved",
        version=version or "1.0.0",
        is_builtin=False,
    )
    db.add(new_listing)
    await db.commit()
    await db.refresh(new_listing)
    return new_listing


@router.get("/my", response_model=List[ListingOut])
async def my_listings(
    account: Account = Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.author_account_id == account.account_id
    ).order_by(MarketplaceListing.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/install/{author}/{slug}")
async def install_listing(
    author: str,
    slug: str,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.author_username == author,
        MarketplaceListing.slug == slug,
        MarketplaceListing.status == "approved"
    )
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        raise HTTPException(status_code=404, detail="Approved listing not found")

    check_stmt = select(UserInstall).where(
        UserInstall.account_id == account.account_id,
        UserInstall.listing_id == listing.listing_id
    )
    existing = (await db.execute(check_stmt)).scalars().first()
    if not existing:
        new_install = UserInstall(
            account_id=account.account_id,
            listing_id=listing.listing_id,
            installed_version=listing.version or "1.0.0",
            status="active",
        )
        db.add(new_install)
        listing.install_count += 1
        await db.commit()
    else:
        existing.installed_version = listing.version or existing.installed_version or "1.0.0"
        existing.status = "active"
        await db.commit()
    return {"ok": True}


@router.delete("/install/{author}/{slug}")
async def uninstall_listing(
    author: str,
    slug: str,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.author_username == author,
        MarketplaceListing.slug == slug
    )
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        return {"ok": True}

    check_stmt = select(UserInstall).where(
        UserInstall.account_id == account.account_id,
        UserInstall.listing_id == listing.listing_id
    )
    install = (await db.execute(check_stmt)).scalars().first()
    if install:
        await db.delete(install)
        if listing.install_count > 0:
            listing.install_count -= 1
        await db.commit()
    return {"ok": True}


@router.get("/installed", response_model=List[ListingDetailOut])
async def get_installed(
    account: Optional[Account] = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    if not account:
        return []
    stmt = select(MarketplaceListing).join(UserInstall).where(
        UserInstall.account_id == account.account_id
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/download/{author}/{slug}")
async def download_listing(
    author: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.author_username == author,
        MarketplaceListing.slug == slug
    )
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    # Check for Tigris S3 package version first
    ver_stmt = select(MarketplacePackageVersion).where(
        MarketplacePackageVersion.listing_id == listing.listing_id,
        MarketplacePackageVersion.status.in_(["approved", "verified", "published"])
    ).order_by(MarketplacePackageVersion.created_at.desc())
    latest_ver = (await db.execute(ver_stmt)).scalars().first()

    if latest_ver and latest_ver.storage_key:
        try:
            storage = get_storage_service()
            if await storage.provider.exists(latest_ver.storage_key, bucket=latest_ver.bucket):
                url = await storage.provider.create_download_url(
                    key=latest_ver.storage_key,
                    expires_in=900,
                    filename=f"{slug}.zip",
                    bucket=latest_ver.bucket,
                )
                return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

            dl_info = await storage.create_package_download_url(
                resource_type=listing.kind,
                resource_id=listing.slug,
                version=latest_ver.version,
                filename=f"{slug}.zip",
            )
            return RedirectResponse(url=dl_info.download_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        except Exception as e:
            logger.warning("Could not generate presigned download URL for %s: %s", slug, e)

    if listing.manifest_yaml:
        import io, zipfile
        buf = io.BytesIO()
        filename = "SKILL.md" if listing.kind == "skill" else f"{listing.kind}.yaml"
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(filename, listing.manifest_yaml)
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{slug}.zip"'}
        )

    raise HTTPException(status_code=404, detail="No package content available")


@router.patch("/admin/{author}/{slug}/approve")
async def approve_listing(
    author: str,
    slug: str,
    account: Account = Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
):
    if account.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.author_username == author,
        MarketplaceListing.slug == slug
    )
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
        
    listing.status = "approved"
    await db.commit()
    return {"ok": True}


@router.patch("/admin/{author}/{slug}/reject")
async def reject_listing(
    author: str,
    slug: str,
    account: Account = Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
):
    if account.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
        
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.author_username == author,
        MarketplaceListing.slug == slug
    )
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
        
    listing.status = "rejected"
    await db.commit()
    return {"ok": True}


@router.delete("/listings/{author}/{slug}")
async def delete_listing(
    author: str,
    slug: str,
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.slug == slug
    )
    if author and author not in ("all", "local", "talos", "undefined"):
        stmt = select(MarketplaceListing).where(
            (MarketplaceListing.author_username == author) & (MarketplaceListing.slug == slug)
        )
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        stmt = select(MarketplaceListing).where(MarketplaceListing.slug == slug)
        listing = (await db.execute(stmt)).scalars().first()

    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    is_admin = getattr(session, "role", "user") == "admin"
    user_prefix = (session.email.split("@")[0] if session.email else "").lower()
    listing_author = (listing.author_username or "").lower()
    is_author = (
        (listing.author_account_id == session.account_id)
        or (listing_author == user_prefix)
        or (listing_author == (session.email or "").lower())
    )

    if not is_admin and not is_author:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You can only unpublish your own listings unless you are an admin.",
        )

    # Dependency protection (Task 16 & 17): Block unpublishing if other approved listings depend on this
    dep_token = f"{listing.author_username}/{listing.slug}"
    dep_stmt = select(MarketplaceListing).where(
        MarketplaceListing.status == "approved",
        MarketplaceListing.listing_id != listing.listing_id,
        MarketplaceListing.manifest_yaml.contains(dep_token),
    )
    dependents = (await db.execute(dep_stmt)).scalars().all()
    if dependents:
        dep_names = [d.full_slug for d in dependents]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot unpublish: package is required as a dependency by approved packages: {', '.join(dep_names)}",
        )

    # Safe unpublishing: if active installs exist and caller is not admin,
    # tombstone it so existing users keep their installs, while new discovery/installs are stopped.
    if listing.install_count > 0 and not is_admin:
        listing.status = "tombstoned"
        await db.commit()
        return {"ok": True, "status": "tombstoned"}

    await db.execute(
        delete(UserInstall).where(UserInstall.listing_id == listing.listing_id)
    )
    await db.delete(listing)
    await db.commit()
    return {"ok": True}


@router.get("/inspect/tree/{author}/{slug}")
async def inspect_cloud_listing_tree(author: str, slug: str, db: AsyncSession = Depends(get_db)):
    """Returns the full file tree inside a cloud package zip."""
    import io, zipfile
    stmt = select(MarketplaceListing).where(
        or_(
            MarketplaceListing.author_username == author,
            MarketplaceListing.publisher_slug == author,
        ),
        MarketplaceListing.slug == slug
    )
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        return {"tree": [{"path": "SKILL.md", "type": "file"}], "found": False}

    ver_stmt = (
        select(MarketplacePackageVersion)
        .where(
            MarketplacePackageVersion.listing_id == listing.listing_id,
            MarketplacePackageVersion.status.in_(["published", "approved", "verified"]),
        )
        .order_by(MarketplacePackageVersion.created_at.desc())
    )
    version = (await db.execute(ver_stmt)).scalars().first()
    if version and version.storage_key:
        try:
            storage = get_storage_service()
            data = await storage.provider.download(version.storage_key, bucket=version.bucket)
            zf = zipfile.ZipFile(io.BytesIO(data))
            tree = []
            dirs_seen = set()
            for name in sorted(zf.namelist()):
                clean_name = name.replace("\\", "/").strip("/")
                if not clean_name:
                    continue
                parts = clean_name.split("/")
                for i in range(1, len(parts)):
                    d_path = "/".join(parts[:i])
                    if d_path not in dirs_seen:
                        dirs_seen.add(d_path)
                        tree.append({"path": d_path, "type": "dir"})
                if not name.endswith("/"):
                    tree.append({"path": clean_name, "type": "file"})
                else:
                    if clean_name not in dirs_seen:
                        dirs_seen.add(clean_name)
                        tree.append({"path": clean_name, "type": "dir"})
            return {"tree": tree, "found": True}
        except Exception as e:
            logger.warning("Failed to inspect zip from storage for %s/%s: %s", author, slug, e)

    manifest_file = "SKILL.md" if listing.kind in ("skill", "skills") else f"{listing.kind}.yaml"
    return {"tree": [{"path": manifest_file, "type": "file"}], "found": True}


@router.get("/inspect/file/{author}/{slug}")
async def inspect_cloud_listing_file(author: str, slug: str, path: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Returns the content of a specific file inside a cloud package zip."""
    import io, zipfile
    stmt = select(MarketplaceListing).where(
        or_(
            MarketplaceListing.author_username == author,
            MarketplaceListing.publisher_slug == author,
        ),
        MarketplaceListing.slug == slug
    )
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    ver_stmt = (
        select(MarketplacePackageVersion)
        .where(
            MarketplacePackageVersion.listing_id == listing.listing_id,
            MarketplacePackageVersion.status.in_(["published", "approved", "verified"]),
        )
        .order_by(MarketplacePackageVersion.created_at.desc())
    )
    version = (await db.execute(ver_stmt)).scalars().first()
    if version and version.storage_key:
        try:
            storage = get_storage_service()
            data = await storage.provider.download(version.storage_key, bucket=version.bucket)
            zf = zipfile.ZipFile(io.BytesIO(data))
            clean_p = path.replace("\\", "/").lstrip("/")
            for name in zf.namelist():
                if name.replace("\\", "/").lstrip("/").lower() == clean_p.lower():
                    content = zf.read(name).decode("utf-8", errors="ignore")
                    return {"path": path, "content": content}
        except Exception:
            pass

    if path.lower() in ("skill.md", f"{listing.kind}.yaml"):
        return {"path": path, "content": listing.manifest_yaml or ""}

    raise HTTPException(status_code=404, detail="File not found in package")


# =============================================================================
# Tigris S3 Object Storage Endpoints
# =============================================================================


async def _init_upload_handler(
    resource_type: str,
    resource_id: str,
    version: str,
    payload: UploadInitRequest,
    account: Account,
    db: AsyncSession,
) -> UploadInitResponse:
    norm_type = sanitize_identifier(resource_type, "resource_type").lower()
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_version = sanitize_identifier(version, "version")

    # Verify listing ownership if listing already exists
    stmt = select(MarketplaceListing).where(MarketplaceListing.slug == clean_id)
    listing = (await db.execute(stmt)).scalars().first()

    if listing:
        if listing.author_account_id != account.account_id and getattr(account, "role", "user") != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You can only publish new versions for your own packages.",
            )

        # Immutability check: cannot overwrite already published version
        ver_stmt = select(MarketplacePackageVersion).where(
            MarketplacePackageVersion.listing_id == listing.listing_id,
            MarketplacePackageVersion.version == clean_version,
            MarketplacePackageVersion.status.in_([VersionState.APPROVED.value, VersionState.VERIFIED.value]),
        )
        existing_ver = (await db.execute(ver_stmt)).scalars().first()
        if existing_ver:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Version '{clean_version}' of '{clean_id}' is already published and immutable. "
                    "Please publish a new version."
                ),
            )

    upload_id = uuid.uuid4()
    expires_in = 900
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    try:
        storage = get_storage_service()
    except Exception as e:
        logger.error("Storage service unavailable: %s", e)
        raise HTTPException(status_code=500, detail="Storage service is currently unavailable.")

    # Generate server-side staging key
    temp_key = temp_upload_object_key(norm_type, clean_id, str(upload_id))

    upload_record = PackageUpload(
        upload_id=upload_id,
        account_id=account.account_id,
        listing_id=listing.listing_id if listing else None,
        resource_type=norm_type,
        resource_id=clean_id,
        version=clean_version,
        object_key=temp_key,
        bucket=storage.default_bucket,
        expected_size=payload.file_size,
        sha256=payload.sha256,
        status=UploadState.PENDING.value,
        expires_at=expires_at,
    )
    db.add(upload_record)
    await db.commit()

    presigned = await storage.create_package_upload_url(
        resource_type=norm_type,
        resource_id=clean_id,
        upload_id=str(upload_id),
        expires_in=expires_in,
        content_type="application/zip",
    )

    logger.info(
        "Upload initialized: upload_id=%s, resource=%s/%s, version=%s, key=%s",
        upload_id,
        norm_type,
        clean_id,
        clean_version,
        presigned.object_key,
    )

    return UploadInitResponse(
        upload_id=str(upload_id),
        object_key=presigned.object_key,
        upload_url=presigned.upload_url,
        expires_in=presigned.expires_in,
    )


async def _complete_upload_handler(
    resource_type: str,
    resource_id: str,
    version: str,
    payload: UploadCompleteRequest,
    account: Account,
    db: AsyncSession,
) -> PackageVersionOut:
    norm_type = sanitize_identifier(resource_type, "resource_type").lower()
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_version = sanitize_identifier(version, "version")

    try:
        req_upload_id = uuid.UUID(payload.upload_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload_id format (must be UUID)")

    stmt = select(PackageUpload).where(PackageUpload.upload_id == req_upload_id)
    upload = (await db.execute(stmt)).scalars().first()

    if not upload or upload.resource_id != clean_id or upload.version != clean_version:
        raise HTTPException(status_code=404, detail="Upload session not found or resource mismatch")

    if upload.account_id != account.account_id and getattr(account, "role", "user") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: You can only complete your own uploads.")

    # Idempotent response if already completed
    if upload.status == UploadState.COMPLETED.value:
        ver_stmt = select(MarketplacePackageVersion).where(
            MarketplacePackageVersion.listing_id == upload.listing_id,
            MarketplacePackageVersion.version == clean_version,
        )
        existing_ver = (await db.execute(ver_stmt)).scalars().first()
        if existing_ver:
            return existing_ver

    if upload.status not in (UploadState.PENDING.value, UploadState.UPLOADED.value):
        raise HTTPException(
            status_code=400,
            detail=f"Upload session cannot be completed from status '{upload.status}'.",
        )

    now = datetime.now(timezone.utc)
    exp = upload.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp <= now:
        upload.status = UploadState.EXPIRED.value
        await db.commit()
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Upload session has expired.")

    # Transition to verifying state
    upload.status = UploadState.VERIFYING.value
    await db.commit()

    storage = get_storage_service()
    canonical_key = package_object_key(norm_type, clean_id, clean_version)

    # Immutability check
    if await storage.exists(canonical_key):
        upload.status = UploadState.FAILED.value
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Canonical key '{canonical_key}' already exists in storage. Immutability violation.",
        )

    try:
        verification = await storage.verify_and_promote_package(
            temp_key=upload.object_key,
            canonical_key=canonical_key,
            resource_type=norm_type,
            expected_sha256=upload.sha256,
        )
    except StorageNotFoundError:
        upload.status = UploadState.FAILED.value
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Staged object not found in storage. Ensure client PUT completed before calling complete.",
        )
    except Exception as e:
        upload.status = UploadState.FAILED.value
        await db.commit()
        logger.error("Error during package verification/promotion: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Server error during package verification.")

    if not verification.valid:
        upload.status = UploadState.FAILED.value
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Package validation failed: {'; '.join(verification.errors)}",
        )

    # Find or create MarketplaceListing
    listing_stmt = select(MarketplaceListing).where(MarketplaceListing.slug == clean_id)
    listing = (await db.execute(listing_stmt)).scalars().first()

    author_username = account.email.split("@")[0] if account.email else "talos"

    if not listing:
        listing = MarketplaceListing(
            author_account_id=account.account_id,
            author_username=author_username,
            kind=norm_type,
            slug=clean_id,
            display_name=payload.display_name or clean_id,
            tagline=payload.tagline or "",
            icon_emoji=payload.icon_emoji or "📦",
            icon_color=payload.icon_color or "#a3e635",
            tags=payload.tags or [norm_type],
            manifest_yaml=verification.manifest_yaml or "",
            status="approved",
            version=clean_version,
            is_builtin=False,
        )
        db.add(listing)
        await db.flush()
    else:
        listing.version = clean_version
        if verification.manifest_yaml:
            listing.manifest_yaml = verification.manifest_yaml
        if payload.display_name:
            listing.display_name = payload.display_name
        if payload.tagline:
            listing.tagline = payload.tagline
        if payload.tags:
            listing.tags = payload.tags

    pkg_version = MarketplacePackageVersion(
        listing_id=listing.listing_id,
        version=clean_version,
        storage_key=canonical_key,
        bucket=storage.default_bucket,
        file_size=verification.file_size,
        sha256=verification.sha256,
        mime_type=verification.mime_type,
        storage_provider="s3",
        manifest_yaml=verification.manifest_yaml or "",
        status=VersionState.APPROVED.value,
    )
    db.add(pkg_version)

    upload.status = UploadState.COMPLETED.value
    upload.listing_id = listing.listing_id
    await db.commit()
    await db.refresh(pkg_version)

    logger.info(
        "Package published successfully: %s/%s version %s (sha256=%s, size=%d)",
        norm_type,
        clean_id,
        clean_version,
        pkg_version.sha256,
        pkg_version.file_size,
    )

    return pkg_version


async def _download_handler(
    resource_type: str,
    resource_id: str,
    version: str,
    account: Account,
    db: AsyncSession,
) -> DownloadUrlResponse:
    norm_type = sanitize_identifier(resource_type, "resource_type").lower()
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_version = sanitize_identifier(version, "version")

    stmt = select(MarketplaceListing).where(MarketplaceListing.slug == clean_id)
    listing = (await db.execute(stmt)).scalars().first()
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing '{clean_id}' not found")

    ver_stmt = select(MarketplacePackageVersion).where(
        MarketplacePackageVersion.listing_id == listing.listing_id,
        MarketplacePackageVersion.version == clean_version,
    )
    pkg_ver = (await db.execute(ver_stmt)).scalars().first()

    if not pkg_ver:
        raise HTTPException(status_code=404, detail=f"Version '{clean_version}' not found for '{clean_id}'")

    if pkg_ver.status not in (VersionState.APPROVED.value, VersionState.VERIFIED.value):
        if getattr(account, "role", "user") != "admin" and listing.author_account_id != account.account_id:
            raise HTTPException(status_code=403, detail="Package version is not available for public download.")

    storage = get_storage_service()
    try:
        dl_res = await storage.create_package_download_url(
            resource_type=norm_type,
            resource_id=clean_id,
            version=clean_version,
            filename=f"{clean_id}-{clean_version}.zip",
        )
        return DownloadUrlResponse(download_url=dl_res.download_url, expires_in=dl_res.expires_in)
    except StorageNotFoundError:
        raise HTTPException(status_code=404, detail="Package binary not found in storage.")
    except Exception as e:
        logger.error("Error generating presigned download URL: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate download URL.")


# -----------------------------------------------------------------------------
# Generic Package Storage Routes: /marketplace/{resource_type}/{resource_id}/...
# -----------------------------------------------------------------------------


@router.post("/{resource_type}/{resource_id}/versions/{version}/upload/init", response_model=UploadInitResponse)
async def init_package_upload(
    resource_type: str,
    resource_id: str,
    version: str,
    payload: UploadInitRequest = UploadInitRequest(),
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    """Initiates an authenticated presigned upload for a marketplace package version."""
    return await _init_upload_handler(resource_type, resource_id, version, payload, account, db)


@router.post("/{resource_type}/{resource_id}/versions/{version}/upload/complete", response_model=PackageVersionOut)
async def complete_package_upload(
    resource_type: str,
    resource_id: str,
    version: str,
    payload: UploadCompleteRequest,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    """Verifies and promotes a staged package upload into an approved immutable package version."""
    return await _complete_upload_handler(resource_type, resource_id, version, payload, account, db)


@router.get("/{resource_type}/{resource_id}/versions/{version}/download", response_model=DownloadUrlResponse)
async def get_package_download_url(
    resource_type: str,
    resource_id: str,
    version: str,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    """Generates a short-lived presigned GET URL for downloading a published package version."""
    return await _download_handler(resource_type, resource_id, version, account, db)


# -----------------------------------------------------------------------------
# Type-Specific Aliases: agents, skills, mcp, tools
# -----------------------------------------------------------------------------


@router.post("/agents/{agent_id}/versions/{version}/upload/init", response_model=UploadInitResponse)
async def init_agent_upload(
    agent_id: str,
    version: str,
    payload: UploadInitRequest = UploadInitRequest(),
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _init_upload_handler("agents", agent_id, version, payload, account, db)


@router.post("/agents/{agent_id}/versions/{version}/upload/complete", response_model=PackageVersionOut)
async def complete_agent_upload(
    agent_id: str,
    version: str,
    payload: UploadCompleteRequest,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _complete_upload_handler("agents", agent_id, version, payload, account, db)


@router.get("/agents/{agent_id}/versions/{version}/download", response_model=DownloadUrlResponse)
async def download_agent_package(
    agent_id: str,
    version: str,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _download_handler("agents", agent_id, version, account, db)


@router.post("/skills/{skill_id}/versions/{version}/upload/init", response_model=UploadInitResponse)
async def init_skill_upload(
    skill_id: str,
    version: str,
    payload: UploadInitRequest = UploadInitRequest(),
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _init_upload_handler("skills", skill_id, version, payload, account, db)


@router.post("/skills/{skill_id}/versions/{version}/upload/complete", response_model=PackageVersionOut)
async def complete_skill_upload(
    skill_id: str,
    version: str,
    payload: UploadCompleteRequest,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _complete_upload_handler("skills", skill_id, version, payload, account, db)


@router.get("/skills/{skill_id}/versions/{version}/download", response_model=DownloadUrlResponse)
async def download_skill_package(
    skill_id: str,
    version: str,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _download_handler("skills", skill_id, version, account, db)


@router.post("/mcp/{mcp_id}/versions/{version}/upload/init", response_model=UploadInitResponse)
async def init_mcp_upload(
    mcp_id: str,
    version: str,
    payload: UploadInitRequest = UploadInitRequest(),
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _init_upload_handler("mcp", mcp_id, version, payload, account, db)


@router.post("/mcp/{mcp_id}/versions/{version}/upload/complete", response_model=PackageVersionOut)
async def complete_mcp_upload(
    mcp_id: str,
    version: str,
    payload: UploadCompleteRequest,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _complete_upload_handler("mcp", mcp_id, version, payload, account, db)


@router.get("/mcp/{mcp_id}/versions/{version}/download", response_model=DownloadUrlResponse)
async def download_mcp_package(
    mcp_id: str,
    version: str,
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    return await _download_handler("mcp", mcp_id, version, account, db)


# -----------------------------------------------------------------------------
# Asset Storage Routes: /marketplace/{resource_type}/{resource_id}/assets/...
# -----------------------------------------------------------------------------


@router.post("/{resource_type}/{resource_id}/assets/{asset_name}/upload/init", response_model=UploadInitResponse)
async def init_asset_upload(
    resource_type: str,
    resource_id: str,
    asset_name: str = "icon.png",
    payload: AssetUploadInitRequest = AssetUploadInitRequest(),
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    norm_type = sanitize_identifier(resource_type, "resource_type").lower()
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_asset = sanitize_identifier(asset_name, "asset_name")

    upload_id = uuid.uuid4()
    storage = get_storage_service()

    presigned = await storage.create_asset_upload_url(
        resource_type=norm_type,
        resource_id=clean_id,
        upload_id=str(upload_id),
        filename=clean_asset,
        expires_in=900,
        content_type=payload.content_type,
    )

    return UploadInitResponse(
        upload_id=str(upload_id),
        object_key=presigned.object_key,
        upload_url=presigned.upload_url,
        expires_in=presigned.expires_in,
    )


@router.post("/{resource_type}/{resource_id}/assets/{asset_name}/upload/complete")
async def complete_asset_upload(
    resource_type: str,
    resource_id: str,
    asset_name: str = "icon.png",
    payload: AssetUploadCompleteRequest = AssetUploadCompleteRequest(upload_id=""),
    account: Account = Depends(get_optional_account),
    db: AsyncSession = Depends(get_db),
):
    norm_type = sanitize_identifier(resource_type, "resource_type").lower()
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_asset = sanitize_identifier(asset_name, "asset_name")

    storage = get_storage_service()
    temp_key = temp_asset_upload_object_key(norm_type, clean_id, payload.upload_id, clean_asset)
    canonical_key = asset_object_key(norm_type, clean_id, clean_asset)

    try:
        await storage.promote_asset(temp_key, canonical_key)
    except StorageNotFoundError:
        raise HTTPException(
            status_code=400,
            detail="Staged asset not found in storage. Ensure client PUT completed before calling complete.",
        )

    return {"ok": True, "asset_key": canonical_key}


@router.get("/{resource_type}/{resource_id}/assets/{asset_name}/download", response_model=DownloadUrlResponse)
async def download_asset(
    resource_type: str,
    resource_id: str,
    asset_name: str = "icon.png",
    db: AsyncSession = Depends(get_db),
):
    norm_type = sanitize_identifier(resource_type, "resource_type").lower()
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_asset = sanitize_identifier(asset_name, "asset_name")

    storage = get_storage_service()
    try:
        dl_res = await storage.create_asset_download_url(norm_type, clean_id, clean_asset)
        return DownloadUrlResponse(download_url=dl_res.download_url, expires_in=dl_res.expires_in)
    except StorageNotFoundError:
        raise HTTPException(status_code=404, detail="Asset not found in storage.")


# ─── Canonical /api/v1/marketplace routes (Phase 6) ──────────────────────────
from app.api.v1.marketplace import router as router_v1  # noqa: F401

