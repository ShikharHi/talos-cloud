import asyncio
from fastapi.testclient import TestClient
from main import app
from marketplace.repositories.plugins import PluginRepository
from marketplace.repositories.ratings import RatingRepository
from marketplace.repositories.reviews import ReviewRepository

client = TestClient(app)
plugin_repo = PluginRepository()
rating_repo = RatingRepository()
review_repo = ReviewRepository()

def test_install_gate_and_self_rating_prevention():
    # 1. Builder self-rating attempt -> Must be 400 Bad Request
    with plugin_repo.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sessions (id, user_id, expires_at)
                VALUES ('sess_builder_dev', 'usr_local_dev', NOW() + INTERVAL '1 day')
                ON CONFLICT (id) DO NOTHING;
            """)
            conn.commit()

    client.cookies.set("talos_session", "sess_builder_dev")
    res_builder = client.put(
        "/v1/marketplace/plugins/plg_research_test/rating",
        json={"stars": 5}
    )
    assert res_builder.status_code == 400
    assert "cannot rate their own" in res_builder.json()["detail"]

    # 2. Test verified download for non-author user
    with plugin_repo.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (id, email, hashed_password, display_name)
                VALUES ('usr_regular_fan', 'fan@talos.dev', 'hash', 'Regular Fan'),
                       ('usr_other_builder', 'other@talos.dev', 'hash', 'Other Builder')
                ON CONFLICT (id) DO NOTHING;
                INSERT INTO publishers (id, handle, display_name)
                VALUES ('usr_other_builder', 'otherbuilder', 'Other Builder')
                ON CONFLICT (id) DO NOTHING;
                INSERT INTO builders (id, user_id, username, display_name)
                VALUES ('usr_other_builder', 'usr_other_builder', 'otherbuilder', 'Other Builder')
                ON CONFLICT (id) DO NOTHING;
                INSERT INTO plugins (id, builder_id, name, slug, category, short_desc, description, plugin_type, status, publisher_id)
                VALUES ('plg_other_agent', 'usr_other_builder', 'Coder Bot', 'coder-bot', 'agents', 'Coding Bot', 'desc', 'agent', 'published', 'usr_other_builder')
                ON CONFLICT (id) DO NOTHING;
            """)
            conn.commit()

    # Ensure clean test state by removing prior install records if test is re-run
    with plugin_repo.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM plugin_installs WHERE plugin_id = 'plg_other_agent';")
            cur.execute("""
                INSERT INTO sessions (id, user_id, expires_at)
                VALUES ('sess_fan_123', 'usr_regular_fan', NOW() + INTERVAL '1 day')
                ON CONFLICT (id) DO UPDATE SET expires_at = NOW() + INTERVAL '1 day';
            """)
            conn.commit()

    client.cookies.set("talos_session", "sess_fan_123")
    res_not_downloaded = client.put(
        "/v1/marketplace/plugins/plg_other_agent/rating",
        json={"stars": 5}
    )
    assert res_not_downloaded.status_code == 403
    assert "must install and download" in res_not_downloaded.json()["detail"]

    # Now simulate authentic server-side download verification
    asyncio.run(plugin_repo.record_download_verified("usr_regular_fan", "plg_other_agent"))

    # Now rating succeeds!
    res_rated = client.put(
        "/v1/marketplace/plugins/plg_other_agent/rating",
        json={"stars": 5, "version_used": "1.0.0"}
    )
    assert res_rated.status_code == 200
    assert res_rated.json()["status"] == "success"

    # Post a review succeeds!
    res_review = client.post(
        "/v1/marketplace/plugins/plg_other_agent/reviews",
        json={"body": "Incredible agent! Boosted my workflow 10x."}
    )
    assert res_review.status_code == 200
    rev_id = res_review.json()["review"]["id"]

    # Toggle helpful vote
    res_vote = client.post(f"/v1/marketplace/reviews/{rev_id}/helpful")
    assert res_vote.status_code == 200
    assert res_vote.json()["voted"] is True
    assert res_vote.json()["helpful_count"] == 1

    # Report review
    res_report = client.post(
        f"/v1/marketplace/reviews/{rev_id}/report",
        json={"reason": "Testing the moderation queue"}
    )
    assert res_report.status_code == 200
    assert res_report.json()["status"] == "reported"

    print("ALL INSTALL GATING AND RATING/REVIEW TESTS PASSED!")

if __name__ == "__main__":
    test_install_gate_and_self_rating_prevention()
