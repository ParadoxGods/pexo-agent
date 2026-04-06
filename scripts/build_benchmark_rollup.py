from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DOCS_DIR = REPO_ROOT / "docs" / "benchmarks"
README_PATH = REPO_ROOT / "README.md"
RESULTS_JSON = DOCS_DIR / "benchmark_rollup.json"
RESULTS_MD = DOCS_DIR / "benchmark_rollup.md"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(name: str) -> dict:
    return json.loads((DOCS_DIR / name).read_text(encoding="utf-8"))


def format_number(value: int | float, digits: int = 2) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digits}f}"


def normalize_suite(name: str, payload: dict) -> dict:
    if name == "context_compaction":
        before_tokens = int(sum(item["traditional_tokens"] for item in payload["cases"]))
        after_tokens = int(sum(item["pexo_tokens"] for item in payload["cases"]))
        dataset_bytes = int(payload["summary"]["total_dataset_bytes"])
        case_count = int(payload["summary"]["case_count"])
        perf = payload["suite_performance"]
        return {
            "slug": name,
            "title": "Context Compaction",
            "script": "scripts/run_context_compaction_benchmarks.py",
            "results_json": "docs/benchmarks/context_compaction_results.json",
            "results_md": "docs/benchmarks/context_compaction_results.md",
            "description": "10 synthetic retrieval workloads over padded corpora.",
            "case_count": case_count,
            "dataset_bytes": dataset_bytes,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "direct_wall_seconds": float(perf["baseline"]["wall_seconds"]),
            "pexo_wall_seconds": float(perf["pexo"]["wall_seconds"]),
            "overhead_wall_seconds": float(perf["delta"]["wall_seconds"]),
            "direct_peak_rss_mb": float(perf["baseline"]["peak_rss_mb"]),
            "pexo_peak_rss_mb": float(perf["pexo"]["peak_rss_mb"]),
            "rss_delta_mb": float(perf["delta"]["peak_rss_mb"]),
            "state_mb": float(perf["pexo_state_mb"]),
        }
    if name == "operator_workflow":
        summary = payload["summary"]
        perf = payload["suite_performance"]
        return {
            "slug": name,
            "title": "Real-World Workflow",
            "script": "scripts/run_operator_workflow_benchmarks.py",
            "results_json": "docs/benchmarks/operator_workflow_results.json",
            "results_md": "docs/benchmarks/operator_workflow_results.md",
            "description": "10 repo, handoff, compounding, and resilience scenarios.",
            "case_count": int(summary["case_count"]),
            "dataset_bytes": int(summary["source_bytes"]),
            "before_tokens": int(summary["traditional_tokens"]),
            "after_tokens": int(summary["pexo_tokens"]),
            "direct_wall_seconds": float(perf["baseline"]["wall_seconds"]),
            "pexo_wall_seconds": float(perf["pexo"]["wall_seconds"]),
            "overhead_wall_seconds": float(perf["delta"]["wall_seconds"]),
            "direct_peak_rss_mb": float(perf["baseline"]["peak_rss_mb"]),
            "pexo_peak_rss_mb": float(perf["pexo"]["peak_rss_mb"]),
            "rss_delta_mb": float(perf["delta"]["peak_rss_mb"]),
            "state_mb": float(perf["pexo_state_mb"]),
        }
    if name == "large_context_stress":
        case = payload["case"]
        perf = payload["performance"]
        return {
            "slug": name,
            "title": "Large Context Stress",
            "script": "scripts/run_large_context_stress_benchmark.py",
            "results_json": "docs/benchmarks/large_context_stress_results.json",
            "results_md": "docs/benchmarks/large_context_stress_results.md",
            "description": "One oversized exact-token lookup over a very large synthetic corpus.",
            "case_count": 1,
            "dataset_bytes": int(case["dataset_bytes"]),
            "before_tokens": int(case["traditional_tokens"]),
            "after_tokens": int(case["pexo_tokens"]),
            "direct_wall_seconds": float(perf["baseline"]["wall_seconds"]),
            "pexo_wall_seconds": float(perf["pexo"]["wall_seconds"]),
            "overhead_wall_seconds": float(perf["delta"]["wall_seconds"]),
            "direct_peak_rss_mb": float(perf["baseline"]["peak_rss_mb"]),
            "pexo_peak_rss_mb": float(perf["pexo"]["peak_rss_mb"]),
            "rss_delta_mb": float(perf["delta"]["peak_rss_mb"]),
            "state_mb": float(perf["pexo_state_mb"]),
        }
    raise ValueError(f"Unknown benchmark suite: {name}")


