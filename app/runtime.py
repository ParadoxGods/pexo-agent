from __future__ import annotations

import shutil
import subprocess
import sys
import time
from importlib.util import find_spec

from sqlalchemy.orm import Session

from .cache import cached_value, invalidate_runtime_caches
from .dependency_profiles import PROFILE_DEPENDENCIES, PROFILE_ORDER
from .paths import CODE_ROOT, PROJECT_ROOT, RUNTIME_MARKER_PATH, STATE_ROOT, running_from_repo_checkout


def get_profile_rank(profile: str | None) -> int:
    return PROFILE_ORDER.get((profile or "").strip().lower(), 0)


def runtime_dependencies(profile: str) -> list[str]:
    try:
        return PROFILE_DEPENDENCIES[profile]
    except KeyError as exc:  # pragma: no cover - input validated by callers
        raise ValueError(f"Unsupported runtime profile '{profile}'.") from exc


def get_runtime_marker_profile() -> str:
    if not RUNTIME_MARKER_PATH.exists():
        return ""
    return RUNTIME_MARKER_PATH.read_text(encoding="utf-8").strip().lower()


def _write_runtime_marker_profile(profile: str) -> None:
    RUNTIME_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_MARKER_PATH.write_text(profile, encoding="utf-8")
    invalidate_runtime_caches()


def set_runtime_marker_profile(profile: str) -> None:
    current = get_runtime_marker_profile()
    effective = profile if get_profile_rank(profile) >= get_profile_rank(current) else current
    _write_runtime_marker_profile(effective)


def _module_available(module_name: str) -> bool:
    try:
        return find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _profile_install_matrix() -> dict[str, bool]:
    return {
        "core": True,
        "mcp": _module_available("mcp.server.fastmcp"),
        "full": _module_available("uvicorn") and _module_available("langgraph.graph"),
        "vector": _module_available("chromadb"),
    }


def _highest_installed_profile(installed_profiles: dict[str, bool]) -> str:
    for profile in ("full", "mcp", "core"):
        if installed_profiles.get(profile):
            return profile
    return "core"


def reconcile_runtime_marker_profile(installed_profiles: dict[str, bool] | None = None) -> str:
    matrix = installed_profiles or _profile_install_matrix()
    effective = _highest_installed_profile(matrix)
    if get_runtime_marker_profile() != effective:
        _write_runtime_marker_profile(effective)
    return effective


def detect_runtime_profile() -> str:
    return _highest_installed_profile(_profile_install_matrix())


def build_vector_promotion_offer() -> dict:
    return {
        "profile": "vector",
        "reason": "Semantic memory acceleration is not available in this runtime yet. Pexo is using the local keyword fallback instead.",
        "suggested_command": "pexo --promote vector",
        "promotion_endpoint": "/runtime/promote/vector",
        "status_endpoint": "/runtime/status",
    }


def maybe_issue_vector_promotion_offer(db: Session | None = None) -> dict | None:
    return None


def build_runtime_status(db: Session | None = None) -> dict:
    marker_key = get_runtime_marker_profile()

    def loader():
        installed_profiles = _profile_install_matrix()
        marker_profile = reconcile_runtime_marker_profile(installed_profiles)
        active_profile = _highest_installed_profile(installed_profiles)
        vector_offer_pending = False

        return {
            "active_profile": active_profile,
            "marker_profile": marker_profile or None,
            "installed_profiles": installed_profiles,
            "vector_embeddings_available": installed_profiles["vector"],
            "semantic_memory_ready": installed_profiles["vector"],
            "memory_backend": "semantic" if installed_profiles["vector"] else "keyword",
            "project_root": str(PROJECT_ROOT),
            "state_root": str(STATE_ROOT),
            "code_root": str(CODE_ROOT),
            "install_mode": "checkout" if running_from_repo_checkout() else "packaged",
            "recommended_promotions": [
                profile
                for profile in ("mcp", "full")
                if not installed_profiles[profile]
            ],
            "vector_promotion_offer_pending": vector_offer_pending,
            "vector_promotion_offer": None,
        }

    return cached_value("runtime_status", marker_key, 5.0, loader)


def promote_runtime(profile: str) -> dict:
    normalized_profile = profile.lower()
    if normalized_profile not in PROFILE_DEPENDENCIES:
        raise ValueError(f"Unsupported runtime profile '{profile}'.")

    dependency_specs = runtime_dependencies(normalized_profile)

    command: list[str]
    if shutil.which("uv"):
        command = [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            *dependency_specs,
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *dependency_specs,
        ]

    if running_from_repo_checkout():
        constraints_file = CODE_ROOT / "constraints.txt"
        if constraints_file.exists():
            command.extend(["-c", str(constraints_file)])
    started_at = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=str(CODE_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    duration_ms = int((time.monotonic() - started_at) * 1000)

    if completed.returncode == 0:
        set_runtime_marker_profile(normalized_profile)

    return {
        "status": "success" if completed.returncode == 0 else "error",
        "profile": normalized_profile,
        "command": command,
        "duration_ms": duration_ms,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
        "runtime": build_runtime_status(),
    }
