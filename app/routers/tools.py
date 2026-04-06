import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..cache import invalidate_surface_caches, invalidate_telemetry_caches
from ..context_metrics import annotate_context_metrics, measure_context_payload
from ..database import get_db
from ..models import AgentState, DynamicTool, SystemSetting
from ..orchestration_context import invalidate_session_context_snapshot
from ..paths import DYNAMIC_TOOLS_DIR, PROJECT_ROOT, normalize_user_path

router = APIRouter()
SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_TOOL_TIMEOUT_SECONDS = 30
MAX_TOOL_TIMEOUT_SECONDS = 300
GENESIS_POLICY_KEY = "genesis_policy"
GENESIS_POLICY_MODES = {"read-only", "approval-required", "full-local-exec"}
DEFAULT_GENESIS_POLICY = {
    "mode": "approval-required",
    "approved_tools": ["safe_tool", "cwd_echo"],
}

SUBPROCESS_TOOL_HARNESS = """
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import traceback

tool_path = pathlib.Path(sys.argv[1])
tool_kwargs = json.loads(sys.argv[2])
module_name = f"pexo_dynamic_tool_runner_{tool_path.stem}"
stdout_buffer = io.StringIO()
stderr_buffer = io.StringIO()
payload = {
    "status": "error",
    "result": None,
    "stdout": "",
    "stderr": "",
    "exception": None,
}

try:
    with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
        spec = importlib.util.spec_from_file_location(module_name, tool_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load tool at {tool_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run_callable = getattr(module, "run", None)
        if not callable(run_callable):
            raise RuntimeError(f"Tool '{tool_path.stem}' does not implement a callable 'run' function.")
        payload["result"] = run_callable(**tool_kwargs)
    payload["status"] = "success"
except Exception as exc:  # noqa: BLE001
    payload["exception"] = {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }
finally:
    payload["stdout"] = stdout_buffer.getvalue()
    payload["stderr"] = stderr_buffer.getvalue()
    print(json.dumps(payload, default=str))
"""


class ToolRegistrationRequest(BaseModel):
    name: str
    description: str
    python_code: str


class ToolUpdateRequest(BaseModel):
    description: str | None = None
    python_code: str | None = None


class ToolExecutionRequest(BaseModel):
    kwargs: dict = Field(default_factory=dict)
    session_id: str = "tool_execution"
    working_directory: str | None = None
    allow_outside_project: bool = False
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS


def _normalize_genesis_policy(value: dict | None) -> dict:
    payload = value or {}
    mode = str(payload.get("mode") or DEFAULT_GENESIS_POLICY["mode"]).strip().lower()
    if mode not in GENESIS_POLICY_MODES:
        mode = DEFAULT_GENESIS_POLICY["mode"]
    approved_tools = [
        validate_tool_name(str(name).strip())
        for name in (payload.get("approved_tools") or DEFAULT_GENESIS_POLICY["approved_tools"])
        if str(name).strip()
    ]
    return {
        "mode": mode,
        "approved_tools": sorted(set(approved_tools)),
    }


def get_genesis_policy(db: Session) -> dict:
    env_mode = str(os.environ.get("PEXO_GENESIS_TRUST_MODE") or "").strip().lower()
    env_tools = str(os.environ.get("PEXO_GENESIS_APPROVED_TOOLS") or "").strip()
    if env_mode:
        return _normalize_genesis_policy(
            {
                "mode": env_mode,
                "approved_tools": [item.strip() for item in env_tools.split(",") if item.strip()],
            }
        )

    setting = db.query(SystemSetting).filter(SystemSetting.key == GENESIS_POLICY_KEY).first()
    return _normalize_genesis_policy(setting.value if setting else None)


