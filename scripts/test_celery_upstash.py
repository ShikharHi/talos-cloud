import os
import sys
import time

from app.celery_app.app import celery_app
from app.celery_app.tasks.maintenance import cleanup_stale_reservations_task

def test_celery_broker():
    print("--- Testing Celery Broker & Task Pipeline with Upstash Redis ---")
    broker_url = celery_app.conf.broker_url
    print(f"Broker configured: {broker_url.split('@')[-1] if '@' in broker_url else broker_url}")
    print(f"SSL settings: {celery_app.conf.broker_use_ssl}")

    # Inspect broker connection
    with celery_app.connection_for_write() as conn:
        conn.connect()
        print("1. Broker connection established successfully (SSL handshake passed).")

    # Enqueue a task to Upstash Redis
    async_result = celery_app.send_task("app.celery_app.tasks.ping", args=["production_upstash_test"], queue="maintenance")
    task_id = async_result.id
    print(f"2. Enqueued task id: {task_id}")

    # Inspect message directly from Upstash Redis via redis client
    import redis
    kwargs = {
        "decode_responses": False,
        "socket_timeout": 5.0,
    }
    if broker_url.startswith("rediss://"):
        kwargs["ssl_cert_reqs"] = "required"
    r = redis.from_url(broker_url, **kwargs)
    q_len = r.llen("maintenance")
    print(f"3. Upstash Redis queue 'maintenance' length: {q_len}")
    assert q_len >= 1, "Task should be in the Redis 'maintenance' queue"

    # Now execute the registered task directly to verify execution semantics
    print("4. Executing registered task directly...")
    ping_task = celery_app.tasks["app.celery_app.tasks.ping"]
    task_res = ping_task("production_upstash_test")
    print(f"   Task executed successfully, result: {task_res}")
    assert task_res == {"status": "ok", "value": "production_upstash_test"}

    # Clear the test queue
    r.delete("maintenance")
    print("5. Test queue cleared in Upstash Redis.")
    print("\nALL CELERY UPSTASH REDIS BROKER TESTS PASSED!")


if __name__ == "__main__":
    test_celery_broker()
