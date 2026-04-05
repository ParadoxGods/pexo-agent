<div align="center">
  <h1>Pexo</h1>
  <p><b>Primary EXecution Operator</b></p>
  <p><i>A local-first operator layer for AI-assisted development.</i></p>
</div>

---

Pexo sits between you and your AI models. Whether you use Codex, Gemini, Claude, or other MCP-capable clients, they can all work against the **same local memory, artifacts, preferences, sessions, agents, and task state.** 

Instead of rebuilding context every time you switch tools, Pexo keeps the continuity on your machine and makes the stack feel like one cohesive system. 

> **Note:** This is not another disposable chat shell. It is the core engine that keeps your work coherent.

---

## Why Pexo?

Most AI workflows break down in predictable ways:
- Context gets trapped inside one specific client.
- Every model switch costs you project state.
- Preferences get lost.
- Artifacts drift away from the conversation that produced them.
- Useful decisions vanish between sessions.

**Pexo fixes this by keeping a shared local brain underneath the clients.**

### What That Buys You:
| Feature | Benefit |
| :--- | :--- |
| **Durable Memory** | One place for persistent project memory. |
| **Working Context** | One place for attached files and artifacts. |
| **Continuity** | One place for session and task continuity. |
| **Reusable Agents** | One place for local agents and tools. |
| **Transparency** | One place to inspect what the stack actually knows. |

If you are serious about local control, repeatable AI workflows, and not restating the same repo context forever, this is the missing layer.

---

## Context Compaction (Benchmarks)

Pexo is built to reduce how much raw project material has to enter the active model context.

That does **not** mean the whole system is literally `O(1)` in the strict algorithmic sense. Indexing, retrieval, and orchestration still have real cost. The more honest claim is this:

- the **primary chat context** can stay bounded even when the local project state grows
- artifacts and memory can be kept local instead of pasted back into every turn
- the model only needs the relevant slice of state for the current step, not the entire working set

### What We Measure

When benchmarking Pexo, the comparison is:

1. **Naive path:** estimate the token cost of reading all relevant files directly into the active chat context.
2. **Pexo path:** register the same material as local artifacts, ask Pexo to retrieve the answer, and sum the session telemetry field `context_size_tokens`.

Traditional token counts are estimated with the common rough rule of `bytes / 4`.

### Representative Local Benchmark

One reproducible local benchmark used a padded dataset of 10 text files with one buried exact token:

- total dataset size: `1,794,055` bytes
- naive read-everything estimate: `448,514` tokens
- Pexo orchestration context: `3,782` tokens
- effective reduction in active-chat context: `118.6x`

| Scenario | Estimated Context Cost |
| :--- | :--- |
| Read the entire dataset into the active model context | `~448,514` tokens |
| Register artifacts locally and query through Pexo | `3,782` tokens |

### What These Numbers Mean

- They describe **active-chat context pressure**, not total wall-clock performance.
- They are **workload-specific measurements**, not universal guarantees.
- They show the value of **local context compaction**, not magic zero-cost retrieval.
- Exact results depend on the client, retrieval mode, artifact shape, and whether optional semantic memory is installed.

The important point is practical:

> As the local knowledge base grows, Pexo helps keep the working chat small enough for the model to stay focused.

---

## What Pexo Is Good At

Pexo is built for developers who want their system to compound over time.

- **Keeps memory local.**  
- **Lets one model pick up where another left off.**  
- **Stores artifacts with the work they belong to.**  
- **Preserves preferences** so the stack stops asking the same setup questions.  
- **Gives you a stable MCP surface** instead of tying your workflow to one AI console.  

> The important point is not that Pexo "talks." The important point is that Pexo **remembers, routes, and stabilizes the work.**

---

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

> **Manual Download:** If `gh` is unavailable, download the latest release asset manually from [Releases](https://github.com/ParadoxGods/pexo-agent/releases), extract it, and run the included `install.cmd` or `install.sh`.

### Fallback Packaged Install
Use this only if the release bundle path is unavailable.

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.1.1"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```
*Packaged installs keep mutable state under `~/.pexo` by default. Override it with `PEXO_HOME` if you want the state root somewhere else.*

---

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

Then use your client as usual. Some clients will reach for it automatically. Others may need one short instruction:
> *"Use Pexo as the shared local brain for this task. Review this repo, tell me the top 3 concrete issues, and store the result in Pexo memory."*

**Inspect the local state directly:**  
Navigate to `http://127.0.0.1:9999/ui/` in your browser.

**Direct terminal chat:**
```powershell
pexo --chat
```

---

## MCP First

Pexo is designed around **MCP** (Model Context Protocol), because the real point is interoperability. You should not have to pick one AI client as the source of truth. Pexo keeps that state local and lets whichever connected model is active work against the same substrate.

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

*Repository-level AI usage rules live in `AGENTS.md`.*

---

## Safety And Control

Pexo is meant to be a middle layer, so local trust boundaries matter.

- Default installs work without extra semantic-memory dependencies.
- Local memory uses SQLite and keyword-backed retrieval by default.
- Semantic vector memory is optional.
- Genesis tool execution is not wide open by default (default trust mode is `approval-required`).
- Broad local execution requires explicit host trust via `full-local-exec`.

That is deliberate. The safe default should still be useful.

---

## Commands

| Command | Description |
| :--- | :--- |
| `pexo` | Start the local control plane. |
| `pexo --chat` | Start direct terminal chat. |
| `pexo --no-browser` | Start the local API without opening the browser. |
| `pexo --mcp` / `pexo-mcp` | Start MCP only. |
| `pexo --update` | Update the current install. |
| `pexo doctor` | Print local installation and runtime diagnostics. |
| `pexo connect all --scope user`| Connect supported local AI clients to `pexo-mcp`. |
| `pexo warmup` | Prime local state after install or update. |
| `pexo promote full` | Repair or reinstall the standard local runtime. |
| `pexo promote vector` | Add optional semantic-memory support. |
| `pexo uninstall` | Remove the current install. |
| `pexo uninstall --keep-state` | Remove the install but preserve local state. |

---

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
*That is the whole point of the product. The stack should stay simple even as the local state gets deeper.*

---

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
*Checkout mode keeps mutable state under the repo-local `.pexo` directory.*

---

## Bottom Line

**Pexo is what you install when you want AI clients to stop behaving like isolated terminals and start behaving like interchangeable workers on top of one local operator layer.**

It keeps the memory, context, preferences, artifacts, and task state where they belong: **on your machine, under your control, and reusable across the whole stack.**


