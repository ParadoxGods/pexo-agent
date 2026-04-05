from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from .client_connect import SUPPORTED_CLIENTS, SUPPORTED_SCOPES, connect_clients
from .cli import build_parser as build_cli_parser
from .database import SessionLocal, ensure_db_ready
from .direct_chat import create_chat_session, get_chat_session_payload, send_chat_message, update_chat_session
from .paths import (
    ARTIFACTS_DIR,
    CHROMA_DB_DIR,
    CODE_ROOT,
    DYNAMIC_TOOLS_DIR,
    INSTALL_METADATA_PATH,
    PEXO_DB_PATH,
    PROJECT_ROOT,
    RUNTIME_MARKER_PATH,
    STATE_ROOT,
    UPDATE_STAMP_PATH,
    resolve_editable_source_root,
    running_from_repo_checkout,
)
from .runtime import build_runtime_status, promote_runtime
from .version import __version__

UPDATE_INTERVAL_SECONDS = 12 * 60 * 60
GITHUB_API_ROOT = "https://api.github.com/repos"
RELEASE_WHEEL_PREFIX = "pexo_agent-"
RELEASE_WHEEL_SUFFIX = "-py3-none-any.whl"
RELEASE_CHECKSUM_ASSET = "SHA256SUMS.txt"
RELEASE_MANIFEST_ASSET = "pexo-install-manifest.json"
PACKAGE_UPDATE_COMMAND = "pexo --update"
PEXO_ASCII_BANNER = r"""
    ____  ________  ______
   / __ \/ ____/ |/_/ __ \
  / /_/ / __/  >  </ / / /
 / ____/ /___ / /| / /_/ /
/_/   /_____//_/ |_\____/
"""
PACKAGED_UPDATE_HELPER = r"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path


def _print_progress(percent: int, status: str) -> None:
    width = 28
    bounded = max(0, min(100, int(percent)))
    filled = min(width, int(round((bounded / 100) * width)))
    bar = ("#" * filled) + ("-" * (width - filled))
    print(f"[{bar}] {bounded:3d}% {status}", flush=True)


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pexo-updater",
        },
    )


def _download(url: str, destination: Path) -> None:
    with urllib.request.urlopen(_request(url), timeout=60) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_metadata(
    metadata_path: Path,
    *,
    version: str,
    release_url: str,
    wheel_sha256: str,
    dependency_fingerprint: str,
) -> None:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        metadata = {}
    guidance = metadata.get("guidance") or {}
    guidance["update"] = "pexo --update"
    metadata["guidance"] = guidance
    metadata["version"] = version
    metadata["release"] = release_url
    metadata["wheel_sha256"] = wheel_sha256
    metadata["dependency_fingerprint"] = dependency_fingerprint
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _site_packages_path() -> Path:
    return Path(sysconfig.get_paths()["purelib"]).resolve()


def _read_requires_dist(wheel_path: Path) -> list[str]:
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_name = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
    return [entry.strip() for entry in (metadata.get_all("Requires-Dist") or []) if entry.strip()]


def _remove_existing_package_files(site_packages_path: Path) -> None:
    for pattern in ("__editable__.pexo_agent-*.pth", "__editable___pexo_agent_*_finder.py"):
        for editable_artifact in site_packages_path.glob(pattern):
            editable_artifact.unlink(missing_ok=True)
    for dist_info_dir in site_packages_path.glob("pexo_agent-*.dist-info"):
        record_path = dist_info_dir / "RECORD"
        if record_path.exists():
            try:
                with record_path.open("r", encoding="utf-8", newline="") as handle:
                    for row in csv.reader(handle):
                        if not row:
                            continue
                        relative_path = row[0].strip()
                        if not relative_path:
                            continue
                        candidate = (site_packages_path / relative_path).resolve(strict=False)
                        try:
                            candidate.relative_to(site_packages_path)
                        except ValueError:
                            continue
                        if candidate.is_file():
                            candidate.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(dist_info_dir, ignore_errors=True)


def _iter_overlay_members(archive: zipfile.ZipFile):
    for member in archive.infolist():
        name = member.filename
        if member.is_dir():
            continue
        if ".data/scripts/" in name:
            continue
        if ".data/purelib/" in name:
            yield member, name.split(".data/purelib/", 1)[1]
            continue
        if ".data/platlib/" in name:
            yield member, name.split(".data/platlib/", 1)[1]
            continue
        yield member, name


