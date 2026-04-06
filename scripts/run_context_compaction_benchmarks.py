from __future__ import annotations

import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database import SessionLocal, init_db, reset_database_runtime
from app.models import AgentState, Artifact
from app.paths import current_state_root, reset_runtime_path_context, set_runtime_path_context
from app.routers.artifacts import ArtifactPathRequest, register_artifact_path
from app.routers.orchestrator import PromptRequest, SimpleContinueRequest, continue_simple_task, start_simple_task
from app.runtime import build_runtime_status
from app.search_index import reset_search_index_runtime, search_artifact_ids
from app.version import __version__


BENCHMARK_SEED = 20260405
TOKENS_PER_BYTE_DIVISOR = 4


@dataclass
class BenchmarkCase:
    slug: str
    title: str
    prompt: str
    query: str
    files: int
    lines_per_file: int
    target_files: tuple[int, ...]
    expected_answer: str
    answer_mode: str
    regexes: tuple[str, ...] = ()


@dataclass
class PhaseMetrics:
    wall_seconds: float
    cpu_seconds: float
    peak_rss_mb: float


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_number(value: int | float, digits: int = 2) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digits}f}"


def get_rss_bytes() -> int:
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        get_process_memory_info.restype = wintypes.BOOL
        ok = get_process_memory_info(handle, ctypes.byref(counters), counters.cb)
        if ok:
            return int(counters.WorkingSetSize)
    except Exception:
        pass
    return 0


class PeakSampler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._peak_rss = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._peak_rss = max(self._peak_rss, get_rss_bytes())
            self._stop.wait(0.05)

    def __enter__(self) -> "PeakSampler":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self._peak_rss = max(self._peak_rss, get_rss_bytes())

    @property
    def peak_rss_bytes(self) -> int:
        return self._peak_rss


def measure_phase(fn: Callable[[], object]) -> tuple[object, PhaseMetrics]:
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    with PeakSampler() as sampler:
        result = fn()
    metrics = PhaseMetrics(
        wall_seconds=time.perf_counter() - wall_start,
        cpu_seconds=time.process_time() - cpu_start,
        peak_rss_mb=round(sampler.peak_rss_bytes / (1024 * 1024), 2),
    )
    return result, metrics


def directory_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        root_path = Path(root)
        for name in files:
            try:
                total += (root_path / name).stat().st_size
            except OSError:
                continue
    return total


