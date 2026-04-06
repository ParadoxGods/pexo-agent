from __future__ import annotations

import json
import random
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database import init_db, reset_database_runtime
from app.mcp_server import (
    pexo_continue_task,
    pexo_find_artifact,
    pexo_find_memory,
    pexo_get_artifact,
    pexo_get_session_activity,
    pexo_register_artifact_path,
    pexo_start_task,
    pexo_store_memory,
)
from app.paths import current_state_root, set_runtime_path_context
from app.runtime import build_runtime_status
from app.search_index import reset_search_index_runtime
from scripts.run_context_compaction_benchmarks import (
    PhaseMetrics,
    _format_number,
    build_host_specs,
    directory_size_bytes,
    estimate_tokens,
    format_seconds,
    measure_phase,
)

BENCHMARK_SEED = 20260405
SUITE_ROOT = REPO_ROOT / "sandbox" / "benchmark_realworld_compression_recollection"
DOCS_DIR = REPO_ROOT / "docs" / "benchmarks"
RESULTS_JSON = DOCS_DIR / "realworld_compression_recollection_results.json"
RESULTS_MD = DOCS_DIR / "realworld_compression_recollection_results.md"
README_PATH = REPO_ROOT / "README.md"

REPO_CORPUS = [
    "README.md",
    "AGENTS.md",
    "docs/ARCHITECTURE.md",
    "app/launcher.py",
    "app/mcp_server.py",
    "app/routers/tools.py",
    "app/agents/graph.py",
    "app/paths.py",
    "app/runtime.py",
    "tests/test_hardening.py",
]


@dataclass
class WorkloadResult:
    title: str
    query: str
    expected_answer: str
    direct_answer: str
    pexo_answer: str
    correct: bool
    session_id: str


@dataclass
class SuiteResult:
    slug: str
    title: str
    description: str
    what_it_tests: str
    workload_count: int
    corpus_bytes: int
    direct_tokens: int
    pexo_tokens: int
    reduction_factor: float
    retained_pct: float
    exact_match_accuracy_pct: float
    direct_metrics: PhaseMetrics
    pexo_setup_metrics: PhaseMetrics
    pexo_query_metrics: PhaseMetrics
    pexo_total_metrics: PhaseMetrics
    pexo_state_mb: float
    workload_results: list[WorkloadResult]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def regex_extract(pattern: str, text: str, *, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    if not match:
        raise RuntimeError(f"Pattern not found: {pattern}")
    return match.group(1).strip()


def joined_texts(texts: dict[str, str]) -> str:
    return "\n".join(texts.values())


def safe_resolve(resolver: Callable[[dict[str, str]], str], texts: dict[str, str]) -> str:
    try:
        return resolver(texts)
    except Exception as exc:
        return f"<error: {exc}>"


def read_text_map(paths: list[Path], root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="ignore")
        for path in paths
    }


def prepare_runtime(state_root: Path) -> None:
    shutil.rmtree(state_root, ignore_errors=True)
    state_root.mkdir(parents=True, exist_ok=True)
    reset_search_index_runtime()
    reset_database_runtime()
    set_runtime_path_context(env_override=str(state_root))
    init_db()


def phase_metrics_sum(first: PhaseMetrics, second: PhaseMetrics) -> PhaseMetrics:
    return PhaseMetrics(
        wall_seconds=round(first.wall_seconds + second.wall_seconds, 6),
        cpu_seconds=round(first.cpu_seconds + second.cpu_seconds, 6),
        peak_rss_mb=max(first.peak_rss_mb, second.peak_rss_mb),
    )


def register_paths(paths: list[Path], root: Path, *, task_context: str, session_id: str) -> None:
    for path in paths:
        pexo_register_artifact_path(
            path=str(path),
            session_id=session_id,
            task_context=task_context,
            name=path.relative_to(root).as_posix(),
        )


def store_memory_entries(entries: list[str], *, task_context: str, session_prefix: str) -> None:
    for index, content in enumerate(entries, start=1):
        pexo_store_memory(
            content=content,
            task_context=task_context,
            session_id=f"{session_prefix}-memory-{index:02d}",
        )


