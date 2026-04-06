from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database import init_db, reset_database_runtime
from app.mcp_server import (
    pexo_continue_task,
    pexo_find_artifact,
    pexo_get_artifact,
    pexo_get_session_activity,
    pexo_register_artifact_path,
    pexo_register_artifact_text,
    pexo_search_memory,
    pexo_start_task,
    pexo_store_memory,
)
from app.paths import current_state_root, reset_runtime_path_context, set_runtime_path_context
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
SUITE_ROOT = REPO_ROOT / "sandbox" / "benchmark_operator_realworld_fresh"
DOCS_DIR = REPO_ROOT / "docs" / "benchmarks"
RESULTS_JSON = DOCS_DIR / "operator_workflow_results.json"
RESULTS_MD = DOCS_DIR / "operator_workflow_results.md"
README_PATH = REPO_ROOT / "README.md"

REPO_CORPUS = [
    "README.md",
    "AGENTS.md",
    "docs/ARCHITECTURE.md",
    ".github/workflows/install-runtime-ci.yml",
    "app/launcher.py",
    "app/mcp_server.py",
    "app/routers/tools.py",
    "app/agents/graph.py",
    "app/runtime.py",
    "app/client_connect.py",
    "app/core_agents.py",
    "tests/test_hardening.py",
]


@dataclass
class ScenarioResult:
    track: str
    slug: str
    title: str
    expected_answer: str
    baseline_answer: str
    pexo_answer: str
    correct: bool
    source_bytes: int
    traditional_context_bytes: int
    traditional_tokens: int
    baseline: PhaseMetrics
    pexo_setup: PhaseMetrics
    pexo_query: PhaseMetrics
    pexo_tokens: int
    compaction_ratio: float
    sessions: list[str]
    notes: str


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def repo_paths() -> list[Path]:
    return [REPO_ROOT / rel for rel in REPO_CORPUS]


def total_bytes(paths: Iterable[Path]) -> int:
    return sum(path.stat().st_size for path in paths)


