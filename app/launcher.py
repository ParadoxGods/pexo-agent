from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

from .cli import build_parser as build_cli_parser
from .paths import (
    ARTIFACTS_DIR,
    CHROMA_DB_DIR,
    CODE_ROOT,
    DYNAMIC_TOOLS_DIR,
    PEXO_DB_PATH,
    PROJECT_ROOT,
    RUNTIME_MARKER_PATH,
    UPDATE_STAMP_PATH,
    running_from_repo_checkout,
)
from .runtime import build_runtime_status, promote_runtime
from .version import __version__

UPDATE_INTERVAL_SECONDS = 12 * 60 * 60


def _coerce_repo_source() -> str:
    return "ParadoxGods/pexo-agent"


def _package_update_guidance() -> str:
    if shutil_which("uv"):
        return "uv tool upgrade pexo-agent"
    if shutil_which("pipx"):
        return "pipx upgrade pexo-agent"
    return "Reinstall the packaged tool from GitHub"


def _package_uninstall_guidance() -> str:
    if shutil_which("uv"):
        return "uv tool uninstall pexo-agent"
    if shutil_which("pipx"):
        return "pipx uninstall pexo-agent"
    return "Uninstall the packaged tool with your Python tool manager"


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


def maybe_update(skip_update: bool = False) -> None:
    if skip_update or not running_from_repo_checkout():
        return
    if _update_stamp_is_fresh():
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
        "Run 'pexo update' for full git output. If this checkout is private or detached, verify authentication and branch state.",
        file=sys.stderr,
    )


def run_update() -> int:
    if running_from_repo_checkout():
        completed = subprocess.run(["git", "pull", "--ff-only"], cwd=str(PROJECT_ROOT), check=False)
        if completed.returncode == 0:
            _write_update_stamp()
        return completed.returncode

    print(f"Installed package detected. Run `{_package_update_guidance()}` to refresh this tool installation.")
    return 0


def shutil_which(command_name: str) -> str | None:
    from shutil import which

    return which(command_name)


def run_server(no_browser: bool = False) -> int:
    import uvicorn

    if no_browser:
        os.environ["PEXO_NO_BROWSER"] = "1"
    uvicorn.run("app.main:app", host="127.0.0.1", port=9999, workers=1)
    return 0


def run_mcp() -> int:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pexo", description="Pexo command launcher.")
    parser.add_argument("--version", action="store_true", help="Display the current Pexo version.")
    parser.add_argument("--no-browser", action="store_true", help="Start the API without opening the dashboard.")
    parser.add_argument("--offline", action="store_true", help="Skip automatic repository update checks.")
    parser.add_argument("--skip-update", action="store_true", help="Skip automatic repository update checks.")
    parser.add_argument("--mcp", action="store_true", help="Start Pexo in native MCP stdio mode.")
    parser.add_argument("--update", action="store_true", help="Pull the latest repository changes when running from a checkout.")
    parser.add_argument(
        "--promote",
        nargs="?",
        const="full",
        metavar="PROFILE",
        help="Install or upgrade the runtime dependency profile (core, mcp, full, vector).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("mcp", help="Start Pexo in native MCP stdio mode.")
    subparsers.add_parser("update", help="Update the local checkout or print package upgrade guidance.")
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
    print("  pexo update          Pulls the latest repository changes or prints package upgrade guidance")
    print("  pexo doctor          Prints local installation and runtime diagnostics")
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
        },
        "runtime": runtime_status,
        "database": sqlite_report,
        "guidance": {
            "update": "git pull --ff-only" if install_mode == "checkout" else _package_update_guidance(),
            "uninstall": (
                "pexo uninstall"
                if install_mode == "checkout" else _package_uninstall_guidance()
            ),
            "mcp": str(CODE_ROOT / "pexo") + " --mcp" if install_mode == "checkout" else "pexo-mcp",
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
    print(f"Commands: pexo={report['commands']['pexo'] or 'not found'} | pexo-mcp={report['commands']['pexo_mcp'] or 'not found'}")
    print(f"Update command: {report['guidance']['update']}")
    print(f"Uninstall command: {report['guidance']['uninstall']}")
    print(f"MCP command: {report['guidance']['mcp']}")
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
    if args.command == "uninstall":
        return print_uninstall_guidance()

    maybe_update(skip_update=skip_update)
    return run_server(no_browser=args.no_browser)


def mcp_main() -> int:
    return run_mcp()


if __name__ == "__main__":
    raise SystemExit(main())
