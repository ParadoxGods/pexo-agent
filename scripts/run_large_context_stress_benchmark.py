from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db, reset_database_runtime
from app.models import AgentState, Artifact
from app.paths import current_state_root, set_runtime_path_context
from app.routers.artifacts import ArtifactPathRequest, get_artifact, register_artifact_path
from app.routers.orchestrator import PromptRequest, SimpleContinueRequest, continue_simple_task, start_simple_task
from app.runtime import build_runtime_status
from app.search_index import reset_search_index_runtime, search_artifact_ids
from scripts.run_context_compaction_benchmarks import (
    _format_number,
    build_host_specs,
    directory_size_bytes,
    estimate_tokens,
    format_seconds,
    measure_phase,
)


FILE_COUNT = 96
LINES_PER_FILE = 4500
NEEDLE = "PEXO_LARGE_STRESS_NEEDLE_20260405"
TARGET_FILE_INDEX = 73
TARGET_LINE_INDEX = 40
DOCS_DIR = REPO_ROOT / "docs" / "benchmarks"
RESULTS_JSON = DOCS_DIR / "large_context_stress_results.json"
RESULTS_MD = DOCS_DIR / "large_context_stress_results.md"
README_PATH = REPO_ROOT / "README.md"
SUITE_ROOT = REPO_ROOT / "sandbox" / "benchmark_large_context_stress"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_line(file_index: int, line_index: int) -> str:
    segment = (
        "Pexo local context buffer benchmark filler. "
        "This line exists to simulate oversized project material that would otherwise be shoved directly into an LLM context window. "
        "The point is to force a read-everything baseline to pay the full O(N) payload cost while Pexo only retrieves the relevant slice. "
        "segment="
        f"{file_index:03d}-{line_index:05d} "
        "memory=local artifacts=attached preferences=sticky continuity=shared clients=codex/gemini/claude "
        "operator=Primary EXecution Operator "
    )
    return f"{segment}\n"


def create_dataset(dataset_root: Path) -> list[Path]:
    dataset_root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for file_index in range(FILE_COUNT):
        path = dataset_root / f"dataset_{file_index:03d}.txt"
        paths.append(path)
        with path.open("w", encoding="utf-8") as handle:
            for line_index in range(LINES_PER_FILE):
                if file_index == TARGET_FILE_INDEX and line_index == TARGET_LINE_INDEX:
                    handle.write(
                        f"TARGET_RECORD file=dataset_{file_index:03d}.txt token={NEEDLE} owner=large-context-benchmark\n"
                    )
                else:
                    handle.write(build_line(file_index, line_index))
    return paths


def total_bytes(paths: list[Path]) -> int:
    return sum(path.stat().st_size for path in paths)


def direct_scan(paths: list[Path]) -> str:
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if NEEDLE in text:
            return path.name
    raise RuntimeError("Needle not found in direct scan.")


def register_artifacts(db: Session, paths: list[Path], task_context: str, session_id: str) -> None:
    for path in paths:
        stored = register_artifact_path(
            ArtifactPathRequest(
                path=str(path),
                task_context=task_context,
                session_id=session_id,
                name=path.name,
            ),
            db,
        )
        artifact_id = (stored.get("artifact") or {}).get("id")
        if artifact_id is not None:
            get_artifact(artifact_id, db)


def run_pexo_query(db: Session, task_context: str, session_id: str) -> tuple[str, int, list[dict]]:
    start_simple_task(
        PromptRequest(
            user_id="benchmark_user",
            prompt=f"Search the artifacts for the exact {NEEDLE} and return the file it is in.",
            session_id=session_id,
        ),
        db,
    )
    artifact_ids = search_artifact_ids(NEEDLE, limit=20)
    artifacts = (
        db.query(Artifact)
        .filter(Artifact.id.in_(artifact_ids), Artifact.task_context == task_context)
        .all()
    )
    if not artifacts:
        raise RuntimeError("Pexo search returned no matching artifacts for large stress benchmark.")
    answer = artifacts[0].name
    continue_simple_task(SimpleContinueRequest(session_id=session_id, result_data=answer), db)
    activities = (
        db.query(AgentState)
        .filter(AgentState.session_id == session_id)
        .order_by(AgentState.id.asc())
        .all()
    )
    token_total = sum(int(activity.context_size_tokens or 0) for activity in activities)
    compact_activity = [
        {
            "agent_name": activity.agent_name,
            "status": activity.status,
            "context_size_tokens": int(activity.context_size_tokens or 0),
        }
        for activity in activities
    ]
    return answer, token_total, compact_activity