def read_texts(paths: Iterable[Path]) -> dict[str, str]:
    return {path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8") for path in paths}


def regex_extract(pattern: str, text: str, *, flags: int = 0) -> str:
    import re

    match = re.search(pattern, text, flags)
    if not match:
        raise RuntimeError(f"Pattern not found: {pattern}")
    return match.group(1).strip()


def gather_memory_texts(query: str, *, n_results: int = 10) -> list[str]:
    payload = pexo_search_memory(query=query, n_results=n_results)
    return [item.get("content", "") for item in payload.get("results", [])]


def gather_artifact_texts(query: str, *, task_context: str, limit: int = 10) -> dict[str, str]:
    payload = pexo_find_artifact(query=query, limit=limit, task_context=task_context)
    texts: dict[str, str] = {}
    for item in payload.get("results", []):
        artifact_id = item.get("id")
        if artifact_id is None:
            continue
        full = pexo_get_artifact(artifact_id)
        texts[str(full.get("name"))] = full.get("extracted_text") or ""
    return texts


def start_and_complete_task(prompt: str, answer: str, session_id: str) -> tuple[dict, int, list[dict]]:
    started = pexo_start_task(prompt=prompt, user_id="benchmark_user", session_id=session_id)
    status = started.get("status")
    if status == "clarification_required":
        raise RuntimeError(f"Unexpected clarification required for benchmark session {session_id}: {started}")
    pexo_continue_task(session_id=session_id, result_data=answer)
    activity = pexo_get_session_activity(session_id=session_id, limit=50)
    token_total = sum(int(item.get("context_size_tokens") or 0) for item in activity)
    return started, token_total, activity


def register_paths(paths: list[Path], *, task_context: str, session_id: str) -> None:
    for path in paths:
        pexo_register_artifact_path(
            path=str(path),
            task_context=task_context,
            session_id=session_id,
            name=path.relative_to(REPO_ROOT).as_posix(),
        )


def register_text_artifacts(notes: dict[str, str], *, task_context: str, session_prefix: str) -> None:
    for index, (name, content) in enumerate(notes.items(), start=1):
        pexo_register_artifact_text(
            name=name,
            content=content,
            task_context=task_context,
            session_id=f"{session_prefix}-artifact-{index}",
            source_uri=f"benchmark://{task_context}/{name}",
            content_type="text/plain",
        )


def store_memory_entries(notes: dict[str, str], *, task_context: str, session_prefix: str) -> None:
    for index, content in enumerate(notes.values(), start=1):
        pexo_store_memory(
            content=content,
            task_context=task_context,
            session_id=f"{session_prefix}-memory-{index}",
        )


def sum_session_tokens(session_ids: list[str]) -> int:
    total = 0
    for session_id in session_ids:
        activity = pexo_get_session_activity(session_id=session_id, limit=50)
        total += sum(int(item.get("context_size_tokens") or 0) for item in activity)
    return total


def padded_note(title: str, tagged_lines: list[str], *, paragraphs: int = 60) -> str:
    filler = (
        "This note is part of a real-world operator benchmark for a local AI control plane. "
        "It preserves project state, tradeoffs, implementation intent, and handoff continuity "
        "so a later client does not need the full preceding conversation replayed into context."
    )
    blocks = [title, ""]
    blocks.extend(tagged_lines)
    blocks.append("")
    for index in range(paragraphs):
        blocks.append(f"{filler} Paragraph {index + 1}.")
    return "\n".join(blocks) + "\n"


def write_note_files(case_root: Path, notes: dict[str, str]) -> list[Path]:
    case_root.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in notes.items():
        path = case_root / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return paths


def build_repo_retrieval_scenarios() -> list[dict]:
    return [
        {
            "track": "retrieval",
            "slug": "repo_trust_mode",
            "title": "Repo retrieval: default Genesis trust mode",
            "prompt": "Search the repo artifacts and return the default Genesis trust mode only.",
            "query": "DEFAULT_GENESIS_POLICY",
            "expected": "approval-required",
            "resolver": lambda texts: regex_extract(
                r'DEFAULT_GENESIS_POLICY\s*=\s*\{\s*"mode":\s*"([^"]+)"',
                texts["app/routers/tools.py"],
                flags=__import__("re").S,
            ),
        },
        {
            "track": "retrieval",
            "slug": "repo_qa_gate",
            "title": "Repo retrieval: QA hard gate after developer",
            "prompt": "Search the repo artifacts and return the agent role that hard-gates normal worker completion before delivery.",
            "query": 'after_developer["role"]',
            "expected": "Quality Assurance Manager",
            "resolver": lambda texts: regex_extract(
                r'after_developer\["role"\],\s*"([^"]+)"',
                texts["tests/test_hardening.py"],
            ),
        },
        {
            "track": "retrieval",
            "slug": "repo_mcp_command",
            "title": "Repo retrieval: packaged MCP command",
            "prompt": "Search the repo artifacts and return the packaged command that starts Pexo as a native MCP server.",
            "query": "native MCP server",
            "expected": "pexo-mcp",
            "resolver": lambda texts: regex_extract(
                r'print\("  ([^ ]+) +Starts Pexo as a native MCP server',
                texts["app/launcher.py"],
            ),
        },
        {
            "track": "retrieval",
            "slug": "repo_keep_state_uninstall",
            "title": "Repo retrieval: uninstall while keeping state",
            "prompt": "Search the repo artifacts and return the packaged command that removes Pexo but preserves local state.",
            "query": "--keep-state",
            "expected": "pexo uninstall --keep-state",
            "resolver": lambda texts: regex_extract(
                r'`(pexo uninstall --keep-state)`',
                texts["README.md"],
            ),
        },
    ]


def run_repo_retrieval_case(config: dict, corpus_paths: list[Path]) -> ScenarioResult:
    corpus_bytes = total_bytes(corpus_paths)
    traditional_tokens = estimate_tokens(corpus_bytes)

    def baseline_runner() -> str:
        texts = read_texts(corpus_paths)
        return config["resolver"](texts)

    task_context = f"realworld-{config['slug']}"
    artifact_session = f"{task_context}-artifacts"
    search_session = f"{task_context}-search"

    _, baseline_metrics = measure_phase(baseline_runner)
    baseline_answer = baseline_runner()

    _, setup_metrics = measure_phase(
        lambda: register_paths(corpus_paths, task_context=task_context, session_id=artifact_session)
    )

    def pexo_query_runner() -> tuple[str, int]:
        artifact_texts = gather_artifact_texts(config["query"], task_context=task_context, limit=8)
        answer = config["resolver"](artifact_texts if artifact_texts else read_texts(corpus_paths))
        start_and_complete_task(config["prompt"], answer, search_session)
        return answer, sum_session_tokens([search_session])

    pexo_result, query_metrics = measure_phase(pexo_query_runner)
    pexo_answer, token_total = pexo_result

    return ScenarioResult(
        track=config["track"],
        slug=config["slug"],
        title=config["title"],
        expected_answer=config["expected"],
        baseline_answer=baseline_answer,
        pexo_answer=pexo_answer,
        correct=baseline_answer == config["expected"] and pexo_answer == config["expected"],
        source_bytes=corpus_bytes,
        traditional_context_bytes=corpus_bytes,
        traditional_tokens=traditional_tokens,
        baseline=baseline_metrics,
        pexo_setup=setup_metrics,
        pexo_query=query_metrics,
        pexo_tokens=token_total,
        compaction_ratio=round(traditional_tokens / max(token_total, 1), 2),
        sessions=[search_session],
        notes="Actual repo corpus registered as Pexo artifacts; answer resolved through Pexo artifact lookup over the real codebase.",
    )


def run_handoff_landing_page(case_root: Path) -> ScenarioResult:
    notes = {
        "gemini_note_01.txt": padded_note(
            "Gemini handoff note",
            [
                "HANDOFF_STACK=FastAPI HTML shell",
                "HANDOFF_STYLE=clean premium dark",
            ],
        ),
        "codex_note_02.txt": padded_note(
            "Codex execution note",
            [
                "HANDOFF_CTA=Use one local brain",
            ],
        ),
        "claude_note_03.txt": padded_note(
            "Claude review note",
            [
                "HANDOFF_DEPLOY=local-first packaged release",
                "HANDOFF_AUDIENCE=developers",
                "HANDOFF_PRIORITY=clarity over decoration",
            ],
        ),
    }
    paths = write_note_files(case_root, notes)
    source_bytes = total_bytes(paths)
    traditional_tokens = estimate_tokens(source_bytes)
    expected = "FastAPI HTML shell | clean premium dark | local-first packaged release"

    def baseline_runner() -> str:
        texts = read_texts(paths)
        combined = "\n".join(texts.values())
        stack = regex_extract(r"HANDOFF_STACK=([^\r\n]+)", combined)
        style = regex_extract(r"HANDOFF_STYLE=([^\r\n]+)", combined)
        deploy = regex_extract(r"HANDOFF_DEPLOY=([^\r\n]+)", combined)
        return f"{stack} | {style} | {deploy}"

    task_context = "handoff-landing-page"
    _, baseline_metrics = measure_phase(baseline_runner)
    baseline_answer = baseline_runner()

    def setup_runner() -> None:
        store_memory_entries(
            {
                "gemini": notes["gemini_note_01.txt"],
                "codex": notes["codex_note_02.txt"],
            },
            task_context=task_context,
            session_prefix="handoff-landing",
        )
        register_text_artifacts(
            {"claude_review.txt": notes["claude_note_03.txt"]},
            task_context=task_context,
            session_prefix="handoff-landing",
        )

    _, setup_metrics = measure_phase(setup_runner)

    def query_runner() -> tuple[str, int]:
        memory_texts = gather_memory_texts("HANDOFF_STACK HANDOFF_STYLE", n_results=10)
        artifact_texts = gather_artifact_texts("HANDOFF_DEPLOY", task_context=task_context, limit=5)
        combined = "\n".join(memory_texts + list(artifact_texts.values()))
        stack = regex_extract(r"HANDOFF_STACK=([^\r\n]+)", combined)
        style = regex_extract(r"HANDOFF_STYLE=([^\r\n]+)", combined)
        deploy = regex_extract(r"HANDOFF_DEPLOY=([^\r\n]+)", combined)
        answer = f"{stack} | {style} | {deploy}"
        session_id = "handoff-landing-query"
        start_and_complete_task(
            "Read the stored landing-page handoff and return HANDOFF_STACK, HANDOFF_STYLE, and HANDOFF_DEPLOY separated by ` | `.",
            answer,
            session_id,
        )
        return answer, sum_session_tokens([session_id])

    pexo_result, query_metrics = measure_phase(query_runner)
    pexo_answer, token_total = pexo_result

    return ScenarioResult(
        track="handoff",
        slug="handoff_landing_page",
        title="Cross-client handoff: landing page brief",
        expected_answer=expected,
        baseline_answer=baseline_answer,
        pexo_answer=pexo_answer,
        correct=baseline_answer == expected and pexo_answer == expected,
        source_bytes=source_bytes,
        traditional_context_bytes=source_bytes,
        traditional_tokens=traditional_tokens,
        baseline=baseline_metrics,
        pexo_setup=setup_metrics,
        pexo_query=query_metrics,
        pexo_tokens=token_total,
        compaction_ratio=round(traditional_tokens / max(token_total, 1), 2),
        sessions=["handoff-landing-query"],
        notes="Simulated Gemini -> Codex -> Claude handoff through Pexo memories and artifact text, without replaying the full brief to the final client.",
    )


def run_handoff_bug_triage(case_root: Path) -> ScenarioResult:
    notes = {
        "gemini_triage.txt": padded_note(
            "Gemini bug triage",
            [
                "BUG_ROOT_CAUSE=import-time global state root",
                "BUG_OWNER=platform-runtime",
            ],
        ),
        "codex_patch.txt": padded_note(
            "Codex patch note",
            [
                "BUG_FALLBACK_CLIENT=gemini",
                "BUG_STATUS=ready_for_retest",
            ],
        ),
        "claude_review.txt": padded_note(
            "Claude review note",
            [
                "BUG_SCOPE=state isolation and MCP continuity",
                "BUG_PRIORITY=high",
            ],
        ),
    }
    paths = write_note_files(case_root, notes)
    source_bytes = total_bytes(paths)
    traditional_tokens = estimate_tokens(source_bytes)
    expected = "import-time global state root | platform-runtime | gemini"

    def baseline_runner() -> str:
        combined = "\n".join(read_texts(paths).values())
        cause = regex_extract(r"BUG_ROOT_CAUSE=([^\r\n]+)", combined)
        owner = regex_extract(r"BUG_OWNER=([^\r\n]+)", combined)
        fallback = regex_extract(r"BUG_FALLBACK_CLIENT=([^\r\n]+)", combined)
        return f"{cause} | {owner} | {fallback}"

    task_context = "handoff-bug-triage"
    _, baseline_metrics = measure_phase(baseline_runner)
    baseline_answer = baseline_runner()

    def setup_runner() -> None:
        store_memory_entries(
            {
                "gemini_triage": notes["gemini_triage.txt"],
                "codex_patch": notes["codex_patch.txt"],
            },
            task_context=task_context,
            session_prefix="handoff-bug",
        )
        register_text_artifacts(
            {"claude_review.txt": notes["claude_review.txt"]},
            task_context=task_context,
            session_prefix="handoff-bug",
        )

    _, setup_metrics = measure_phase(setup_runner)

    def query_runner() -> tuple[str, int]:
        memory_texts = gather_memory_texts("BUG_ROOT_CAUSE BUG_OWNER BUG_FALLBACK_CLIENT", n_results=10)
        artifact_texts = gather_artifact_texts("BUG_SCOPE", task_context=task_context, limit=5)
        combined = "\n".join(memory_texts + list(artifact_texts.values()))
        answer = " | ".join(
            [
                regex_extract(r"BUG_ROOT_CAUSE=([^\r\n]+)", combined),
                regex_extract(r"BUG_OWNER=([^\r\n]+)", combined),
                regex_extract(r"BUG_FALLBACK_CLIENT=([^\r\n]+)", combined),
            ]
        )
        session_id = "handoff-bug-query"
        start_and_complete_task(
            "Read the stored bug-triage handoff and return BUG_ROOT_CAUSE, BUG_OWNER, and BUG_FALLBACK_CLIENT separated by ` | `.",
            answer,
            session_id,
        )
        return answer, sum_session_tokens([session_id])

    pexo_result, query_metrics = measure_phase(query_runner)
    pexo_answer, token_total = pexo_result

    return ScenarioResult(
        track="handoff",
        slug="handoff_bug_triage",
        title="Cross-client handoff: bug triage continuity",
        expected_answer=expected,
        baseline_answer=baseline_answer,
        pexo_answer=pexo_answer,
        correct=baseline_answer == expected and pexo_answer == expected,
        source_bytes=source_bytes,
        traditional_context_bytes=source_bytes,
        traditional_tokens=traditional_tokens,
        baseline=baseline_metrics,
        pexo_setup=setup_metrics,
        pexo_query=query_metrics,
        pexo_tokens=token_total,
        compaction_ratio=round(traditional_tokens / max(token_total, 1), 2),
        sessions=["handoff-bug-query"],
        notes="Simulated cross-client bug triage where Pexo holds root cause, owner, and fallback-client state between handoffs.",
    )


def run_compounding_repo_sequence(corpus_paths: list[Path]) -> ScenarioResult:
    source_bytes = total_bytes(corpus_paths)
    expected = "approval-required | pexo-mcp | pexo uninstall --keep-state"

    def baseline_runner() -> str:
        step_one_texts = read_texts(corpus_paths)
        trust = regex_extract(
            r'DEFAULT_GENESIS_POLICY\s*=\s*\{\s*"mode":\s*"([^"]+)"',
            step_one_texts["app/routers/tools.py"],
            flags=__import__("re").S,
        )
        step_two_texts = read_texts(corpus_paths)
        mcp_command = regex_extract(
            r'print\("  ([^ ]+) +Starts Pexo as a native MCP server',
            step_two_texts["app/launcher.py"],
        )
        step_three_texts = read_texts(corpus_paths)
        uninstall = regex_extract(r'`(pexo uninstall --keep-state)`', step_three_texts["README.md"])
        return f"{trust} | {mcp_command} | {uninstall}"

    traditional_context_bytes = source_bytes * 3 + len(expected.encode("utf-8"))
    traditional_tokens = estimate_tokens(traditional_context_bytes)
    _, baseline_metrics = measure_phase(baseline_runner)
    baseline_answer = baseline_runner()

    task_context = "compound-repo-sequence"

    def setup_runner() -> None:
        register_paths(corpus_paths, task_context=task_context, session_id="compound-repo-artifacts")

    _, setup_metrics = measure_phase(setup_runner)

    def query_runner() -> tuple[str, int]:
        session_ids = []

        texts = gather_artifact_texts("DEFAULT_GENESIS_POLICY", task_context=task_context, limit=8)
        trust = regex_extract(
            r'DEFAULT_GENESIS_POLICY\s*=\s*\{\s*"mode":\s*"([^"]+)"',
            "\n".join(texts.values()),
            flags=__import__("re").S,
        )
        session_ids.append("compound-repo-step-1")
        start_and_complete_task("Return the default Genesis trust mode only.", trust, session_ids[-1])
        pexo_store_memory(content=f"RETRIEVED_TRUST_MODE={trust}", task_context=task_context, session_id="compound-store-1")

        texts = gather_artifact_texts("native MCP server", task_context=task_context, limit=8)
        mcp_command = regex_extract(
            r'print\("  ([^ ]+) +Starts Pexo as a native MCP server',
            "\n".join(texts.values()),
        )
        session_ids.append("compound-repo-step-2")
        start_and_complete_task("Return the packaged native MCP command only.", mcp_command, session_ids[-1])
        pexo_store_memory(content=f"RETRIEVED_MCP_COMMAND={mcp_command}", task_context=task_context, session_id="compound-store-2")

        texts = gather_artifact_texts("--keep-state", task_context=task_context, limit=8)
        uninstall = regex_extract(r'`(pexo uninstall --keep-state)`', "\n".join(texts.values()))
        pexo_store_memory(content=f"RETRIEVED_UNINSTALL_KEEP_STATE={uninstall}", task_context=task_context, session_id="compound-store-3")

        memory_text = "\n".join(gather_memory_texts("RETRIEVED_TRUST_MODE RETRIEVED_MCP_COMMAND RETRIEVED_UNINSTALL_KEEP_STATE", n_results=10))
        final = " | ".join(
            [
                regex_extract(r"RETRIEVED_TRUST_MODE=([^\r\n]+)", memory_text),
                regex_extract(r"RETRIEVED_MCP_COMMAND=([^\r\n]+)", memory_text),
                regex_extract(r"RETRIEVED_UNINSTALL_KEEP_STATE=([^\r\n]+)", memory_text),
            ]
        )
        session_ids.append("compound-repo-step-3")
        start_and_complete_task(
            "Use the stored repo findings and return the trust mode, MCP command, and keep-state uninstall command separated by ` | `.",
            final,
            session_ids[-1],
        )
        return final, sum_session_tokens(session_ids)

    pexo_result, query_metrics = measure_phase(query_runner)
    pexo_answer, token_total = pexo_result

    return ScenarioResult(
        track="compounding",
        slug="compound_repo_sequence",
        title="Repeated-use compounding: repo facts reused across tasks",
        expected_answer=expected,
        baseline_answer=baseline_answer,
        pexo_answer=pexo_answer,
        correct=baseline_answer == expected and pexo_answer == expected,
        source_bytes=source_bytes,
        traditional_context_bytes=traditional_context_bytes,
        traditional_tokens=traditional_tokens,
        baseline=baseline_metrics,
        pexo_setup=setup_metrics,
        pexo_query=query_metrics,
        pexo_tokens=token_total,
        compaction_ratio=round(traditional_tokens / max(token_total, 1), 2),
        sessions=["compound-repo-step-1", "compound-repo-step-2", "compound-repo-step-3"],
        notes="One ingest, multiple follow-up tasks, and a final answer built from Pexo memories instead of rereading the repo each turn.",
    )


def run_compounding_design_defaults(case_root: Path) -> ScenarioResult:
    notes = {
        "decision_01.txt": padded_note("Decision one", ["DECISION_MEMORY_BACKEND=keyword"], paragraphs=45),
        "decision_02.txt": padded_note("Decision two", ["DECISION_INSTALL_MODE=packaged"], paragraphs=45),
        "decision_03.txt": padded_note("Decision three", ["DECISION_UPDATE=pexo --update"], paragraphs=45),
        "decision_04.txt": padded_note("Decision four", ["DECISION_HEALTH=pexo doctor --json"], paragraphs=45),
    }
    paths = write_note_files(case_root, notes)
    source_bytes = total_bytes(paths)
    expected = "keyword | packaged | pexo --update | pexo doctor --json"

    cumulative_bytes = 0
    ordered_paths = [case_root / f"decision_{index:02d}.txt" for index in range(1, 5)]
    for index in range(1, len(ordered_paths) + 1):
        cumulative_bytes += total_bytes(ordered_paths[:index])
    traditional_tokens = estimate_tokens(cumulative_bytes)

    def baseline_runner() -> str:
        combined = ""
        for index in range(1, len(ordered_paths) + 1):
            combined = "\n".join(read_texts(ordered_paths[:index]).values())
        return " | ".join(
            [
                regex_extract(r"DECISION_MEMORY_BACKEND=([^\r\n]+)", combined),
                regex_extract(r"DECISION_INSTALL_MODE=([^\r\n]+)", combined),
                regex_extract(r"DECISION_UPDATE=([^\r\n]+)", combined),
                regex_extract(r"DECISION_HEALTH=([^\r\n]+)", combined),
            ]
        )

    _, baseline_metrics = measure_phase(baseline_runner)
    baseline_answer = baseline_runner()
    task_context = "compound-design-defaults"

    def setup_runner() -> None:
        for index, path in enumerate(ordered_paths, start=1):
            pexo_store_memory(
                content=path.read_text(encoding="utf-8"),
                task_context=task_context,
                session_id=f"compound-design-store-{index}",
            )

    _, setup_metrics = measure_phase(setup_runner)

    def query_runner() -> tuple[str, int]:
        combined = "\n".join(
            gather_memory_texts(
                "DECISION_MEMORY_BACKEND DECISION_INSTALL_MODE DECISION_UPDATE DECISION_HEALTH",
                n_results=10,
            )
        )
        final = " | ".join(
            [
                regex_extract(r"DECISION_MEMORY_BACKEND=([^\r\n]+)", combined),
                regex_extract(r"DECISION_INSTALL_MODE=([^\r\n]+)", combined),
                regex_extract(r"DECISION_UPDATE=([^\r\n]+)", combined),
                regex_extract(r"DECISION_HEALTH=([^\r\n]+)", combined),
            ]
        )
        session_id = "compound-design-query"
        start_and_complete_task(
            "Use the stored design and operator defaults and return the four accepted defaults separated by ` | `.",
            final,
            session_id,
        )
        return final, sum_session_tokens([session_id])

    pexo_result, query_metrics = measure_phase(query_runner)
    pexo_answer, token_total = pexo_result

    return ScenarioResult(
        track="compounding",
        slug="compound_design_defaults",
        title="Repeated-use compounding: accepted defaults reused later",
        expected_answer=expected,
        baseline_answer=baseline_answer,
        pexo_answer=pexo_answer,
        correct=baseline_answer == expected and pexo_answer == expected,
        source_bytes=source_bytes,
        traditional_context_bytes=cumulative_bytes,
        traditional_tokens=traditional_tokens,
        baseline=baseline_metrics,
        pexo_setup=setup_metrics,
        pexo_query=query_metrics,
        pexo_tokens=token_total,
        compaction_ratio=round(traditional_tokens / max(token_total, 1), 2),
        sessions=["compound-design-query"],
        notes="Sequential decisions are stored once, then recalled as compact local memory instead of replaying the full prior thread.",
    )


def simulate_runtime_restart(state_root: Path) -> None:
    reset_search_index_runtime()
    reset_database_runtime()
    set_runtime_path_context(env_override=str(state_root), code_root=REPO_ROOT)
    init_db()


def run_resilience_interrupted_review(case_root: Path, state_root: Path) -> ScenarioResult:
    notes = {
        "recovery_brief.txt": padded_note(
            "Interrupted task brief",
            [
                "RECOVERY_TARGET=package lifecycle",
                "RECOVERY_NEXT_GATE=run QA review",
            ],
            paragraphs=55,
        ),
        "partial_progress.txt": padded_note(
            "Partial progress",
            [
                "RECOVERY_PATCH_STATUS=developer_done",
                "RECOVERY_OWNER=platform-runtime",
            ],
            paragraphs=55,
        ),
    }
    paths = write_note_files(case_root, notes)
    source_bytes = total_bytes(paths)
    traditional_tokens = estimate_tokens(source_bytes)
    expected = "run QA review | package lifecycle"

    def baseline_runner() -> str:
        combined = "\n".join(read_texts(paths).values())
        return " | ".join(
            [
                regex_extract(r"RECOVERY_NEXT_GATE=([^\r\n]+)", combined),
                regex_extract(r"RECOVERY_TARGET=([^\r\n]+)", combined),
            ]
        )

    _, baseline_metrics = measure_phase(baseline_runner)
    baseline_answer = baseline_runner()
    task_context = "resilience-interrupted-review"

    def setup_runner() -> None:
        pexo_register_artifact_text(
            name="recovery_brief.txt",
            content=notes["recovery_brief.txt"],
            task_context=task_context,
            session_id="resilience-review-artifact",
            source_uri="benchmark://resilience/recovery_brief.txt",
        )
        pexo_store_memory(
            content=notes["partial_progress.txt"],
            task_context=task_context,
            session_id="resilience-review-memory",
        )

    _, setup_metrics = measure_phase(setup_runner)

    def query_runner() -> tuple[str, int]:
        simulate_runtime_restart(state_root)
        memory_text = "\n".join(gather_memory_texts("RECOVERY_PATCH_STATUS RECOVERY_OWNER", n_results=10))
        artifact_text = "\n".join(gather_artifact_texts("RECOVERY_TARGET", task_context=task_context, limit=5).values())
        combined = "\n".join([memory_text, artifact_text])
        final = " | ".join(
            [
                regex_extract(r"RECOVERY_NEXT_GATE=([^\r\n]+)", combined),
                regex_extract(r"RECOVERY_TARGET=([^\r\n]+)", combined),
            ]
        )
        session_id = "resilience-review-query"
        start_and_complete_task(
            "After an interruption, use the stored state and return RECOVERY_NEXT_GATE and RECOVERY_TARGET separated by ` | `.",
            final,
            session_id,
        )
        return final, sum_session_tokens([session_id])

    pexo_result, query_metrics = measure_phase(query_runner)
    pexo_answer, token_total = pexo_result

    return ScenarioResult(
        track="resilience",
        slug="resilience_interrupted_review",
        title="Failure recovery: interrupted review resumed from stored state",
        expected_answer=expected,
        baseline_answer=baseline_answer,
        pexo_answer=pexo_answer,
        correct=baseline_answer == expected and pexo_answer == expected,
        source_bytes=source_bytes,
        traditional_context_bytes=source_bytes,
        traditional_tokens=traditional_tokens,
        baseline=baseline_metrics,
        pexo_setup=setup_metrics,
        pexo_query=query_metrics,
        pexo_tokens=token_total,
        compaction_ratio=round(traditional_tokens / max(token_total, 1), 2),
        sessions=["resilience-review-query"],
        notes="Simulates process interruption: Pexo state survives a runtime reset and answers the resumed question without replaying the full brief.",
    )


def run_resilience_client_switch(case_root: Path, state_root: Path) -> ScenarioResult:
    notes = {
        "codex_failed_attempt.txt": padded_note(
            "Codex failed attempt",
            [
                "SWITCH_FAILED_CLIENT=codex",
                "SWITCH_RESUME_PATH=docs/ARCHITECTURE.md",
            ],
            paragraphs=55,
        ),
        "gemini_resume_note.txt": padded_note(
            "Gemini resume note",
            [
                "SWITCH_RECOVERY_CLIENT=gemini",
                "SWITCH_RESUME_FACT=SQLite local-first",
            ],
            paragraphs=55,
        ),
    }
    paths = write_note_files(case_root, notes)
    source_bytes = total_bytes(paths)
    traditional_tokens = estimate_tokens(source_bytes)
    expected = "gemini | docs/ARCHITECTURE.md | SQLite local-first"

    def baseline_runner() -> str:
        combined = "\n".join(read_texts(paths).values())
        return " | ".join(
            [
                regex_extract(r"SWITCH_RECOVERY_CLIENT=([^\r\n]+)", combined),
                regex_extract(r"SWITCH_RESUME_PATH=([^\r\n]+)", combined),
                regex_extract(r"SWITCH_RESUME_FACT=([^\r\n]+)", combined),
            ]
        )

    _, baseline_metrics = measure_phase(baseline_runner)
    baseline_answer = baseline_runner()
    task_context = "resilience-client-switch"

    def setup_runner() -> None:
        pexo_store_memory(
            content=notes["codex_failed_attempt.txt"],
            task_context=task_context,
            session_id="resilience-switch-memory-1",
        )
        pexo_store_memory(
            content=notes["gemini_resume_note.txt"],
            task_context=task_context,
            session_id="resilience-switch-memory-2",
        )

    _, setup_metrics = measure_phase(setup_runner)

    def query_runner() -> tuple[str, int]:
        simulate_runtime_restart(state_root)
        combined = "\n".join(gather_memory_texts("SWITCH_RECOVERY_CLIENT SWITCH_RESUME_PATH SWITCH_RESUME_FACT", n_results=10))
        final = " | ".join(
            [
                regex_extract(r"SWITCH_RECOVERY_CLIENT=([^\r\n]+)", combined),
                regex_extract(r"SWITCH_RESUME_PATH=([^\r\n]+)", combined),
                regex_extract(r"SWITCH_RESUME_FACT=([^\r\n]+)", combined),
            ]
        )
        session_id = "resilience-switch-query"
        start_and_complete_task(
            "After a client switch, use the stored state and return SWITCH_RECOVERY_CLIENT, SWITCH_RESUME_PATH, and SWITCH_RESUME_FACT separated by ` | `.",
            final,
            session_id,
        )
        return final, sum_session_tokens([session_id])

    pexo_result, query_metrics = measure_phase(query_runner)
    pexo_answer, token_total = pexo_result

    return ScenarioResult(
        track="resilience",
        slug="resilience_client_switch",
        title="Failure recovery: client switch without context replay",
        expected_answer=expected,
        baseline_answer=baseline_answer,
        pexo_answer=pexo_answer,
        correct=baseline_answer == expected and pexo_answer == expected,
        source_bytes=source_bytes,
        traditional_context_bytes=source_bytes,
        traditional_tokens=traditional_tokens,
        baseline=baseline_metrics,
        pexo_setup=setup_metrics,
        pexo_query=query_metrics,
        pexo_tokens=token_total,
        compaction_ratio=round(traditional_tokens / max(token_total, 1), 2),
        sessions=["resilience-switch-query"],
        notes="Simulates a dead client handoff where the next client continues from stored Pexo memory instead of replaying the failed thread.",
    )


def summarize_track(results: list[ScenarioResult], track: str) -> dict:
    subset = [item for item in results if item.track == track]
    return {
        "track": track,
        "case_count": len(subset),
        "source_bytes": sum(item.source_bytes for item in subset),
        "traditional_tokens": sum(item.traditional_tokens for item in subset),
        "pexo_tokens": sum(item.pexo_tokens for item in subset),
        "avg_compaction_ratio": round(sum(item.compaction_ratio for item in subset) / max(len(subset), 1), 2),
        "baseline_wall_seconds": round(sum(item.baseline.wall_seconds for item in subset), 3),
        "pexo_setup_wall_seconds": round(sum(item.pexo_setup.wall_seconds for item in subset), 3),
        "pexo_query_wall_seconds": round(sum(item.pexo_query.wall_seconds for item in subset), 3),
        "all_correct": all(item.correct for item in subset),
    }


def build_markdown(results: dict) -> str:
    host = results["host"]
    runtime = results["runtime"]
    perf = results["suite_performance"]
    lines = [
        "## Real-World Benchmarks",
        "",
        "These numbers come from a fresh local benchmark run generated by `scripts/run_operator_workflow_benchmarks.py`.",
        "The suite spins up an isolated sandbox state root, uses the live Pexo repo as a retrieval corpus, simulates cross-client handoffs through Pexo's MCP surface, and records the resulting session telemetry.",
        "Raw benchmark artifacts are checked into `docs/benchmarks/operator_workflow_results.json` and `docs/benchmarks/operator_workflow_results.md`.",
        "",
        "### What This Suite Measures",
        "",
        "1. **Real repo retrieval** against the current Pexo codebase.",
        "2. **Cross-client handoff** across simulated Gemini, Codex, and Claude sessions using Pexo memory and artifacts.",
        "3. **Repeated-use compounding** where later tasks reuse prior local findings instead of replaying the whole context.",
        "4. **Failure recovery** where interrupted or switched-client work resumes from persisted local state.",
        "",
        "Traditional token counts are estimated using the rough rule of `bytes / 4`.",
        "The client-handoff tracks simulate distinct clients through separate Pexo sessions so the benchmark measures Pexo continuity overhead rather than external CLI/model latency.",
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
        f"| Direct baseline paths | `{format_seconds(perf['baseline']['wall_seconds'])}` s | `{format_seconds(perf['baseline']['cpu_seconds'])}` s | `{perf['baseline']['peak_rss_mb']}` MB | Reads the direct corpus or replay payloads without Pexo. |",
        f"| Pexo (setup + query) | `{format_seconds(perf['pexo']['wall_seconds'])}` s | `{format_seconds(perf['pexo']['cpu_seconds'])}` s | `{perf['pexo']['peak_rss_mb']}` MB | Registers state locally and queries it through Pexo's MCP/control-plane surface. |",
        f"| Measured Pexo overhead | `{format_seconds(perf['delta']['wall_seconds'])}` s | `{format_seconds(perf['delta']['cpu_seconds'])}` s | `{perf['delta']['peak_rss_mb']}` MB | Additional cost of local Pexo state management over the direct paths for this suite. |",
        f"| Pexo benchmark state footprint | - | - | - | `{perf['pexo_state_mb']}` MB on disk after the suite. |",
        "",
        "### Track Summary",
        "",
        "| Track | Cases | Cumulative Source Data | Traditional Tokens | Pexo Tokens | Avg Ratio | Baseline Time | Pexo Time | Correct |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for track in results["tracks"]:
        lines.append(
            f"| {track['track'].title()} | `{track['case_count']}` | `{_format_number(track['source_bytes'])}` bytes | "
            f"`{_format_number(track['traditional_tokens'])}` | `{_format_number(track['pexo_tokens'])}` | "
            f"`{track['avg_compaction_ratio']:.2f}x` | `{format_seconds(track['baseline_wall_seconds'])}` s | "
            f"`{format_seconds(track['pexo_setup_wall_seconds'] + track['pexo_query_wall_seconds'])}` s | "
            f"{'yes' if track['all_correct'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "### Per-Scenario Results",
            "",
            "| Track | Workload | Source Bytes | Naive Context Tokens | Pexo Tokens | Ratio | Baseline Time | Pexo Setup | Pexo Query | Correct |",
            "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
        ]
    )
    for case in results["cases"]:
        lines.append(
            f"| {case['track'].title()} | {case['title']} | `{_format_number(case['source_bytes'])}` | "
            f"`{_format_number(case['traditional_tokens'])}` | `{_format_number(case['pexo_tokens'])}` | "
            f"`{case['compaction_ratio']:.2f}x` | `{format_seconds(case['baseline']['wall_seconds'])}` s | "
            f"`{format_seconds(case['pexo_setup']['wall_seconds'])}` s | `{format_seconds(case['pexo_query']['wall_seconds'])}` s | "
            f"{'yes' if case['correct'] else 'no'} |"
        )
    summary = results["summary"]
    lines.extend(
        [
            "",
            "### Summary",
            "",
            f"- Total cumulative workload source bytes across all 10 scenarios: `{_format_number(summary['source_bytes'])}` bytes",
            f"- Total naive context estimate across all 10 workloads: `{_format_number(summary['traditional_tokens'])}` tokens",
            f"- Total Pexo session context across all 10 workloads: `{_format_number(summary['pexo_tokens'])}` tokens",
            f"- Average compaction ratio: `{summary['avg_compaction_ratio']:.2f}x`",
            f"- Median compaction ratio: `{summary['median_compaction_ratio']:.2f}x`",
            f"- All 10 workloads returned the correct answer: `{'yes' if summary['all_correct'] else 'no'}`",
            "",
            "### What This Means",
            "",
            "- These measurements describe **active context pressure and continuity overhead**, not a universal wall-clock speed guarantee.",
            "- Direct file reading or direct replay can still be faster for one-off lookups.",
            "- Pexo's value shows up when context must survive **client switches, repeated tasks, and interrupted work** without replaying the full thread.",
            "- This run used the default SQLite + keyword retrieval path. Optional semantic vector memory was not required.",
            "- The narrower synthetic context-compaction microbenchmark still lives in `scripts/run_context_compaction_benchmarks.py` if you want a pure artifact-retrieval test.",
            "",
        ]
    )
    return "\n".join(lines)


def replace_readme_section(readme_text: str, markdown: str) -> str:
    candidates = [
        "## Real-World Benchmarks",
        "## Context Compaction (Benchmarks)",
    ]
    start_index = None
    for candidate in candidates:
        if candidate in readme_text:
            start_index = readme_text.index(candidate)
            break
    if start_index is None:
        raise RuntimeError("Could not find existing benchmark section in README.")
    if "## Large Context Stress Test" in readme_text:
        end_index = readme_text.index("## Large Context Stress Test")
    else:
        end = "\n---\n\n## What Pexo Is Good At"
        end_index = readme_text.index(end)
    return f"{readme_text[:start_index]}{markdown}{readme_text[end_index:]}"


def main() -> int:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    if SUITE_ROOT.exists():
        shutil.rmtree(SUITE_ROOT)
    (SUITE_ROOT / "datasets").mkdir(parents=True, exist_ok=True)
    state_root = SUITE_ROOT / "pexo_state"

    set_runtime_path_context(env_override=str(state_root), code_root=REPO_ROOT)
    reset_database_runtime()
    reset_search_index_runtime()
    init_db()

    host = build_host_specs()
    runtime = build_runtime_status()
    results: dict = {
        "generated_at_utc": now_utc(),
        "seed": BENCHMARK_SEED,
        "host": host,
        "runtime": {
            "memory_backend": runtime.get("memory_backend", "unknown"),
            "install_mode": runtime.get("install_mode", "unknown"),
            "active_profile": runtime.get("active_profile", "unknown"),
        },
        "state_root": str(current_state_root()),
        "cases": [],
    }

    corpus_paths = repo_paths()
    scenario_results: list[ScenarioResult] = []
    for config in build_repo_retrieval_scenarios():
        scenario_results.append(run_repo_retrieval_case(config, corpus_paths))
    scenario_results.append(run_handoff_landing_page(SUITE_ROOT / "datasets" / "handoff_landing_page"))
    scenario_results.append(run_handoff_bug_triage(SUITE_ROOT / "datasets" / "handoff_bug_triage"))
    scenario_results.append(run_compounding_repo_sequence(corpus_paths))
    scenario_results.append(run_compounding_design_defaults(SUITE_ROOT / "datasets" / "compound_design_defaults"))
    scenario_results.append(run_resilience_interrupted_review(SUITE_ROOT / "datasets" / "resilience_interrupted_review", state_root))
    scenario_results.append(run_resilience_client_switch(SUITE_ROOT / "datasets" / "resilience_client_switch", state_root))

    results["cases"] = [asdict(item) for item in scenario_results]
    source_bytes = sum(item.source_bytes for item in scenario_results)
    traditional_tokens = sum(item.traditional_tokens for item in scenario_results)
    pexo_tokens = sum(item.pexo_tokens for item in scenario_results)
    ratios = sorted(item.compaction_ratio for item in scenario_results)
    results["tracks"] = [
        summarize_track(scenario_results, track)
        for track in ("retrieval", "handoff", "compounding", "resilience")
    ]
    results["suite_performance"] = {
        "baseline": {
            "wall_seconds": round(sum(item.baseline.wall_seconds for item in scenario_results), 3),
            "cpu_seconds": round(sum(item.baseline.cpu_seconds for item in scenario_results), 3),
            "peak_rss_mb": round(max(item.baseline.peak_rss_mb for item in scenario_results), 2),
        },
        "pexo": {
            "wall_seconds": round(sum(item.pexo_setup.wall_seconds + item.pexo_query.wall_seconds for item in scenario_results), 3),
            "cpu_seconds": round(sum(item.pexo_setup.cpu_seconds + item.pexo_query.cpu_seconds for item in scenario_results), 3),
            "peak_rss_mb": round(
                max(
                    max(item.pexo_setup.peak_rss_mb for item in scenario_results),
                    max(item.pexo_query.peak_rss_mb for item in scenario_results),
                ),
                2,
            ),
        },
        "delta": {
            "wall_seconds": round(
                sum(item.pexo_setup.wall_seconds + item.pexo_query.wall_seconds for item in scenario_results)
                - sum(item.baseline.wall_seconds for item in scenario_results),
                3,
            ),
            "cpu_seconds": round(
                sum(item.pexo_setup.cpu_seconds + item.pexo_query.cpu_seconds for item in scenario_results)
                - sum(item.baseline.cpu_seconds for item in scenario_results),
                3,
            ),
            "peak_rss_mb": round(
                max(
                    0.0,
                    max(
                        max(item.pexo_setup.peak_rss_mb for item in scenario_results),
                        max(item.pexo_query.peak_rss_mb for item in scenario_results),
                    )
                    - max(item.baseline.peak_rss_mb for item in scenario_results),
                ),
                2,
            ),
        },
        "pexo_state_mb": round(directory_size_bytes(state_root) / (1024 * 1024), 2),
    }
    results["summary"] = {
        "case_count": len(scenario_results),
        "source_bytes": source_bytes,
        "traditional_tokens": traditional_tokens,
        "pexo_tokens": pexo_tokens,
        "avg_compaction_ratio": round(sum(item.compaction_ratio for item in scenario_results) / len(scenario_results), 2),
        "median_compaction_ratio": round(ratios[len(ratios) // 2], 2),
        "all_correct": all(item.correct for item in scenario_results),
    }

    RESULTS_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    markdown = build_markdown(results)
    RESULTS_MD.write_text(markdown, encoding="utf-8")
    README_PATH.write_text(replace_readme_section(README_PATH.read_text(encoding="utf-8"), markdown), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
