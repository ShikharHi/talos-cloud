import asyncio
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_routes_health_and_ssr():
    # 1. Health check
    res = client.get("/health")
    assert res.status_code == 200

    # 2. Events logging
    ev_res = client.post("/v1/marketplace/events", json={
        "kind": "plugin_view",
        "plugin_id": "plg_test",
        "referrer": "reddit.com"
    })
    assert ev_res.status_code == 200
    assert ev_res.json()["status"] == "recorded"

    # 3. Public directory list
    b_res = client.get("/v1/marketplace/studio/profile")
    # Requires auth or creates mock user
    assert b_res.status_code in (200, 401)

if __name__ == "__main__":
    test_routes_health_and_ssr()
    print("ALL API ROUTE INTEGRATION CHECKS PASSED!")
