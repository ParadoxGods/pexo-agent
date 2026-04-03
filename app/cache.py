from __future__ import annotations

import copy
import time
from threading import RLock
from typing import Any, Callable

_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_LOCK = RLock()


def _normalize_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    return repr(key)


def cached_value(namespace: str, key: Any, ttl_seconds: float, loader: Callable[[], Any]) -> Any:
    normalized_key = (namespace, _normalize_key(key))
    now = time.monotonic()

    with _LOCK:
        cached = _CACHE.get(normalized_key)
        if cached and cached[0] > now:
            return copy.deepcopy(cached[1])

    value = loader()
    expires_at = now + max(0.0, ttl_seconds)
    with _LOCK:
        _CACHE[normalized_key] = (expires_at, copy.deepcopy(value))
    return copy.deepcopy(value)


def invalidate_namespace(namespace: str) -> None:
    with _LOCK:
        for cache_key in [key for key in _CACHE if key[0] == namespace]:
            _CACHE.pop(cache_key, None)


def invalidate_many(*namespaces: str) -> None:
    for namespace in namespaces:
        invalidate_namespace(namespace)


def invalidate_surface_caches() -> None:
    invalidate_many(
        "admin_snapshot",
        "telemetry",
        "client_surface",
        "runtime_status",
        "session_context_seed",
    )


def invalidate_telemetry_caches() -> None:
    invalidate_many("admin_snapshot", "telemetry")


def invalidate_runtime_caches() -> None:
    invalidate_many("admin_snapshot", "runtime_status", "client_surface")


def invalidate_context_caches() -> None:
    invalidate_many("admin_snapshot", "session_context_seed")
