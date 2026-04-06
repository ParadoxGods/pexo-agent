from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = "Design a modern landing page for my product with a clean premium look."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a real cross-client MCP acceptance test against Pexo.")
    parser.add_argument("--json", action="store_true", help="Emit the final report as JSON.")
    parser.add_argument("--keep-state", action="store_true", help="Keep the temporary isolated PEXO_HOME after the run.")
    parser.add_argument("--server-command", default=sys.executable, help="Command used to start the MCP server.")
    parser.add_argument(
        "--server-args",
        nargs="*",
        default=["-m", "app.launcher", "--mcp"],
        help="Arguments passed to the MCP server command.",
    )
    parser.add_argument(
        "--workspace",
        default=str(REPO_ROOT),
        help="Workspace path exposed in the acceptance artifact payload.",
    )
    return parser


@asynccontextmanager
async def open_client(*, command: str, args: list[str], state_root: Path):
    env = {
        "PEXO_HOME": str(state_root),
        "PYTHONIOENCODING": "utf-8",
    }
    params = StdioServerParameters(command=command, args=args, env=env)
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


def _extract_text_parts(result: Any) -> list[str]:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text.strip())
    return [part for part in parts if part]


def _extract_text_payload(result: Any) -> str:
    return "\n".join(_extract_text_parts(result)).strip()


def decode_tool_result(result: Any) -> Any:
    parts = _extract_text_parts(result)
    if not parts:
        return {}
    if len(parts) > 1:
        decoded_parts: list[Any] = []
        for part in parts:
            try:
                decoded_parts.append(json.loads(part))
            except json.JSONDecodeError:
                decoded_parts.append({"raw_text": part})
        return decoded_parts
    text = parts[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text}