def collect_pexo_texts(query: str, *, task_context: str, artifact_limit: int = 12, memory_limit: int = 12) -> dict[str, str]:
    texts: dict[str, str] = {}
    artifact_payload = pexo_find_artifact(query=query, limit=artifact_limit, task_context=task_context)
    for item in artifact_payload.get("results", []):
        artifact_id = item.get("id")
        if artifact_id is None:
            continue
        full = pexo_get_artifact(artifact_id)
        texts[str(full.get("name") or f"artifact-{artifact_id}")] = full.get("extracted_text") or ""
    memory_payload = pexo_find_memory(query=query, limit=memory_limit)
    for index, item in enumerate(memory_payload.get("results", []), start=1):
        texts[f"memory_{index:02d}.txt"] = item.get("content") or ""
    return texts


def start_and_complete_task(prompt: str, answer: str, session_id: str) -> int:
    started = pexo_start_task(prompt=prompt, user_id="benchmark_user", session_id=session_id)
    if started.get("status") == "clarification_required":
        raise RuntimeError(f"Unexpected clarification request for {session_id}: {started}")
    pexo_continue_task(session_id=session_id, result_data=answer)
    activity = pexo_get_session_activity(session_id=session_id, limit=80)
    return sum(int(item.get("context_size_tokens") or 0) for item in activity)


def build_noise_document(topic: str, index: int, *, paragraphs: int) -> str:
    base = (
        "This benchmark document is intentionally padded with realistic software delivery prose about context windows, "
        "handoffs, packaging, release management, architecture drift, tests, telemetry, and local-first operator layers."
    )
    lines = [f"Noise Topic: {topic} {index:03d}", ""]
    for paragraph in range(paragraphs):
        lines.append(
            f"{base} Paragraph {paragraph + 1} for {topic} {index:03d}. It discusses UI, packaging, owners, sessions, "
            "performance, routing, and memory compaction without containing the exact benchmark facts."
        )
    return "\n".join(lines) + "\n"


def copy_repo_corpus(corpus_root: Path) -> list[Path]:
    paths: list[Path] = []
    for rel in REPO_CORPUS:
        source = REPO_ROOT / rel
        target = corpus_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        paths.append(target)
    return paths


def write_noise_files(root: Path, *, count: int, paragraphs: int, topic: str) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for index in range(1, count + 1):
        path = root / f"noise_{index:03d}.txt"
        path.write_text(build_noise_document(topic, index, paragraphs=paragraphs), encoding="utf-8")
        created.append(path)
    return created

def latest_tagged_value(texts: dict[str, str], index_key: str, value_key: str) -> str:
    winner_index = -1
    winner_value = ""
    for text in texts.values():
        index_match = re.search(fr"{re.escape(index_key)}=(\d+)", text)
        value_match = re.search(fr"{re.escape(value_key)}=([^\r\n|]+)", text)
        if not index_match or not value_match:
            continue
        current_index = int(index_match.group(1))
        if current_index >= winner_index:
            winner_index = current_index
            winner_value = value_match.group(1).strip()
    if winner_index < 0:
        raise RuntimeError(f"Missing {value_key}")
    return winner_value


def combined_latest_values(texts: dict[str, str], index_key: str, value_keys: list[str]) -> str:
    return " | ".join(latest_tagged_value(texts, index_key, key) for key in value_keys)


def repo_workloads() -> list[dict]:
    return [
        {
            "title": "Default Genesis trust mode",
            "query": "DEFAULT_GENESIS_POLICY approval-required",
            "prompt": "Search the repo artifacts and return the default Genesis trust mode only.",
            "expected": "approval-required",
            "resolver": lambda texts: regex_extract(
                r'DEFAULT_GENESIS_POLICY\s*=\s*\{\s*"mode":\s*"([^"]+)"',
                joined_texts(texts),
                flags=re.S,
            ),
        },
        {
            "title": "QA gate after developer",
            "query": "Quality Assurance Manager reviewer",
            "prompt": "Search the repo artifacts and return the agent role that hard-gates normal worker completion before delivery.",
            "expected": "Quality Assurance Manager",
            "resolver": lambda texts: "Quality Assurance Manager" if "Quality Assurance Manager" in joined_texts(texts) else (_ for _ in ()).throw(RuntimeError("QA gate not found")),
        },
        {
            "title": "Packaged MCP command",
            "query": "native MCP server pexo-mcp",
            "prompt": "Search the repo artifacts and return the packaged command that starts Pexo as a native MCP server.",
            "expected": "pexo-mcp",
            "resolver": lambda texts: regex_extract(
                r'(pexo-mcp)\s+Starts Pexo as a native MCP server',
                joined_texts(texts),
            ),
        },
        {
            "title": "Keep-state uninstall command",
            "query": "keep-state uninstall preserve local state",
            "prompt": "Search the repo artifacts and return the command that removes Pexo but preserves local state.",
            "expected": "pexo uninstall --keep-state",
            "resolver": lambda texts: regex_extract(r'(pexo uninstall --keep-state)', joined_texts(texts)),
        },
        {
            "title": "Checkout mutable state directory",
            "query": "Checkout mode keeps mutable state under",
            "prompt": "Search the repo artifacts and return the checkout-mode mutable state directory only.",
            "expected": ".pexo",
            "resolver": lambda texts: regex_extract(r'Checkout mode keeps mutable state under the repo-local `([^`]+)` directory', joined_texts(texts)),
        },
        {
            "title": "Default memory backend",
            "query": "SQLite keyword-backed retrieval",
            "prompt": "Search the repo artifacts and return the default local memory backend only.",
            "expected": "SQLite",
            "resolver": lambda texts: regex_extract(r'local memory uses ([A-Za-z0-9_-]+) and keyword-backed retrieval by default', joined_texts(texts)),
        },
    ]