def build_markdown(results: dict) -> str:
    host = results["host"]
    runtime = results["runtime"]
    perf = results["performance"]
    case = results["case"]
    lines = [
        "## Large Context Stress Test",
        "",
        "This is a single oversized synthetic stress benchmark generated by `scripts/run_large_context_stress_benchmark.py`.",
        "It creates one fresh large dataset, buries one exact needle in one file, compares direct raw scan against Pexo retrieval, records telemetry, and then removes the generated dataset.",
        "For this stress run, the Pexo ingest phase forces full artifact materialization so the exact token is searchable anywhere in the large corpus, not just in the deferred preview window.",
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
        f"- Pexo execution mode during stress run: `{runtime['install_mode']}` (isolated sandbox state root)",
        "",
        "### Result",
        "",
        "| Metric | Value |",
        "| :--- | ---: |",
        f"| Files generated | `{case['files']}` |",
        f"| Lines per file | `{case['lines_per_file']}` |",
        f"| Dataset size | `{_format_number(case['dataset_bytes'])}` bytes |",
        f"| Naive direct-context estimate | `{_format_number(case['traditional_tokens'])}` tokens |",
        f"| Direct raw scan time | `{format_seconds(case['baseline']['wall_seconds'])}` s |",
        f"| Pexo ingest time | `{format_seconds(case['pexo_ingest']['wall_seconds'])}` s |",
        f"| Pexo query time | `{format_seconds(case['pexo_query']['wall_seconds'])}` s |",
        f"| Pexo session context | `{_format_number(case['pexo_tokens'])}` tokens |",
        f"| Compaction ratio | `{case['compaction_ratio']:.2f}x` |",
        f"| Correct answer | `{'yes' if case['correct'] else 'no'}` |",
        f"| Needle file | `{case['expected_answer']}` |",
        "",
        "### Measured Machine Impact",
        "",
        "| Mode | Wall Time | CPU Time | Peak RSS | Notes |",
        "| :--- | ---: | ---: | ---: | :--- |",
        f"| Direct raw scan | `{format_seconds(perf['baseline']['wall_seconds'])}` s | `{format_seconds(perf['baseline']['cpu_seconds'])}` s | `{perf['baseline']['peak_rss_mb']}` MB | Reads the whole oversized dataset directly. |",
        f"| Pexo (ingest + query) | `{format_seconds(perf['pexo']['wall_seconds'])}` s | `{format_seconds(perf['pexo']['cpu_seconds'])}` s | `{perf['pexo']['peak_rss_mb']}` MB | Registers the large dataset locally, materializes full text for exact search, and answers via Pexo retrieval. |",
        f"| Measured Pexo overhead | `{format_seconds(perf['delta']['wall_seconds'])}` s | `{format_seconds(perf['delta']['cpu_seconds'])}` s | `{perf['delta']['peak_rss_mb']}` MB | Additional local state-management cost versus direct scanning for this one stress run. |",
        f"| Pexo stress-run state footprint | `{perf['pexo_state_mb']}` MB |",
        "",
        "### What This Means",
        "",
        "- This is deliberately synthetic and oversized. It is meant to stress context volume, not mirror human-readable repo structure.",
        "- The Pexo ingest phase in this run includes full artifact text materialization so exact-token search works across the entire oversized corpus.",
        "- The direct path still wins on pure one-shot wall-clock lookup because it avoids local indexing work.",
        "- Pexo wins on active context pressure: the model-facing session stayed tiny while the source corpus grew very large.",
        "",
    ]
    return "\n".join(lines)


def replace_or_append_readme_section(readme_text: str, markdown: str) -> str:
    marker = "## Large Context Stress Test"
    insert_after = "## Real-World Benchmarks"
    if marker in readme_text:
        start_index = readme_text.index(marker)
        end_token = "\n---\n\n## What Pexo Is Good At"
        end_index = readme_text.index(end_token, start_index)
        return f"{readme_text[:start_index]}{markdown}\n\n{readme_text[end_index:]}"
    if insert_after not in readme_text:
        raise RuntimeError("Could not find benchmark section anchor in README.")
    anchor = "\n---\n\n## What Pexo Is Good At"
    end_index = readme_text.index(anchor)
    return f"{readme_text[:end_index]}\n\n{markdown}{readme_text[end_index:]}"