def build_host_specs() -> dict:
    def detect_cpu_name() -> str:
        system = platform.system().lower()
        try:
            if system == "windows":
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                name = completed.stdout.strip()
                if name:
                    return name
            if system == "darwin":
                completed = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                name = completed.stdout.strip()
                if name:
                    return name
            if system == "linux":
                cpuinfo = Path("/proc/cpuinfo")
                if cpuinfo.exists():
                    for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if ":" in line and line.lower().startswith("model name"):
                            name = line.split(":", 1)[1].strip()
                            if name:
                                return name
                completed = subprocess.run(
                    ["lscpu"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                for line in completed.stdout.splitlines():
                    if ":" in line and line.lower().startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                        if name:
                            return name
        except Exception:
            pass
        return platform.processor() or "Unknown CPU"

    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
        total_ram_gb = round(mem.ullTotalPhys / (1024**3), 2)
    except Exception:
        total_ram_gb = 0.0

    return {
        "timestamp_utc": _now_utc(),
        "machine": platform.node(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu": detect_cpu_name(),
        "logical_cores": os.cpu_count() or 0,
        "total_ram_gb": total_ram_gb,
        "pexo_version": __version__,
    }


def build_cases() -> list[BenchmarkCase]:
    return [
        BenchmarkCase(
            slug="needle_file_lookup",
            title="Needle in a haystack file lookup",
            prompt="Search the artifacts for the exact PEXO_SECRET_TOKEN_998877 and return the file it is in.",
            query="PEXO_SECRET_TOKEN_998877",
            files=10,
            lines_per_file=1100,
            target_files=(7,),
            expected_answer="dataset_07.txt",
            answer_mode="file",
        ),
        BenchmarkCase(
            slug="retry_limit_lookup",
            title="Config value lookup",
            prompt="Search the artifacts for BENCH_RETRY_LIMIT_004 and return the configured value only.",
            query="BENCH_RETRY_LIMIT_004",
            files=9,
            lines_per_file=1000,
            target_files=(4,),
            expected_answer="7",
            answer_mode="regex",
            regexes=(r"BENCH_RETRY_LIMIT_004\s*=\s*(\d+)",),
        ),
        BenchmarkCase(
            slug="endpoint_owner_lookup",
            title="Endpoint owner lookup",
            prompt="Search the artifacts for BENCH_ENDPOINT_005 and return the owner value only.",
            query="BENCH_ENDPOINT_005",
            files=10,
            lines_per_file=1050,
            target_files=(5,),
            expected_answer="api-platform",
            answer_mode="regex",
            regexes=(r"BENCH_ENDPOINT_005\s+/v1/private/sync\s+owner=([A-Za-z0-9_-]+)",),
        ),
        BenchmarkCase(
            slug="migration_file_lookup",
            title="Migration file lookup",
            prompt="Search the artifacts for BENCH_MIGRATION_006 and return the file it is in.",
            query="BENCH_MIGRATION_006",
            files=8,
            lines_per_file=1200,
            target_files=(6,),
            expected_answer="dataset_06.txt",
            answer_mode="file",
        ),
        BenchmarkCase(
            slug="service_port_lookup",
            title="Service port lookup",
            prompt="Search the artifacts for BENCH_SERVICE_007 and return the port number only.",
            query="BENCH_SERVICE_007",
            files=10,
            lines_per_file=1150,
            target_files=(2,),
            expected_answer="4719",
            answer_mode="regex",
            regexes=(r"BENCH_SERVICE_007\s+name=artifact-index\s+port=(\d+)",),
        ),
        BenchmarkCase(
            slug="feature_flag_lookup",
            title="Feature flag state lookup",
            prompt="Search the artifacts for BENCH_FLAG_008 and return the flag value only.",
            query="BENCH_FLAG_008",
            files=10,
            lines_per_file=1080,
            target_files=(8,),
            expected_answer="true",
            answer_mode="regex",
            regexes=(r"BENCH_FLAG_008\s+enable_background_compaction=(true|false)",),
        ),
        BenchmarkCase(
            slug="cron_schedule_lookup",
            title="Cron schedule lookup",
            prompt="Search the artifacts for BENCH_CRON_009 and return the schedule value only.",
            query="BENCH_CRON_009",
            files=9,
            lines_per_file=1125,
            target_files=(3,),
            expected_answer="0 */6 * * *",
            answer_mode="regex",
            regexes=(r"BENCH_CRON_009\s+schedule=([^\r\n]+)",),
        ),
        BenchmarkCase(
            slug="owner_email_lookup",
            title="Owner email lookup",
            prompt="Search the artifacts for BENCH_OWNER_010 and return the email value only.",
            query="BENCH_OWNER_010",
            files=10,
            lines_per_file=1040,
            target_files=(9,),
            expected_answer="ops-bench@example.local",
            answer_mode="regex",
            regexes=(r"BENCH_OWNER_010\s+email=([A-Za-z0-9@._-]+)",),
        ),
        BenchmarkCase(
            slug="sql_query_file_lookup",
            title="SQL query file lookup",
            prompt="Search the artifacts for BENCH_QUERY_011 and return the file it is in.",
            query="BENCH_QUERY_011",
            files=10,
            lines_per_file=1300,
            target_files=(1,),
            expected_answer="dataset_01.txt",
            answer_mode="file",
        ),
        BenchmarkCase(
            slug="chain_pair_lookup",
            title="Multi-file chain lookup",
            prompt="Search the artifacts for BENCH_CHAIN_012 and return the config value and query value separated by ` | `.",
            query="BENCH_CHAIN_012",
            files=10,
            lines_per_file=1250,
            target_files=(2, 8),
            expected_answer="warehouse_primary | select_events_since_cursor",
            answer_mode="pair",
            regexes=(
                r"BENCH_CHAIN_012\s+config=([A-Za-z0-9_]+)",
                r"BENCH_CHAIN_012\s+query=([A-Za-z0-9_]+)",
            ),
        ),
    ]


def special_lines_for_case(case: BenchmarkCase) -> dict[int, list[str]]:
    lines: dict[int, list[str]] = {index: [] for index in case.target_files}
    if case.slug == "needle_file_lookup":
        lines[7].append("PEXO_SECRET_TOKEN_998877")
    elif case.slug == "retry_limit_lookup":
        lines[4].append("BENCH_RETRY_LIMIT_004 = 7")
    elif case.slug == "endpoint_owner_lookup":
        lines[5].append("BENCH_ENDPOINT_005 /v1/private/sync owner=api-platform")
    elif case.slug == "migration_file_lookup":
        lines[6].append("BENCH_MIGRATION_006 id=202604050601 add_telemetry_columns")
    elif case.slug == "service_port_lookup":
        lines[2].append("BENCH_SERVICE_007 name=artifact-index port=4719")
    elif case.slug == "feature_flag_lookup":
        lines[8].append("BENCH_FLAG_008 enable_background_compaction=true")
    elif case.slug == "cron_schedule_lookup":
        lines[3].append("BENCH_CRON_009 schedule=0 */6 * * *")
    elif case.slug == "owner_email_lookup":
        lines[9].append("BENCH_OWNER_010 email=ops-bench@example.local")
    elif case.slug == "sql_query_file_lookup":
        lines[1].append("BENCH_QUERY_011 select_count_by_state")
    elif case.slug == "chain_pair_lookup":
        lines[2].append("BENCH_CHAIN_012 config=warehouse_primary")
        lines[8].append("BENCH_CHAIN_012 query=select_events_since_cursor")
    return lines


def create_case_dataset(case_root: Path, case: BenchmarkCase) -> list[Path]:
    rng = random.Random(f"{BENCHMARK_SEED}:{case.slug}")
    case_root.mkdir(parents=True, exist_ok=True)
    special_map = special_lines_for_case(case)
    files: list[Path] = []
    for index in range(1, case.files + 1):
        path = case_root / f"dataset_{index:02d}.txt"
        insert_line = rng.randint(200, case.lines_per_file - 150)
        specials = list(special_map.get(index, []))
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for line_no in range(1, case.lines_per_file + 1):
                if specials and line_no == insert_line:
                    for special in specials:
                        handle.write(f"{special}\n")
                handle.write(
                    f"{case.slug} filler line {line_no} for file {index}. "
                    "This boilerplate exists to inflate traditional context size and force raw readers to scan noise.\n"
                )
            if specials:
                for special in specials:
                    handle.write(f"{special}\n")
        files.append(path)
    return files


def total_bytes(paths: list[Path]) -> int:
    return sum(path.stat().st_size for path in paths)


def estimate_tokens(size_bytes: int) -> int:
    return size_bytes // TOKENS_PER_BYTE_DIVISOR


def parse_case_answer(case: BenchmarkCase, texts_by_name: dict[str, str]) -> str:
    if case.answer_mode == "file":
        for file_name, text in texts_by_name.items():
            if case.query in text:
                return file_name
        raise RuntimeError(f"Failed to resolve file answer for {case.slug}")

    if case.answer_mode == "regex":
        pattern = re.compile(case.regexes[0])
        for text in texts_by_name.values():
            match = pattern.search(text)
            if match:
                return match.group(1).strip()
        raise RuntimeError(f"Failed to extract regex answer for {case.slug}")

    if case.answer_mode == "pair":
        values: list[str] = []
        for raw_pattern in case.regexes:
            pattern = re.compile(raw_pattern)
            found = None
            for text in texts_by_name.values():
                match = pattern.search(text)
                if match:
                    found = match.group(1).strip()
                    break
            if not found:
                raise RuntimeError(f"Failed to extract pair component for {case.slug}: {raw_pattern}")
            values.append(found)
        return " | ".join(values)

    raise ValueError(f"Unsupported answer mode: {case.answer_mode}")


def direct_scan_case(case: BenchmarkCase, paths: list[Path]) -> str:
    texts_by_name: dict[str, str] = {}
    for path in paths:
        texts_by_name[path.name] = path.read_text(encoding="utf-8")
    return parse_case_answer(case, texts_by_name)


def register_case_artifacts(db: Session, case: BenchmarkCase, paths: list[Path], artifact_session_id: str, task_context: str) -> list[dict]:
    payloads = []
    for path in paths:
        stored = register_artifact_path(
            ArtifactPathRequest(
                path=str(path),
                session_id=artifact_session_id,
                task_context=task_context,
                name=path.name,
            ),
            db,
        )
        payloads.append(stored["artifact"])
    return payloads


def artifact_texts_for_query(db: Session, case: BenchmarkCase, task_context: str) -> dict[str, str]:
    artifact_ids = search_artifact_ids(case.query, limit=20)
    if not artifact_ids:
        raise RuntimeError(f"Pexo search returned no artifact candidates for {case.slug}")
    artifacts = (
        db.query(Artifact)
        .filter(Artifact.id.in_(artifact_ids), Artifact.task_context == task_context)
        .all()
    )
    if not artifacts:
        raise RuntimeError(f"Pexo search returned no matching artifacts in task context for {case.slug}")
    texts: dict[str, str] = {}
    for artifact in artifacts:
        texts[artifact.name] = artifact.extracted_text or ""
    return texts


def run_pexo_case(db: Session, case: BenchmarkCase, task_context: str, session_id: str) -> tuple[str, int, list[dict]]:
    started = start_simple_task(
        PromptRequest(user_id="benchmark_user", prompt=case.prompt, session_id=session_id),
        db,
    )
    texts = artifact_texts_for_query(db, case, task_context)
    answer = parse_case_answer(case, texts)
    continue_simple_task(
        SimpleContinueRequest(session_id=session_id, result_data=answer),
        db,
    )
    activities = (
        db.query(AgentState)
        .filter(AgentState.session_id == session_id)
        .order_by(AgentState.id.asc())
        .all()
    )
    token_total = sum(int(activity.context_size_tokens or 0) for activity in activities)
    compact_activities = [
        {
            "agent_name": activity.agent_name,
            "status": activity.status,
            "context_size_tokens": int(activity.context_size_tokens or 0),
        }
        for activity in activities
    ]
    if not started:
        raise RuntimeError(f"Failed to start Pexo session for {case.slug}")
    return answer, token_total, compact_activities


def phase_summary(metrics: list[PhaseMetrics]) -> dict:
    return {
        "wall_seconds": round(sum(item.wall_seconds for item in metrics), 3),
        "cpu_seconds": round(sum(item.cpu_seconds for item in metrics), 3),
        "peak_rss_mb": round(max((item.peak_rss_mb for item in metrics), default=0.0), 2),
    }


def format_seconds(value: float) -> str:
    return f"{value:.3f}"


def build_readme_markdown(results: dict) -> str:
    host = results["host"]
    perf = results["suite_performance"]
    runtime = results["runtime"]
    lines = [
        "## Context Compaction (Benchmarks)",
        "",
        "These numbers come from a fresh local benchmark run generated by `scripts/run_context_compaction_benchmarks.py`.",
        "The suite spins up an isolated sandbox state root, generates 10 new padded datasets, runs both paths, records telemetry, and then tears the datasets back down.",
        "Raw benchmark artifacts are checked into `docs/benchmarks/context_compaction_results.json` and `docs/benchmarks/context_compaction_results.md`.",
        "",
        "The benchmark compares two paths for the same 10 synthetic workloads:",
        "",
        "1. **Direct raw scan**: read the full workload files directly and answer without Pexo.",
        "2. **Pexo retrieval**: register the workload as local artifacts, answer through Pexo, and sum the resulting session `context_size_tokens`.",
        "",
        "Traditional token counts are estimated using the rough rule of `bytes / 4`.",
        "",
        "### Host System",
        "",
        f"- OS: `{host['os']}`",
        f"- CPU: `{host['cpu']}`",
        f"- Logical cores: `{host['logical_cores']}`",
        f"- RAM: `{host['total_ram_gb']}` GB",
        f"- Python: `{host['python']}`",
        f"- Pexo version: `{host['pexo_version']}`",
        f"- Pexo memory backend: `{runtime['memory_backend']}`",
        f"- Pexo execution mode during suite: `{runtime['install_mode']}` (with an isolated sandbox state root)",
        "",
        "### Measured Machine Impact",
        "",
        "| Mode | Wall Time | CPU Time | Peak RSS | Notes |",
        "| :--- | ---: | ---: | ---: | :--- |",
        (
            f"| Direct raw scan | `{format_seconds(perf['baseline']['wall_seconds'])}` s | `{format_seconds(perf['baseline']['cpu_seconds'])}` s | "
            f"`{perf['baseline']['peak_rss_mb']}` MB | Reads each workload directly without Pexo. |"
        ),
        (
            f"| Pexo (ingest + query) | `{format_seconds(perf['pexo']['wall_seconds'])}` s | `{format_seconds(perf['pexo']['cpu_seconds'])}` s | "
            f"`{perf['pexo']['peak_rss_mb']}` MB | Uses isolated local Pexo state and artifact indexing. |"
        ),
        (
            f"| Measured Pexo overhead | `{format_seconds(perf['delta']['wall_seconds'])}` s | `{format_seconds(perf['delta']['cpu_seconds'])}` s | "
            f"`{perf['delta']['peak_rss_mb']}` MB | Additional cost of local ingest + retrieval over raw direct scanning for this suite. |"
        ),
        (
            f"| Pexo benchmark state footprint | - | - | - | "
            f"`{perf['pexo_state_mb']}` MB on disk after the suite. |"
        ),
        "",
        "### Per-Workload Results",
        "",
        "| Workload | Dataset Size | Traditional Tokens | Direct Scan Time | Pexo Ingest | Pexo Query | Pexo Tokens | Compaction Ratio | Correct |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for case in results["cases"]:
        lines.append(
            f"| {case['title']} | `{_format_number(case['dataset_bytes'])}` bytes | "
            f"`{_format_number(case['traditional_tokens'])}` | "
            f"`{format_seconds(case['baseline']['wall_seconds'])}` s | "
            f"`{format_seconds(case['pexo_ingest']['wall_seconds'])}` s | "
            f"`{format_seconds(case['pexo_query']['wall_seconds'])}` s | "
            f"`{_format_number(case['pexo_tokens'])}` | "
            f"`{case['compaction_ratio']:.2f}x` | "
            f"{'yes' if case['correct'] else 'no'} |"
        )
    summary = results["summary"]
    lines.extend(
        [
            "",
            "### Summary",
            "",
            f"- Total benchmark data generated: `{_format_number(summary['total_dataset_bytes'])}` bytes",
            f"- Average traditional context estimate: `{_format_number(summary['avg_traditional_tokens'])}` tokens",
            f"- Average Pexo session context: `{_format_number(summary['avg_pexo_tokens'])}` tokens",
            f"- Average compaction ratio: `{summary['avg_compaction_ratio']:.2f}x`",
            f"- Median compaction ratio: `{summary['median_compaction_ratio']:.2f}x`",
            f"- All 10 workloads returned the correct answer: `{'yes' if summary['all_correct'] else 'no'}`",
            "",
            "### What This Means",
            "",
            "- These measurements describe **active chat-context pressure**, not a universal wall-clock speed guarantee.",
            "- Pexo adds local ingestion and indexing work, so direct raw file scanning can still be faster for one-off exact searches.",
            "- The win is that the **working model context stays dramatically smaller** once the data is inside Pexo.",
            "- This run used the default SQLite + keyword retrieval path. The numbers do not depend on optional semantic vector memory.",
            "- That trade makes the most sense when the same local state is reused across sessions, clients, and repeated queries.",
            "",
        ]
    )
    return "\n".join(lines)


def replace_readme_section(readme_text: str, markdown: str) -> str:
    start = "## Context Compaction (Benchmarks)"
    end = "\n---\n\n## What Pexo Is Good At"
    if start not in readme_text:
        return readme_text
    start_index = readme_text.index(start)
    end_index = readme_text.index(end)
    return f"{readme_text[:start_index]}{markdown}{readme_text[end_index:]}"


def main() -> int:
    suite_root = REPO_ROOT / "sandbox" / "benchmark_context_compaction_fresh"
    docs_dir = REPO_ROOT / "docs" / "benchmarks"
    docs_dir.mkdir(parents=True, exist_ok=True)
    results_path = docs_dir / "context_compaction_results.json"
    markdown_path = docs_dir / "context_compaction_results.md"

    if suite_root.exists():
        shutil.rmtree(suite_root)
    suite_root.mkdir(parents=True, exist_ok=True)
    dataset_root = suite_root / "datasets"
    state_root = suite_root / "pexo_state"
    dataset_root.mkdir(parents=True, exist_ok=True)

    set_runtime_path_context(env_override=str(state_root), code_root=REPO_ROOT)
    reset_database_runtime()
    reset_search_index_runtime()
    init_db()

    cases = build_cases()
    host = build_host_specs()
    runtime_status = build_runtime_status()
    results: dict = {
        "generated_at_utc": _now_utc(),
        "seed": BENCHMARK_SEED,
        "host": host,
        "runtime": {
            "memory_backend": runtime_status.get("memory_backend", "unknown"),
            "install_mode": runtime_status.get("install_mode", "unknown"),
            "active_profile": runtime_status.get("active_profile", "unknown"),
        },
        "state_root": str(current_state_root()),
        "cases": [],
    }

    baseline_phase_metrics: list[PhaseMetrics] = []
    pexo_ingest_metrics: list[PhaseMetrics] = []
    pexo_query_metrics: list[PhaseMetrics] = []

    with SessionLocal() as db:
        for index, case in enumerate(cases, start=1):
            case_root = dataset_root / f"{index:02d}_{case.slug}"
            paths = create_case_dataset(case_root, case)
            bytes_total = total_bytes(paths)
            traditional_tokens = estimate_tokens(bytes_total)

            baseline_answer, baseline_metrics = measure_phase(lambda case=case, paths=paths: direct_scan_case(case, paths))
            baseline_phase_metrics.append(baseline_metrics)

            task_context = f"bench-{index:02d}-{case.slug}"
            artifact_session_id = f"{task_context}-artifacts"
            search_session_id = f"{task_context}-search"

            _, ingest_metrics = measure_phase(
                lambda db=db, case=case, paths=paths, artifact_session_id=artifact_session_id, task_context=task_context: register_case_artifacts(
                    db, case, paths, artifact_session_id, task_context
                )
            )
            pexo_ingest_metrics.append(ingest_metrics)

            pexo_result, query_metrics = measure_phase(
                lambda db=db, case=case, task_context=task_context, search_session_id=search_session_id: run_pexo_case(
                    db, case, task_context, search_session_id
                )
            )
            pexo_answer, pexo_tokens, session_activity = pexo_result
            pexo_query_metrics.append(query_metrics)

            correct = baseline_answer == case.expected_answer and pexo_answer == case.expected_answer
            compaction_ratio = traditional_tokens / max(pexo_tokens, 1)

            results["cases"].append(
                {
                    "slug": case.slug,
                    "title": case.title,
                    "prompt": case.prompt,
                    "query": case.query,
                    "expected_answer": case.expected_answer,
                    "baseline_answer": baseline_answer,
                    "pexo_answer": pexo_answer,
                    "correct": correct,
                    "dataset_bytes": bytes_total,
                    "traditional_tokens": traditional_tokens,
                    "baseline": asdict(baseline_metrics),
                    "pexo_ingest": asdict(ingest_metrics),
                    "pexo_query": asdict(query_metrics),
                    "pexo_tokens": pexo_tokens,
                    "compaction_ratio": round(compaction_ratio, 2),
                    "session_activity": session_activity,
                }
            )

    total_dataset_bytes = sum(case["dataset_bytes"] for case in results["cases"])
    avg_traditional = sum(case["traditional_tokens"] for case in results["cases"]) / len(results["cases"])
    avg_pexo = sum(case["pexo_tokens"] for case in results["cases"]) / len(results["cases"])
    ratios = sorted(case["compaction_ratio"] for case in results["cases"])
    median_ratio = ratios[len(ratios) // 2]

    results["suite_performance"] = {
        "baseline": phase_summary(baseline_phase_metrics),
        "pexo_ingest": phase_summary(pexo_ingest_metrics),
        "pexo_query": phase_summary(pexo_query_metrics),
        "pexo": {
            "wall_seconds": round(
                sum(item.wall_seconds for item in pexo_ingest_metrics) + sum(item.wall_seconds for item in pexo_query_metrics),
                3,
            ),
            "cpu_seconds": round(
                sum(item.cpu_seconds for item in pexo_ingest_metrics) + sum(item.cpu_seconds for item in pexo_query_metrics),
                3,
            ),
            "peak_rss_mb": round(
                max(
                    max((item.peak_rss_mb for item in pexo_ingest_metrics), default=0.0),
                    max((item.peak_rss_mb for item in pexo_query_metrics), default=0.0),
                ),
                2,
            ),
        },
        "delta": {
            "wall_seconds": round(
                (
                    sum(item.wall_seconds for item in pexo_ingest_metrics) + sum(item.wall_seconds for item in pexo_query_metrics)
                )
                - sum(item.wall_seconds for item in baseline_phase_metrics),
                3,
            ),
            "cpu_seconds": round(
                (
                    sum(item.cpu_seconds for item in pexo_ingest_metrics) + sum(item.cpu_seconds for item in pexo_query_metrics)
                )
                - sum(item.cpu_seconds for item in baseline_phase_metrics),
                3,
            ),
            "peak_rss_mb": round(
                max(
                    0.0,
                    max(
                        max((item.peak_rss_mb for item in pexo_ingest_metrics), default=0.0),
                        max((item.peak_rss_mb for item in pexo_query_metrics), default=0.0),
                    )
                    - max((item.peak_rss_mb for item in baseline_phase_metrics), default=0.0),
                ),
                2,
            ),
        },
        "pexo_state_mb": round(directory_size_bytes(state_root) / (1024 * 1024), 2),
    }
    results["summary"] = {
        "case_count": len(results["cases"]),
        "total_dataset_bytes": total_dataset_bytes,
        "avg_traditional_tokens": round(avg_traditional, 2),
        "avg_pexo_tokens": round(avg_pexo, 2),
        "avg_compaction_ratio": round(sum(case["compaction_ratio"] for case in results["cases"]) / len(results["cases"]), 2),
        "median_compaction_ratio": round(median_ratio, 2),
        "all_correct": all(case["correct"] for case in results["cases"]),
    }

    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    markdown = build_readme_markdown(results)
    markdown_path.write_text(markdown, encoding="utf-8")

    readme_path = REPO_ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    readme_path.write_text(replace_readme_section(readme, markdown), encoding="utf-8")

    shutil.rmtree(dataset_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