def enrich_suite(suite: dict) -> dict:
    before = max(suite["before_tokens"], 1)
    after = max(suite["after_tokens"], 1)
    suite["reduction_factor"] = round(before / after, 2)
    suite["retained_pct"] = round((after / before) * 100, 4)
    return suite


def build_rollup() -> dict:
    context_payload = load_json("context_compaction_results.json")
    workflow_payload = load_json("operator_workflow_results.json")
    large_payload = load_json("large_context_stress_results.json")

    host = large_payload["host"]
    runtime = large_payload["runtime"]

    suites = [
        enrich_suite(normalize_suite("context_compaction", context_payload)),
        enrich_suite(normalize_suite("operator_workflow", workflow_payload)),
        enrich_suite(normalize_suite("large_context_stress", large_payload)),
    ]

    total_before = sum(item["before_tokens"] for item in suites)
    total_after = sum(item["after_tokens"] for item in suites)
    total_bytes = sum(item["dataset_bytes"] for item in suites)

    return {
        "generated_at_utc": now_utc(),
        "host": host,
        "runtime": runtime,
        "suites": suites,
        "summary": {
            "suite_count": len(suites),
            "total_dataset_bytes": total_bytes,
            "total_before_tokens": total_before,
            "total_after_tokens": total_after,
            "overall_reduction_factor": round(total_before / max(total_after, 1), 2),
            "overall_retained_pct": round((total_after / max(total_before, 1)) * 100, 4),
        },
    }