def _require_genesis_access(
    db: Session,
    *,
    action: str,
    tool_name: str | None = None,
    allow_outside_project: bool = False,
) -> dict:
    policy = get_genesis_policy(db)
    mode = policy["mode"]

    if allow_outside_project and mode != "full-local-exec":
        raise HTTPException(
            status_code=403,
            detail=(
                "Genesis tools may only run outside the Pexo project in full-local-exec mode. "
                f"Current mode: {mode}."
            ),
        )

    if action in {"register", "update", "delete"} and mode != "full-local-exec":
        raise HTTPException(
            status_code=403,
            detail=(
                "Genesis tool mutation is disabled unless Pexo is explicitly placed in full-local-exec mode. "
                f"Current mode: {mode}."
            ),
        )

    if action == "execute":
        if mode == "read-only":
            raise HTTPException(
                status_code=403,
                detail="Genesis tool execution is disabled in read-only mode.",
            )
        if mode == "approval-required" and tool_name not in policy["approved_tools"]:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Tool '{tool_name}' is not approved for execution in approval-required mode. "
                    "Use a pre-approved tool or explicitly enable full-local-exec mode on the host."
                ),
            )

    return policy


def validate_tool_name(tool_name: str) -> str:
    if not SAFE_TOOL_NAME_RE.fullmatch(tool_name):
        raise HTTPException(
            status_code=400,
            detail="Tool names must start with a letter or underscore and contain only letters, numbers, and underscores.",
        )
    return tool_name


def resolve_tool_path(tool_name: str, base_dir: Path | None = None) -> Path:
    safe_tool_name = validate_tool_name(tool_name)
    base_dir = base_dir or DYNAMIC_TOOLS_DIR
    return base_dir / f"{safe_tool_name}.py"


def resolve_execution_directory(raw_path: str | None, allow_outside_project: bool = False) -> Path:
    if raw_path:
        resolved = normalize_user_path(raw_path)
        if resolved is None:
            raise HTTPException(status_code=400, detail="Invalid working directory.")
    else:
        resolved = PROJECT_ROOT

    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"Working directory does not exist: {resolved}")

    if not allow_outside_project:
        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Working directory must remain inside the Pexo project unless allow_outside_project is explicitly enabled.",
            ) from exc

    return resolved


def _ensure_tool_code_compiles(tool_name: str, python_code: str) -> None:
    try:
        compile(python_code, f"<dynamic_tool:{tool_name}>", "exec")
    except SyntaxError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Tool '{tool_name}' contains invalid Python syntax: {exc.msg} (line {exc.lineno}).",
        ) from exc


def serialize_tool(tool: DynamicTool, include_code: bool = False) -> dict:
    payload = {
        "id": tool.id,
        "name": tool.name,
        "description": tool.description,
        "created_at": tool.created_at.isoformat() if getattr(tool, "created_at", None) else None,
    }
    if include_code:
        payload["python_code"] = tool.python_code
    return payload


def _truncate_payload(payload, limit: int = 240) -> str:
    serialized = json.dumps(payload, default=str)
    if len(serialized) > limit:
        return f"{serialized[:limit].rstrip()}..."
    return serialized


def _log_tool_execution(
    db: Session,
    *,
    tool_name: str,
    session_id: str,
    status: str,
    timeout_seconds: int,
    working_directory: Path,
    kwargs: dict,
    duration_ms: int,
    result=None,
    stdout: str = "",
    stderr: str = "",
    error_detail: dict | None = None,
    policy: dict | None = None,
) -> None:
    metrics = measure_context_payload(kwargs)
    db.add(
        AgentState(
            session_id=session_id,
            agent_name=f"Genesis:{tool_name}",
            status=status,
            context_size_tokens=int(metrics["token_estimate"]),
            data=annotate_context_metrics({
                "tool_name": tool_name,
                "execution_mode": "subprocess",
                "timeout_seconds": timeout_seconds,
                "working_directory": str(working_directory),
                "duration_ms": duration_ms,
                "kwargs": kwargs,
                "stdout_preview": stdout[:500],
                "stderr_preview": stderr[:500],
                "result_preview": _truncate_payload(result) if result is not None else None,
                "error_detail": error_detail,
                "genesis_policy": policy,
            }, kwargs),
        )
    )
    db.commit()
    invalidate_telemetry_caches()