def _overlay_wheel(wheel_path: Path) -> None:
    site_packages_path = _site_packages_path()
    _remove_existing_package_files(site_packages_path)
    with zipfile.ZipFile(wheel_path) as archive:
        for member, relative_target in _iter_overlay_members(archive):
            destination = site_packages_path / relative_target
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def _sync_dependencies(target_python: str, wheel_path: Path) -> int:
    requirements = _read_requires_dist(wheel_path)
    if not requirements:
        return 0
    completed = subprocess.run(
        [target_python, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", *requirements],
        check=False,
    )
    return int(completed.returncode)


def _warmup(target_python: str) -> None:
    try:
        subprocess.run(
            [target_python, "-m", "app.launcher", "warmup", "--quiet"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
    except Exception:
        return


def main() -> int:
    plan_path = Path(sys.argv[1]).resolve()
    temp_root = plan_path.parent
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        wheel_path = temp_root / plan["wheel_name"]
        checksum_path = temp_root / "SHA256SUMS.txt"

        print(f"Updating Pexo to {plan['version']}...")
        _print_progress(5, "Preparing update plan")
        _print_progress(20, "Downloading release assets")
        _download(plan["wheel_url"], wheel_path)
        _download(plan["checksum_url"], checksum_path)

        expected = None
        for line in checksum_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1] == plan["wheel_name"]:
                expected = parts[0].strip().lower()
                break
        if not expected:
            print(f"Unable to verify checksum for {plan['wheel_name']}.", file=sys.stderr)
            return 1

        actual = _sha256(wheel_path)
        if actual != expected:
            print(f"Checksum mismatch for {plan['wheel_name']}.", file=sys.stderr)
            return 1

        install_label = plan.get("install_label") or "Installing update..."
        _print_progress(45, "Verifying release checksum")
        _print_progress(70, install_label)
        target_python = plan["target_python"]
        if plan.get("operation") == "full":
            subprocess.run(
                [target_python, "-m", "ensurepip", "--upgrade"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            completed_code = _sync_dependencies(target_python, wheel_path)
            if completed_code != 0:
                return completed_code
        _overlay_wheel(wheel_path)
        _print_progress(88, "Priming local runtime")
        _warmup(target_python)

        _write_metadata(
            Path(plan["install_metadata_path"]),
            version=plan["version"],
            release_url=plan["release_url"],
            wheel_sha256=plan.get("wheel_sha256", ""),
            dependency_fingerprint=plan.get("dependency_fingerprint", ""),
        )
        Path(plan["update_stamp_path"]).write_text(str(int(time.time())), encoding="utf-8")
        _print_progress(100, f"Pexo updated to {plan['version']}.")

        return 0
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _read_install_metadata() -> dict | None:
    if not INSTALL_METADATA_PATH.exists():
        return None
    try:
        return json.loads(INSTALL_METADATA_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _coerce_repo_source() -> str:
    return "ParadoxGods/pexo-agent"


def _package_update_guidance() -> str:
    return PACKAGE_UPDATE_COMMAND


def _site_packages_path() -> Path:
    return Path(sysconfig.get_paths()["purelib"]).resolve()


def _editable_install_artifacts_present(site_packages_path: Path | None = None) -> bool:
    purelib = site_packages_path or _site_packages_path()
    for pattern in ("__editable__.pexo_agent-*.pth", "__editable___pexo_agent_*_finder.py"):
        if next(purelib.glob(pattern), None) is not None:
            return True
    return False


def _resolve_runtime_python_executable() -> str:
    executable = Path(sys.executable).resolve(strict=False)
    if executable.name.lower().startswith("python"):
        return str(executable)

    candidates: list[Path] = []
    if os.name == "nt":
        candidates.extend(
            [
                executable.with_name("python.exe"),
                Path(sys.prefix).resolve(strict=False) / "Scripts" / "python.exe",
            ]
        )
    else:
        candidates.extend(
            [
                executable.with_name("python3"),
                executable.with_name("python"),
                Path(sys.prefix).resolve(strict=False) / "bin" / "python3",
                Path(sys.prefix).resolve(strict=False) / "bin" / "python",
            ]
        )

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return str(candidate.resolve(strict=False))
    return str(executable)


def _package_uninstall_guidance() -> str:
    return "pexo uninstall"


def _print_start_banner() -> None:
    banner = PEXO_ASCII_BANNER.strip("\n")
    print(banner)
    print("")
    print("PEXO | Primary EXecution Operator | local-first control plane")


def _render_progress_bar(percent: int, status: str, *, width: int = 28) -> str:
    bounded = max(0, min(100, int(percent)))
    filled = min(width, int(round((bounded / 100) * width)))
    bar = ("#" * filled) + ("-" * (width - filled))
    return f"[{bar}] {bounded:3d}% {status}"


def _print_progress_bar(percent: int, status: str) -> None:
    print(_render_progress_bar(percent, status))


def _start_terminal_fetch_animation(label: str = "pexo> fetching answer") -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def worker() -> None:
        frames = ("   ", ".  ", ".. ", "...")
        index = 0
        while not stop_event.is_set():
            sys.stdout.write("\r" + label + frames[index % len(frames)])
            sys.stdout.flush()
            index += 1
            time.sleep(0.15)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return stop_event, thread


def _stop_terminal_fetch_animation(stop_event: threading.Event, thread: threading.Thread, label: str = "pexo> fetching answer") -> None:
    stop_event.set()
    thread.join(timeout=0.5)
    clear_width = len(label) + 3
    sys.stdout.write("\r" + (" " * clear_width) + "\r")
    sys.stdout.flush()


def _github_api_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"pexo/{__version__}",
        },
    )


def _fetch_latest_release(repo_source: str | None = None) -> dict:
    source = repo_source or _coerce_repo_source()
    with urllib.request.urlopen(
        _github_api_request(f"{GITHUB_API_ROOT}/{source}/releases/latest"),
        timeout=20,
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_release_manifest(asset_url: str) -> dict | None:
    request = urllib.request.Request(
        asset_url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": f"pexo/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _select_release_asset(release: dict, *, exact_name: str | None = None, suffix: str | None = None) -> dict:
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        if exact_name and name == exact_name:
            return asset
        if suffix and name.startswith(RELEASE_WHEEL_PREFIX) and name.endswith(suffix):
            return asset
    raise RuntimeError("Required release asset is missing from the latest GitHub release.")


def _build_packaged_update_plan() -> dict:
    release = _fetch_latest_release()
    wheel_asset = _select_release_asset(release, suffix=RELEASE_WHEEL_SUFFIX)
    checksum_asset = _select_release_asset(release, exact_name=RELEASE_CHECKSUM_ASSET)
    manifest_asset = _select_release_asset(release, exact_name=RELEASE_MANIFEST_ASSET)
    version = str(release.get("tag_name") or "").lstrip("v") or __version__
    release_url = str(release.get("html_url") or f"https://github.com/{_coerce_repo_source()}/releases/tag/v{version}")
    current_metadata = _read_install_metadata() or {}

    manifest: dict | None = None
    try:
        manifest = _fetch_release_manifest(str(manifest_asset["browser_download_url"]))
    except Exception:
        manifest = None

    current_wheel_sha = str(current_metadata.get("wheel_sha256") or "").lower()
    current_dependency_fingerprint = str(current_metadata.get("dependency_fingerprint") or "")
    target_wheel_sha = str(((manifest or {}).get("wheel") or {}).get("sha256") or "").lower()
    target_dependency_fingerprint = str((manifest or {}).get("dependency_fingerprint") or "")
    editable_residue = _editable_install_artifacts_present()

    operation = "full"
    install_label = "Installing update..."
    pip_args = ["install", "--disable-pip-version-check", "--force-reinstall"]
    if editable_residue:
        operation = "wheel-only"
        install_label = "Normalizing packaged runtime..."
        pip_args = ["install", "--disable-pip-version-check", "--force-reinstall", "--no-deps"]
    elif current_wheel_sha and target_wheel_sha and current_wheel_sha == target_wheel_sha:
        operation = "skip"
        install_label = "Pexo is already up to date."
        pip_args = []
    elif current_dependency_fingerprint and target_dependency_fingerprint and current_dependency_fingerprint == target_dependency_fingerprint:
        operation = "wheel-only"
        install_label = "Installing update (wheel refresh only)..."
        pip_args = ["install", "--disable-pip-version-check", "--force-reinstall", "--no-deps"]

    return {
        "version": version,
        "release_url": release_url,
        "wheel_name": str(wheel_asset["name"]),
        "wheel_url": str(wheel_asset["browser_download_url"]),
        "checksum_url": str(checksum_asset["browser_download_url"]),
        "manifest_url": str(manifest_asset["browser_download_url"]),
        "target_python": _resolve_runtime_python_executable(),
        "install_metadata_path": str(INSTALL_METADATA_PATH),
        "update_stamp_path": str(UPDATE_STAMP_PATH),
        "operation": operation,
        "install_label": install_label,
        "pip_args": pip_args,
        "wheel_sha256": target_wheel_sha,
        "dependency_fingerprint": target_dependency_fingerprint,
        "editable_residue": editable_residue,
    }


def _prepare_packaged_update_helper(plan: dict) -> tuple[Path, Path]:
    temp_root = Path(tempfile.mkdtemp(prefix="pexo-update-"))
    helper_path = temp_root / "pexo_update_helper.py"
    plan_path = temp_root / "update-plan.json"
    helper_path.write_text(PACKAGED_UPDATE_HELPER, encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return helper_path, plan_path


def _exec_update_helper(helper_path: Path, plan_path: Path) -> int:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        target_python = str(plan.get("target_python") or _resolve_runtime_python_executable())
    except (OSError, ValueError, json.JSONDecodeError):
        target_python = _resolve_runtime_python_executable()
    sys.stdout.flush()
    sys.stderr.flush()
    completed = subprocess.run(
        [target_python, str(helper_path), str(plan_path)],
        check=False,
    )
    return int(completed.returncode)


def _restart_launcher_process(argv: list[str] | None = None) -> int:
    os.execv(sys.executable, [sys.executable, "-m", "app.launcher", *(argv if argv is not None else sys.argv[1:])])
    return 0


def _update_stamp_is_fresh() -> bool:
    if not UPDATE_STAMP_PATH.exists():
        return False
    try:
        last = int(UPDATE_STAMP_PATH.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return False
    return (int(time.time()) - last) < UPDATE_INTERVAL_SECONDS


def _write_update_stamp() -> None:
    UPDATE_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_STAMP_PATH.write_text(str(int(time.time())), encoding="utf-8")


def _extract_release_version_from_url(url: str | None) -> str | None:
    raw_url = str(url or "").strip()
    if not raw_url:
        return None
    match = re.search(r"/releases/tag/v([^/?#]+)$", raw_url)
    if not match:
        return None
    return match.group(1).strip() or None


def _git_checkout_branch() -> str | None:
    if not running_from_repo_checkout():
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    branch_name = completed.stdout.strip()
    return branch_name or None


def _checkout_is_detached() -> bool:
    branch_name = _git_checkout_branch()
    return branch_name in {None, "", "HEAD"}


def maybe_update(skip_update: bool = False) -> None:
    if skip_update or not running_from_repo_checkout():
        return
    if _update_stamp_is_fresh():
        return
    if _checkout_is_detached():
        print("Update check skipped because this checkout is pinned to a detached git HEAD.", file=sys.stderr)
        _write_update_stamp()
        return

    completed = subprocess.run(
        ["git", "pull", "--ff-only", "--quiet"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        _write_update_stamp()
        return

    print("Update check failed. Continuing with the local version.", file=sys.stderr)
    print(
        "Run 'pexo update' for full git output. If this checkout is private, detached, or access-controlled, verify authentication and branch state.",
        file=sys.stderr,
    )


def run_update() -> int:
    if running_from_repo_checkout():
        if _checkout_is_detached():
            print("Update skipped because this checkout is pinned to a detached git HEAD. Checkout a branch before pulling updates.")
            return 0
        completed = subprocess.run(["git", "pull", "--ff-only"], cwd=str(PROJECT_ROOT), check=False)
        if completed.returncode == 0:
            _write_update_stamp()
            if _local_pexo_http_available("127.0.0.1", 9999):
                print("A Pexo server is already running. Restart it to load the updated build.")
        return completed.returncode

    try:
        plan = _build_packaged_update_plan()
    except Exception as exc:
        print(f"Unable to prepare a packaged update: {exc}", file=sys.stderr)
        return 1

    if plan["operation"] == "skip":
        print(f"Installed package detected. Pexo v{plan['version']} is already current.")
        if _local_pexo_http_available("127.0.0.1", 9999):
            print("A Pexo server is already running. Restart it if you need the in-memory server to load the current build.")
        _write_update_stamp()
        return 0

    print(f"Installed package detected. Preparing update to v{plan['version']}...")
    server_state = _maybe_stop_existing_server_for_update("127.0.0.1", 9999)
    if server_state == "declined":
        return 0
    if server_state == "failed":
        print("Unable to stop the running Pexo server. Close it and run `pexo --update` again.", file=sys.stderr)
        return 1
    if server_state == "unavailable":
        print(
            "A Pexo server is already running and must be stopped before this packaged update can continue.",
            file=sys.stderr,
        )
        return 1
    helper_path, plan_path = _prepare_packaged_update_helper(plan)
    return _exec_update_helper(helper_path, plan_path)


def shutil_which(command_name: str) -> str | None:
    from shutil import which

    return which(command_name)


def _port_is_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((host, port)) == 0


def _local_pexo_http_available(host: str, port: int) -> bool:
    for path in ("/admin/snapshot", "/docs", "/ui/"):
        try:
            with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=0.5):
                return True
        except Exception:
            continue
    return False


def _can_prompt_for_restart() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)()) and bool(getattr(sys.stdout, "isatty", lambda: False)())


def _find_listening_pids(port: int) -> list[int]:
    pids: set[int] = set()
    if os.name == "nt":
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$ErrorActionPreference='SilentlyContinue'; Get-NetTCPConnection -State Listen -LocalPort {port} | "
                "Select-Object -ExpandProperty OwningProcess -Unique",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            for line in completed.stdout.splitlines():
                value = line.strip()
                if value.isdigit():
                    pids.add(int(value))
        if not pids:
            completed = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                for line in completed.stdout.splitlines():
                    if "LISTENING" not in line.upper():
                        continue
                    match = re.search(rf":{port}\s+.+LISTENING\s+(\d+)\s*$", line, re.IGNORECASE)
                    if match:
                        pids.add(int(match.group(1)))
    else:
        if shutil_which("lsof"):
            completed = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                for line in completed.stdout.splitlines():
                    value = line.strip()
                    if value.isdigit():
                        pids.add(int(value))
        if not pids and shutil_which("fuser"):
            completed = subprocess.run(
                ["fuser", f"{port}/tcp"],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                for value in re.findall(r"\d+", completed.stdout + " " + completed.stderr):
                    pids.add(int(value))
        if not pids and shutil_which("ss"):
            completed = subprocess.run(
                ["ss", "-ltnp", f"sport = :{port}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                for value in re.findall(r"pid=(\d+)", completed.stdout):
                    pids.add(int(value))
    pids.discard(os.getpid())
    return sorted(pids)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return not _pid_exists(pid)
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.05)
    return not _pid_exists(pid)


def _stop_local_pexo_server(host: str, port: int) -> bool:
    pids = _find_listening_pids(port)
    if not pids:
        return False
    stopped_any = False
    for pid in pids:
        stopped_any = _terminate_pid(pid) or stopped_any
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _port_is_in_use(host, port):
            return stopped_any
        time.sleep(0.1)
    return False


def _maybe_restart_existing_server(host: str, port: int) -> str:
    if not _can_prompt_for_restart():
        return "unavailable"
    try:
        answer = input(
            f"Pexo is already running at http://{host}:{port}. "
            "Stop the old server and start the current build instead? [Y/n]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        return "declined"
    if answer not in {"", "y", "yes"}:
        print("Keeping the existing Pexo server running.")
        return "declined"
    print("Stopping the running Pexo server...")
    return "restarted" if _stop_local_pexo_server(host, port) else "failed"


def _maybe_stop_existing_server_for_update(host: str, port: int) -> str:
    return _maybe_stop_existing_server_for_maintenance(host, port, action_label="the update", cancel_label="Update")


def _maybe_stop_existing_server_for_maintenance(host: str, port: int, *, action_label: str, cancel_label: str) -> str:
    if not _local_pexo_http_available(host, port):
        return "not_running"
    if not _can_prompt_for_restart():
        return "unavailable"
    try:
        answer = input(
            f"Pexo is currently running at http://{host}:{port}. "
            f"Stop it now so {action_label} can continue? [Y/n]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        return "declined"
    if answer not in {"", "y", "yes"}:
        print(f"{cancel_label} cancelled. The running Pexo server was left in place.")
        return "declined"
    print("Stopping the running Pexo server...")
    return "stopped" if _stop_local_pexo_server(host, port) else "failed"


def _build_packaged_uninstall_helper_script(*, keep_state: bool) -> str:
    metadata = _read_install_metadata() or {}
    install_method = str(metadata.get("method") or "")
    state_root = str(STATE_ROOT)
    metadata_path = str(INSTALL_METADATA_PATH)
    update_stamp_path = str(UPDATE_STAMP_PATH)
    runtime_marker_path = str(RUNTIME_MARKER_PATH)
    command_path = str(metadata.get("command_path") or "")
    uninstall_command = str(metadata.get("guidance", {}).get("uninstall") or "")
    if uninstall_command == "pexo uninstall" or not uninstall_command:
        lowered_method = install_method.lower()
        if "pipx" in lowered_method:
            uninstall_command = "pipx uninstall pexo-agent"
        elif "uv" in lowered_method:
            uninstall_command = "uv tool uninstall pexo-agent"

    if os.name == "nt":
        if install_method == "release_bundle_managed_venv" and command_path:
            scripts_dir = str(Path(command_path).resolve(strict=False).parent)
            ps_state_root = state_root.replace("'", "''")
            ps_metadata_path = metadata_path.replace("'", "''")
            ps_update_stamp = update_stamp_path.replace("'", "''")
            ps_runtime_marker = runtime_marker_path.replace("'", "''")
            ps_scripts_dir = scripts_dir.replace("'", "''")
            remove_state_ps = "$true" if keep_state is False else "$false"
            return f"""$ErrorActionPreference = "SilentlyContinue"
Start-Sleep -Seconds 1
$scriptsDir = '{ps_scripts_dir}'
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not [string]::IsNullOrWhiteSpace($currentPath)) {{
    $parts = $currentPath.Split(";") | Where-Object {{ $_ -and $_ -ne $scriptsDir }}
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}}
Remove-Item -LiteralPath '{ps_metadata_path}' -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '{ps_update_stamp}' -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '{ps_runtime_marker}' -Force -ErrorAction SilentlyContinue
if ({remove_state_ps}) {{
    Remove-Item -LiteralPath '{ps_state_root}' -Recurse -Force -ErrorAction SilentlyContinue
}} else {{
    Remove-Item -LiteralPath (Join-Path '{ps_state_root}' 'venv') -Recurse -Force -ErrorAction SilentlyContinue
}}
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""
        ps_state_root = state_root.replace("'", "''")
        ps_metadata_path = metadata_path.replace("'", "''")
        ps_update_stamp = update_stamp_path.replace("'", "''")
        ps_runtime_marker = runtime_marker_path.replace("'", "''")
        ps_uninstall = uninstall_command.replace("'", "''")
        remove_state_ps = "$true" if keep_state is False else "$false"
        return f"""$ErrorActionPreference = "SilentlyContinue"
Start-Sleep -Seconds 1
$toolCommand = '{ps_uninstall}'
if (-not [string]::IsNullOrWhiteSpace($toolCommand) -and $toolCommand -ne 'pexo uninstall') {{
    & cmd.exe /c $toolCommand | Out-Null
}}
Remove-Item -LiteralPath '{ps_metadata_path}' -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '{ps_update_stamp}' -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '{ps_runtime_marker}' -Force -ErrorAction SilentlyContinue
if ({remove_state_ps}) {{
    Remove-Item -LiteralPath '{ps_state_root}' -Recurse -Force -ErrorAction SilentlyContinue
}}
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""

    shell_quote = lambda value: "'" + str(value).replace("'", "'\"'\"'") + "'"
    if install_method == "release_bundle_managed_venv" and command_path:
        bin_dir = str(Path(command_path).resolve(strict=False).parent)
        remove_state_value = "1" if keep_state is False else "0"
        return f"""#!/bin/sh
set +e
sleep 1
PATH_ENTRY={shell_quote(bin_dir)}
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -f "$rc" ] || continue
  tmp="${{rc}}.pexo.$$"
  grep -Fvx "export PATH=\\"\\$PATH:${{PATH_ENTRY}}\\"" "$rc" > "$tmp" 2>/dev/null && mv "$tmp" "$rc" || rm -f "$tmp"
done
rm -f {shell_quote(metadata_path)} {shell_quote(update_stamp_path)} {shell_quote(runtime_marker_path)}
if [ "{remove_state_value}" = "1" ]; then
  rm -rf {shell_quote(state_root)}
else
  rm -rf {shell_quote(str(Path(state_root) / "venv"))}
fi
rm -f "$0"
"""
    remove_state_value = "1" if keep_state is False else "0"
    return f"""#!/bin/sh
set +e
sleep 1
tool_uninstall_command={shell_quote(uninstall_command)}
if [ -n "$tool_uninstall_command" ] && [ "$tool_uninstall_command" != "pexo uninstall" ]; then
  sh -lc "$tool_uninstall_command" >/dev/null 2>&1
fi
rm -f {shell_quote(metadata_path)} {shell_quote(update_stamp_path)} {shell_quote(runtime_marker_path)}
if [ "{remove_state_value}" = "1" ]; then
  rm -rf {shell_quote(state_root)}
fi
rm -f "$0"
"""


def _prepare_packaged_uninstall_helper(*, keep_state: bool) -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="pexo-uninstall-"))
    suffix = ".ps1" if os.name == "nt" else ".sh"
    helper_path = temp_root / f"pexo_uninstall_helper{suffix}"
    helper_path.write_text(_build_packaged_uninstall_helper_script(keep_state=keep_state), encoding="utf-8")
    if os.name != "nt":
        helper_path.chmod(0o700)
    return helper_path


def _launch_packaged_uninstall_helper(helper_path: Path) -> int:
    stdout = subprocess.DEVNULL
    stderr = subprocess.DEVNULL
    if os.name == "nt":
        creationflags = 0
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper_path)],
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    else:
        subprocess.Popen(
            ["sh", str(helper_path)],
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    return 0


def run_server(no_browser: bool = False) -> int:
    status = build_runtime_status()
    if not status["installed_profiles"].get("full", False):
        _print_progress_bar(10, "Preparing full runtime")
        promotion_result = promote_runtime("full")
        if promotion_result["status"] != "success":
            print(
                promotion_result.get("stderr") or promotion_result.get("stdout") or "Failed to prepare the full runtime.",
                file=sys.stderr,
            )
            return 1
        _print_progress_bar(80, "Priming full runtime")
        run_warmup(quiet=True)
        _print_progress_bar(100, "Full runtime installed")
        print("Full runtime installed. Restarting Pexo to activate the new environment...")
        return _restart_launcher_process()

    import uvicorn

    if no_browser:
        os.environ["PEXO_NO_BROWSER"] = "1"
    _print_start_banner()
    host = "127.0.0.1"
    port = 9999
    if _port_is_in_use(host, port):
        if _local_pexo_http_available(host, port):
            restart_result = _maybe_restart_existing_server(host, port)
            if restart_result == "restarted":
                if _port_is_in_use(host, port):
                    print(f"Pexo is still running at http://{host}:{port} after the restart attempt.", file=sys.stderr)
                    return 1
            elif restart_result == "declined":
                return 0
            else:
                print(
                    f"Pexo already appears to be running at http://{host}:{port}. "
                    "If you just updated Pexo, stop the running server and start it again to load the new build.",
                    file=sys.stderr,
                )
                return 1
        else:
            print(f"Port {port} is already in use on {host}. Stop the existing process or free the port before starting Pexo.", file=sys.stderr)
            return 1
    uvicorn.run("app.main:app", host=host, port=port, workers=1, use_colors=False)
    return 0


def run_mcp() -> int:
    status = build_runtime_status()
    if not status["installed_profiles"].get("mcp", False):
        _print_progress_bar(10, "Preparing MCP runtime")
        promotion_result = promote_runtime("mcp")
        if promotion_result["status"] != "success":
            print(
                promotion_result.get("stderr") or promotion_result.get("stdout") or "Failed to prepare the MCP runtime.",
                file=sys.stderr,
            )
            return 1
        _print_progress_bar(80, "Priming MCP runtime")
        run_warmup(quiet=True)
        _print_progress_bar(100, "MCP runtime installed")
        return _restart_launcher_process()

    from .mcp_server import start_mcp_server

    start_mcp_server()
    return 0


def run_promote(profile: str) -> int:
    result = promote_runtime(profile)
    if result["status"] == "success":
        print(f"Pexo runtime is ready at profile '{profile.lower()}'.")
        return 0
    print(result.get("stderr") or result.get("stdout") or "Runtime promotion failed.", file=sys.stderr)
    return 1


def run_connect(target: str = "all", scope: str = "user", dry_run: bool = False, as_json: bool = False) -> int:
    report = connect_clients(target=target, scope=scope, dry_run=dry_run)

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print("Pexo Client Connect")
        print(f"MCP target: {report['mcp_server']['display']}")
        print(f"Scope: {report['scope']}")
        for result in report["results"]:
            print(f"{result['client']}: {result['status']}")
            print(f"  manual command: {result['manual_command']}")
            if result.get("message"):
                print(f"  message: {result['message']}")
            if result.get("verify_output"):
                print(f"  verify: {result['verify_output']}")

    if report["status"] == "failed":
        return 1
    if report["status"] == "partial" and report["target"] != "all":
        return 1
    return 0


def run_warmup(quiet: bool = False) -> int:
    def step(percent: int, status: str) -> None:
        if not quiet:
            _print_progress_bar(percent, status)

    try:
        step(10, "Preparing local state")
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        DYNAMIC_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

        step(35, "Bootstrapping database")
        ensure_db_ready()

        step(60, "Loading runtime state")
        build_runtime_status()

        step(80, "Priming client integrations")
        connect_clients(target="all", scope="user", dry_run=True, verify_existing=False)

        step(100, "Pexo is primed")
        return 0
    except Exception as exc:
        if not quiet:
            print(f"Warmup failed: {exc}", file=sys.stderr)
        return 1


def run_chat_mode(backend: str = "auto", workspace_path: str | None = None) -> int:
    ensure_db_ready()
    status = build_runtime_status()
    if not status["installed_profiles"].get("mcp", False):
        promotion_result = promote_runtime("mcp")
        if promotion_result["status"] != "success":
            print(
                promotion_result.get("stderr") or promotion_result.get("stdout") or "Failed to prepare the MCP runtime.",
                file=sys.stderr,
            )
            return 1
        print("MCP runtime installed. Restarting Pexo chat to activate the new environment...")
        return _restart_launcher_process()

    _print_start_banner()
    print("")
    print("PEXO Direct Chat | terminal mode")
    print("Commands: /new  /status  /backend <name>  /workspace <path>  /exit")

    try:
        db = SessionLocal()
        try:
            session_payload = create_chat_session(
                db,
                backend=backend,
                workspace_path=workspace_path,
                title="Terminal Chat",
            )
        finally:
            db.close()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    session_id = session_payload["id"]
    current_backend = session_payload["backend"]
    current_workspace = session_payload["workspace_path"]
    print(f"Backend: {current_backend}")
    print(f"Workspace: {current_workspace}")
    print("")

    while True:
        try:
            raw_input_text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            print("Exiting Pexo chat.")
            return 0

        if not raw_input_text:
            continue

        if raw_input_text.lower() in {"exit", "quit", "/exit", "/quit"}:
            print("Exiting Pexo chat.")
            return 0

        if raw_input_text.lower() in {"/new", "new"}:
            try:
                db = SessionLocal()
                try:
                    session_payload = create_chat_session(
                        db,
                        backend=current_backend,
                        workspace_path=current_workspace,
                        title="Terminal Chat",
                    )
                finally:
                    db.close()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                continue
            session_id = session_payload["id"]
            print(f"Started new chat session {session_id[:8]}.")
            continue

        if raw_input_text.lower() in {"/status", "status"}:
            try:
                db = SessionLocal()
                try:
                    payload = get_chat_session_payload(db, session_id)
                finally:
                    db.close()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                continue
            session = payload["session"]
            reply_details = (payload["messages"][-1]["details"] if payload["messages"] else {}) or {}
            print(f"Session: {session['title']} ({session['id'][:8]})")
            print(f"Backend: {session['backend']}")
            print(f"Workspace: {session['workspace_path']}")
            print(f"Status: {session['status']}")
            session_details = session.get("details") or {}
            if str(session_details.get("task_run_status") or "").strip().lower() == "running":
                role = str(session_details.get("task_run_role") or "worker").strip()
                backend_label = str(session_details.get("task_run_backend") or session["backend"]).strip()
                elapsed = session_details.get("task_run_elapsed_seconds")
                progress = str(session_details.get("task_run_progress_message") or "").strip()
                print(f"Active run: {role} via {backend_label}")
                if elapsed is not None:
                    print(f"Elapsed: {elapsed}s")
                if progress:
                    print(f"Progress: {progress}")
            if reply_details.get("role"):
                print(f"Last role: {reply_details['role']}")
            if session_details.get("last_assistant_message"):
                print(f"Last reply: {session_details['last_assistant_message']}")
            continue

        if raw_input_text.lower().startswith("/backend "):
            requested_backend = raw_input_text.split(" ", 1)[1].strip() or "auto"
            try:
                db = SessionLocal()
                try:
                    session_payload = update_chat_session(
                        db,
                        session_id=session_id,
                        backend=requested_backend,
                    )
                finally:
                    db.close()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                continue
            current_backend = session_payload["backend"]
            print(f"Backend set to {current_backend}.")
            continue

        if raw_input_text.lower().startswith("/workspace "):
            requested_workspace = raw_input_text.split(" ", 1)[1].strip()
            try:
                db = SessionLocal()
                try:
                    session_payload = update_chat_session(
                        db,
                        session_id=session_id,
                        workspace_path=requested_workspace,
                    )
                finally:
                    db.close()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                continue
            current_workspace = session_payload["workspace_path"]
            print(f"Workspace set to {current_workspace}.")
            continue

        print("")
        stop_animation, animation_thread = _start_terminal_fetch_animation()
        try:
            db = SessionLocal()
            try:
                payload = send_chat_message(
                    db,
                    session_id=session_id,
                    message=raw_input_text,
                )
            finally:
                db.close()
        except RuntimeError as exc:
            _stop_terminal_fetch_animation(stop_animation, animation_thread)
            print(str(exc), file=sys.stderr)
            continue
        except Exception:
            _stop_terminal_fetch_animation(stop_animation, animation_thread)
            print("Pexo hit an internal chat error while fetching that answer. Try again or switch backends with /backend <name>.", file=sys.stderr)
            continue

        _stop_terminal_fetch_animation(stop_animation, animation_thread)
        session = payload["session"]
        reply = payload["reply"]
        session_id = session["id"]
        current_backend = session["backend"]
        current_workspace = session["workspace_path"]
        
        print(f"pexo> {reply['user_message']}")
        
        # Terminal auto-follow: monitor background task activity
        if str((session.get("details") or {}).get("task_run_status") or "").strip().lower() == "running":
            print("pexo> Swarm active. Following progress... (Ctrl+C to stop following)")
            last_msg = ""
            try:
                while True:
                    time.sleep(2)
                    db_follow = SessionLocal()
                    try:
                        p = get_chat_session_payload(db_follow, session_id)
                        s = p["session"]
                        details = s.get("details") or {}
                        if str(details.get("task_run_status")).strip().lower() != "running":
                            # Task finished!
                            print("\npexo> Swarm task complete.")
                            if p["messages"]:
                                final_msg = p["messages"][-1]["content"]
                                print(f"\n{final_msg}\n")
                            break
                        
                        msg = str(details.get("task_run_progress_message") or "Working...").strip()
                        if msg != last_msg:
                            sys.stdout.write(f"\rpexo> [Swarm] {msg}                                ")
                            sys.stdout.flush()
                            last_msg = msg
                    finally:
                        db_follow.close()
            except KeyboardInterrupt:
                print("\npexo> Stopped following. Swarm continues in background. Use /status to check.")
        print("")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pexo", description="Pexo command launcher.")
    parser.add_argument("--version", action="store_true", help="Display the current Pexo version.")
    parser.add_argument("--no-browser", action="store_true", help="Start the API without opening the dashboard.")
    parser.add_argument("--offline", action="store_true", help="Skip automatic repository update checks.")
    parser.add_argument("--skip-update", action="store_true", help="Skip automatic repository update checks.")
    parser.add_argument("--chat", action="store_true", help="Start Pexo direct chat in the terminal.")
    parser.add_argument("--mcp", action="store_true", help="Start Pexo in native MCP stdio mode.")
    parser.add_argument("--update", action="store_true", help="Update the current Pexo installation.")
    parser.add_argument(
        "--promote",
        nargs="?",
        const="full",
        metavar="PROFILE",
        help="Install or upgrade the runtime dependency profile (core, mcp, full, vector). 'vector' is optional advanced semantic-memory support.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("mcp", help="Start Pexo in native MCP stdio mode.")
    subparsers.add_parser("update", help="Update the current Pexo installation.")
    promote_parser = subparsers.add_parser("promote", help="Install or upgrade a runtime dependency profile.")
    promote_parser.add_argument("profile", nargs="?", default="full", choices=["core", "mcp", "full", "vector"])
    subparsers.add_parser("list-presets", help="List available profile presets.")
    setup_parser = subparsers.add_parser("headless-setup", help="Initialize a profile without opening the web UI.")
    setup_parser.add_argument("--preset", default="efficient_operator")
    setup_parser.add_argument("--name", default="default_user")
    setup_parser.add_argument("--backup-path", default="")
    setup_parser.add_argument("--clear-backup-path", action="store_true")
    setup_parser.add_argument("--json", action="store_true")
    doctor_parser = subparsers.add_parser("doctor", help="Run local diagnostics for the current Pexo installation.")
    doctor_parser.add_argument("--json", action="store_true", help="Emit diagnostic data as JSON.")
    connect_parser = subparsers.add_parser("connect", help="Configure Codex, Claude, or Gemini to use Pexo as an MCP server.")
    connect_parser.add_argument("client", nargs="?", default="all", choices=["all", *SUPPORTED_CLIENTS])
    connect_parser.add_argument("--scope", default="user", choices=list(SUPPORTED_SCOPES))
    connect_parser.add_argument("--dry-run", action="store_true", help="Print the connection plan without changing client configuration.")
    connect_parser.add_argument("--json", action="store_true", help="Emit connection results as JSON.")
    uninstall_parser = subparsers.add_parser("uninstall", help="Uninstall Pexo from the current delivery mode.")
    uninstall_parser.add_argument("--yes", action="store_true", help="Confirm uninstall without prompting.")
    uninstall_parser.add_argument("--keep-state", action="store_true", help="Remove the packaged install but keep the local Pexo state directory.")
    warmup_parser = subparsers.add_parser("warmup", help="Prime local Pexo state after install or update.")
    warmup_parser.add_argument("--quiet", action="store_true", help="Suppress warmup progress output.")
    chat_parser = subparsers.add_parser("chat", help="Start Pexo direct chat in the terminal.")
    chat_parser.add_argument("--backend", default="auto", choices=["auto", *SUPPORTED_CLIENTS])
    chat_parser.add_argument("--workspace", default="", help="Workspace path to expose to the hidden AI worker.")
    return parser


def print_help() -> None:
    print("Pexo: Primary EXecution Operator")
    print("")
    print("Usage:")
    print("  pexo                 Starts the Pexo API and Control Panel")
    print("  pexo list-presets    Lists available profile presets for terminal-first setup")
    print("  pexo headless-setup  Initializes the local profile without opening the web UI")
    print("  pexo --chat          Starts a direct terminal chat with Pexo")
    print("  pexo promote [full]  Installs or upgrades the local runtime dependency profile")
    print("  pexo update          Updates the current Pexo installation")
    print("  pexo warmup          Primes local state after install or update")
    print("  pexo doctor          Prints local installation and runtime diagnostics")
    print("  pexo connect all     Connects Codex, Claude, and Gemini to pexo-mcp when installed")
    print("  pexo uninstall       Removes the current packaged Pexo install")
    print("  pexo --mcp           Starts Pexo as a native MCP server (stdio)")
    print("  pexo-mcp             Starts Pexo as a native MCP server (stdio)")
    print("  pexo --version       Displays the current version")
    print("  pexo --help          Displays this help menu")


def print_uninstall_guidance() -> int:
    if running_from_repo_checkout():
        print("Checkout install detected. Use `pexo uninstall` from the launcher script or run the local uninstall script.")
        return 0

    print(
        "Package install detected. Run `pexo uninstall` to remove the packaged install and local state, "
        "or `pexo uninstall --keep-state` to keep the local Pexo state directory."
    )
    return 0


def run_uninstall(*, confirm: bool = False, keep_state: bool = False) -> int:
    if running_from_repo_checkout():
        return print_uninstall_guidance()

    if not confirm:
        if not _can_prompt_for_restart():
            print("Uninstall requires confirmation. Re-run with `pexo uninstall --yes`.", file=sys.stderr)
            return 1
        scope = f"remove the packaged install and local state at {STATE_ROOT}"
        if keep_state:
            scope = f"remove the packaged install and keep local state at {STATE_ROOT}"
        try:
            answer = input(f"Uninstall Pexo and {scope}? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 1
        if answer not in {"y", "yes"}:
            print("Uninstall cancelled.")
            return 0

    server_state = _maybe_stop_existing_server_for_maintenance(
        "127.0.0.1",
        9999,
        action_label="the uninstall",
        cancel_label="Uninstall",
    )
    if server_state == "declined":
        return 0
    if server_state == "failed":
        print("Unable to stop the running Pexo server. Close it and run `pexo uninstall` again.", file=sys.stderr)
        return 1
    if server_state == "unavailable":
        print(
            "A Pexo server is already running and must be stopped before this packaged uninstall can continue.",
            file=sys.stderr,
        )
        return 1

    helper_path = _prepare_packaged_uninstall_helper(keep_state=keep_state)
    _launch_packaged_uninstall_helper(helper_path)
    if keep_state:
        print(f"Started packaged uninstall. Local state under {STATE_ROOT} will be kept.")
    else:
        print(f"Started packaged uninstall. Local state under {STATE_ROOT} will also be removed.")
    return 0


def dispatch_cli_subcommand(argv: list[str]) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    from .cli import headless_setup, list_presets

    if args.command == "list-presets":
        return list_presets(as_json=args.json)
    if args.command == "headless-setup":
        return headless_setup(
            preset=args.preset,
            name=args.name,
            backup_path=args.backup_path,
            clear_backup_path=args.clear_backup_path,
            as_json=args.json,
        )
    parser.error(f"Unknown command: {args.command}")
    return 2


def _sqlite_diagnostics() -> dict:
    if not PEXO_DB_PATH.exists():
        return {
            "db_exists": False,
            "connectable": False,
            "table_count": 0,
            "profile_configured": False,
        }

    table_names: list[str] = []
    profile_configured = False
    try:
        with sqlite3.connect(PEXO_DB_PATH) as connection:
            table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            table_names = sorted(row[0] for row in table_rows)
            if "profiles" in table_names:
                profile_row = connection.execute(
                    "SELECT 1 FROM profiles WHERE name = ? LIMIT 1",
                    ("default_user",),
                ).fetchone()
                profile_configured = profile_row is not None
    except sqlite3.Error as exc:
        return {
            "db_exists": True,
            "connectable": False,
            "error": str(exc),
            "table_count": 0,
            "profile_configured": False,
        }

    return {
        "db_exists": True,
        "connectable": True,
        "table_count": len(table_names),
        "tables": table_names,
        "profile_configured": profile_configured,
    }


def build_doctor_report() -> dict:
    install_mode = "checkout" if running_from_repo_checkout() else "packaged"
    runtime_status = build_runtime_status()
    sqlite_report = _sqlite_diagnostics()
    editable_source_root = resolve_editable_source_root()
    editable_residue = _editable_install_artifacts_present()
    install_metadata = _read_install_metadata()
    report = {
        "version": __version__,
        "install_mode": install_mode,
        "code_root": str(CODE_ROOT),
        "state_root": str(STATE_ROOT),
        "install_source": {
            "editable": editable_source_root is not None,
            "editable_root": str(editable_source_root) if editable_source_root is not None else None,
            "editable_residue": editable_residue,
        },
        "paths": {
            "database": str(PEXO_DB_PATH),
            "vector_store": str(CHROMA_DB_DIR),
            "artifacts": str(ARTIFACTS_DIR),
            "dynamic_tools": str(DYNAMIC_TOOLS_DIR),
            "runtime_marker": str(RUNTIME_MARKER_PATH),
            "update_stamp": str(UPDATE_STAMP_PATH),
            "install_metadata": str(INSTALL_METADATA_PATH),
        },
        "path_health": {
            "state_root_exists": STATE_ROOT.exists(),
            "database_exists": PEXO_DB_PATH.exists(),
            "vector_store_exists": CHROMA_DB_DIR.exists(),
            "artifacts_exists": ARTIFACTS_DIR.exists(),
            "dynamic_tools_exists": DYNAMIC_TOOLS_DIR.exists(),
            "runtime_marker_exists": RUNTIME_MARKER_PATH.exists(),
        },
        "commands": {
            "pexo": shutil_which("pexo"),
            "pexo_mcp": shutil_which("pexo-mcp"),
            "python": sys.executable,
            "git": shutil_which("git"),
            "gh": shutil_which("gh"),
            "uv": shutil_which("uv"),
            "pipx": shutil_which("pipx"),
            "codex": shutil_which("codex"),
            "claude": shutil_which("claude"),
            "gemini": shutil_which("gemini"),
        },
        "runtime": runtime_status,
        "database": sqlite_report,
        "install_metadata": install_metadata,
        "guidance": {
            "update": (
                "Checkout a branch, then run git pull --ff-only"
                if install_mode == "checkout" and _checkout_is_detached()
                else ("git pull --ff-only" if install_mode == "checkout" else _package_update_guidance())
            ),
            "uninstall": (
                "pexo uninstall"
                if install_mode == "checkout" else _package_uninstall_guidance()
            ),
            "mcp": (
                str(CODE_ROOT / "pexo") + " --mcp"
                if install_mode == "checkout"
                else str((install_metadata or {}).get("mcp_command") or "pexo-mcp")
            ),
            "connect": "pexo connect all --scope user",
            "full_runtime": "pexo promote full",
        },
        "issues": [],
    }

    issues = report["issues"]
    if not STATE_ROOT.exists():
        issues.append("State root does not exist yet.")
    if not sqlite_report["db_exists"]:
        issues.append("SQLite state database has not been created yet.")
    elif not sqlite_report["connectable"]:
        issues.append("SQLite state database exists but could not be opened.")
    elif not sqlite_report["profile_configured"]:
        issues.append("Default profile has not been initialized yet.")
    if install_mode == "packaged" and not report["commands"]["pexo"]:
        issues.append("The packaged pexo command is not visible in PATH for this shell.")
    if install_mode == "checkout" and not report["commands"]["git"]:
        issues.append("Git is not available; checkout update commands will fail.")
    if install_mode == "checkout" and _checkout_is_detached():
        issues.append("Checkout is on detached git HEAD; automatic update checks are skipped.")
    if install_mode == "packaged" and editable_source_root is not None:
        issues.append(
            "Packaged runtime is still importing Pexo from an editable checkout; run `pexo --update` once to normalize the install."
        )
    if install_mode == "packaged" and editable_residue:
        issues.append(
            "Packaged runtime still has editable-install residue in site-packages; run `pexo --update` once to normalize the install."
        )
    if install_mode == "packaged" and install_metadata:
        metadata_version = str(install_metadata.get("version") or "").strip()
        metadata_release_version = _extract_release_version_from_url(install_metadata.get("release"))
        if metadata_version and metadata_version != report["version"]:
            issues.append(
                f"Install metadata version ({metadata_version}) does not match the running Pexo version ({report['version']}). Reinstall or run `pexo --update` to normalize the public release metadata."
            )
        if metadata_version and metadata_release_version and metadata_release_version != metadata_version:
            issues.append(
                f"Install metadata release tag ({metadata_release_version}) does not match the installed packaged version ({metadata_version}). Reinstall from the current public release bundle to normalize the install metadata."
            )
        command_path = str(install_metadata.get("command_path") or "").strip()
        if command_path and Path(command_path).suffix:
            command_target = Path(command_path).expanduser().resolve(strict=False)
            if not command_target.exists():
                issues.append(
                    f"Install metadata command path is missing: {command_target}. Reinstall from the current public release bundle."
                )
        mcp_command = str(install_metadata.get("mcp_command") or "").strip()
        if mcp_command and Path(mcp_command).suffix:
            mcp_target = Path(mcp_command).expanduser().resolve(strict=False)
            if not mcp_target.exists():
                issues.append(
                    f"Install metadata MCP command path is missing: {mcp_target}. Reinstall from the current public release bundle."
                )

    return report


def run_doctor(as_json: bool = False) -> int:
    report = build_doctor_report()
    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    print("Pexo Doctor")
    print(f"Version: {report['version']}")
    print(f"Install mode: {report['install_mode']}")
    print(f"Code root: {report['code_root']}")
    print(f"State root: {report['state_root']}")
    print(f"Runtime profile: {report['runtime']['active_profile']}")
    print(f"Memory backend: {report['runtime'].get('memory_backend', 'unknown')}")
    print(f"Profile configured: {'yes' if report['database']['profile_configured'] else 'no'}")
    print(f"Database: {report['paths']['database']} ({'present' if report['database']['db_exists'] else 'missing'})")
    print(f"Vector store: {report['paths']['vector_store']} ({'present' if report['path_health']['vector_store_exists'] else 'missing'})")
    print(f"Artifacts: {report['paths']['artifacts']} ({'present' if report['path_health']['artifacts_exists'] else 'missing'})")
    print(f"Dynamic tools: {report['paths']['dynamic_tools']} ({'present' if report['path_health']['dynamic_tools_exists'] else 'missing'})")
    if report["install_metadata"]:
        print(f"Install method: {report['install_metadata'].get('method', 'unknown')}")
    print(f"Commands: pexo={report['commands']['pexo'] or 'not found'} | pexo-mcp={report['commands']['pexo_mcp'] or 'not found'}")
    print(
        "Client CLIs: "
        f"codex={report['commands']['codex'] or 'not found'} | "
        f"claude={report['commands']['claude'] or 'not found'} | "
        f"gemini={report['commands']['gemini'] or 'not found'}"
    )
    print(f"Update command: {report['guidance']['update']}")
    print(f"Uninstall command: {report['guidance']['uninstall']}")
    print(f"MCP command: {report['guidance']['mcp']}")
    print(f"Fleet connect command: {report['guidance']['connect']}")
    print("Semantic memory: optional advanced add-on")
    print(f"Full runtime command: {report['guidance']['full_runtime']}")
    if report["issues"]:
        print("Issues:")
        for issue in report["issues"]:
            print(f"- {issue}")
    else:
        print("Issues: none")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_args = list(argv if argv is not None else sys.argv[1:])

    if not raw_args:
        maybe_update()
        return run_server(no_browser=False)
    if raw_args == ["--help"] or raw_args == ["help"]:
        print_help()
        return 0
    if raw_args == ["--version"] or raw_args == ["version"]:
        print(f"Pexo v{__version__}")
        return 0
    if raw_args and raw_args[0] == "--list-presets":
        return dispatch_cli_subcommand(["list-presets", *raw_args[1:]])
    if raw_args and raw_args[0] == "--headless-setup":
        return dispatch_cli_subcommand(["headless-setup", *raw_args[1:]])
    if raw_args and raw_args[0] == "--uninstall":
        return run_uninstall()
    if raw_args and raw_args[0] == "--doctor":
        return run_doctor(as_json="--json" in raw_args[1:])
    if raw_args and raw_args[0] == "--connect":
        raw_args = ["connect", *raw_args[1:]]
    if raw_args and raw_args[0] == "--chat":
        raw_args = ["chat", *raw_args[1:]]

    parser = build_parser()
    args, extras = parser.parse_known_args(raw_args)
    skip_update = args.offline or args.skip_update

    if args.version:
        print(f"Pexo v{__version__}")
        return 0
    if args.chat:
        maybe_update(skip_update=skip_update)
        return run_chat_mode()
    if args.mcp or args.command == "mcp":
        return run_mcp()
    if args.update or args.command == "update":
        return run_update()
    if args.promote:
        return run_promote(args.promote)
    if args.command == "promote":
        return run_promote(args.profile)
    if args.command == "list-presets":
        return dispatch_cli_subcommand(["list-presets", *extras])
    if args.command == "headless-setup":
        cli_args = ["headless-setup"]
        if args.preset:
            cli_args.extend(["--preset", args.preset])
        if args.name:
            cli_args.extend(["--name", args.name])
        if args.backup_path:
            cli_args.extend(["--backup-path", args.backup_path])
        if args.clear_backup_path:
            cli_args.append("--clear-backup-path")
        if args.json:
            cli_args.append("--json")
        cli_args.extend(extras)
        return dispatch_cli_subcommand(cli_args)
    if args.command == "doctor":
        return run_doctor(as_json=args.json)
    if args.command == "connect":
        return run_connect(target=args.client, scope=args.scope, dry_run=args.dry_run, as_json=args.json)
    if args.command == "warmup":
        return run_warmup(quiet=args.quiet)
    if args.command == "uninstall":
        return run_uninstall(confirm=args.yes, keep_state=args.keep_state)
    if args.command == "chat":
        maybe_update(skip_update=skip_update)
        return run_chat_mode(
            backend=getattr(args, "backend", "auto"),
            workspace_path=getattr(args, "workspace", "") or None,
        )

    maybe_update(skip_update=skip_update)
    return run_server(no_browser=args.no_browser)


def mcp_main() -> int:
    return run_mcp()


if __name__ == "__main__":
    raise SystemExit(main())
