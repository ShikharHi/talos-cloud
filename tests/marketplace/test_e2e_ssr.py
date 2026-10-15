import asyncio
from fastapi.testclient import TestClient
from main import app
from marketplace.repositories.plugins import PluginRepository
from marketplace.repositories.builders import BuilderRepository

client = TestClient(app)
plugin_repo = PluginRepository()
builder_repo = BuilderRepository()

async def setup_test_entities():
    with builder_repo.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (id, email, hashed_password, display_name)
                VALUES ('usr_local_dev', 'shikhar@talos.dev', 'dummy_hash', 'Shikhar Dev')
                ON CONFLICT (id) DO NOTHING;
            """)
            conn.commit()

    builder = await builder_repo.get_or_create_builder("usr_local_dev", "shikhardev", "Shikhar Dev")
    # create sample plugin
    with plugin_repo.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO plugins (
                    id, builder_id, name, slug, category, short_desc, description,
                    plugin_type, status, downloads_count, rating_avg, rating_count, rating_hist, publisher_id
                ) VALUES (
                    'plg_research_test', 'usr_local_dev', 'Research Agent Pro', 'research-agent-pro',
                    'agents', 'AI Researcher for Talos', 'Full details on deep research agent.',
                    'agent', 'published', 1500, 4.8, 12, '{0,0,1,2,9}', 'usr_local_dev'
                ) ON CONFLICT (id) DO UPDATE SET
                    slug = EXCLUDED.slug, category = EXCLUDED.category, status = 'published';
            """)
            conn.commit()

def test_ssr_plugin_and_builder_pages():
    asyncio.run(setup_test_entities())

    # 1. Test SSR plugin page
    p_res = client.get("/plugins/agents/research-agent-pro")
    assert p_res.status_code == 200
    html = p_res.text
    assert "Research Agent Pro" in html
    assert "Built by" in html
    assert "@shikhardev" in html
    assert "talos://install/agents/research-agent-pro" in html
    assert "application/ld+json" in html
    assert "aggregateRating" in html
    assert "Content-Security-Policy" in p_res.headers

    # 2. Test SSR builder profile page
    b_res = client.get("/builders/shikhardev")
    assert b_res.status_code == 200
    b_html = b_res.text
    assert "Shikhar Dev" in b_html
    assert "@shikhardev" in b_html
    assert "Published Plugins" in b_html

    # 3. Test OG image endpoint
    og_res = client.get("/og/plugins/agents/research-agent-pro.png")
    assert og_res.status_code == 200
    assert og_res.headers["content-type"] == "image/png"
    assert og_res.content.startswith(b"\x89PNG")

    print("ALL E2E SSR & OG INTEGRATION TESTS PASSED!")

if __name__ == "__main__":
    test_ssr_plugin_and_builder_pages()
