from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from .cli import build_parser as build_cli_parser
from .mcp_server import start_mcp_server
from .paths import PROJECT_ROOT, UPDATE_STAMP_PATH, running_from_repo_checkout
from .runtime import promote_runtime
from .version import __version__

UPDATE_INTERVAL_SECONDS = 12 * 60 * 60


def _coerce_repo_source() -> str:
    return "ParadoxGods/pexo-agent"


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

    if shutil_which("uv"):
        print("Installed package detected. Run `uv tool upgrade pexo-agent` to refresh this tool installation.")
        return 0
    if shutil_which("pipx"):
        print("Installed package detected. Run `pipx upgrade pexo-agent` to refresh this tool installation.")
        return 0

    print("Installed package detected. Reinstall from GitHub to update this tool installation.")
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
    print("  pexo --mcp           Starts Pexo as a native MCP server (stdio)")
    print("  pexo-mcp             Starts Pexo as a native MCP server (stdio)")
    print("  pexo --version       Displays the current version")
    print("  pexo --help          Displays this help menu")


def print_uninstall_guidance() -> int:
    if running_from_repo_checkout():
        print("Checkout install detected. Use `pexo uninstall` from the launcher script or run the local uninstall script.")
        return 0

    if shutil_which("uv"):
        print("Package install detected. Run `uv tool uninstall pexo-agent` and delete ~/.pexo if you also want to remove local state.")
        return 0
    if shutil_which("pipx"):
        print("Package install detected. Run `pipx uninstall pexo-agent` and delete ~/.pexo if you also want to remove local state.")
        return 0

    print("Package install detected. Uninstall the tool with your Python tool manager and delete ~/.pexo to remove local state.")
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
    if args.command == "uninstall":
        return print_uninstall_guidance()

    maybe_update(skip_update=skip_update)
    return run_server(no_browser=args.no_browser)


def mcp_main() -> int:
    return run_mcp()


if __name__ == "__main__":
    raise SystemExit(main())
