"""
Talos Cloud — Celery Background Processing Package.
"""
from app.celery_app.app import celery_app

__all__ = ["celery_app"]
