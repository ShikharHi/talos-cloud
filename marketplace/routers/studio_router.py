from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from marketplace.repositories.builders import BuilderRepository
from marketplace.repositories.plugins import PluginRepository
from marketplace.repositories.analytics import AnalyticsRepository
from marketplace.plugin_router import get_current_user_required

router = APIRouter(prefix="/v1/marketplace/studio", tags=["Builder Studio"])

builder_repo = BuilderRepository()
plugin_repo = PluginRepository()
analytics_repo = AnalyticsRepository()

class UpdateProfileRequest(BaseModel):
    username: Optional[str] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    website_url: Optional[str] = None
    social_links: Optional[Dict[str, Any]] = None
    avatar_url: Optional[str] = None

class CreatePluginDraftRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    slug: str = Field(..., min_length=2, max_length=50)
    category: str = Field(default="agents")
    short_desc: str = Field(..., max_length=250)
    plugin_type: str = Field(default="agent")

class UpdatePluginRequest(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    short_desc: Optional[str] = None
    long_desc_md: Optional[str] = None
    requirements: Optional[str] = None
    permissions: Optional[List[str]] = None
    supported_models: Optional[List[str]] = None
    screenshots: Optional[List[str]] = None
    demo_url: Optional[str] = None
    category: Optional[str] = None

class VersionUploadRequest(BaseModel):
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+")
    changelog: str = ""
    manifest: Optional[Dict[str, Any]] = None

@router.get("/profile")
async def get_my_studio_profile(user: Dict[str, Any] = Depends(get_current_user_required)):
    """Fetch builder profile and summary metrics for Builder Studio, automatic from user account."""
    username = user.get("username") or user.get("email", "").split("@")[0] or "builder"
    display_name = user.get("display_name") or username.capitalize()
    bio = user.get("bio") or ""
    builder = await builder_repo.get_or_create_builder(
        user["id"],
        username,
        display_name,
        bio=bio
    )
    return builder

@router.patch("/profile")
@router.post("/profile")
async def update_my_studio_profile(payload: UpdateProfileRequest, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Update builder profile information."""
    updated = await builder_repo.update_profile(
        builder_id=user["id"],
        username=payload.username,
        display_name=payload.display_name,
        bio=payload.bio,
        website_url=payload.website_url,
        social_links=payload.social_links,
        avatar_url=payload.avatar_url
    )
    return updated

@router.get("/plugins")
async def list_my_studio_plugins(user: Dict[str, Any] = Depends(get_current_user_required)):
    """List all plugins authored by this builder across all statuses (draft, published, unlisted)."""
    with builder_repo.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM plugins
                WHERE builder_id = %s OR publisher_id = %s
                ORDER BY created_at DESC
            """, (user["id"], user["id"]))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

@router.post("/plugins")
async def create_studio_plugin_draft(payload: CreatePluginDraftRequest, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Initialize a new plugin in draft status."""
    draft = await plugin_repo.create_plugin_draft(
        builder_id=user["id"],
        name=payload.name,
        slug=payload.slug,
        category=payload.category,
        short_desc=payload.short_desc,
        plugin_type=payload.plugin_type
    )
    return draft

@router.patch("/plugins/{plugin_id}")
async def update_studio_plugin(plugin_id: str, payload: UpdatePluginRequest, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Update metadata and content of a plugin."""
    updated = await plugin_repo.update_plugin_metadata(
        plugin_id=plugin_id,
        builder_id=user["id"],
        name=payload.name,
        new_slug=payload.slug,
        short_desc=payload.short_desc,
        long_desc_md=payload.long_desc_md,
        requirements=payload.requirements,
        permissions=payload.permissions,
        supported_models=payload.supported_models,
        screenshots=payload.screenshots,
        demo_url=payload.demo_url,
        category=payload.category
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Plugin not found or unauthorized")
    return updated

@router.post("/plugins/{plugin_id}/publish")
async def publish_studio_plugin(plugin_id: str, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Publish a draft plugin."""
    success = await plugin_repo.set_plugin_status(plugin_id, user["id"], "published")
    if not success:
        raise HTTPException(status_code=404, detail="Plugin not found or unauthorized")
    return {"status": "published"}

@router.post("/plugins/{plugin_id}/unpublish")
async def unpublish_studio_plugin(plugin_id: str, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Unpublish a plugin (set to draft)."""
    success = await plugin_repo.set_plugin_status(plugin_id, user["id"], "draft")
    if not success:
        raise HTTPException(status_code=404, detail="Plugin not found or unauthorized")
    return {"status": "unpublished"}

@router.post("/plugins/{plugin_id}/versions")
async def upload_new_plugin_version(plugin_id: str, payload: VersionUploadRequest, user: Dict[str, Any] = Depends(get_current_user_required)):
    """Upload a new release version with semver and changelog."""
    plugin = await plugin_repo.get_plugin_by_id(plugin_id)
    if not plugin or (plugin.get("builder_id") != user["id"] and plugin.get("publisher_id") != user["id"]):
        raise HTTPException(status_code=403, detail="Unauthorized")

    ver = await plugin_repo.add_plugin_version(
        plugin_id=plugin_id,
        version=payload.version,
        changelog=payload.changelog,
        manifest=payload.manifest
    )
    return ver

@router.get("/analytics")
async def get_studio_analytics(days: int = 30, user: Dict[str, Any] = Depends(get_current_user_required)):
    """View telemetry rollups and timeseries performance."""
    data = await analytics_repo.get_builder_analytics(user["id"], days=days)
    return data
