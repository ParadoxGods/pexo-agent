from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from importlib.util import find_spec
from pathlib import Path

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


PROFILE_PERFORMANCE_HINTS = {
    "core": {
        "impact_label": "Very low",
        "idle_ram_mb": 42,
        "idle_cpu_pct": "0.0-0.1%",
        "startup_seconds": "0.2-0.6s",
        "use_case": "Local API, memory, and profile state only.",
    },
    "mcp": {
        "impact_label": "Low",
        "idle_ram_mb": 56,
        "idle_cpu_pct": "0.0-0.2%",
        "startup_seconds": "0.3-0.8s",
        "use_case": "Adds the MCP bridge so Codex, Gemini, and Claude can share one local state layer.",
    },
    "full": {
        "impact_label": "Low to moderate",
        "idle_ram_mb": 86,
        "idle_cpu_pct": "0.1-0.5%",
        "startup_seconds": "0.7-1.8s",
        "use_case": "Adds the full control plane, browser UI, and heavier orchestration surfaces.",
    },
    "vector": {
        "impact_label": "Moderate",
        "idle_ram_mb": 126,
        "idle_cpu_pct": "0.2-0.8%",
        "startup_seconds": "1.0-2.5s",
        "use_case": "Adds semantic vector memory on top of the full runtime.",
    },
}


def _directory_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda _err: None):
            root_path = Path(root)
            for file_name in files:
                try:
                    total += (root_path / file_name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _bytes_to_mb(size_bytes: int) -> float:
    return round(size_bytes / (1024 * 1024), 1)


def _discover_packaged_runtime_root() -> Path | None:
    code_root = Path(str(CODE_ROOT)).resolve(strict=False)
    for candidate in (code_root, *code_root.parents):
        if candidate.name.lower() == "venv":
            return candidate
    return None


def build_performance_estimate(
    *,
    active_profile: str,
    installed_profiles: dict[str, bool],
    install_mode: str,
    memory_backend: str,
) -> dict:
    profile_key = active_profile if active_profile in PROFILE_PERFORMANCE_HINTS else "core"
    state_root = Path(str(STATE_ROOT)).resolve(strict=False)

    def loader():
        hint = PROFILE_PERFORMANCE_HINTS[profile_key]
        packaged_root = _discover_packaged_runtime_root() if install_mode == "packaged" else None
        install_bytes = _directory_size_bytes(packaged_root) if packaged_root is not None else 0
        state_bytes = _directory_size_bytes(state_root)
        total_bytes = install_bytes + state_bytes

        if install_mode == "packaged":
            install_summary = (
                f"Packaged install footprint is about {_bytes_to_mb(total_bytes)} MB on this machine, "
                f"including {_bytes_to_mb(state_bytes)} MB of local state."
            )
        else:
            install_summary = (
                f"Checkout mode reuses your repo and Python environment. The Pexo-specific local state is about "
                f"{_bytes_to_mb(state_bytes)} MB."
            )

        return {
            "estimated": True,
            "headline": f"{hint['impact_label']} overhead in the {profile_key} profile",
            "summary": (
                "Pexo is mostly a disk and memory cost, not a permanent background drain. "
                "Nothing stays resident until you launch it."
            ),
            "before_install": {
                "label": "Before Pexo",
                "disk_mb": 0.0,
                "idle_ram_mb": 0,
                "idle_cpu_pct": "0%",
                "background_processes": 0,
                "summary": "No local Pexo state, no MCP bridge, and no local control-plane process.",
            },
            "after_install": {
                "label": "Installed, not running",
                "disk_mb": _bytes_to_mb(total_bytes),
                "idle_ram_mb": 0,
                "idle_cpu_pct": "0%",
                "background_processes": 0,
                "summary": install_summary,
            },
            "current_runtime": {
                "label": f"Current runtime ({profile_key})",
                "profile": profile_key,
                "impact_label": hint["impact_label"],
                "idle_ram_mb": hint["idle_ram_mb"],
                "idle_cpu_pct": hint["idle_cpu_pct"],
                "startup_seconds": hint["startup_seconds"],
                "background_processes": 1,
                "memory_backend": memory_backend,
                "summary": hint["use_case"],
            },
            "profile_matrix": [
                {
                    "profile": name,
                    "impact_label": entry["impact_label"],
                    "idle_ram_mb": entry["idle_ram_mb"],
                    "idle_cpu_pct": entry["idle_cpu_pct"],
                    "startup_seconds": entry["startup_seconds"],
                    "available": bool(installed_profiles.get(name)),
                    "use_case": entry["use_case"],
                }
                for name, entry in PROFILE_PERFORMANCE_HINTS.items()
            ],
            "notes": [
                "These numbers are estimates for local developer machines, not live benchmarks of this host.",
                "The largest persistent cost is disk footprint. The largest active cost is one local Pexo process while the control plane is running.",
                (
                    "Keyword memory keeps the runtime lighter than the optional vector layer."
                    if memory_backend != "semantic"
                    else "Semantic vector memory adds the biggest memory and startup hit."
                ),
            ],
        }

    cache_key = (
        profile_key,
        install_mode,
        memory_backend,
        tuple(sorted(name for name, installed in installed_profiles.items() if installed)),
        str(state_root),
    )
    return cached_value("runtime_performance", cache_key, 30.0, loader)


def build_runtime_status(db: Session | None = None) -> dict:
    marker_key = get_runtime_marker_profile()

    def loader():
        from .routers.tools import get_genesis_policy

        installed_profiles = _profile_install_matrix()
        marker_profile = reconcile_runtime_marker_profile(installed_profiles)
        active_profile = _highest_installed_profile(installed_profiles)
        vector_offer_pending = False
        genesis_policy = get_genesis_policy(db) if db is not None else {
            "mode": "approval-required",
            "approved_tools": ["safe_tool", "cwd_echo"],
        }

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
            "genesis_policy": genesis_policy,
            "recommended_promotions": [
                profile
                for profile in ("mcp", "full")
                if not installed_profiles[profile]
            ],
            "vector_promotion_offer_pending": vector_offer_pending,
            "vector_promotion_offer": None,
            "performance": build_performance_estimate(
                active_profile=active_profile,
                installed_profiles=installed_profiles,
                install_mode="checkout" if running_from_repo_checkout() else "packaged",
                memory_backend="semantic" if installed_profiles["vector"] else "keyword",
            ),
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
