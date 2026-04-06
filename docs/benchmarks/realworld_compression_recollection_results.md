## Benchmark Snapshot

These are three fresh isolated real-world benchmark suites for **compression and recollection**.
Each suite compares a naive direct-replay baseline against the same workload routed through Pexo's MCP surfaces.

Methodology:
- **Before Pexo** is the naive context load you would carry if you replayed the full corpus into the model path for every question.
- **After Pexo** is the measured `context_size_tokens` recorded by the Pexo-managed sessions during the same workload.
- **Accuracy** is exact-match against the expected answer for every workload in the suite.
- Timing, CPU, RSS, and state footprint are direct local measurements on the host listed below.

Raw benchmark artifacts:
- `docs/benchmarks/realworld_compression_recollection_results.json`
- `docs/benchmarks/realworld_compression_recollection_results.md`
- `scripts/run_realworld_compression_recollection_benchmarks.py`

### Host System

| Metric | Value |
| :--- | :--- |
| OS | `Windows-11-10.0.26200-SP0` |
| CPU | `Intel(R) Core(TM) i9-14900K` |
| Logical cores | `32` |
| RAM | `47.72 GB` |
| Python | `3.12.10` |
| Pexo version | `1.1.1` |
| Memory backend | `keyword` |
| Benchmark execution mode | `checkout` |

### Summary

| Suite | What it tests | Corpus | Workloads | Before Pexo | After Pexo | Reduction | Accuracy | Direct Time | Pexo Time |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Massive Repo Retrieval | Large noisy codebase retrieval. | `35,387,023` bytes | `6` | `53,080,530` tokens | `17,004` tokens | `3121.65x` | `100.00%` | `0.336` | `2.707` |
| Massive Timeline Recollection | Latest-state recollection across long histories. | `6,528,210` bytes | `6` | `9,792,312` tokens | `18,703` tokens | `523.57x` | `100.00%` | `0.065` | `1.826` |
| Massive Handoff Reconstruction | Cross-client continuity and current-state reconstruction. | `9,472,788` bytes | `6` | `14,209,182` tokens | `19,333` tokens | `734.97x` | `100.00%` | `0.100` | `2.330` |

### Combined Totals

| Metric | Value |
| :--- | :--- |
| Total corpus bytes | `51,388,021` |
| Total before-Pexo context | `77,082,024` tokens |
| Total after-Pexo context | `55,040` tokens |
| Overall reduction | `1400.47x` |
| Overall retained after Pexo | `0.0714%` |
| Exact-match accuracy across all workloads | `100.00%` |

### Massive Repo Retrieval

A real repo corpus plus heavy surrounding noise. The baseline rereads the whole corpus for every question; the Pexo path ingests once and recalls only the needed material.

- What it tests: Large noisy codebase retrieval.
- Corpus size: `35,387,023` bytes
- Workloads: `6`
- Direct replay context: `53,080,530` tokens
- Pexo session context: `17,004` tokens
- Reduction: `3121.65x`
- Exact-match accuracy: `100.00%`

| Workload | Expected | Direct | Pexo | Match |
| :--- | :--- | :--- | :--- | :--- |
| Default Genesis trust mode | `approval-required` | `approval-required` | `approval-required` | yes |
| QA gate after developer | `Quality Assurance Manager` | `Quality Assurance Manager` | `Quality Assurance Manager` | yes |
| Packaged MCP command | `pexo-mcp` | `pexo-mcp` | `pexo-mcp` | yes |
| Keep-state uninstall command | `pexo uninstall --keep-state` | `pexo uninstall --keep-state` | `pexo uninstall --keep-state` | yes |
| Checkout mutable state directory | `.pexo` | `.pexo` | `.pexo` | yes |
| Default memory backend | `SQLite` | `SQLite` | `SQLite` | yes |

| Metric | Direct | Pexo Setup | Pexo Query | Pexo Total |
| :--- | ---: | ---: | ---: | ---: |
| Wall time | `0.336` | `1.957` | `0.750` | `2.707` |
| CPU time | `0.328` | `1.453` | `0.688` | `2.141` |
| Peak RSS | `142.79 MB` | `110.51 MB` | `116.43 MB` | `116.43 MB` |

