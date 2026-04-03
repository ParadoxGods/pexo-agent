import argparse
import json

from .database import SessionLocal, init_db
from .routers.profile import PROFILE_PRESETS, build_profile_from_preset, upsert_profile


def list_presets(as_json: bool = False) -> int:
    payload = [
        {
            "id": preset_id,
            "label": preset["label"],
            "description": preset["description"],
        }
        for preset_id, preset in PROFILE_PRESETS.items()
    ]

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print("Available Pexo profile presets:")
    for preset in payload:
        print(f"- {preset['id']}: {preset['label']} | {preset['description']}")
    return 0


def headless_setup(
    preset: str = "efficient_operator",
    name: str = "default_user",
    backup_path: str = "",
    clear_backup_path: bool = False,
    as_json: bool = False,
) -> int:
    init_db()
    db = SessionLocal()
    try:
        answers = build_profile_from_preset(preset, name=name, backup_path=backup_path)
        answers.clear_backup_path = clear_backup_path
        profile = upsert_profile(answers, db)
    finally:
        db.close()

    payload = {
        "status": "success",
        "profile_name": profile.name,
        "preset": preset,
        "backup_path": profile.backup_path,
        "message": "Headless profile setup complete.",
    }

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Headless profile setup complete for '{profile.name}' using preset '{preset}'.")
    if profile.backup_path:
        print(f"Backup path: {profile.backup_path}")
    else:
        print("Backup path: not set")
    print("Run `pexo` later if you want the web UI for inspecting memory, agents, and configuration.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Utility commands for Pexo setup and local administration.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-presets", help="List available headless profile presets.")
    list_parser.add_argument("--json", action="store_true", help="Emit preset data as JSON.")

    setup_parser = subparsers.add_parser("headless-setup", help="Initialize a profile without opening the web UI.")
    setup_parser.add_argument("--preset", default="efficient_operator", choices=sorted(PROFILE_PRESETS.keys()))
    setup_parser.add_argument("--name", default="default_user", help="Profile name to initialize.")
    setup_parser.add_argument("--backup-path", default="", help="Optional backup path for archived local state.")
    setup_parser.add_argument(
        "--clear-backup-path",
        action="store_true",
        help="Clear any previously configured backup path while applying the preset.",
    )
    setup_parser.add_argument("--json", action="store_true", help="Emit setup result as JSON.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main())
