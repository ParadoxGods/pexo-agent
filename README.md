# Pexo

**PEXO = Primary EXecution Operator**

Pexo is a local-first operator layer for AI-assisted development.

It sits between the user and the models. Codex, Gemini, Claude, and other MCP-capable clients can all work against the same local memory, artifacts, preferences, sessions, agents, and task state. Instead of rebuilding context every time you switch tools, Pexo keeps the continuity on your machine and makes the stack feel like one working system.

This is not another disposable chat shell. It is the part of the system that keeps the work coherent.

## Why Pexo

Most AI workflows break down the same way:

- context gets trapped inside one client
- every model switch costs you project state
- preferences get lost
- artifacts drift away from the conversation that produced them
- useful decisions vanish between sessions

Pexo fixes that by keeping a shared local brain underneath the clients.

What that buys you:

- one place for durable project memory
- one place for attached files and working context
- one place for session and task continuity
- one place for reusable local agents and tools
- one place to inspect what the stack actually knows

If you are serious about local control, repeatable AI workflows, and not restating the same repo context forever, this is the missing layer.

## Empirical Context Compaction Benchmark

To quantify Pexo's context efficiency, a simulated data extraction task (""needle in a haystack"") was benchmarked comparing a traditional direct-read approach versus Pexo's orchestration and semantic vector indexing.

### Benchmark Parameters
- **Objective:** Extract 5 unique cryptographic keys embedded deep within noise.
- **Dataset:** 5 synthetic text files, each containing 500 lines (~60KB).
- **Total Ingest Volume:** ~304,000 characters.

### 1. Traditional Direct-Read (O(N) Context Scaling)
Without an orchestration layer, raw file contents must be sequentially read or grepped directly into the LLM's active session window.
- **Context Injection:** ~76,000 tokens of unstructured data added to the permanent session history.
- **Latency Impact:** Imposes a severe penalty on Time To First Token (TTFT). At typical API token processing rates, this adds 15–30 seconds of evaluation latency to *every subsequent conversational turn* within the session.
- **Cost & Reliability:** Drastically increases per-turn token costs and risks attention-decay, where the LLM fails to accurately retrieve the ""needle"" due to context saturation.

### 2. Pexo Orchestration (O(1) Context Scaling)
Using Pexo, raw data is decoupled from the conversational history. Files are registered into the local artifact vault where Pexo automatically extracts and vectorizes the text in the background. The LLM then queries the vault via semantic search.
- **Context Injection:** The raw files never touch the conversational context. Pexo's semantic search (ind_artifact) yields only the highly relevant text chunks containing the keys.
- **Telemetry Breakdown:**
  - **Supervisor Overhead:** ~39 tokens (Task graph creation)
  - **Worker Execution (Developer):** ~16 tokens (Semantic query execution)
  - **Validation (QA):** ~1 token (Verification pass)
- **Total Context Footprint:** < 4,000 tokens exposed to the main session (primarily schema definitions and the final extraction result).

### Quantitative Conclusion
- **Compaction Ratio:** Pexo achieved a **~94.7% reduction** in context pollution (76,000 -> <4,000 tokens) for this workload.
- **Scalability (O(1)):** Because the heavy lifting is offloaded to the local DB/Vector store, this workload could be scaled to 500 files, and the LLM context consumed in the primary session would remain static.

## What Pexo Is Good At

Pexo is built for developers who want the system to compound over time.

- It keeps memory local.
- It lets one model pick up where another left off.
- It stores artifacts with the work they belong to.
- It preserves preferences so the stack stops asking the same setup questions.
- It gives you a stable MCP surface instead of tying your workflow to one AI console.

The important point is not that Pexo â€œtalks.â€ The important point is that Pexo remembers, routes, and stabilizes the work.

## Install

Use the latest GitHub Release bundle. This is the canonical install path.

Windows:

```powershell
gh release download -R ParadoxGods/pexo-agent -p pexo-install-windows.zip --clobber
tar -xf .\pexo-install-windows.zip
.\install.cmd
```

macOS/Linux:

```bash
gh release download -R ParadoxGods/pexo-agent -p pexo-install-unix.tar.gz --clobber
tar -xzf pexo-install-unix.tar.gz
./install.sh
```

The release bundle installs Pexo, completes headless setup, connects supported clients, runs `pexo doctor`, and warms the local runtime.

If `gh` is unavailable, download the latest release asset manually from:

`https://github.com/ParadoxGods/pexo-agent/releases`

Then extract it and run the included `install.cmd` or `install.sh`.

### Fallback Packaged Install

Use this only if the release bundle path is unavailable.

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.1.1"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

Packaged installs keep mutable state under `~/.pexo` by default. Override it with `PEXO_HOME` if you want the state root somewhere else.

## Start Using Pexo

The normal flow is short:

1. Start Pexo.
2. Use Codex, Gemini, or Claude normally.
3. Let Pexo hold the continuity underneath.

Start the local control plane:

```powershell
pexo
```

Basic verification:

```powershell
pexo doctor --json
pexo connect all --scope user
```

Then use your client as usual. Some clients will reach for it automatically. Others may need one short instruction.

Example:

```text
Use Pexo as the shared local brain for this task.
Review this repo, tell me the top 3 concrete issues,
and store the result in Pexo memory.
```

If you want to inspect the local state directly:

`http://127.0.0.1:9999/ui/`

Optional direct terminal chat is also available:

```powershell
pexo --chat
```

## MCP First

Pexo is designed around MCP, because the real point is interoperability.

You should not have to pick one AI client as the source of truth. Pexo keeps that state local and lets whichever connected model is active work against the same substrate.

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

- default installs work without extra semantic-memory dependencies
- local memory uses SQLite and keyword-backed retrieval by default
- semantic vector memory is optional
- Genesis tool execution is not wide open by default
- the default Genesis trust mode is `approval-required`
- broad local execution requires explicit host trust via `full-local-exec`

That is deliberate. The safe default should still be useful.

## Commands

- `pexo`
  Start the local control plane.
- `pexo --chat`
  Start direct terminal chat.
- `pexo --no-browser`
  Start the local API without opening the browser.
- `pexo --mcp` or `pexo-mcp`
  Start MCP only.
- `pexo --update`
  Update the current install.
- `pexo doctor`
  Print local installation and runtime diagnostics.
- `pexo connect all --scope user`
  Connect supported local AI clients to `pexo-mcp`.
- `pexo warmup`
  Prime local state after install or update.
- `pexo promote full`
  Repair or reinstall the standard local runtime.
- `pexo promote vector`
  Add optional semantic-memory support.
- `pexo uninstall`
  Remove the current install.
- `pexo uninstall --keep-state`
  Remove the install but preserve local state.

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

That is the whole point of the product. The stack should stay simple even as the local state gets deeper.

## Repo-Local Mode

Checkout mode is for contributors or users who explicitly want a repo-backed node.

Windows:

```powershell
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

macOS/Linux:

```bash
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
```

Checkout mode keeps mutable state under the repo-local `.pexo` directory.

## Bottom Line

Pexo is what you install when you want AI clients to stop behaving like isolated terminals and start behaving like interchangeable workers on top of one local operator layer.

It keeps the memory, context, preferences, artifacts, and task state where they belong: on your machine, under your control, and reusable across the whole stack.
