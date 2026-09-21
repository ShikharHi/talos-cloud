"""
Talos Cloud — Celery Application Configuration.

Design Invariants:
  1. Dedicated Broker: Connects strictly to redis-celery:6379 (persistent AOF volume).
  2. Result Backend Disabled: Result backend is None. All state is tracked in PostgreSQL.
  3. Crash Recovery: task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1.
  4. Extended Visibility Timeout: 1800s (30 minutes) to prevent premature redelivery during package analysis.
  5. Partitioned Queues:
       - maintenance: concurrency = 2 (Reservation expiry, staging upload cleanup)
       - billing: concurrency = 1 (Subscription cycle renewals, order reconciliation)
       - package-security: concurrency = 1 (Sandboxed zip validation & AST analysis)
"""

try:
    from celery import Celery
    from kombu import Queue
    _HAS_CELERY = True
except ImportError:
    Celery = None
    Queue = None
    _HAS_CELERY = False

from app.config import get_settings

settings = get_settings()

if _HAS_CELERY and Celery is not None and Queue is not None:
    celery_app = Celery(
        "talos_cloud",
        broker=settings.resolved_redis_celery_url,
        include=[
            "app.celery_app.tasks.maintenance",
            "app.celery_app.tasks.billing",
            "app.celery_app.tasks.security",
            "app.celery_app.tasks.marketplace",
        ],
    )

    broker_url = settings.resolved_redis_celery_url
    broker_use_ssl = {"ssl_cert_reqs": "required"} if broker_url.startswith("rediss://") else None

    celery_app.conf.update(
        result_backend=None,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        broker_use_ssl=broker_use_ssl,
        broker_transport_options={
            "visibility_timeout": 1800,  # 30 minutes
        },
        task_default_queue="maintenance",
        task_queues=[
            Queue("maintenance"),
            Queue("billing"),
            Queue("package-security"),
        ],
        task_routes={
            "app.celery_app.tasks.maintenance.*": {"queue": "maintenance"},
            "app.celery_app.tasks.billing.*": {"queue": "billing"},
            "app.celery_app.tasks.security.*": {"queue": "package-security"},
            "app.celery_app.tasks.marketplace.*": {"queue": "package-security"},
        },
        timezone="UTC",
        enable_utc=True,
    )


    # Load beat schedule
    try:
        from app.celery_app.beat_schedule import BEAT_SCHEDULE
        celery_app.conf.beat_schedule = BEAT_SCHEDULE
    except Exception:
        pass

    @celery_app.task(name="app.celery_app.tasks.ping", bind=True)
    def ping_task(self, value="pong"):
        return {"status": "ok", "value": value}
else:
    class DummyCelery:
        class Conf:
            beat_schedule = {}
            result_backend = None
            task_acks_late = True
            task_reject_on_worker_lost = True
            worker_prefetch_multiplier = 1
            broker_transport_options = {"visibility_timeout": 1800}
            task_routes = {
                "app.celery_app.tasks.maintenance.*": {"queue": "maintenance"},
                "app.celery_app.tasks.billing.*": {"queue": "billing"},
                "app.celery_app.tasks.security.*": {"queue": "package-security"},
            }

            def update(self, *args, **kwargs):
                for d in args:
                    if isinstance(d, dict):
                        for k, v in d.items():
                            setattr(self, k, v)
                for k, v in kwargs.items():
                    setattr(self, k, v)

        conf = Conf()

        def task(self, *args, **kwargs):
            def decorator(fn):
                def delay(*a, **k):
                    return fn(*a, **k)
                fn.delay = delay
                return fn
            return decorator

    celery_app = DummyCelery()
