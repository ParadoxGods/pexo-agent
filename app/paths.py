from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CODE_ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
_ENV_UNSET = object()


def normalize_user_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    return Path(raw_path).expanduser().resolve(strict=False)


def looks_like_repo_checkout(root: Path) -> bool:
    return (
        (root / "app").is_dir()
        and (root / "README.md").exists()
        and (root / "requirements.txt").exists()
        and ((root / ".git").exists() or (root / "install.ps1").exists() or (root / "install.sh").exists())
    )


def resolve_state_root(
    *,
    code_root: Path | None = None,
    env_override: str | None | object = _ENV_UNSET,
    home_dir: Path | None = None,
) -> Path:
    override_source = os.environ.get("PEXO_HOME") if env_override is _ENV_UNSET else env_override
    override = normalize_user_path(override_source)
    if override is not None:
        return override

    candidate_root = code_root or CODE_ROOT
    if looks_like_repo_checkout(candidate_root):
        return candidate_root

    base_home = home_dir or Path.home()
    return (base_home / ".pexo").resolve(strict=False)


STATE_ROOT = resolve_state_root()
PROJECT_ROOT = STATE_ROOT
DYNAMIC_TOOLS_DIR = STATE_ROOT / "dynamic_tools"
ARTIFACTS_DIR = STATE_ROOT / "artifacts"
PEXO_DB_PATH = STATE_ROOT / "pexo.db"
CHROMA_DB_DIR = STATE_ROOT / "chroma_db"
RUNTIME_MARKER_PATH = STATE_ROOT / ".pexo-deps-profile"
UPDATE_STAMP_PATH = STATE_ROOT / ".pexo-update-check"
INSTALL_METADATA_PATH = STATE_ROOT / ".pexo-install.json"


def running_from_repo_checkout() -> bool:
    return looks_like_repo_checkout(CODE_ROOT)
