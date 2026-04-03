from __future__ import annotations

import shutil
import subprocess
import sys
import time
from importlib.util import find_spec

from sqlalchemy.orm import Session

from .dependency_profiles import PROFILE_DEPENDENCIES, PROFILE_ORDER
from .models import SystemSetting
from .paths import CODE_ROOT, PROJECT_ROOT, RUNTIME_MARKER_PATH, running_from_repo_checkout
VECTOR_PROMOTION_NOTICE_KEY = "runtime.vector_promotion_notice_issued"


def get_profile_rank(profile: str | None) -> int:
    return PROFILE_ORDER.get((profile or "").lower(), 0)


def runtime_dependencies(profile: str) -> list[str]:
    try:
        return PROFILE_DEPENDENCIES[profile]
    except KeyError as exc:  # pragma: no cover - input validated by callers
        raise ValueError(f"Unsupported runtime profile '{profile}'.") from exc


def get_runtime_marker_profile() -> str:
    if not RUNTIME_MARKER_PATH.exists():
        return ""
    return RUNTIME_MARKER_PATH.read_text(encoding="utf-8").strip().lower()


def set_runtime_marker_profile(profile: str) -> None:
    current = get_runtime_marker_profile()
    effective = profile if get_profile_rank(profile) >= get_profile_rank(current) else current
    RUNTIME_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_MARKER_PATH.write_text(effective, encoding="utf-8")


def _module_available(module_name: str) -> bool:
    return find_spec(module_name) is not None


def detect_runtime_profile() -> str:
    marker_profile = get_runtime_marker_profile()
    if get_profile_rank(marker_profile):
        return marker_profile

    if _module_available("chromadb"):
        return "vector"
    if _module_available("langgraph.graph") and _module_available("uvicorn"):
        return "full"
    if _module_available("mcp.server.fastmcp"):
        return "mcp"
    return "core"


def _profile_install_matrix() -> dict[str, bool]:
    return {
        "core": True,
        "mcp": _module_available("mcp.server.fastmcp"),
        "full": _module_available("uvicorn") and _module_available("langgraph.graph"),
        "vector": _module_available("chromadb"),
    }


def get_system_setting(db: Session, key: str, default=None):
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if setting is None:
        return default
    return setting.value


def set_system_setting(db: Session, key: str, value) -> SystemSetting:
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if setting is None:
        setting = SystemSetting(key=key, value=value)
        db.add(setting)
    else:
        setting.value = value
    db.commit()
    db.refresh(setting)
    return setting


def build_vector_promotion_offer() -> dict:
    return {
        "profile": "vector",
        "reason": "Semantic vector memory is not installed. Pexo is using the SQLite keyword fallback until the optional vector runtime is promoted.",
        "suggested_command": "pexo --promote vector",
        "promotion_endpoint": "/runtime/promote/vector",
        "status_endpoint": "/runtime/status",
    }


def maybe_issue_vector_promotion_offer(db: Session | None = None) -> dict | None:
    if _module_available("chromadb"):
        return None
    if db is None:
        return build_vector_promotion_offer()

    if get_system_setting(db, VECTOR_PROMOTION_NOTICE_KEY, False):
        return None

    set_system_setting(db, VECTOR_PROMOTION_NOTICE_KEY, True)
    return build_vector_promotion_offer()


def build_runtime_status(db: Session | None = None) -> dict:
    installed_profiles = _profile_install_matrix()
    active_profile = detect_runtime_profile()
    vector_offer_available = not installed_profiles["vector"]
    vector_offer_pending = vector_offer_available and not bool(
        get_system_setting(db, VECTOR_PROMOTION_NOTICE_KEY, False) if db is not None else False
    )

    return {
        "active_profile": active_profile,
        "marker_profile": get_runtime_marker_profile() or None,
        "installed_profiles": installed_profiles,
        "vector_embeddings_available": installed_profiles["vector"],
        "project_root": str(PROJECT_ROOT),
        "code_root": str(CODE_ROOT),
        "install_mode": "checkout" if running_from_repo_checkout() else "packaged",
        "recommended_promotions": [
            profile
            for profile in ("mcp", "full", "vector")
            if not installed_profiles[profile]
        ],
        "vector_promotion_offer_pending": vector_offer_pending,
        "vector_promotion_offer": build_vector_promotion_offer() if vector_offer_available else None,
    }


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
