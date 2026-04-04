from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .client_connect import SUPPORTED_CLIENTS, SUPPORTED_SCOPES, connect_clients
from .cli import build_parser as build_cli_parser
from .paths import (
    ARTIFACTS_DIR,
    CHROMA_DB_DIR,
    CODE_ROOT,
    DYNAMIC_TOOLS_DIR,
    INSTALL_METADATA_PATH,
    PEXO_DB_PATH,
    PROJECT_ROOT,
    RUNTIME_MARKER_PATH,
    UPDATE_STAMP_PATH,
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

import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


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


def main() -> int:
    plan_path = Path(sys.argv[1]).resolve()
    temp_root = plan_path.parent
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        wheel_path = temp_root / plan["wheel_name"]
        checksum_path = temp_root / "SHA256SUMS.txt"

        print(f"Updating Pexo to {plan['version']}...")
        print("Downloading release assets...")
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

        target_python = plan["target_python"]
        subprocess.run(
            [target_python, "-m", "ensurepip", "--upgrade"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        install_label = plan.get("install_label") or "Installing update..."
        print(install_label)
        completed = subprocess.run(
            [target_python, "-m", "pip", *plan["pip_args"], str(wheel_path)],
            check=False,
        )

        if completed.returncode == 0:
            _write_metadata(
                Path(plan["install_metadata_path"]),
                version=plan["version"],
                release_url=plan["release_url"],
                wheel_sha256=plan.get("wheel_sha256", ""),
                dependency_fingerprint=plan.get("dependency_fingerprint", ""),
            )
            Path(plan["update_stamp_path"]).write_text(str(int(time.time())), encoding="utf-8")
            print(f"Pexo updated to {plan['version']}.")

        return int(completed.returncode)
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


def _package_uninstall_guidance() -> str:
    metadata = _read_install_metadata()
    if metadata:
        guidance = metadata.get("guidance", {}).get("uninstall")
        if guidance:
            return str(guidance)
    if shutil_which("uv"):
        return "uv tool uninstall pexo-agent"
    if shutil_which("pipx"):
        return "pipx uninstall pexo-agent"
    return "Uninstall the packaged tool with your Python tool manager"


def _print_start_banner() -> None:
    banner = PEXO_ASCII_BANNER.strip("\n")
    print(banner)
    print("")
    print("PEXO | Primary EXecution Operator | local-first control plane")


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

    operation = "full"
    install_label = "Installing update..."
    pip_args = ["install", "--disable-pip-version-check", "--force-reinstall"]
    if current_wheel_sha and target_wheel_sha and current_wheel_sha == target_wheel_sha:
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
        "target_python": sys.executable,
        "install_metadata_path": str(INSTALL_METADATA_PATH),
        "update_stamp_path": str(UPDATE_STAMP_PATH),
        "operation": operation,
        "install_label": install_label,
        "pip_args": pip_args,
        "wheel_sha256": target_wheel_sha,
        "dependency_fingerprint": target_dependency_fingerprint,
    }


def _prepare_packaged_update_helper(plan: dict) -> tuple[Path, Path]:
    temp_root = Path(tempfile.mkdtemp(prefix="pexo-update-"))
    helper_path = temp_root / "pexo_update_helper.py"
    plan_path = temp_root / "update-plan.json"
    helper_path.write_text(PACKAGED_UPDATE_HELPER, encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return helper_path, plan_path


def _exec_update_helper(helper_path: Path, plan_path: Path) -> int:
    os.execv(sys.executable, [sys.executable, str(helper_path), str(plan_path)])
    return 0


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
        return completed.returncode

    try:
        plan = _build_packaged_update_plan()
    except Exception as exc:
        print(f"Unable to prepare a packaged update: {exc}", file=sys.stderr)
        return 1

    if plan["operation"] == "skip":
        print(f"Installed package detected. Pexo v{plan['version']} is already current.")
        _write_update_stamp()
        return 0

    print(f"Installed package detected. Preparing update to v{plan['version']}...")
    helper_path, plan_path = _prepare_packaged_update_helper(plan)
    return _exec_update_helper(helper_path, plan_path)


def shutil_which(command_name: str) -> str | None:
    from shutil import which

    return which(command_name)


def run_server(no_browser: bool = False) -> int:
    status = build_runtime_status()
    if not status["installed_profiles"].get("full", False):
        promotion_result = promote_runtime("full")
        if promotion_result["status"] != "success":
            print(
                promotion_result.get("stderr") or promotion_result.get("stdout") or "Failed to prepare the full runtime.",
                file=sys.stderr,
            )
            return 1
        print("Full runtime installed. Restarting Pexo to activate the new environment...")
        return _restart_launcher_process()

    import uvicorn

    if no_browser:
        os.environ["PEXO_NO_BROWSER"] = "1"
    _print_start_banner()
    uvicorn.run("app.main:app", host="127.0.0.1", port=9999, workers=1)
    return 0


def run_mcp() -> int:
    status = build_runtime_status()
    if not status["installed_profiles"].get("mcp", False):
        promotion_result = promote_runtime("mcp")
        if promotion_result["status"] != "success":
            print(
                promotion_result.get("stderr") or promotion_result.get("stdout") or "Failed to prepare the MCP runtime.",
                file=sys.stderr,
            )
            return 1
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pexo", description="Pexo command launcher.")
    parser.add_argument("--version", action="store_true", help="Display the current Pexo version.")
    parser.add_argument("--no-browser", action="store_true", help="Start the API without opening the dashboard.")
    parser.add_argument("--offline", action="store_true", help="Skip automatic repository update checks.")
    parser.add_argument("--skip-update", action="store_true", help="Skip automatic repository update checks.")
    parser.add_argument("--mcp", action="store_true", help="Start Pexo in native MCP stdio mode.")
    parser.add_argument("--update", action="store_true", help="Update the current Pexo installation.")
    parser.add_argument(
        "--promote",
        nargs="?",
        const="full",
        metavar="PROFILE",
        help="Install or upgrade the runtime dependency profile (core, mcp, full, vector).",
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
    subparsers.add_parser("uninstall", help="Show uninstall guidance for the current delivery mode.")
    return parser


def print_help() -> None:
    print("Pexo: Primary EXecution Operator")
    print("")
    print("Usage:")
    print("  pexo                 Starts the Pexo API and Control Panel")
    print("  pexo list-presets    Lists available profile presets for terminal-first setup")
    print("  pexo headless-setup  Initializes the local profile without opening the web UI")
    print("  pexo promote [full]  Installs or upgrades the local runtime dependency profile")
    print("  pexo update          Updates the current Pexo installation")
    print("  pexo doctor          Prints local installation and runtime diagnostics")
    print("  pexo connect all     Connects Codex, Claude, and Gemini to pexo-mcp when installed")
    print("  pexo --mcp           Starts Pexo as a native MCP server (stdio)")
    print("  pexo-mcp             Starts Pexo as a native MCP server (stdio)")
    print("  pexo --version       Displays the current version")
    print("  pexo --help          Displays this help menu")


def print_uninstall_guidance() -> int:
    if running_from_repo_checkout():
        print("Checkout install detected. Use `pexo uninstall` from the launcher script or run the local uninstall script.")
        return 0

    print(f"Package install detected. Run `{_package_uninstall_guidance()}` and delete {PROJECT_ROOT} if you also want to remove local state.")
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
    report = {
        "version": __version__,
        "install_mode": install_mode,
        "code_root": str(CODE_ROOT),
        "state_root": str(PROJECT_ROOT),
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
            "state_root_exists": PROJECT_ROOT.exists(),
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
        "install_metadata": _read_install_metadata(),
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
                else str((_read_install_metadata() or {}).get("mcp_command") or "pexo-mcp")
            ),
            "connect": "pexo connect all --scope user",
            "vector": "pexo promote vector",
        },
        "issues": [],
    }

    issues = report["issues"]
    if not PROJECT_ROOT.exists():
        issues.append("State root does not exist yet.")
    if not sqlite_report["db_exists"]:
        issues.append("SQLite state database has not been created yet.")
    elif not sqlite_report["connectable"]:
        issues.append("SQLite state database exists but could not be opened.")
    elif not sqlite_report["profile_configured"]:
        issues.append("Default profile has not been initialized yet.")
    if not runtime_status["installed_profiles"].get("vector", False):
        issues.append("Vector runtime is not installed; SQLite keyword fallback is active.")
    if install_mode == "packaged" and not report["commands"]["pexo"]:
        issues.append("The packaged pexo command is not visible in PATH for this shell.")
    if install_mode == "checkout" and not report["commands"]["git"]:
        issues.append("Git is not available; checkout update commands will fail.")
    if install_mode == "checkout" and _checkout_is_detached():
        issues.append("Checkout is on detached git HEAD; automatic update checks are skipped.")

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
    print(f"Vector promote command: {report['guidance']['vector']}")
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
        return print_uninstall_guidance()
    if raw_args and raw_args[0] == "--doctor":
        return run_doctor(as_json="--json" in raw_args[1:])
    if raw_args and raw_args[0] == "--connect":
        raw_args = ["connect", *raw_args[1:]]

    parser = build_parser()
    args, extras = parser.parse_known_args(raw_args)
    skip_update = args.offline or args.skip_update

    if args.version:
        print(f"Pexo v{__version__}")
        return 0
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
    if args.command == "uninstall":
        return print_uninstall_guidance()

    maybe_update(skip_update=skip_update)
    return run_server(no_browser=args.no_browser)


def mcp_main() -> int:
    return run_mcp()


if __name__ == "__main__":
    raise SystemExit(main())
