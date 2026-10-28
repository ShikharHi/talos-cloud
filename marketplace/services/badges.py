"""Marketplace badge helpers for objective, testable quality thresholds."""

from __future__ import annotations

from typing import Any, Mapping


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_popular(stats: Mapping[str, Any] | None) -> bool:
    """A plugin is considered popular once it has at least 1,000 installs."""
    if not stats:
        return False
    return _as_int(stats.get("install_count"), 0) >= 1000


def is_top_rated(stats: Mapping[str, Any] | None) -> bool:
    """A plugin is top rated when it meets rating and review volume thresholds."""
    if not stats:
        return False
    rating_avg = _as_float(stats.get("rating_avg"), 0.0)
    rating_count = _as_int(stats.get("rating_count"), 0)
    return rating_avg >= 4.5 and rating_count >= 50


def is_top_builder(stats: Mapping[str, Any] | None) -> bool:
    """A builder is top builder when they are top rated, have multiple plugins, and strong installs."""
    if not stats:
        return False
    return (
        is_top_rated(stats)
        and _as_int(stats.get("plugin_count"), 0) >= 3
        and _as_int(stats.get("install_count"), 0) >= 5000
    )