def build_timeline_suite(corpus_root: Path) -> tuple[list[Path], list[str], list[dict]]:
    phases_dir = corpus_root / "timeline"
    phases_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    memories: list[str] = []

    for phase in range(1, 37):
        ui_stack = "static_html" if phase < 13 else "astro_static" if phase < 25 else "nextjs_app_router"
        packaging = "pipx_only" if phase < 10 else "wheel_overlay" if phase < 22 else "release_bundle"
        owner = "chat_shell" if phase < 15 else "shared-state" if phase < 28 else "operator-control"
        gate = "Developer" if phase < 12 else "Reviewer" if phase < 24 else "Quality Assurance Manager"
        rejected = "direct_chat_first" if phase < 18 else "swarm_by_default" if phase < 30 else "vector_by_default"
        note = [
            f"Timeline checkpoint {phase:02d}",
            f"TIMELINE_PHASE={phase:02d}",
            f"CURRENT_UI_STACK={ui_stack}",
            f"CURRENT_PACKAGING={packaging}",
            f"CURRENT_OWNER={owner}",
            f"CURRENT_GATE={gate}",
            f"REJECTED_OPTION={rejected}",
            f"Current UI stack: {ui_stack}.",
            f"Current packaging path: {packaging}.",
            f"Current product owner mode: {owner}.",
            f"Current required gate: {gate}.",
            f"Rejected default: {rejected}.",
            "",
        ]
        filler = (
            "This phase log captures how a real operator layer evolves over time. "
            "It contains design debates, install-path notes, UI revisions, routing choices, failure modes, and packaging tradeoffs."
        )
        for paragraph in range(850):
            note.append(f"{filler} Phase {phase:02d} paragraph {paragraph + 1}.")
        path = phases_dir / f"phase_{phase:02d}.txt"
        path.write_text("\n".join(note) + "\n", encoding="utf-8")
        paths.append(path)

        if phase % 4 == 0:
            memories.append(
                " | ".join(
                    [
                        f"TIMELINE_PHASE={phase:02d}",
                        f"CURRENT_UI_STACK={ui_stack}",
                        f"CURRENT_PACKAGING={packaging}",
                        f"CURRENT_OWNER={owner}",
                        f"CURRENT_GATE={gate}",
                        f"REJECTED_OPTION={rejected}",
                    ]
                )
            )

    workloads = [
        {
            "title": "Current UI stack",
            "query": "Current UI stack CURRENT_UI_STACK",
            "prompt": "Use the stored product-history artifacts and return the current UI stack only.",
            "expected": "nextjs_app_router",
            "resolver": lambda texts: latest_tagged_value(texts, "TIMELINE_PHASE", "CURRENT_UI_STACK"),
        },
        {
            "title": "Current packaging path",
            "query": "Current packaging path CURRENT_PACKAGING",
            "prompt": "Use the stored product-history artifacts and return the current packaging path only.",
            "expected": "release_bundle",
            "resolver": lambda texts: latest_tagged_value(texts, "TIMELINE_PHASE", "CURRENT_PACKAGING"),
        },
        {
            "title": "Current owner mode",
            "query": "Current product owner mode CURRENT_OWNER",
            "prompt": "Use the stored product-history artifacts and return the current owner mode only.",
            "expected": "operator-control",
            "resolver": lambda texts: latest_tagged_value(texts, "TIMELINE_PHASE", "CURRENT_OWNER"),
        },
        {
            "title": "Current required gate",
            "query": "Current required gate CURRENT_GATE",
            "prompt": "Use the stored product-history artifacts and return the current required gate only.",
            "expected": "Quality Assurance Manager",
            "resolver": lambda texts: latest_tagged_value(texts, "TIMELINE_PHASE", "CURRENT_GATE"),
        },
        {
            "title": "Rejected default option",
            "query": "Rejected default REJECTED_OPTION",
            "prompt": "Use the stored product-history artifacts and return the rejected default option only.",
            "expected": "vector_by_default",
            "resolver": lambda texts: latest_tagged_value(texts, "TIMELINE_PHASE", "REJECTED_OPTION"),
        },
        {
            "title": "Combined latest product direction",
            "query": "CURRENT_UI_STACK CURRENT_PACKAGING CURRENT_OWNER",
            "prompt": "Use the stored product-history artifacts and return the current UI stack, packaging path, and owner mode separated by ` | `.",
            "expected": "nextjs_app_router | release_bundle | operator-control",
            "resolver": lambda texts: combined_latest_values(texts, "TIMELINE_PHASE", ["CURRENT_UI_STACK", "CURRENT_PACKAGING", "CURRENT_OWNER"]),
        },
    ]
    return paths, memories, workloads

