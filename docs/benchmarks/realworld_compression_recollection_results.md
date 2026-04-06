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

### At A Glance

| Suite | Before Pexo | After Pexo | Reduction | Accuracy |
| :--- | :--- | :--- | :--- | :--- |
| Massive Repo Retrieval | `53,080,530` tokens<br>`0.336` direct time | `17,004` tokens<br>`2.707` Pexo time | `3121.65x` | `100.00%` |
| Massive Timeline Recollection | `9,792,312` tokens<br>`0.065` direct time | `18,703` tokens<br>`1.826` Pexo time | `523.57x` | `100.00%` |
| Massive Handoff Reconstruction | `14,209,182` tokens<br>`0.100` direct time | `19,333` tokens<br>`2.330` Pexo time | `734.97x` | `100.00%` |

### Combined Totals

| Metric | Before Pexo | After Pexo |
| :--- | :--- | :--- |
| Corpus handled | `51,388,021` bytes | `51,388,021` bytes |
| Active context | `77,082,024` tokens | `55,040` tokens |
| Total wall time | `0.500` | `6.864` |
| Recollection quality | direct baseline replay | `100.00%` exact-match accuracy |
| Net effect | full corpus replay every time | `1400.47x` reduction, `0.0714%` retained |

### Massive Repo Retrieval

A real repo corpus plus heavy surrounding noise. The baseline rereads the whole corpus for every question; the Pexo path ingests once and recalls only the needed material.

| Metric | Before Pexo | After Pexo |
| :--- | :--- | :--- |
| What it tests | Large noisy codebase retrieval. | Large noisy codebase retrieval. |
| Corpus handled | `35,387,023` bytes | `35,387,023` bytes |
| Workloads | `6` | `6` |
| Active context | `53,080,530` tokens | `17,004` tokens |
| Wall time | `0.336` | `2.707` |
| CPU time | `0.328` | `2.141` |
| Peak RSS | `142.79 MB` | `116.43 MB` |
| Retrieval outcome | full direct replay | `3121.65x` reduction, `100.00%` accuracy |
| Local state footprint | none | `44.22 MB` |

Recollection checks:

| Workload | Expected | Direct | Pexo | Match |
| :--- | :--- | :--- | :--- | :--- |
| Default Genesis trust mode | `approval-required` | `approval-required` | `approval-required` | yes |
| QA gate after developer | `Quality Assurance Manager` | `Quality Assurance Manager` | `Quality Assurance Manager` | yes |
| Packaged MCP command | `pexo-mcp` | `pexo-mcp` | `pexo-mcp` | yes |
| Keep-state uninstall command | `pexo uninstall --keep-state` | `pexo uninstall --keep-state` | `pexo uninstall --keep-state` | yes |
| Checkout mutable state directory | `.pexo` | `.pexo` | `.pexo` | yes |
| Default memory backend | `SQLite` | `SQLite` | `SQLite` | yes |

| Pexo timing breakdown | Value |
| :--- | :--- |
| Setup phase | `1.957` wall, `1.453` CPU |
| Query phase | `0.750` wall, `0.688` CPU |

### Massive Timeline Recollection

A long sequence of large decision logs with changing accepted defaults over time. The job is to recall the final accepted state, not just find an old mention.

| Metric | Before Pexo | After Pexo |
| :--- | :--- | :--- |
| What it tests | Latest-state recollection across long histories. | Latest-state recollection across long histories. |
| Corpus handled | `6,528,210` bytes | `6,528,210` bytes |
| Workloads | `6` | `6` |
| Active context | `9,792,312` tokens | `18,703` tokens |
| Wall time | `0.065` | `1.826` |
| CPU time | `0.078` | `1.359` |
| Peak RSS | `117.02 MB` | `116.33 MB` |
| Retrieval outcome | full direct replay | `523.57x` reduction, `100.00%` accuracy |
| Local state footprint | none | `20.69 MB` |

Recollection checks:

| Workload | Expected | Direct | Pexo | Match |
| :--- | :--- | :--- | :--- | :--- |
| Current UI stack | `nextjs_app_router` | `nextjs_app_router` | `nextjs_app_router` | yes |
| Current packaging path | `release_bundle` | `release_bundle` | `release_bundle` | yes |
| Current owner mode | `operator-control` | `operator-control` | `operator-control` | yes |
| Current required gate | `Quality Assurance Manager` | `Quality Assurance Manager` | `Quality Assurance Manager` | yes |
| Rejected default option | `vector_by_default` | `vector_by_default` | `vector_by_default` | yes |
| Combined latest product direction | `nextjs_app_router | release_bundle | operator-control` | `nextjs_app_router | release_bundle | operator-control` | `nextjs_app_router | release_bundle | operator-control` | yes |

| Pexo timing breakdown | Value |
| :--- | :--- |
| Setup phase | `1.303` wall, `0.875` CPU |
| Query phase | `0.523` wall, `0.484` CPU |

### Massive Handoff Reconstruction

A multi-client handoff history where the active issue, next gate, deploy target, and fallback client evolve over many batches.

| Metric | Before Pexo | After Pexo |
| :--- | :--- | :--- |
| What it tests | Cross-client continuity and current-state reconstruction. | Cross-client continuity and current-state reconstruction. |
| Corpus handled | `9,472,788` bytes | `9,472,788` bytes |
| Workloads | `6` | `6` |
| Active context | `14,209,182` tokens | `19,333` tokens |
| Wall time | `0.100` | `2.330` |
| CPU time | `0.094` | `1.969` |
| Peak RSS | `114.96 MB` | `117.46 MB` |
| Retrieval outcome | full direct replay | `734.97x` reduction, `100.00%` accuracy |
| Local state footprint | none | `29.19 MB` |

Recollection checks:

| Workload | Expected | Direct | Pexo | Match |
| :--- | :--- | :--- | :--- | :--- |
| Current issue across handoffs | `mcp_stability` | `mcp_stability` | `mcp_stability` | yes |
| Current required gate across handoffs | `Quality Assurance Manager` | `Quality Assurance Manager` | `Quality Assurance Manager` | yes |
| Current deploy target across handoffs | `packaged_release` | `packaged_release` | `packaged_release` | yes |
| Fallback client after handoff | `gemini` | `gemini` | `gemini` | yes |
| Current owner mode after handoff | `operator-control` | `operator-control` | `operator-control` | yes |
| Combined current handoff state | `mcp_stability | Quality Assurance Manager | packaged_release | gemini` | `mcp_stability | Quality Assurance Manager | packaged_release | gemini` | `mcp_stability | Quality Assurance Manager | packaged_release | gemini` | yes |

| Pexo timing breakdown | Value |
| :--- | :--- |
| Setup phase | `1.681` wall, `1.406` CPU |
| Query phase | `0.649` wall, `0.562` CPU |

How to read this:
- Direct replay can still be faster for one-off local scans because it skips ingestion and retrieval work.
- Pexo wins when the same project state needs to be carried across repeated questions, interruptions, or client handoffs without replaying the whole corpus.
- These suites are intentionally large enough to make both the context savings and the recollection accuracy visible in one place.
