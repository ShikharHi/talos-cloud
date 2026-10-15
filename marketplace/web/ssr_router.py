import os
import json
import urllib.parse
from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from marketplace.repositories.plugins import PluginRepository
from marketplace.repositories.builders import BuilderRepository
from marketplace.repositories.reviews import ReviewRepository
from marketplace.services.sanitizer import render_and_sanitize_markdown
from marketplace.web.seo import generate_plugin_jsonld, generate_builder_jsonld
from marketplace.web.og import generate_plugin_og_image, generate_builder_og_image

router = APIRouter(tags=["Marketplace Public SSR"])

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)
templates.env.autoescape = True

plugin_repo = PluginRepository()
builder_repo = BuilderRepository()
review_repo = ReviewRepository()

@router.get("/plugins/{category}/{slug}", response_class=HTMLResponse)
async def get_plugin_page(category: str, slug: str, request: Request):
    """Server-side rendered plugin page with dynamic Open Graph, SEO, and reviews."""
    plugin = await plugin_repo.get_plugin_by_category_and_slug(category, slug)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    if "redirect_to" in plugin:
        return RedirectResponse(url=plugin["redirect_to"], status_code=301)

    reviews = await review_repo.list_reviews(plugin["id"], limit=10)

    # Sanitize markdown content
    rendered_desc = render_and_sanitize_markdown(plugin.get("long_desc_md") or plugin.get("description") or "")

    base_url = str(request.base_url).rstrip("/")
    canonical_url = f"{base_url}/plugins/{category}/{slug}"

    jsonld_data = generate_plugin_jsonld(
        name=plugin["name"],
        description=plugin.get("short_desc") or plugin.get("description") or "",
        url=canonical_url,
        builder_name=plugin.get("builder_display_name") or plugin.get("builder_username"),
        rating_avg=float(plugin["rating_avg"]) if plugin.get("rating_avg") else None,
        rating_count=int(plugin.get("rating_count") or 0)
    )

    share_text = f"Check out {plugin['name']} for Talos — {plugin.get('short_desc') or ''}"
    share_url = canonical_url

    response = templates.TemplateResponse("plugin.html", {
        "request": request,
        "plugin": plugin,
        "reviews": reviews,
        "rendered_description": rendered_desc,
        "canonical_url": canonical_url,
        "json_ld_str": json.dumps(jsonld_data),
        "share_text_encoded": urllib.parse.quote(share_text),
        "share_url_encoded": urllib.parse.quote(share_url)
    })
    # Apply strict Content Security Policy
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:;"
    )
    return response

@router.get("/builders/{username}", response_class=HTMLResponse)
async def get_builder_page(username: str, request: Request):
    """Server-side rendered builder profile with badges, score, and plugin catalog."""
    builder = await builder_repo.get_builder_by_username(username)
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")

    with builder_repo.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM plugins
                WHERE builder_id = %s AND status = 'published'
                ORDER BY downloads_count DESC
            """, (builder["id"],))
            plugins = [dict(r) for r in cur.fetchall()]

    base_url = str(request.base_url).rstrip("/")
    canonical_url = f"{base_url}/builders/{username}"

    jsonld_data = generate_builder_jsonld(
        name=builder["display_name"],
        username=builder["username"],
        bio=builder.get("bio") or "",
        url=canonical_url
    )

    response = templates.TemplateResponse("builder.html", {
        "request": request,
        "builder": builder,
        "plugins": plugins,
        "canonical_url": canonical_url,
        "json_ld_str": json.dumps(jsonld_data)
    })
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:;"
    )
    return response

@router.get("/og/plugins/{category}/{slug}.png")
async def get_plugin_og_image_route(category: str, slug: str):
    """Serve dynamic 1200x630 Open Graph preview card for plugins."""
    plugin = await plugin_repo.get_plugin_by_category_and_slug(category, slug)
    if not plugin or "redirect_to" in plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    img_bytes = await generate_plugin_og_image(
        title=plugin["name"],
        category=plugin.get("category", "agent"),
        builder_name=plugin.get("builder_username", "talos"),
        builder_verified=bool(plugin.get("builder_verified")),
        rating_avg=float(plugin["rating_avg"]) if plugin.get("rating_avg") else None,
        rating_count=int(plugin.get("rating_count") or 0),
        installs=int(plugin.get("downloads_count") or 0),
        short_desc=plugin.get("short_desc") or plugin.get("description") or ""
    )
    return Response(content=img_bytes, media_type="image/png")

@router.get("/og/builders/{username}.png")
async def get_builder_og_image_route(username: str):
    """Serve dynamic 1200x630 Open Graph preview card for builders."""
    builder = await builder_repo.get_builder_by_username(username)
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")

    img_bytes = await generate_builder_og_image(
        username=builder["username"],
        display_name=builder["display_name"],
        rating_avg=float(builder["rating_avg"]) if builder.get("rating_avg") else None,
        plugin_count=int(builder.get("plugin_count") or 0),
        installs=int(builder.get("install_count") or 0),
        bio=builder.get("bio") or ""
    )
    return Response(content=img_bytes, media_type="image/png")

@router.get("/badges/plugins/{category}/{slug}.svg")
async def get_plugin_badge_svg(category: str, slug: str, metric: str = "version"):
    """
    Dynamic SVG badge for README markdown embedding.
    metric can be: 'version', 'rating', 'installs'
    """
    from marketplace.web.badge_service import generate_svg_badge
    plugin = await plugin_repo.get_plugin_by_category_and_slug(category, slug)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    if metric == "rating":
        rating_val = f"★ {float(plugin['rating_avg']):.1f}" if plugin.get("rating_avg") else "new"
        svg = generate_svg_badge(label="talos rating", message=rating_val, color="#f59e0b")
    elif metric == "installs":
        installs = int(plugin.get("downloads_count") or 0)
        formatted = f"{installs:,}" if installs < 1000 else f"{installs/1000:.1f}k"
        svg = generate_svg_badge(label="talos installs", message=formatted, color="#10b981")
    else:
        version_val = f"v{plugin.get('latest_version') or '1.0.0'}"
        svg = generate_svg_badge(label="talos", message=version_val, color="#6366f1")

    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300, public"})

@router.get("/badges/builders/{username}.svg")
async def get_builder_badge_svg(username: str):
    """Dynamic SVG badge for builder profiles."""
    from marketplace.web.badge_service import generate_svg_badge
    builder = await builder_repo.get_builder_by_username(username)
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")

    installs = int(builder.get("install_count") or 0)
    formatted = f"{installs:,}" if installs < 1000 else f"{installs/1000:.1f}k"
    svg = generate_svg_badge(label="talos builder", message=f"{builder['username']} · {formatted} installs", color="#8b5cf6")
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300, public"})