def build_handoff_suite(corpus_root: Path) -> tuple[list[Path], list[str], list[dict]]:
    handoff_dir = corpus_root / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    memories: list[str] = []
    actors = ["codex", "gemini", "claude"]

    for batch in range(1, 43):
        actor = actors[(batch - 1) % len(actors)]
        issue = "install_drift" if batch < 13 else "ui_overload" if batch < 27 else "mcp_stability"
        gate = "Reviewer" if batch < 14 else "Code Organization Manager" if batch < 28 else "Quality Assurance Manager"
        deploy_target = "repo_checkout" if batch < 16 else "packaged_preview" if batch < 30 else "packaged_release"
        fallback_client = "codex" if batch < 12 else "claude" if batch < 25 else "gemini"
        owner = "assistant-shell" if batch < 18 else "shared-context" if batch < 31 else "operator-control"
        constraint = "preserve_local_state" if batch < 21 else "minimize_context_replay" if batch < 33 else "keep_local_state_authoritative"
        note = [
            f"Client handoff batch {batch:02d}",
            f"HANDOFF_BATCH={batch:02d}",
            f"HANDOFF_ACTOR={actor}",
            f"HANDOFF_CURRENT_ISSUE={issue}",
            f"HANDOFF_NEXT_GATE={gate}",
            f"HANDOFF_DEPLOY_TARGET={deploy_target}",
            f"HANDOFF_FALLBACK_CLIENT={fallback_client}",
            f"HANDOFF_OWNER={owner}",
            f"HANDOFF_CONSTRAINT={constraint}",
            f"Current issue: {issue}.",
            f"Next gate: {gate}.",
            f"Deploy target: {deploy_target}.",
            f"Fallback client: {fallback_client}.",
            f"Current owner: {owner}.",
            f"Primary operating constraint: {constraint}.",
            "",
        ]
        filler = (
            "This handoff log simulates real multi-client relay work across Codex, Gemini, and Claude. "
            "It captures the active issue, the required gate, the deployment target, the fallback client, and the operator constraint."
        )
        for paragraph in range(950):
            note.append(f"{filler} Batch {batch:02d} paragraph {paragraph + 1}.")
        path = handoff_dir / f"handoff_{batch:02d}_{actor}.txt"
        path.write_text("\n".join(note) + "\n", encoding="utf-8")
        paths.append(path)

        if batch % 3 == 0:
            memories.append(
                " | ".join(
                    [
                        f"HANDOFF_BATCH={batch:02d}",
                        f"HANDOFF_CURRENT_ISSUE={issue}",
                        f"HANDOFF_NEXT_GATE={gate}",
                        f"HANDOFF_DEPLOY_TARGET={deploy_target}",
                        f"HANDOFF_FALLBACK_CLIENT={fallback_client}",
                        f"HANDOFF_OWNER={owner}",
                        f"HANDOFF_CONSTRAINT={constraint}",
                    ]
                )
            )

    workloads = [
        {
            "title": "Current issue across handoffs",
            "query": "Current issue HANDOFF_CURRENT_ISSUE",
            "prompt": "Use the stored client-handoff history and return the current issue only.",
            "expected": "mcp_stability",
            "resolver": lambda texts: latest_tagged_value(texts, "HANDOFF_BATCH", "HANDOFF_CURRENT_ISSUE"),
        },
        {
            "title": "Current required gate across handoffs",
            "query": "Next gate HANDOFF_NEXT_GATE",
            "prompt": "Use the stored client-handoff history and return the current required gate only.",
            "expected": "Quality Assurance Manager",
            "resolver": lambda texts: latest_tagged_value(texts, "HANDOFF_BATCH", "HANDOFF_NEXT_GATE"),
        },
        {
            "title": "Current deploy target across handoffs",
            "query": "Deploy target HANDOFF_DEPLOY_TARGET",
            "prompt": "Use the stored client-handoff history and return the current deploy target only.",
            "expected": "packaged_release",
            "resolver": lambda texts: latest_tagged_value(texts, "HANDOFF_BATCH", "HANDOFF_DEPLOY_TARGET"),
        },
        {
            "title": "Fallback client after handoff",
            "query": "Fallback client HANDOFF_FALLBACK_CLIENT",
            "prompt": "Use the stored client-handoff history and return the fallback client only.",
            "expected": "gemini",
            "resolver": lambda texts: latest_tagged_value(texts, "HANDOFF_BATCH", "HANDOFF_FALLBACK_CLIENT"),
        },
        {
            "title": "Current owner mode after handoff",
            "query": "Current owner HANDOFF_OWNER",
            "prompt": "Use the stored client-handoff history and return the current owner mode only.",
            "expected": "operator-control",
            "resolver": lambda texts: latest_tagged_value(texts, "HANDOFF_BATCH", "HANDOFF_OWNER"),
        },
        {
            "title": "Combined current handoff state",
            "query": "HANDOFF_CURRENT_ISSUE HANDOFF_NEXT_GATE HANDOFF_DEPLOY_TARGET HANDOFF_FALLBACK_CLIENT",
            "prompt": "Use the stored client-handoff history and return the current issue, required gate, deploy target, and fallback client separated by ` | `.",
            "expected": "mcp_stability | Quality Assurance Manager | packaged_release | gemini",
            "resolver": lambda texts: combined_latest_values(texts, "HANDOFF_BATCH", ["HANDOFF_CURRENT_ISSUE", "HANDOFF_NEXT_GATE", "HANDOFF_DEPLOY_TARGET", "HANDOFF_FALLBACK_CLIENT"]),
        },
    ]
    return paths, memories, workloads