def build_markdown(rollup: dict) -> str:
    host = rollup["host"]
    runtime = rollup["runtime"]
    suites = rollup["suites"]
    summary = rollup["summary"]

    x_labels = ", ".join(f'"{item["title"]}"' for item in suites)
    before_series = ", ".join(str(round(item["before_tokens"] / 1000, 2)) for item in suites)
    after_series = ", ".join(str(round(item["after_tokens"] / 1000, 2)) for item in suites)
    retained_series = ", ".join(str(item["retained_pct"]) for item in suites)
    raw_axis_max = round(max(item["before_tokens"] for item in suites) / 1000)
    retained_axis_max = max(5, int(max(item["retained_pct"] for item in suites)) + 1)

    lines = [
        "## Benchmark Rollup",
        "",
        "These are real local benchmarks for wall time, CPU time, peak RSS, on-disk state, and Pexo session-context usage.",
        "The only estimated figure is the **naive before-Pexo context load**, which is approximated as `bytes / 4` so the direct path can be compared against Pexo's measured session telemetry.",
        "",
        "Raw benchmark artifacts:",
        "",
        "- `docs/benchmarks/context_compaction_results.json`",
        "- `docs/benchmarks/operator_workflow_results.json`",
        "- `docs/benchmarks/large_context_stress_results.json`",
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
        f"- Pexo execution mode during rollup runs: `{runtime['install_mode']}`",
        "",
        "### Data Usage Before vs After Pexo",
        "",
        "```mermaid",
        "xychart-beta",
        '    title "Context Usage Before vs After Pexo (thousands of tokens)"',
        f"    x-axis [{x_labels}]",
        f'    y-axis "Tokens (thousands)" 0 --> {raw_axis_max}',
        f"    bar \"Before Pexo\" [{before_series}]",
        f"    bar \"After Pexo\" [{after_series}]",
        "```",
        "",
        "```mermaid",
        "xychart-beta",
        '    title "Context Retained After Pexo (% of original)"',
        f"    x-axis [{x_labels}]",
        f'    y-axis "Retained %" 0 --> {retained_axis_max}',
        f"    bar \"After / Before %\" [{retained_series}]",
        "```",
        "",
        "### Combined Suite Summary",
        "",
        "| Suite | Workloads | Dataset Size | Before Pexo | After Pexo | Retained | Reduction |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in suites:
        lines.append(
            f"| {item['title']} | `{item['case_count']}` | `{format_number(item['dataset_bytes'])}` bytes | "
            f"`{format_number(item['before_tokens'])}` tokens | `{format_number(item['after_tokens'])}` tokens | "
            f"`{item['retained_pct']:.4f}%` | `{item['reduction_factor']:.2f}x` |"
        )
    lines.extend(
        [
            "",
            "### Machine Impact Per Suite",
            "",
            "| Suite | Direct Time | Pexo Time | Overhead | Direct RSS | Pexo RSS | RSS Delta | Pexo State |",
            "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in suites:
        lines.append(
            f"| {item['title']} | `{item['direct_wall_seconds']:.3f}` s | `{item['pexo_wall_seconds']:.3f}` s | "
            f"`{item['overhead_wall_seconds']:.3f}` s | `{item['direct_peak_rss_mb']:.2f}` MB | "
            f"`{item['pexo_peak_rss_mb']:.2f}` MB | `{item['rss_delta_mb']:.2f}` MB | `{item['state_mb']:.2f}` MB |"
        )
    lines.extend(
        [
            "",
            "### Overall Totals",
            "",
            f"- Total data across all benchmark suites: `{format_number(summary['total_dataset_bytes'])}` bytes",
            f"- Total naive before-Pexo context: `{format_number(summary['total_before_tokens'])}` tokens",
            f"- Total Pexo session context: `{format_number(summary['total_after_tokens'])}` tokens",
            f"- Overall retained context after Pexo: `{summary['overall_retained_pct']:.4f}%`",
            f"- Overall reduction factor: `{summary['overall_reduction_factor']:.2f}x`",
            "",
            "### How To Read This",
            "",
            "- **Before Pexo** is the naive context load you would pay if you shoved the source material directly into the model path.",
            "- **After Pexo** is what the Pexo-managed session actually carried according to recorded `context_size_tokens` telemetry.",
            "- The timing numbers are true wall-clock and CPU measurements on this machine.",
            "- The token comparison is partly measured and partly derived: Pexo tokens are measured, the direct-path token count is estimated from bytes.",
            "- The large stress suite dominates the raw chart by design. The retained-percent chart shows the same data normalized.",
            "",
        ]
    )
    return "\n".join(lines)


def replace_or_insert_rollup(readme_text: str, markdown: str) -> str:
    marker = "## Benchmark Rollup"
    anchor = "## Real-World Benchmarks"
    if marker in readme_text:
        start = readme_text.index(marker)
        end = readme_text.index(anchor)
        return f"{readme_text[:start]}{markdown}\n\n{readme_text[end:]}"
    if anchor not in readme_text:
        raise RuntimeError("Could not find Real-World Benchmarks anchor in README.")
    anchor_index = readme_text.index(anchor)
    return f"{readme_text[:anchor_index]}{markdown}\n\n{readme_text[anchor_index:]}"


def main() -> int:
    rollup = build_rollup()
    RESULTS_JSON.write_text(json.dumps(rollup, indent=2), encoding="utf-8")
    markdown = build_markdown(rollup)
    RESULTS_MD.write_text(markdown, encoding="utf-8")
    README_PATH.write_text(replace_or_insert_rollup(README_PATH.read_text(encoding="utf-8"), markdown), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
