"""
Talos Cloud — Package Security Analysis & Promotion Celery Tasks (Queue: package-security).

Runs sandboxed, asynchronous package static analysis and promotion:
  - Strict Zip-Slip and path traversal verification
  - AST dangerous module / call analysis
  - Decompression ratio / Zip bomb defenses
  - Idempotent promotion to canonical immutable production S3 keys
"""

import asyncio
import concurrent.futures
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app.app import celery_app

logger = logging.getLogger("talos.celery.security")


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def run_verify_and_promote_package(
    upload_id_str: str,
    db: AsyncSession | None = None,
    storage=None,
) -> dict[str, Any]:
    from app.celery_app.tasks.marketplace import async_verify_and_promote

    result = await async_verify_and_promote(upload_id_str, db=db)
    if result.get("idempotent"):
        return {"status": "already_promoted", "upload_id": upload_id_str}
    return result


@celery_app.task(name="app.celery_app.tasks.security.verify_and_promote_package_task", bind=True)
def verify_and_promote_package_task(self=None, upload_id_str: str = "", db: AsyncSession | None = None):
    """
    Asynchronous package verification and promotion worker.
    Runs on dedicated queue: package-security (concurrency 1).
    """
    result = _run_async(run_verify_and_promote_package(upload_id_str=upload_id_str, db=db))
    return result

