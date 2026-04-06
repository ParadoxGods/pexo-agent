<div align="center">
  <h1>PEXO</h1>
  <p><b>Primary EXecution Operator</b></p>
  <p><i>A local-first operator layer for AI-assisted development.</i></p>
</div>

---

Pexo sits between you and your AI clients and keeps the work coherent on your machine. Codex, Gemini, Claude, and any other MCP-capable client can all work against the same local memory, artifacts, preferences, sessions, agents, and task state.

It is not another disposable chat shell. It is the local operator layer that stops your workflow from fragmenting every time you switch tools.

## Why Pexo

Most AI workflows fail in the same ways:
- context gets trapped inside one client
- switching models costs you project state
- preferences vanish between sessions
- files and artifacts drift away from the work that produced them
- interrupted work has to be reconstructed from scratch

Pexo fixes that by keeping a shared local brain underneath the clients.

| What Pexo keeps | Why it matters |
| :--- | :--- |
| Durable memory | Project facts and accepted decisions survive client switches. |
| Artifacts and files | Important context stays attached to the work. |
| Session continuity | One model can pick up where another left off. |
| Preferences | The stack stops asking the same setup questions. |
| Agents and tools | Local operating rules stay reusable and inspectable. |

If you want local control, lower active context pressure, and a workflow that compounds over time instead of resetting, this is the missing layer.

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

| Suite | Before Pexo | After Pexo |
| :--- | :--- | :--- |
| Massive Repo Retrieval | Large noisy codebase retrieval.<br>`35,387,023` bytes corpus<br>`6` workloads<br>`53,080,530` tokens<br>`0.336` direct time | `17,004` tokens<br>`3121.65x` reduction<br>`100.00%` accuracy<br>`2.707` Pexo time |
| Massive Timeline Recollection | Latest-state recollection across long histories.<br>`6,528,210` bytes corpus<br>`6` workloads<br>`9,792,312` tokens<br>`0.065` direct time | `18,703` tokens<br>`523.57x` reduction<br>`100.00%` accuracy<br>`1.826` Pexo time |
| Massive Handoff Reconstruction | Cross-client continuity and current-state reconstruction.<br>`9,472,788` bytes corpus<br>`6` workloads<br>`14,209,182` tokens<br>`0.100` direct time | `19,333` tokens<br>`734.97x` reduction<br>`100.00%` accuracy<br>`2.330` Pexo time |

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

## Install

Use the latest GitHub Release bundle. This is the canonical install path.

**Windows:**
```powershell
gh release download -R ParadoxGods/pexo-agent -p pexo-install-windows.zip --clobber
tar -xf .\pexo-install-windows.zip
.\install.cmd
```

**macOS/Linux:**
```bash
gh release download -R ParadoxGods/pexo-agent -p pexo-install-unix.tar.gz --clobber
tar -xzf pexo-install-unix.tar.gz
./install.sh
```

The release bundle installs Pexo, completes headless setup, connects supported clients, runs `pexo doctor`, and warms the local runtime.

If `gh` is unavailable, download the latest release asset manually from [Releases](https://github.com/ParadoxGods/pexo-agent/releases), extract it, and run the included `install.cmd` or `install.sh`.

### Fallback Packaged Install

Use this only if the release bundle path is unavailable.

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.1.1"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

Packaged installs keep mutable state under `~/.pexo` by default. Set `PEXO_HOME` if you want the state root somewhere else.

## Start Using Pexo

The normal flow is short:
1. Start Pexo.
2. Use Codex, Gemini, or Claude normally.
3. Let Pexo hold the continuity underneath.

**Start the local control plane:**
```powershell
pexo
```

**Basic verification:**
```powershell
pexo doctor --json
pexo connect all --scope user
```

Some clients will reach for it automatically. Others may need one short instruction:

> Use Pexo as the shared local brain for this task. Review this repo, tell me the top 3 concrete issues, and store the result in Pexo memory.

Open `http://127.0.0.1:9999/ui/` if you want to inspect the local state directly.

**Direct terminal chat:**
```powershell
pexo --chat
```

## MCP First

Pexo is designed around MCP because the real point is interoperability. You should not have to pick one AI client as the source of truth. Pexo keeps the state local and lets whichever connected model is active work against the same substrate.

Packaged installs expose this MCP entrypoint:

```json
{
  "mcpServers": {
    "pexo": {
      "command": "pexo-mcp",
      "args": []
    }
  }
}
```

Repository-level AI usage rules live in `AGENTS.md`.

## Safety And Control

Pexo is meant to be a middle layer, so local trust boundaries matter.

- local memory uses SQLite and keyword-backed retrieval by default
- semantic vector memory is optional
- Genesis tool execution is not wide open by default
- the default Genesis trust mode is `approval-required`
- broad local execution requires explicit host trust via `full-local-exec`

The default install is meant to be useful without silently turning your machine into an unrestricted execution surface.

## Commands

| Command | Description |
| :--- | :--- |
| `pexo` | Start the local control plane. |
| `pexo --chat` | Start direct terminal chat. |
| `pexo --no-browser` | Start the local API without opening the browser. |
| `pexo --mcp` / `pexo-mcp` | Start MCP only. |
| `pexo --update` | Update the current install. |
| `pexo doctor` | Print local installation and runtime diagnostics. |
| `pexo connect all --scope user` | Connect supported local AI clients to `pexo-mcp`. |
| `pexo warmup` | Prime local state after install or update. |
| `pexo promote full` | Repair or reinstall the standard local runtime. |
| `pexo promote vector` | Add optional semantic-memory support. |
| `pexo uninstall` | Remove the current install. |
| `pexo uninstall --keep-state` | Remove the install but preserve local state. |

## Maintenance

For most users, maintenance is just:

```powershell
pexo --update
pexo doctor --json
```

If client wiring drifts:

```powershell
pexo connect all --scope user
```

## Repo-Local Mode

Checkout mode is for contributors or users who explicitly want a repo-backed node.

**Windows:**
```powershell
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

**macOS/Linux:**
```bash
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
```

Checkout mode keeps mutable state under the repo-local `.pexo` directory.

## Bottom Line

Pexo is what you install when you want AI clients to stop behaving like isolated terminals and start behaving like interchangeable workers on top of one local operator layer.

It keeps the memory, context, preferences, artifacts, and task state where they belong: on your machine, under your control, and reusable across the whole stack.
