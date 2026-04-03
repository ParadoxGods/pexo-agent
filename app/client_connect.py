from __future__ import annotations

import os
import shlex
import subprocess
import json
from shutil import which

from .paths import CODE_ROOT, INSTALL_METADATA_PATH, running_from_repo_checkout

SUPPORTED_CLIENTS = ("codex", "claude", "gemini")
SUPPORTED_SCOPES = ("user", "project")


def _format_command(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _read_install_metadata() -> dict | None:
    if not INSTALL_METADATA_PATH.exists():
        return None
    try:
        return json.loads(INSTALL_METADATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def build_mcp_stdio_target() -> dict:
    if running_from_repo_checkout():
        if os.name == "nt":
            command = "cmd.exe"
            args = ["/c", str(CODE_ROOT / "pexo.bat"), "--mcp"]
        else:
            command = "bash"
            args = [str(CODE_ROOT / "pexo"), "--mcp"]
    else:
        metadata = _read_install_metadata()
        command = str(metadata.get("mcp_command", "")).strip() if metadata else ""
        if not command:
            command = "pexo-mcp"
        args = []

    return {
        "command": command,
        "args": args,
        "display": _format_command([command, *args]),
    }


def build_client_connection_plan(client: str, scope: str = "user") -> dict:
    normalized_client = client.lower()
    normalized_scope = scope.lower()

    if normalized_client not in SUPPORTED_CLIENTS:
        raise ValueError(f"Unsupported client '{client}'.")
    if normalized_scope not in SUPPORTED_SCOPES:
        raise ValueError(f"Unsupported scope '{scope}'.")

    target = build_mcp_stdio_target()
    binary = which(normalized_client)

    if normalized_client == "codex":
        add_command = ["codex", "mcp", "add", "pexo", "--", target["command"], *target["args"]]
        remove_command = ["codex", "mcp", "remove", "pexo"]
        verify_command = ["codex", "mcp", "get", "pexo"]
    elif normalized_client == "claude":
        add_command = ["claude", "mcp", "add", "pexo", "--scope", normalized_scope, "--", target["command"], *target["args"]]
        remove_command = ["claude", "mcp", "remove", "pexo"]
        verify_command = ["claude", "mcp", "get", "pexo"]
    else:
        add_command = [
            "gemini",
            "mcp",
            "add",
            "--scope",
            normalized_scope,
            "--transport",
            "stdio",
            "pexo",
            target["command"],
            *target["args"],
        ]
        remove_command = ["gemini", "mcp", "remove", "pexo"]
        verify_command = ["gemini", "mcp", "list"]

    return {
        "client": normalized_client,
        "scope": normalized_scope,
        "available": binary is not None,
        "binary": binary,
        "target": target,
        "remove_command": remove_command,
        "add_command": add_command,
        "verify_command": verify_command,
        "manual_command": _format_command(add_command),
    }


def connect_clients(target: str = "all", scope: str = "user", dry_run: bool = False) -> dict:
    normalized_target = target.lower()
    if normalized_target == "all":
        clients = list(SUPPORTED_CLIENTS)
    elif normalized_target in SUPPORTED_CLIENTS:
        clients = [normalized_target]
    else:
        raise ValueError(f"Unsupported client target '{target}'.")

    results = []
    failed_clients: list[str] = []

    for client in clients:
        plan = build_client_connection_plan(client, scope=scope)
        result = {
            "client": client,
            "available": plan["available"],
            "scope": plan["scope"],
            "target_command": plan["target"]["display"],
            "manual_command": plan["manual_command"],
        }

        if not plan["available"]:
            result["status"] = "missing"
            result["message"] = f"{client} is not installed or not visible in PATH."
            results.append(result)
            continue

        if dry_run:
            result["status"] = "planned"
            result["message"] = "Dry run only. No client configuration was changed."
            results.append(result)
            continue

        subprocess.run(plan["remove_command"], capture_output=True, text=True, check=False)
        add_completed = subprocess.run(plan["add_command"], capture_output=True, text=True, check=False)

        result["stdout"] = (add_completed.stdout or "").strip()
        result["stderr"] = (add_completed.stderr or "").strip()
        result["returncode"] = add_completed.returncode

        if add_completed.returncode != 0:
            result["status"] = "failed"
            result["message"] = result["stderr"] or result["stdout"] or "Client configuration failed."
            failed_clients.append(client)
            results.append(result)
            continue

        verify_completed = subprocess.run(plan["verify_command"], capture_output=True, text=True, check=False)
        result["status"] = "connected"
        result["message"] = "Client MCP configuration updated."
        result["verify_output"] = ((verify_completed.stdout or "") + (verify_completed.stderr or "")).strip()
        results.append(result)

    status = "success"
    if failed_clients:
        status = "failed"
    elif any(result["status"] == "missing" for result in results):
        status = "partial"

    return {
        "status": status,
        "target": normalized_target,
        "scope": scope.lower(),
        "mcp_server": build_mcp_stdio_target(),
        "results": results,
        "failed_clients": failed_clients,
    }