def run_suite(
    *,
    slug: str,
    title: str,
    description: str,
    what_it_tests: str,
    corpus_paths: list[Path],
    corpus_root: Path,
    memory_entries: list[str],
    workloads: list[dict],
) -> SuiteResult:
    state_root = SUITE_ROOT / "state" / slug
    prepare_runtime(state_root)
    task_context = f"benchmark-{slug}"
    artifact_session = f"{slug}-artifacts"
    corpus_bytes = sum(path.stat().st_size for path in corpus_paths)
    direct_tokens = estimate_tokens(corpus_bytes) * len(workloads)

    def direct_runner() -> list[str]:
        answers: list[str] = []
        for workload in workloads:
            answers.append(safe_resolve(workload["resolver"], read_text_map(corpus_paths, corpus_root)))
        return answers

    direct_answers, direct_metrics = measure_phase(direct_runner)

    def pexo_setup_runner() -> None:
        register_paths(corpus_paths, corpus_root, task_context=task_context, session_id=artifact_session)
        if memory_entries:
            store_memory_entries(memory_entries, task_context=task_context, session_prefix=slug)

    _, pexo_setup_metrics = measure_phase(pexo_setup_runner)

    def pexo_query_runner() -> tuple[list[str], int, list[str]]:
        answers: list[str] = []
        session_ids: list[str] = []
        token_total = 0
        for index, workload in enumerate(workloads, start=1):
            retrieved = collect_pexo_texts(workload["query"], task_context=task_context)
            answer = safe_resolve(workload["resolver"], retrieved)
            session_id = f"{slug}-task-{index:02d}"
            token_total += start_and_complete_task(workload["prompt"], answer, session_id)
            answers.append(answer)
            session_ids.append(session_id)
        return answers, token_total, session_ids

    pexo_result, pexo_query_metrics = measure_phase(pexo_query_runner)
    pexo_answers, pexo_tokens, session_ids = pexo_result
    pexo_total_metrics = phase_metrics_sum(pexo_setup_metrics, pexo_query_metrics)
    pexo_state_mb = round(directory_size_bytes(current_state_root()) / (1024 * 1024), 2)

    workload_results: list[WorkloadResult] = []
    correct_count = 0
    for workload, direct_answer, pexo_answer, session_id in zip(workloads, direct_answers, pexo_answers, session_ids):
        correct = direct_answer == workload["expected"] and pexo_answer == workload["expected"]
        correct_count += int(correct)
        workload_results.append(
            WorkloadResult(
                title=workload["title"],
                query=workload["query"],
                expected_answer=workload["expected"],
                direct_answer=direct_answer,
                pexo_answer=pexo_answer,
                correct=correct,
                session_id=session_id,
            )
        )

    return SuiteResult(
        slug=slug,
        title=title,
        description=description,
        what_it_tests=what_it_tests,
        workload_count=len(workloads),
        corpus_bytes=corpus_bytes,
        direct_tokens=direct_tokens,
        pexo_tokens=pexo_tokens,
        reduction_factor=round(direct_tokens / max(pexo_tokens, 1), 2),
        retained_pct=round((pexo_tokens / max(direct_tokens, 1)) * 100, 4),
        exact_match_accuracy_pct=round((correct_count / max(len(workloads), 1)) * 100, 2),
        direct_metrics=direct_metrics,
        pexo_setup_metrics=pexo_setup_metrics,
        pexo_query_metrics=pexo_query_metrics,
        pexo_total_metrics=pexo_total_metrics,
        pexo_state_mb=pexo_state_mb,
        workload_results=workload_results,
    )

