# Pexo - Primary EXecution Officer (The OpenClaw Killer)

```text
  ____  _____ __  __ ___  
 |  _ \| ____|\ \/ // _ \ 
 | |_) |  _|   \  /| | | |
 |  __/| |___  /  \| |_| |
 |_|   |_____|/_/\_\\___/ 
```

Pexo is a hyper-lightweight, purely local multi-agent orchestration primer. It lives entirely within the folder you drop it into.

**Why Pexo over OpenClaw?**
OpenClaw is viral, but it's a massive, bloated "always-on" Node.js daemon that requires heavy system access, raising severe privacy and security concerns. 

Pexo takes a different approach:
- **Zero Daemons:** It only runs when you ask your AI to use it.
- **Zero External Dependencies:** No Docker. No heavy Node environments. Pexo runs on a lightweight Python script using a local SQLite file (`pexo.db`) and ChromaDB.
- **Total Local Privacy:** Your memory, user profiles, and workspaces stay right here in the folder.
- **"Bring Your Own AI":** Just plug your favorite AI model (Gemini, Claude Code, Codex) into this directory.

## 🚀 The Epiphany Features (What makes Pexo next-level)

1. **Native MCP Server Integration:** Pexo completely obsoletes clunky REST API polling. It acts as a native **Model Context Protocol (MCP)** server. You can plug it directly into Claude Desktop, Cursor, or Zed, and its memory tools, evolution tools, and Genesis Engine tools will physically appear as clickable, native tools in your AI's UI.
2. **Self-Evolving Agents (RLAIF):** Pexo gets smarter every time you use it. When your AI makes a mistake or learns a user preference during a task, it posts a "Lesson Learned" to Pexo. Pexo *permanently mutates* the base system prompt of that agent in the database. The same mistake will never be made twice. Your swarm literally evolves to fit your exact coding style.
3. **The Global Vector Brain:** Pexo uses ChromaDB to vectorize every bug fix, architecture decision, and code snippet you complete. Before an agent writes a line of code, it semantically searches Pexo's brain for past solutions, creating an unbroken chain of persistent memory across your entire project lifecycle.
4. **The Genesis Engine (Dynamic Tool Creation):** If the AI encounters a task it can't perform (like parsing a weird file type or hitting a proprietary API), it is explicitly instructed to *write a Python tool for itself*. It POSTs the code to Pexo's Genesis Engine, which dynamically loads the script and exposes it. Pexo literally writes its own API extensions on the fly to expand its physical capabilities.
5. **Private Local UI Control Panel:** Once running, Pexo hosts a sleek, local-only web portal at `http://127.0.0.1:8000`. You can manually add custom agents, trigger brain backups, search vector memory, and view dynamically generated tools without ever exposing your data to the internet.

## How to Use (For Users)

### The 1-Liner AI Installation (Easiest)
1. Open your favorite AI model (Gemini, Claude Code, Codex, Cursor, etc.).
2. Tell the AI: **"Install Pexo from https://github.com/ParadoxGods/pexo-agent"**

The AI will automatically run the global installer, set up your Python environment, and add Pexo to your system PATH. Then, simply type `pexo` in any terminal to boot the server. The AI will then tell you to open `http://127.0.0.1:8000` in your web browser to access your private Control Panel.

### Option 1: Native MCP Integration (Recommended)
You can connect Pexo directly to **Claude Desktop** or **Cursor** so the AI natively understands its tools.
Add this to your `claude_desktop_config.json` or Cursor MCP settings:
```json
{
  "mcpServers": {
    "pexo": {
      "command": "cmd.exe",
      "args": ["/c", "pexo", "--mcp"]
    }
  }
}
```
*(On Mac/Linux, set command to `bash` and args to `["pexo", "--mcp"]`)*.

**Note for Windows Users:** Pexo uses ChromaDB for its Global Vector Brain. During the `pip install` phase, if you get an error regarding `hnswlib` or missing C++ Build Tools, you will need to install the [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/). 

The AI will automatically read the `PEXO_AI_PRIMER.md` protocol, set up the lightweight local Python environment, and assimilate itself under Pexo's orchestration. From that point on, Pexo acts as the primary "brain."