def _run_tool_subprocess(tool_path: Path, request: ToolExecutionRequest, working_directory: Path) -> tuple[dict, int]:
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", SUBPROCESS_TOOL_HARNESS, str(tool_path), json.dumps(request.kwargs)],
            cwd=str(working_directory),
            capture_output=True,
            text=True,
            timeout=max(1, min(request.timeout_seconds, MAX_TOOL_TIMEOUT_SECONDS)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        raise HTTPException(
            status_code=504,
            detail={
                "message": f"Tool '{tool_path.stem}' timed out after {request.timeout_seconds} seconds.",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "duration_ms": duration_ms,
            },
        ) from exc

    duration_ms = int((time.monotonic() - started_at) * 1000)
    if completed.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"Tool '{tool_path.stem}' subprocess failed before returning a valid payload.",
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "returncode": completed.returncode,
                "duration_ms": duration_ms,
            },
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"Tool '{tool_path.stem}' returned a non-JSON payload.",
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "duration_ms": duration_ms,
            },
        ) from exc

    return payload, duration_ms


@router.post("/execute/{tool_name}")
def execute_tool(tool_name: str, request: ToolExecutionRequest, db: Session = Depends(get_db)):
    """
    Executes a dynamically created tool in a subprocess.
    The tool script must define a function named 'run' that accepts **kwargs.
    """
    safe_tool_name = validate_tool_name(tool_name)
    tool = db.query(DynamicTool).filter(DynamicTool.name == safe_tool_name).first()
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{safe_tool_name}' not found.")

    policy = _require_genesis_access(
        db,
        action="execute",
        tool_name=safe_tool_name,
        allow_outside_project=request.allow_outside_project,
    )

    tool_path = resolve_tool_path(safe_tool_name)
    if not tool_path.exists():
        raise HTTPException(status_code=500, detail=f"Tool file missing from disk: {tool_path}")

    working_directory = resolve_execution_directory(
        request.working_directory,
        allow_outside_project=request.allow_outside_project,
    )

    try:
        payload, duration_ms = _run_tool_subprocess(tool_path, request, working_directory)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        _log_tool_execution(
            db,
            tool_name=safe_tool_name,
            session_id=request.session_id,
            status="error",
            timeout_seconds=request.timeout_seconds,
            working_directory=working_directory,
            kwargs=request.kwargs,
            duration_ms=detail.get("duration_ms", 0),
            stdout=detail.get("stdout", ""),
            stderr=detail.get("stderr", ""),
            error_detail=detail,
            policy=policy,
        )
        raise

    stdout = payload.get("stdout", "")
    stderr = payload.get("stderr", "")
    error_detail = payload.get("exception")

    if payload.get("status") != "success":
        _log_tool_execution(
            db,
            tool_name=safe_tool_name,
            session_id=request.session_id,
            status="error",
            timeout_seconds=request.timeout_seconds,
            working_directory=working_directory,
            kwargs=request.kwargs,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            error_detail=error_detail,
            policy=policy,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"Tool '{safe_tool_name}' raised an exception.",
                "stdout": stdout,
                "stderr": stderr,
                "exception": error_detail,
                "duration_ms": duration_ms,
            },
        )

    result = payload.get("result")
    _log_tool_execution(
        db,
        tool_name=safe_tool_name,
        session_id=request.session_id,
        status="completed",
        timeout_seconds=request.timeout_seconds,
        working_directory=working_directory,
        kwargs=request.kwargs,
        duration_ms=duration_ms,
        result=result,
        stdout=stdout,
        stderr=stderr,
        policy=policy,
    )
    return {
        "status": "success",
        "result": result,
        "stdout": stdout,
        "stderr": stderr,
        "working_directory": str(working_directory),
        "duration_ms": duration_ms,
        "execution_mode": "subprocess",
        "genesis_policy": policy,
    }


