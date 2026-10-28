import os
import io
import asyncio
from typing import Optional, Dict, Any
from PIL import Image, ImageDraw, ImageFont

OG_STORAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "storage", "og_cache"))
os.makedirs(OG_STORAGE_DIR, exist_ok=True)

def _render_plugin_card_sync(
    title: str,
    category: str,
    builder_name: str,
    builder_verified: bool,
    rating_avg: Optional[float],
    rating_count: int,
    installs: int,
    short_desc: str
) -> bytes:
    """Synchronous Pillow card rendering (1200x630)."""
    width, height = 1200, 630
    img = Image.new("RGB", (width, height), color=(15, 17, 23)) # Dark slate background
    draw = ImageDraw.Draw(img)

    # Accent decorative gradient bar at top
    draw.rectangle([0, 0, width, 8], fill=(99, 102, 241)) # Indigo accent

    # Use default or system font
    try:
        font_logo = ImageFont.truetype("arial.ttf", 26)
        font_cat = ImageFont.truetype("arial.ttf", 22)
        font_title = ImageFont.truetype("arialbd.ttf", 56)
        font_builder = ImageFont.truetype("arial.ttf", 28)
        font_stats = ImageFont.truetype("arialbd.ttf", 32)
        font_desc = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        font_logo = font_cat = font_title = font_builder = font_stats = font_desc = ImageFont.load_default()

    # Top brand header
    draw.text((60, 50), "TALOS MARKETPLACE", fill=(148, 163, 184), font=font_logo)

    # Category Pill
    cat_text = category.upper()
    draw.rounded_rectangle([60, 100, 60 + len(cat_text)*14 + 20, 134], radius=6, fill=(30, 41, 59))
    draw.text((70, 106), cat_text, fill=(129, 140, 248), font=font_cat)

    # Plugin Title (truncate if too long)
    display_title = title if len(title) <= 32 else title[:30] + "..."
    draw.text((60, 160), display_title, fill=(248, 250, 252), font=font_title)

    # Builder byline
    byline = f"Built by @{builder_name}" + (" [Verified]" if builder_verified else "")
    draw.text((60, 240), byline, fill=(203, 213, 225), font=font_builder)

    # Stats Bar Box
    draw.rounded_rectangle([60, 310, 1140, 410], radius=12, fill=(24, 30, 45), outline=(51, 65, 85))
    star_str = f"⭐ {rating_avg:.1f}" if rating_avg else "⭐ New"
    stats_text = f"{star_str}  ·  {rating_count:,} ratings  ·  {installs:,} installs"
    draw.text((90, 345), stats_text, fill=(241, 245, 249), font=font_stats)

    # Short description snippet
    desc_snippet = short_desc if len(short_desc) <= 120 else short_desc[:117] + "..."
    draw.text((60, 450), desc_snippet, fill=(148, 163, 184), font=font_desc)

    # Footer
    draw.text((60, 560), "talos.ai/marketplace", fill=(100, 116, 139), font=font_logo)

    output = io.BytesIO()
    img.save(output, format="PNG", optimize=True)
    return output.getvalue()

def _render_builder_card_sync(
    username: str,
    display_name: str,
    badges: list,
    rating_avg: Optional[float],
    plugin_count: int,
    installs: int,
    bio: str
) -> bytes:
    """Synchronous Pillow builder profile card rendering (1200x630)."""
    width, height = 1200, 630
    img = Image.new("RGB", (width, height), color=(15, 17, 23))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width, 8], fill=(16, 185, 129)) # Emerald accent

    try:
        font_logo = ImageFont.truetype("arial.ttf", 26)
        font_title = ImageFont.truetype("arialbd.ttf", 52)
        font_sub = ImageFont.truetype("arial.ttf", 30)
        font_stats = ImageFont.truetype("arialbd.ttf", 32)
        font_bio = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        font_logo = font_title = font_sub = font_stats = font_bio = ImageFont.load_default()

    draw.text((60, 50), "TALOS BUILDER PROFILE", fill=(148, 163, 184), font=font_logo)

    # Builder Name & Handle
    draw.text((60, 130), display_name, fill=(248, 250, 252), font=font_title)
    draw.text((60, 205), f"@{username}", fill=(52, 211, 153), font=font_sub)

    # Bio
    bio_text = bio if bio and len(bio) <= 120 else (bio[:117] + "..." if bio else "Creator in the Talos ecosystem.")
    draw.text((60, 270), bio_text, fill=(203, 213, 225), font=font_bio)

    # Stats Bar Box
    draw.rounded_rectangle([60, 340, 1140, 440], radius=12, fill=(24, 30, 45), outline=(51, 65, 85))
    rating_display = f"⭐ {rating_avg:.1f} Builder Score" if rating_avg else "⭐ New Builder"
    stats_text = f"{rating_display}  ·  {plugin_count} plugins  ·  {installs:,} installs"
    draw.text((90, 375), stats_text, fill=(241, 245, 249), font=font_stats)

    draw.text((60, 560), "talos.ai/builders", fill=(100, 116, 139), font=font_logo)

    output = io.BytesIO()
    img.save(output, format="PNG", optimize=True)
    return output.getvalue()

async def generate_plugin_og_image(
    title: str,
    category: str,
    builder_name: str,
    builder_verified: bool,
    rating_avg: Optional[float],
    rating_count: int,
    installs: int,
    short_desc: str
) -> bytes:
    """Generate OG image asynchronously in threadpool to keep FastAPI event loop free."""
    return await asyncio.to_thread(
        _render_plugin_card_sync,
        title, category, builder_name, builder_verified, rating_avg, rating_count, installs, short_desc
    )

async def generate_builder_og_image(
    username: str,
    display_name: str,
    badges: Optional[list[str]] = None,
    rating_avg: Optional[float] = None,
    plugin_count: int = 0,
    installs: int = 0,
    bio: str = ""
) -> bytes:
    """Generate builder OG image asynchronously in threadpool."""
    rendered_badges = badges or []
    return await asyncio.to_thread(
        _render_builder_card_sync,
        username, display_name, rendered_badges, rating_avg, plugin_count, installs, bio
    )
