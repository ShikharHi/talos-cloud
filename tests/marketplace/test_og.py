import pytest
import asyncio
from marketplace.web.og import generate_plugin_og_image, generate_builder_og_image

@pytest.mark.asyncio
async def test_og_generation():
    png_plugin = await generate_plugin_og_image(
        title="Research Agent",
        category="agents",
        builder_name="alexdev",
        builder_verified=True,
        rating_avg=4.8,
        rating_count=2431,
        installs=18200,
        short_desc="An autonomous research agent executing deep web analysis and syntheses."
    )
    assert len(png_plugin) > 1000
    assert png_plugin.startswith(b"\x89PNG")
    print(f"Plugin OG Image successfully generated: {len(png_plugin)} bytes")

    png_builder = await generate_builder_og_image(
        username="alexdev",
        display_name="Alex Rivera",
        badges=["verified", "top_builder"],
        rating_avg=4.85,
        plugin_count=5,
        installs=23400,
        bio="AI Engineer and builder of multi-agent frameworks."
    )
    assert len(png_builder) > 1000
    assert png_builder.startswith(b"\x89PNG")
    print(f"Builder OG Image successfully generated: {len(png_builder)} bytes")

if __name__ == "__main__":
    asyncio.run(test_og_generation())