@router.post("/register")
def register_tool(request: ToolRegistrationRequest, db: Session = Depends(get_db)):
    """
    Allows connected AI clients to register local tools for Pexo.
    If a task needs a specific API integration or data-processing capability,
    it can write a Python script and register it here.
    """
    safe_tool_name = validate_tool_name(request.name)
    _require_genesis_access(db, action="register", tool_name=safe_tool_name)
    _ensure_tool_code_compiles(safe_tool_name, request.python_code)
    existing_tool = db.query(DynamicTool).filter(DynamicTool.name == safe_tool_name).first()
    if existing_tool:
        raise HTTPException(status_code=400, detail=f"Tool '{safe_tool_name}' already exists.")

    DYNAMIC_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    tool_path = resolve_tool_path(safe_tool_name)
    if tool_path.exists():
        raise HTTPException(status_code=400, detail=f"Tool file '{tool_path.name}' already exists on disk.")

    try:
        with tool_path.open("w", encoding="utf-8") as handle:
            handle.write(request.python_code)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write tool to disk: {str(exc)}")

    new_tool = DynamicTool(
        name=safe_tool_name,
        description=request.description,
        python_code=request.python_code,
    )
    try:
        db.add(new_tool)
        db.commit()
        db.refresh(new_tool)
    except Exception as exc:
        db.rollback()
        tool_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to register tool metadata: {str(exc)}")
    invalidate_surface_caches()
    invalidate_session_context_snapshot()

    return {
        "status": "Success. Genesis Engine has assimilated the new tool.",
        "message": f"Tool '{safe_tool_name}' is now available through Pexo.",
        "tool_id": new_tool.id,
    }


@router.get("/")
def list_tools(db: Session = Depends(get_db)):
    """Returns a list of all dynamically generated tools."""
    tools = db.query(DynamicTool).order_by(DynamicTool.name.asc()).all()
    return [serialize_tool(tool) for tool in tools]


@router.get("/policy")
def get_tool_policy(db: Session = Depends(get_db)):
    return get_genesis_policy(db)


@router.get("/{tool_name}")
def get_tool(tool_name: str, db: Session = Depends(get_db)):
    safe_tool_name = validate_tool_name(tool_name)
    tool = db.query(DynamicTool).filter(DynamicTool.name == safe_tool_name).first()
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{safe_tool_name}' not found.")
    return serialize_tool(tool, include_code=True)


@router.put("/{tool_name}")
def update_tool(tool_name: str, request: ToolUpdateRequest, db: Session = Depends(get_db)):
    safe_tool_name = validate_tool_name(tool_name)
    _require_genesis_access(db, action="update", tool_name=safe_tool_name)
    tool = db.query(DynamicTool).filter(DynamicTool.name == safe_tool_name).first()
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{safe_tool_name}' not found.")

    tool_path = resolve_tool_path(safe_tool_name)
    if not tool_path.exists():
        raise HTTPException(status_code=500, detail=f"Tool file missing from disk: {tool_path}")

    if request.description is not None:
        tool.description = request.description

    if request.python_code is not None:
        _ensure_tool_code_compiles(safe_tool_name, request.python_code)
        try:
            with tool_path.open("w", encoding="utf-8") as handle:
                handle.write(request.python_code)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to update tool on disk: {str(exc)}")
        tool.python_code = request.python_code

    db.commit()
    db.refresh(tool)
    invalidate_surface_caches()
    invalidate_session_context_snapshot()
    return {
        "status": "success",
        "message": f"Tool '{safe_tool_name}' updated successfully.",
        "tool": serialize_tool(tool, include_code=True),
    }


@router.delete("/{tool_name}")
def delete_tool(tool_name: str, db: Session = Depends(get_db)):
    safe_tool_name = validate_tool_name(tool_name)
    _require_genesis_access(db, action="delete", tool_name=safe_tool_name)
    tool = db.query(DynamicTool).filter(DynamicTool.name == safe_tool_name).first()
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{safe_tool_name}' not found.")

    tool_path = resolve_tool_path(safe_tool_name)
    db.delete(tool)
    db.commit()
    tool_path.unlink(missing_ok=True)
    invalidate_surface_caches()
    invalidate_session_context_snapshot()
    return {
        "status": "success",
        "message": f"Tool '{safe_tool_name}' deleted successfully.",
    }