async def call_tool_json(session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> Any:
    result = await session.call_tool(name, arguments or {})
    if getattr(result, "isError", False):
        raise RuntimeError(f"{name} failed: {_extract_text_payload(result)}")
    return decode_tool_result(result)


async def run_acceptance(*, command: str, args: list[str], workspace: str) -> dict[str, Any]:
    temp_root = Path(tempfile.mkdtemp(prefix="pexo-mcp-acceptance-"))
    state_root = temp_root / "state"
    state_root.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time())
    memory_token = f"PEXO_ACCEPTANCE_MEMORY_{timestamp}"
    artifact_token = f"PEXO_ACCEPTANCE_ARTIFACT_{timestamp}"
    session_id = f"acceptance-task-{timestamp}"
    task_context = "mcp-acceptance"
    artifact_name = f"{session_id}.txt"

    report: dict[str, Any] = {
        "status": "success",
        "state_root": str(state_root),
        "memory_token": memory_token,
        "artifact_token": artifact_token,
        "task_session_id": session_id,
        "steps": [],
    }

    try:
        async with open_client(command=command, args=args, state_root=state_root) as client_a:
            tools = await client_a.list_tools()
            tool_names = sorted(tool.name for tool in tools.tools)
            required_tools = {
                "pexo_store_memory",
                "pexo_find_memory",
                "pexo_attach_text_context",
                "pexo_find_artifact",
                "pexo_start_task",
                "pexo_continue_task",
                "pexo_get_session_activity",
                "pexo_get_telemetry",
            }
            missing_tools = sorted(required_tools.difference(tool_names))
            if missing_tools:
                raise RuntimeError(f"MCP server is missing required tools: {', '.join(missing_tools)}")
            report["steps"].append({"name": "client_a_list_tools", "ok": True, "tool_count": len(tool_names)})

            remembered = await call_tool_json(
                client_a,
                "pexo_store_memory",
                {
                    "content": memory_token,
                    "task_context": task_context,
                    "session_id": session_id,
                },
            )
            report["steps"].append(
                {
                    "name": "client_a_store_memory",
                    "ok": remembered.get("memory_id") is not None,
                    "memory_id": remembered.get("memory_id"),
                }
            )

            attached = await call_tool_json(
                client_a,
                "pexo_attach_text_context",
                {
                    "name": artifact_name,
                    "content": f"{artifact_token}\nworkspace={workspace}",
                    "task_context": task_context,
                    "session_id": session_id,
                },
            )
            report["steps"].append(
                {
                    "name": "client_a_attach_artifact",
                    "ok": (attached.get("artifact") or {}).get("name") == artifact_name,
                    "artifact_id": (attached.get("artifact") or {}).get("id"),
                }
            )

            started = await call_tool_json(
                client_a,
                "pexo_start_task",
                {
                    "prompt": DEFAULT_PROMPT,
                    "user_id": "acceptance-user",
                    "session_id": session_id,
                },
            )
            if started.get("status") != "agent_action_required" or started.get("role") != "Supervisor":
                raise RuntimeError(f"Unexpected task start payload: {started}")
            report["steps"].append(
                {
                    "name": "client_a_start_task",
                    "ok": True,
                    "status": started.get("status"),
                    "role": started.get("role"),
                }
            )

        async with open_client(command=command, args=args, state_root=state_root) as client_b:
            memory_lookup = await call_tool_json(
                client_b,
                "pexo_find_memory",
                {"query": memory_token, "limit": 3},
            )
            best_memory = memory_lookup.get("best_match") or {}
            if best_memory.get("content") != memory_token:
                raise RuntimeError(f"Cross-client memory recall failed: {memory_lookup}")
            report["steps"].append(
                {
                    "name": "client_b_find_memory",
                    "ok": True,
                    "memory_id": best_memory.get("id"),
                }
            )

            artifact_lookup = await call_tool_json(
                client_b,
                "pexo_find_artifact",
                {
                    "query": artifact_token,
                    "limit": 3,
                    "session_id": session_id,
                    "task_context": task_context,
                },
            )
            best_artifact = artifact_lookup.get("best_match") or {}
            if best_artifact.get("name") != artifact_name:
                raise RuntimeError(f"Cross-client artifact recall failed: {artifact_lookup}")
            report["steps"].append(
                {
                    "name": "client_b_find_artifact",
                    "ok": True,
                    "artifact_id": best_artifact.get("id"),
                }
            )

            after_supervisor = await call_tool_json(
                client_b,
                "pexo_continue_task",
                {
                    "session_id": session_id,
                    "result_data": [{"id": "task-1", "description": "Build the page", "assigned_agent": "Developer"}],
                },
            )
            if after_supervisor.get("status") != "agent_action_required" or after_supervisor.get("role") != "Developer":
                raise RuntimeError(f"Supervisor handoff failed: {after_supervisor}")
            report["steps"].append(
                {
                    "name": "client_b_continue_supervisor",
                    "ok": True,
                    "role": after_supervisor.get("role"),
                }
            )

        async with open_client(command=command, args=args, state_root=state_root) as client_a_again:
            after_developer = await call_tool_json(
                client_a_again,
                "pexo_continue_task",
                {
                    "session_id": session_id,
                    "result_data": "Built the landing page structure.",
                },
            )
            if after_developer.get("status") != "agent_action_required" or after_developer.get("role") != "Quality Assurance Manager":
                raise RuntimeError(f"Developer handoff failed: {after_developer}")
            report["steps"].append(
                {
                    "name": "client_a_continue_developer",
                    "ok": True,
                    "role": after_developer.get("role"),
                }
            )

        async with open_client(command=command, args=args, state_root=state_root) as client_b_again:
            after_qa = await call_tool_json(
                client_b_again,
                "pexo_continue_task",
                {
                    "session_id": session_id,
                    "result_data": "PASS",
                },
            )
            if after_qa.get("status") != "agent_action_required" or after_qa.get("role") != "Code Organization Manager":
                raise RuntimeError(f"QA handoff failed: {after_qa}")
            report["steps"].append(
                {
                    "name": "client_b_continue_qa",
                    "ok": True,
                    "role": after_qa.get("role"),
                }
            )

            completed = await call_tool_json(
                client_b_again,
                "pexo_continue_task",
                {
                    "session_id": session_id,
                    "result_data": "The landing page is complete and ready for review.",
                },
            )
            final_response = str(completed.get("final_response") or completed.get("user_message") or "")
            if completed.get("status") != "complete" or "landing page is complete" not in final_response.lower():
                raise RuntimeError(f"Final session completion failed: {completed}")
            report["steps"].append(
                {
                    "name": "client_b_complete_task",
                    "ok": True,
                    "status": completed.get("status"),
                }
            )

            activity = await call_tool_json(
                client_b_again,
                "pexo_get_session_activity",
                {
                    "session_id": session_id,
                    "limit": 20,
                },
            )
            activity_rows = activity if isinstance(activity, list) else []
            if not activity_rows:
                raise RuntimeError("Session activity was empty after cross-client task flow.")
            report["steps"].append(
                {
                    "name": "client_b_verify_activity",
                    "ok": True,
                    "activity_rows": len(activity_rows),
                }
            )

            telemetry = await call_tool_json(client_b_again, "pexo_get_telemetry", {})
            recent_sessions = telemetry.get("recent_sessions") or []
            session_hit = any(str(item.get("id") or item.get("session_id") or "") == session_id for item in recent_sessions)
            report["steps"].append(
                {
                    "name": "client_b_verify_telemetry",
                    "ok": session_hit,
                    "recent_sessions": len(recent_sessions),
                }
            )
            if not session_hit:
                raise RuntimeError("Cross-client task session was not visible in telemetry.")

        report["summary"] = {
            "checks_passed": len(report["steps"]),
            "checks_total": len(report["steps"]),
            "session_id": session_id,
        }
        return report
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["error"] = str(exc)
        return report


async def async_main(args: argparse.Namespace) -> int:
    report = await run_acceptance(
        command=args.server_command,
        args=list(args.server_args),
        workspace=args.workspace,
    )

    if report.get("status") == "success" and not args.keep_state:
        shutil.rmtree(Path(report["state_root"]).parent, ignore_errors=True)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Pexo MCP Cross-Client Acceptance")
        print(f"Status: {report['status']}")
        print(f"State root: {report['state_root']}")
        if report.get("summary"):
            print(f"Checks: {report['summary']['checks_passed']}/{report['summary']['checks_total']}")
            print(f"Session: {report['summary']['session_id']}")
        for step in report.get("steps", []):
            label = "PASS" if step.get("ok") else "FAIL"
            print(f"{label} {step['name']}")
        if report.get("error"):
            print(f"Error: {report['error']}")

    return 0 if report.get("status") == "success" else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return anyio.run(async_main, args)


if __name__ == "__main__":
    raise SystemExit(main())