Pexo state footprint after this suite: `44.22 MB`.

### Massive Timeline Recollection

A long sequence of large decision logs with changing accepted defaults over time. The job is to recall the final accepted state, not just find an old mention.

- What it tests: Latest-state recollection across long histories.
- Corpus size: `6,528,210` bytes
- Workloads: `6`
- Direct replay context: `9,792,312` tokens
- Pexo session context: `18,703` tokens
- Reduction: `523.57x`
- Exact-match accuracy: `100.00%`

| Workload | Expected | Direct | Pexo | Match |
| :--- | :--- | :--- | :--- | :--- |
| Current UI stack | `nextjs_app_router` | `nextjs_app_router` | `nextjs_app_router` | yes |
| Current packaging path | `release_bundle` | `release_bundle` | `release_bundle` | yes |
| Current owner mode | `operator-control` | `operator-control` | `operator-control` | yes |
| Current required gate | `Quality Assurance Manager` | `Quality Assurance Manager` | `Quality Assurance Manager` | yes |
| Rejected default option | `vector_by_default` | `vector_by_default` | `vector_by_default` | yes |
| Combined latest product direction | `nextjs_app_router | release_bundle | operator-control` | `nextjs_app_router | release_bundle | operator-control` | `nextjs_app_router | release_bundle | operator-control` | yes |

| Metric | Direct | Pexo Setup | Pexo Query | Pexo Total |
| :--- | ---: | ---: | ---: | ---: |
| Wall time | `0.065` | `1.303` | `0.523` | `1.826` |
| CPU time | `0.078` | `0.875` | `0.484` | `1.359` |
| Peak RSS | `117.02 MB` | `114.38 MB` | `116.33 MB` | `116.33 MB` |

Pexo state footprint after this suite: `20.69 MB`.

### Massive Handoff Reconstruction

A multi-client handoff history where the active issue, next gate, deploy target, and fallback client evolve over many batches.

- What it tests: Cross-client continuity and current-state reconstruction.
- Corpus size: `9,472,788` bytes
- Workloads: `6`
- Direct replay context: `14,209,182` tokens
- Pexo session context: `19,333` tokens
- Reduction: `734.97x`
- Exact-match accuracy: `100.00%`

| Workload | Expected | Direct | Pexo | Match |
| :--- | :--- | :--- | :--- | :--- |
| Current issue across handoffs | `mcp_stability` | `mcp_stability` | `mcp_stability` | yes |
| Current required gate across handoffs | `Quality Assurance Manager` | `Quality Assurance Manager` | `Quality Assurance Manager` | yes |
| Current deploy target across handoffs | `packaged_release` | `packaged_release` | `packaged_release` | yes |
| Fallback client after handoff | `gemini` | `gemini` | `gemini` | yes |
| Current owner mode after handoff | `operator-control` | `operator-control` | `operator-control` | yes |
| Combined current handoff state | `mcp_stability | Quality Assurance Manager | packaged_release | gemini` | `mcp_stability | Quality Assurance Manager | packaged_release | gemini` | `mcp_stability | Quality Assurance Manager | packaged_release | gemini` | yes |

| Metric | Direct | Pexo Setup | Pexo Query | Pexo Total |
| :--- | ---: | ---: | ---: | ---: |
| Wall time | `0.100` | `1.681` | `0.649` | `2.330` |
| CPU time | `0.094` | `1.406` | `0.562` | `1.969` |
| Peak RSS | `114.96 MB` | `114.80 MB` | `117.46 MB` | `117.46 MB` |

Pexo state footprint after this suite: `29.19 MB`.

How to read this:
- Direct replay can still be faster for one-off local scans because it skips ingestion and retrieval work.
- Pexo wins when the same project state needs to be carried across repeated questions, interruptions, or client handoffs without replaying the whole corpus.
- These suites are intentionally large enough to make both the context savings and the recollection accuracy visible in one place.