def main() -> int:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    if SUITE_ROOT.exists():
        shutil.rmtree(SUITE_ROOT)
    dataset_root = SUITE_ROOT / "dataset"
    state_root = SUITE_ROOT / "pexo_state"
    dataset_root.mkdir(parents=True, exist_ok=True)

    set_runtime_path_context(env_override=str(state_root), code_root=REPO_ROOT)
    reset_database_runtime()
    reset_search_index_runtime()
    init_db()

    host = build_host_specs()
    runtime = build_runtime_status()
    paths = create_dataset(dataset_root)
    dataset_bytes = total_bytes(paths)
    traditional_tokens = estimate_tokens(dataset_bytes)

    with SessionLocal() as db:
        baseline_answer, baseline_metrics = measure_phase(lambda: direct_scan(paths))
        _, ingest_metrics = measure_phase(lambda: register_artifacts(db, paths, "large-context-stress", "large-context-artifacts"))
        pexo_result, query_metrics = measure_phase(lambda: run_pexo_query(db, "large-context-stress", "large-context-search"))
        pexo_answer, pexo_tokens, activity = pexo_result

    case = {
        "title": "Large context stress lookup",
        "files": FILE_COUNT,
        "lines_per_file": LINES_PER_FILE,
        "needle": NEEDLE,
        "expected_answer": f"dataset_{TARGET_FILE_INDEX:03d}.txt",
        "baseline_answer": baseline_answer,
        "pexo_answer": pexo_answer,
        "correct": baseline_answer == pexo_answer == f"dataset_{TARGET_FILE_INDEX:03d}.txt",
        "dataset_bytes": dataset_bytes,
        "traditional_tokens": traditional_tokens,
        "baseline": asdict(baseline_metrics),
        "pexo_ingest": asdict(ingest_metrics),
        "pexo_query": asdict(query_metrics),
        "pexo_tokens": pexo_tokens,
        "compaction_ratio": round(traditional_tokens / max(pexo_tokens, 1), 2),
        "session_activity": activity,
    }

    results = {
        "generated_at_utc": now_utc(),
        "host": host,
        "runtime": {
            "memory_backend": runtime.get("memory_backend", "unknown"),
            "install_mode": runtime.get("install_mode", "unknown"),
            "active_profile": runtime.get("active_profile", "unknown"),
        },
        "state_root": str(current_state_root()),
        "case": case,
        "performance": {
            "baseline": {
                "wall_seconds": round(baseline_metrics.wall_seconds, 3),
                "cpu_seconds": round(baseline_metrics.cpu_seconds, 3),
                "peak_rss_mb": baseline_metrics.peak_rss_mb,
            },
            "pexo": {
                "wall_seconds": round(ingest_metrics.wall_seconds + query_metrics.wall_seconds, 3),
                "cpu_seconds": round(ingest_metrics.cpu_seconds + query_metrics.cpu_seconds, 3),
                "peak_rss_mb": round(max(ingest_metrics.peak_rss_mb, query_metrics.peak_rss_mb), 2),
            },
            "delta": {
                "wall_seconds": round((ingest_metrics.wall_seconds + query_metrics.wall_seconds) - baseline_metrics.wall_seconds, 3),
                "cpu_seconds": round((ingest_metrics.cpu_seconds + query_metrics.cpu_seconds) - baseline_metrics.cpu_seconds, 3),
                "peak_rss_mb": round(max(0.0, max(ingest_metrics.peak_rss_mb, query_metrics.peak_rss_mb) - baseline_metrics.peak_rss_mb), 2),
            },
            "pexo_state_mb": round(directory_size_bytes(state_root) / (1024 * 1024), 2),
        },
    }

    RESULTS_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    markdown = build_markdown(results)
    RESULTS_MD.write_text(markdown, encoding="utf-8")
    README_PATH.write_text(replace_or_append_readme_section(README_PATH.read_text(encoding="utf-8"), markdown), encoding="utf-8")

    shutil.rmtree(dataset_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
