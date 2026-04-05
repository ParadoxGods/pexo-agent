from __future__ import annotations

import json
import os
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path
from threading import RLock
from urllib.parse import unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
CODE_ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
_ENV_UNSET = object()
CHECKOUT_STATE_DIRNAME = ".pexo"
_context_lock = RLock()
_runtime_path_context = {
    "env_override": _ENV_UNSET,
    "home_dir": None,
    "runtime_invoker": None,
    "code_root": None,
}


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


def set_runtime_path_context(
    *,
    env_override: str | None | object = _ENV_UNSET,
    home_dir: Path | None = None,
    runtime_invoker: str | Path | None = None,
    code_root: Path | None = None,
) -> None:
    with _context_lock:
        _runtime_path_context["env_override"] = env_override
        _runtime_path_context["home_dir"] = home_dir
        _runtime_path_context["runtime_invoker"] = runtime_invoker
        _runtime_path_context["code_root"] = code_root


def reset_runtime_path_context() -> None:
    set_runtime_path_context(
        env_override=_ENV_UNSET,
        home_dir=None,
        runtime_invoker=None,
        code_root=None,
    )


def _current_code_root() -> Path:
    with _context_lock:
        override = _runtime_path_context["code_root"]
    return (override or CODE_ROOT).resolve(strict=False)


def current_state_root() -> Path:
    with _context_lock:
        env_override = _runtime_path_context["env_override"]
        home_dir = _runtime_path_context["home_dir"]
        runtime_invoker = _runtime_path_context["runtime_invoker"]
    return resolve_state_root(
        code_root=_current_code_root(),
        env_override=env_override,
        home_dir=home_dir,
        runtime_invoker=runtime_invoker,
    )


def current_project_root() -> Path:
    code_root = _current_code_root()
    if looks_like_repo_checkout(code_root):
        return code_root.resolve(strict=False)
    return current_state_root()


class RuntimePath(os.PathLike):
    def __init__(self, resolver):
        self._resolver = resolver

    def _path(self) -> Path:
        return self._resolver().resolve(strict=False)

    def __fspath__(self) -> str:
        return os.fspath(self._path())

    def __str__(self) -> str:
        return str(self._path())

    def __repr__(self) -> str:
        return repr(self._path())

    def __truediv__(self, other):
        return self._path() / other

    def __rtruediv__(self, other):
        return Path(other) / self._path()

    def __getattr__(self, name):
        return getattr(self._path(), name)

    def __eq__(self, other) -> bool:
        try:
            other_path = other._path() if isinstance(other, RuntimePath) else Path(other)
        except TypeError:
            return False
        return self._path() == other_path.resolve(strict=False)

    def __hash__(self) -> int:
        return hash(self._path())


STATE_ROOT = RuntimePath(current_state_root)
WORKSPACE_ROOT = RuntimePath(current_project_root)
PROJECT_ROOT = RuntimePath(current_project_root)
DYNAMIC_TOOLS_DIR = RuntimePath(lambda: current_state_root() / "dynamic_tools")
ARTIFACTS_DIR = RuntimePath(lambda: current_state_root() / "artifacts")
PEXO_DB_PATH = RuntimePath(lambda: current_state_root() / "pexo.db")
CHROMA_DB_DIR = RuntimePath(lambda: current_state_root() / "chroma_db")
RUNTIME_MARKER_PATH = RuntimePath(lambda: current_state_root() / ".pexo-deps-profile")
UPDATE_STAMP_PATH = RuntimePath(lambda: current_state_root() / ".pexo-update-check")
INSTALL_METADATA_PATH = RuntimePath(lambda: current_state_root() / ".pexo-install.json")


def running_from_repo_checkout() -> bool:
    return looks_like_repo_checkout(_current_code_root()) and resolve_managed_runtime_state_root(
        _runtime_path_context.get("runtime_invoker")
    ) is None