def build_repo_suite(corpus_root: Path) -> tuple[list[Path], list[str], list[dict]]:
    repo_dir = corpus_root / "repo"
    noise_dir = corpus_root / "noise"
    repo_paths = copy_repo_corpus(repo_dir)
    noise_paths = write_noise_files(noise_dir, count=72, paragraphs=1200, topic="operator-context-noise")
    return repo_paths + noise_paths, [], repo_workloads()


def suite_markdown(suite: dict) -> list[str]:
    lines = [
        f"### {suite['title']}",
        "",
        f"{suite['description']}",
        "",
        f"- What it tests: {suite['what_it_tests']}",
        f"- Corpus size: `{_format_number(suite['corpus_bytes'])}` bytes",
        f"- Workloads: `{suite['workload_count']}`",
        f"- Direct replay context: `{_format_number(suite['direct_tokens'])}` tokens",
        f"- Pexo session context: `{_format_number(suite['pexo_tokens'])}` tokens",
        f"- Reduction: `{suite['reduction_factor']:.2f}x`",
        f"- Exact-match accuracy: `{suite['exact_match_accuracy_pct']:.2f}%`",
        "",
        "| Workload | Expected | Direct | Pexo | Match |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for item in suite["workload_results"]:
        lines.append(
            f"| {item['title']} | `{item['expected_answer']}` | `{item['direct_answer']}` | `{item['pexo_answer']}` | {'yes' if item['correct'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "| Metric | Direct | Pexo Setup | Pexo Query | Pexo Total |",
            "| :--- | ---: | ---: | ---: | ---: |",
            f"| Wall time | `{format_seconds(suite['direct_metrics']['wall_seconds'])}` | `{format_seconds(suite['pexo_setup_metrics']['wall_seconds'])}` | `{format_seconds(suite['pexo_query_metrics']['wall_seconds'])}` | `{format_seconds(suite['pexo_total_metrics']['wall_seconds'])}` |",
            f"| CPU time | `{format_seconds(suite['direct_metrics']['cpu_seconds'])}` | `{format_seconds(suite['pexo_setup_metrics']['cpu_seconds'])}` | `{format_seconds(suite['pexo_query_metrics']['cpu_seconds'])}` | `{format_seconds(suite['pexo_total_metrics']['cpu_seconds'])}` |",
            f"| Peak RSS | `{suite['direct_metrics']['peak_rss_mb']:.2f} MB` | `{suite['pexo_setup_metrics']['peak_rss_mb']:.2f} MB` | `{suite['pexo_query_metrics']['peak_rss_mb']:.2f} MB` | `{suite['pexo_total_metrics']['peak_rss_mb']:.2f} MB` |",
            "",
            f"Pexo state footprint after this suite: `{suite['pexo_state_mb']:.2f} MB`.",
            "",
        ]
    )
    return lines


