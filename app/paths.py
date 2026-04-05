from __future__ import annotations

import json
import os
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
CODE_ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
_ENV_UNSET = object()
CHECKOUT_STATE_DIRNAME = ".pexo"


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


def resolve_editable_source_root() -> Path | None:
    try:
        distribution = importlib_metadata.distribution("pexo-agent")
        direct_url_path = Path(distribution.locate_file("direct_url.json"))
        if not direct_url_path.exists():
            return None
        payload = json.loads(direct_url_path.read_text(encoding="utf-8"))
    except (
        importlib_metadata.PackageNotFoundError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None

    if not bool((payload.get("dir_info") or {}).get("editable")):
        return None

    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return None

    parsed = urlparse(raw_url)
    if parsed.scheme and parsed.scheme != "file":
        return None

    path_text = unquote(parsed.path or "")
    if os.name == "nt" and len(path_text) >= 3 and path_text[0] == "/" and path_text[2] == ":":
        path_text = path_text[1:]
    if not path_text:
        return None
    return Path(path_text).expanduser().resolve(strict=False)


def resolve_managed_runtime_state_root(runtime_invoker: str | Path | None = None) -> Path | None:
    invoker_source = runtime_invoker if runtime_invoker is not None else (sys.argv[0] if sys.argv else "")
    if not invoker_source:
        return None

    invoker_path = Path(str(invoker_source)).expanduser().resolve(strict=False)
    invoker_name = invoker_path.name.lower()
    if not invoker_name.startswith("pexo"):
        return None

    scripts_dir = invoker_path.parent
    if scripts_dir.name.lower() not in {"scripts", "bin"}:
        return None

    venv_dir = scripts_dir.parent
    if venv_dir.name.lower() != "venv":
        return None

    state_root = venv_dir.parent.resolve(strict=False)
    metadata_path = state_root / ".pexo-install.json"
    if metadata_path.exists() or state_root.name.lower() == ".pexo":
        return state_root
    return None


def resolve_checkout_state_root(code_root: Path | None = None) -> Path:
    root = (code_root or CODE_ROOT).resolve(strict=False)
    return (root / CHECKOUT_STATE_DIRNAME).resolve(strict=False)


def resolve_state_root(
    *,
    code_root: Path | None = None,
    env_override: str | None | object = _ENV_UNSET,
    home_dir: Path | None = None,
    runtime_invoker: str | Path | None = None,
) -> Path:
    override_source = os.environ.get("PEXO_HOME") if env_override is _ENV_UNSET else env_override
    override = normalize_user_path(override_source)
    if override is not None:
        return override

    managed_runtime_root = resolve_managed_runtime_state_root(runtime_invoker)
    if managed_runtime_root is not None:
        return managed_runtime_root

    candidate_root = code_root or CODE_ROOT
    if looks_like_repo_checkout(candidate_root):
        return resolve_checkout_state_root(candidate_root)

    base_home = home_dir or Path.home()
    return (base_home / ".pexo").resolve(strict=False)


STATE_ROOT = resolve_state_root()
WORKSPACE_ROOT = CODE_ROOT.resolve(strict=False) if looks_like_repo_checkout(CODE_ROOT) else STATE_ROOT
PROJECT_ROOT = WORKSPACE_ROOT
DYNAMIC_TOOLS_DIR = STATE_ROOT / "dynamic_tools"
ARTIFACTS_DIR = STATE_ROOT / "artifacts"
PEXO_DB_PATH = STATE_ROOT / "pexo.db"
CHROMA_DB_DIR = STATE_ROOT / "chroma_db"
RUNTIME_MARKER_PATH = STATE_ROOT / ".pexo-deps-profile"
UPDATE_STAMP_PATH = STATE_ROOT / ".pexo-update-check"
INSTALL_METADATA_PATH = STATE_ROOT / ".pexo-install.json"


def running_from_repo_checkout() -> bool:
    return looks_like_repo_checkout(CODE_ROOT) and resolve_managed_runtime_state_root() is None
