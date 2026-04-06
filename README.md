<div align="center">
  <h1>PEXO</h1>
  <p><b>Primary EXecution Operator</b></p>
  <p><i>A local-first operator layer for AI-assisted development.</i></p>
</div>

---

Pexo is the local operator layer that sits between you and your AI clients and keeps the work coherent on your machine. Codex, Gemini, Claude, and any other MCP-capable client can all work against the same local memory, artifacts, preferences, sessions, agents, and task state.

## Index

- [Why Pexo](#why-pexo)
- [Install](#install)
- [Start Using Pexo](#start-using-pexo)
- [MCP First](#mcp-first)
- [Safety And Control](#safety-and-control)
- [Benchmark Snapshot](#benchmark-snapshot)
- [Commands](#commands)
- [Maintenance](#maintenance)
- [Repo-Local Mode](#repo-local-mode)

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

## Benchmark Snapshot

Pexo's strongest benchmark is not raw local scan speed. It is **compression plus recollection**: keeping active AI context small while still returning the right answer later.

How to read this:
- **Before Pexo** is the naive context you would carry if you replayed the full corpus for every question.
- **After Pexo** is the measured `context_size_tokens` recorded by the Pexo-managed sessions.
- **Accuracy** is exact-match against the expected answer for every workload in the suite.

### Host System

| Metric | Value |
| :--- | :--- |
| CPU | `Intel(R) Core(TM) i9-14900K` |
| RAM | `47.72 GB` |
| Python | `3.12.10` |
| Pexo version | `1.1.1` |
| Memory backend | `keyword` |

### Summary

| Suite | Before Pexo | After Pexo | Reduction | Accuracy |
| :--- | ---: | ---: | ---: | ---: |
| Massive Repo Retrieval | `53,080,530` tokens | `17,004` tokens | `3121.65x` | `100.00%` |
| Massive Timeline Recollection | `9,792,312` tokens | `18,703` tokens | `523.57x` | `100.00%` |
| Massive Handoff Reconstruction | `14,209,182` tokens | `19,333` tokens | `734.97x` | `100.00%` |
| Combined totals | `77,082,024` tokens | `55,040` tokens | `1400.47x` | `100.00%` |

What this means:
- Direct replay can still be faster for one-off local scans because it skips ingestion and retrieval work.
- Pexo wins when the same project state needs to survive repeated questions, interruptions, and client handoffs without replaying the whole corpus.
- Full benchmark detail lives in:
  - `docs/benchmarks/realworld_compression_recollection_results.md`
  - `docs/benchmarks/realworld_compression_recollection_results.json`
  - `scripts/run_realworld_compression_recollection_benchmarks.py`

## Commands

| Command | Description |
| :--- | :--- |
| `pexo` | Start the local control plane. |
| `pexo --chat` | Start direct terminal chat. |
| `pexo --no-browser` | Start the local API without opening the browser. |
| `pexo --mcp` / `pexo-mcp` | Start MCP only. |
| `pexo --update` | Update the current install. |
| `pexo doctor` | Print local installation and runtime diagnostics. |
| `pexo self-test` | Verify local memory, artifact, and safety basics. |
| `pexo connect all --scope user` | Connect supported local AI clients to `pexo-mcp`. |
| `pexo warmup` | Prime local state after install or update. |
| `pexo promote full` | Repair or reinstall the standard local runtime. |
| `pexo promote vector` | Add optional semantic-memory support. |
| `pexo --chat` | Optional direct terminal chat. |
| `pexo uninstall` | Remove the current install. |
| `pexo uninstall --keep-state` | Remove the install but preserve local state. |

## Maintenance

For most users, maintenance is just:

```powershell
pexo --update
pexo doctor --json
pexo self-test --json
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