def build_markdown(results: dict) -> str:
    host = results["host"]
    runtime = results["runtime"]
    suites = results["suites"]
    summary = results["summary"]
    total_direct_wall = sum(float(suite["direct_metrics"]["wall_seconds"]) for suite in suites)
    total_pexo_wall = sum(float(suite["pexo_total_metrics"]["wall_seconds"]) for suite in suites)

    def before_cell(suite: dict) -> str:
        return "<br>".join(
            [
                suite["what_it_tests"],
                f"`{_format_number(suite['corpus_bytes'])}` bytes corpus",
                f"`{suite['workload_count']}` workloads",
                f"`{_format_number(suite['direct_tokens'])}` tokens",
                f"`{format_seconds(suite['direct_metrics']['wall_seconds'])}` direct time",
            ]
        )

    def after_cell(suite: dict) -> str:
        return "<br>".join(
            [
                f"`{_format_number(suite['pexo_tokens'])}` tokens",
                f"`{suite['reduction_factor']:.2f}x` reduction",
                f"`{suite['exact_match_accuracy_pct']:.2f}%` accuracy",
                f"`{format_seconds(suite['pexo_total_metrics']['wall_seconds'])}` Pexo time",
            ]
        )

    lines = [
        "## Benchmark Snapshot",
        "",
        "These are three fresh isolated real-world benchmark suites for **compression and recollection**.",
        "Each suite compares a naive direct-replay baseline against the same workload routed through Pexo's MCP surfaces.",
        "",
        "Methodology:",
        "- **Before Pexo** is the naive context load you would carry if you replayed the full corpus into the model path for every question.",
        "- **After Pexo** is the measured `context_size_tokens` recorded by the Pexo-managed sessions during the same workload.",
        "- **Accuracy** is exact-match against the expected answer for every workload in the suite.",
        "- Timing, CPU, RSS, and state footprint are direct local measurements on the host listed below.",
        "",
        "Raw benchmark artifacts:",
        "- `docs/benchmarks/realworld_compression_recollection_results.json`",
        "- `docs/benchmarks/realworld_compression_recollection_results.md`",
        "- `scripts/run_realworld_compression_recollection_benchmarks.py`",
        "",
        "### Host System",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| OS | `{host['os']}` |",
        f"| CPU | `{host['cpu']}` |",
        f"| Logical cores | `{host['logical_cores']}` |",
        f"| RAM | `{host['total_ram_gb']} GB` |",
        f"| Python | `{host['python']}` |",
        f"| Pexo version | `{host['pexo_version']}` |",
        f"| Memory backend | `{runtime['memory_backend']}` |",
        f"| Benchmark execution mode | `{runtime['install_mode']}` |",
        "",
        "### Summary",
        "",
        "| Suite | Before Pexo | After Pexo |",
        "| :--- | :--- | :--- |",
    ]
    for suite in suites:
        lines.append(
            f"| {suite['title']} | {before_cell(suite)} | {after_cell(suite)} |"
        )
    lines.extend(
        [
            "",
            "### Combined Totals",
            "",
            "| Metric | Before Pexo | After Pexo |",
            "| :--- | :--- | :--- |",
            f"| Corpus handled | `{_format_number(summary['total_corpus_bytes'])}` bytes | `{_format_number(summary['total_corpus_bytes'])}` bytes |",
            f"| Active context | `{_format_number(summary['total_direct_tokens'])}` tokens | `{_format_number(summary['total_pexo_tokens'])}` tokens |",
            f"| Total wall time | `{format_seconds(total_direct_wall)}` | `{format_seconds(total_pexo_wall)}` |",
            f"| Recollection quality | direct baseline replay | `{summary['overall_accuracy_pct']:.2f}%` exact-match accuracy |",
            f"| Net effect | full corpus replay every time | `{summary['overall_reduction_factor']:.2f}x` reduction, `{summary['overall_retained_pct']:.4f}%` retained |",
            "",
        ]
    )
    for suite in suites:
        lines.extend(suite_markdown(suite))
    lines.extend(
        [
            "How to read this:",
            "- Direct replay can still be faster for one-off local scans because it skips ingestion and retrieval work.",
            "- Pexo wins when the same project state needs to be carried across repeated questions, interruptions, or client handoffs without replaying the whole corpus.",
            "- These suites are intentionally large enough to make both the context savings and the recollection accuracy visible in one place.",
        ]
    )
    return "\n".join(lines) + "\n"


def replace_readme_section(readme_text: str, benchmark_markdown: str) -> str:
    start_marker = "## Benchmark Snapshot"
    end_marker = "## Install"
    start = readme_text.index(start_marker)
    end = readme_text.index(end_marker)
    return f"{readme_text[:start]}{benchmark_markdown}\n{readme_text[end:]}"

def main() -> int:
    random.seed(BENCHMARK_SEED)
    shutil.rmtree(SUITE_ROOT, ignore_errors=True)
    SUITE_ROOT.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    repo_root = SUITE_ROOT / "corpus_repo"
    timeline_root = SUITE_ROOT / "corpus_timeline"
    handoff_root = SUITE_ROOT / "corpus_handoff"

    repo_paths, repo_memories, repo_work = build_repo_suite(repo_root)
    timeline_paths, timeline_memories, timeline_work = build_timeline_suite(timeline_root)
    handoff_paths, handoff_memories, handoff_work = build_handoff_suite(handoff_root)

    suite_results = [
        run_suite(
            slug="repo-noise-retrieval",
            title="Massive Repo Retrieval",
            description="A real repo corpus plus heavy surrounding noise. The baseline rereads the whole corpus for every question; the Pexo path ingests once and recalls only the needed material.",
            what_it_tests="Large noisy codebase retrieval.",
            corpus_paths=repo_paths,
            corpus_root=repo_root,
            memory_entries=repo_memories,
            workloads=repo_work,
        ),
        run_suite(
            slug="timeline-recollection",
            title="Massive Timeline Recollection",
            description="A long sequence of large decision logs with changing accepted defaults over time. The job is to recall the final accepted state, not just find an old mention.",
            what_it_tests="Latest-state recollection across long histories.",
            corpus_paths=timeline_paths,
            corpus_root=timeline_root,
            memory_entries=timeline_memories,
            workloads=timeline_work,
        ),
        run_suite(
            slug="handoff-reconstruction",
            title="Massive Handoff Reconstruction",
            description="A multi-client handoff history where the active issue, next gate, deploy target, and fallback client evolve over many batches.",
            what_it_tests="Cross-client continuity and current-state reconstruction.",
            corpus_paths=handoff_paths,
            corpus_root=handoff_root,
            memory_entries=handoff_memories,
            workloads=handoff_work,
        ),
    ]

    runtime = build_runtime_status()
    host = build_host_specs()
    total_direct_tokens = sum(item.direct_tokens for item in suite_results)
    total_pexo_tokens = sum(item.pexo_tokens for item in suite_results)
    total_corpus_bytes = sum(item.corpus_bytes for item in suite_results)
    total_workloads = sum(item.workload_count for item in suite_results)
    correct_workloads = sum(
        1
        for suite in suite_results
        for item in suite.workload_results
        if item.correct
    )

    payload = {
        "generated_at_utc": now_utc(),
        "host": host,
        "runtime": runtime,
        "suites": [asdict(item) for item in suite_results],
        "summary": {
            "suite_count": len(suite_results),
            "total_corpus_bytes": total_corpus_bytes,
            "total_direct_tokens": total_direct_tokens,
            "total_pexo_tokens": total_pexo_tokens,
            "overall_reduction_factor": round(total_direct_tokens / max(total_pexo_tokens, 1), 2),
            "overall_retained_pct": round((total_pexo_tokens / max(total_direct_tokens, 1)) * 100, 4),
            "overall_accuracy_pct": round((correct_workloads / max(total_workloads, 1)) * 100, 2),
        },
    }

    markdown = build_markdown(payload)
    RESULTS_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    RESULTS_MD.write_text(markdown, encoding="utf-8")
    README_PATH.write_text(replace_readme_section(README_PATH.read_text(encoding="utf-8"), markdown), encoding="utf-8")
    shutil.rmtree(SUITE_ROOT, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
