import asyncio
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

import app.routers.memory as memory_router
import app.runtime as runtime_module
import app.launcher as launcher_module
import app.database as database_module
import app.direct_chat as direct_chat_module
from app.client_connect import build_client_connection_plan, connect_clients
from app.cli import headless_setup, list_presets
from app.agents.graph import FallbackPexoApp
from app.main import app
from app.database import SessionLocal, engine, init_db
from app.direct_chat import create_chat_session, get_chat_session_payload, send_chat_message
from app.mcp_server import (
    mcp,
    pexo,
    pexo_attach_context,
    pexo_attach_text_context,
    pexo_bootstrap_brain,
    pexo_exchange,
    pexo_delete_artifact,
    pexo_create_agent,
    pexo_delete_agent,
    pexo_delete_memory,
    pexo_delete_tool,
    pexo_execute_plan,
    pexo_execute_tool,
    pexo_get_admin_snapshot,
    pexo_get_artifact,
    pexo_get_memory,
    pexo_get_next_task,
    pexo_get_profile,
    pexo_get_profile_questions,
    pexo_get_runtime_status,
    pexo_get_task_status,
    pexo_get_session_activity,
    pexo_get_telemetry,
    pexo_get_tool,
    pexo_intake_prompt,
    pexo_list_agents,
    pexo_list_artifacts,
    pexo_list_profile_presets,
    pexo_list_recent_memories,
    pexo_list_sessions,
    pexo_list_tools,
    pexo_quick_setup_profile,
    pexo_recall_context,
    pexo_read_profile,
    pexo_remember_context,
    pexo_register_artifact_path,
    pexo_register_artifact_text,
    pexo_register_tool,
    pexo_promote_runtime,
    pexo_run_memory_maintenance,
    pexo_start_task,
    pexo_store_memory,
    pexo_submit_task_result,
    pexo_continue_task,
    pexo_update_agent,
    pexo_update_memory,
    pexo_update_profile,
    pexo_update_tool,
)
from app.models import AgentProfile, AgentState, ChatMessage, ChatSession, Memory, Profile
from app.paths import (
    ARTIFACTS_DIR,
    CHROMA_DB_DIR,
    CODE_ROOT,
    PEXO_DB_PATH,
    PROJECT_ROOT,
    looks_like_repo_checkout,
    resolve_managed_runtime_state_root,
    resolve_state_root,
)
from app.routers.admin import build_telemetry_payload, get_admin_snapshot
from app.routers.artifacts import (
    ArtifactPathRequest,
    ArtifactTextRequest,
    delete_artifact,
    get_artifact,
    list_artifacts,
    register_artifact_path,
    register_artifact_text,
)
from app.routers.backup import create_backup_archive
from app.routers.memory import (
    MemorySearchRequest,
    MemoryStoreRequest,
    MemoryUpdateRequest,
    delete_memory,
    maintain_memory_health,
    search_memory,
    store_memory,
    update_memory,
)
from app.routers.orchestrator import PromptRequest, SimpleContinueRequest, continue_simple_task, start_simple_task, should_require_clarification
from app.routers.profile import ProfileAnswers, build_profile_from_preset, derive_profile_answers, upsert_profile
from app.routers.tools import ToolExecutionRequest, ToolRegistrationRequest, execute_tool, register_tool, resolve_tool_path
from app.runtime import build_runtime_status
from app.launcher import build_doctor_report


class FakeCollection:
    def __init__(self):
        self.records = {}

    def upsert(self, *, ids, documents, metadatas):
        for chroma_id, document, metadata in zip(ids, documents, metadatas):
            self.records[chroma_id] = {"document": document, "metadata": metadata}

    def delete(self, *, ids):
        for chroma_id in ids:
            self.records.pop(chroma_id, None)

    def query(self, *, query_texts, n_results):
        query = query_texts[0].lower()
        matches = []
        for chroma_id, record in self.records.items():
            if query in record["document"].lower():
                matches.append((chroma_id, record))
        matches = matches[:n_results]
        return {
            "documents": [[record["document"] for _, record in matches]],
            "ids": [[chroma_id for chroma_id, _ in matches]],
            "metadatas": [[record["metadata"] for _, record in matches]],
            "distances": [[0.0 for _ in matches]],
        }


class HardeningTests(unittest.TestCase):
    def tearDown(self):
        engine.dispose()
        for _ in range(10):
            try:
                PEXO_DB_PATH.unlink(missing_ok=True)
                break
            except PermissionError:
                engine.dispose()
                time.sleep(0.05)
        shutil.rmtree(CHROMA_DB_DIR, ignore_errors=True)
        shutil.rmtree(ARTIFACTS_DIR, ignore_errors=True)

    def test_init_db_creates_all_tables_without_preimporting_models(self):
        engine.dispose()
        PEXO_DB_PATH.unlink(missing_ok=True)

        init_db()

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        self.assertTrue(
            {"profiles", "agent_profiles", "memories", "dynamic_tools", "agent_states", "workspaces", "artifacts", "system_settings", "chat_sessions", "chat_messages"}.issubset(table_names)
        )

    def test_init_db_seeds_core_agents(self):
        init_db()
        db = SessionLocal()
        try:
            agents = db.query(AgentProfile).filter(AgentProfile.is_core.is_(True)).all()
            agent_names = {agent.name for agent in agents}
            self.assertTrue(
                {
                    "Supervisor",
                    "Developer",
                    "Time Manager",
                    "Context Cost Manager",
                    "Resource Manager",
                    "Code Organization Manager",
                }.issubset(agent_names)
            )
        finally:
            db.close()

    def test_apply_sqlite_pragmas_tolerates_locked_wal_upgrade(self):
        statements = []

        class FakeCursor:
            def execute(self, statement):
                statements.append(statement)
                if statement == "PRAGMA journal_mode=WAL":
                    raise sqlite3.OperationalError("database is locked")

        database_module._apply_sqlite_pragmas(FakeCursor())
        self.assertEqual(
            statements,
            [
                "PRAGMA busy_timeout = 30000",
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=NORMAL",
            ],
        )

    @patch("app.direct_chat.time.sleep")
    def test_direct_chat_commit_retry_recovers_from_transient_sqlite_lock(self, mock_sleep):
        tracked = object()
        error = SQLAlchemyOperationalError("statement", {}, sqlite3.OperationalError("database is locked"))

        class FakeSession:
            def __init__(self):
                self.calls = 0
                self.rollback_count = 0
                self.added = []

            def commit(self):
                self.calls += 1
                if self.calls == 1:
                    raise error

            def rollback(self):
                self.rollback_count += 1

            def add(self, obj):
                self.added.append(obj)

        fake_db = FakeSession()
        direct_chat_module._commit_with_retry(fake_db, tracked)

        self.assertEqual(fake_db.calls, 2)
        self.assertEqual(fake_db.rollback_count, 1)
        self.assertEqual(fake_db.added, [tracked])
        mock_sleep.assert_called_once()

    def test_init_db_adds_memory_lifecycle_columns(self):
        init_db()
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("memories")}
        self.assertTrue({"is_pinned", "is_archived", "compacted_into_id", "updated_at"}.issubset(columns))

    def test_resolve_tool_path_rejects_path_traversal(self):
        with self.assertRaises(HTTPException) as ctx:
            resolve_tool_path("../escaped_probe")

        self.assertEqual(ctx.exception.status_code, 400)

    def test_resolve_tool_path_keeps_tools_inside_dynamic_tool_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            tool_path = resolve_tool_path("tool_alpha", base_dir=base_dir)
            self.assertEqual(tool_path, base_dir / "tool_alpha.py")

    def test_create_backup_archive_collects_local_state_without_cache_noise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            backup_target = tmp_path / "backups"
            db_path = tmp_path / "pexo.db"
            db_path.write_text("db", encoding="utf-8")

            chroma_dir = tmp_path / "chroma_db"
            chroma_dir.mkdir()
            (chroma_dir / "index.bin").write_text("vector", encoding="utf-8")

            dynamic_tools_dir = tmp_path / "dynamic_tools"
            dynamic_tools_dir.mkdir()
            (dynamic_tools_dir / "__init__.py").write_text("", encoding="utf-8")
            (dynamic_tools_dir / "tool_alpha.py").write_text("def run(**kwargs):\n    return kwargs\n", encoding="utf-8")
            pycache_dir = dynamic_tools_dir / "__pycache__"
            pycache_dir.mkdir()
            (pycache_dir / "junk.pyc").write_text("junk", encoding="utf-8")

            archive_path = create_backup_archive(
                backup_target,
                db_path=db_path,
                chroma_dir=chroma_dir,
                dynamic_tools_dir=dynamic_tools_dir,
            )

            self.assertTrue(archive_path.exists())
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                self.assertIn("pexo.db", names)
                self.assertIn("chroma_db/index.bin", names)
                self.assertIn("dynamic_tools/tool_alpha.py", names)
                self.assertNotIn("dynamic_tools/__pycache__/junk.pyc", names)

    def test_admin_ui_has_no_remote_font_dependencies(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("fonts.gstatic.com", html)

    def test_admin_ui_supports_agent_editing_and_memory_admin(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("saveAgent()", html)
        self.assertIn("sendChat()", html)
        self.assertIn("Fetching Answer", html)
        self.assertIn("typingDots", html)
        self.assertIn("Pexo is fetching an answer", html)
        self.assertIn("saveMemory(", html)
        self.assertIn("deleteMemory(", html)
        self.assertIn("saveProfile()", html)
        self.assertIn("runMemoryMaintenance()", html)
        self.assertIn("/chat/sessions", html)
        self.assertIn("Direct Chat", html)
        self.assertIn("pexo --chat", html)
        self.assertIn("artifactList", html)
        self.assertIn("promote(", html)
        self.assertIn("/admin/snapshot", html)

    def test_install_scripts_report_progress_percentages(self):
        powershell_installer = Path("install.ps1").read_text(encoding="utf-8")
        shell_installer = Path("install.sh").read_text(encoding="utf-8")

        self.assertIn("Show-InstallProgress", powershell_installer)
        self.assertIn("HeadlessSetup", powershell_installer)
        self.assertIn("InstallDir", powershell_installer)
        self.assertIn("RepoPath", powershell_installer)
        self.assertIn("UseCurrentCheckout", powershell_installer)
        self.assertIn("AllowRepoInstall", powershell_installer)
        self.assertIn("InstallProfile", powershell_installer)
        self.assertIn("--install-dir", shell_installer)
        self.assertIn("--repo-path", shell_installer)
        self.assertIn("--use-current-checkout", shell_installer)
        self.assertIn("--allow-repo-install", shell_installer)
        self.assertIn("--install-profile", shell_installer)
        self.assertIn("gh repo clone", shell_installer)
        self.assertIn("Test-GhAuthentication", powershell_installer)
        self.assertIn("WaitForExit(5000)", powershell_installer)
        self.assertIn("if ($null -eq $stdoutText)", powershell_installer)
        self.assertIn("if ($null -eq $stderrText)", powershell_installer)
        self.assertIn("if ($null -eq $exitCode)", powershell_installer)
        self.assertIn("--disable-pip-version-check", powershell_installer)
        self.assertIn("ensurepip", powershell_installer)
        self.assertIn("Test-GitDetachedHead", powershell_installer)
        self.assertIn("[SAFE] Existing checkout protection is enabled", powershell_installer)
        self.assertIn("Running installer preflight checks", powershell_installer)
        self.assertIn("Running installer preflight checks", shell_installer)
        self.assertIn("Get-PackagedInstallTool", powershell_installer)
        self.assertIn('@("tool", "install", "--reinstall", $packageSource)', powershell_installer)
        self.assertIn('@("install", "--force", $packageSource)', powershell_installer)
        self.assertIn("uv tool install --reinstall", shell_installer)
        self.assertIn("pipx install --force", shell_installer)
        self.assertIn("uv tool update-shell", powershell_installer)
        self.assertIn("uv tool update-shell", shell_installer)
        self.assertIn("& pipx ensurepath", powershell_installer)
        self.assertIn("pipx ensurepath", shell_installer)
        self.assertIn("pexo-mcp", powershell_installer)
        self.assertIn("pexo-mcp", shell_installer)
        self.assertIn("Installing Python dependencies (", powershell_installer)
        self.assertIn("still working", powershell_installer)
        self.assertIn("Same-shell PATH activation verified", powershell_installer)
        self.assertIn("Priming local runtime", powershell_installer)
        self.assertIn('ArgumentList @("warmup", "--quiet")', powershell_installer)
        self.assertIn("Ready-to-paste Windows MCP config", powershell_installer)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", powershell_installer)
        self.assertIn("--headless-setup", shell_installer)
        self.assertIn("--skip-update", shell_installer)
        self.assertIn("gh auth status -h github.com", shell_installer)
        self.assertIn("ensurepip --upgrade", shell_installer)
        self.assertIn("git_checkout_detached_at", shell_installer)
        self.assertIn("Protected checkout left untouched", shell_installer)
        self.assertIn("Same-shell PATH activation verified", shell_installer)
        self.assertIn("Priming local runtime", shell_installer)
        self.assertIn('"$PEXO_DIR/pexo" warmup --quiet', shell_installer)
        self.assertIn("print_progress 100", shell_installer)
        self.assertIn("Installing Python dependencies (", shell_installer)
        self.assertIn("still working", shell_installer)
        self.assertIn("Ready-to-paste MCP config", shell_installer)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", shell_installer)

    def test_launchers_expose_headless_setup_commands(self):
        shell_launcher = Path("pexo").read_text(encoding="utf-8")
        batch_launcher = Path("pexo.bat").read_text(encoding="utf-8")

        self.assertIn("--list-presets", shell_launcher)
        self.assertIn("--headless-setup", shell_launcher)
        self.assertIn("--promote", shell_launcher)
        self.assertIn("--update", shell_launcher)
        self.assertIn("--doctor", shell_launcher)
        self.assertIn("--connect", shell_launcher)
        self.assertIn("warmup", shell_launcher)
        self.assertIn("--no-browser", shell_launcher)
        self.assertIn("--offline", shell_launcher)
        self.assertIn("--skip-update", shell_launcher)
        self.assertIn("requirements-core.txt", shell_launcher)
        self.assertIn("requirements-mcp.txt", shell_launcher)
        self.assertIn("requirements-full.txt", shell_launcher)
        self.assertIn("requirements-vector.txt", shell_launcher)
        self.assertIn("ensurepip --upgrade", shell_launcher)
        self.assertIn("git_checkout_detached", shell_launcher)
        self.assertIn("-m app.launcher warmup", shell_launcher)
        self.assertIn("Dependency marker", shell_launcher)
        self.assertIn(".pexo-update-check", shell_launcher)
        self.assertIn("--list-presets", batch_launcher)
        self.assertIn("--headless-setup", batch_launcher)
        self.assertIn("--promote", batch_launcher)
        self.assertIn("--update", batch_launcher)
        self.assertIn("--doctor", batch_launcher)
        self.assertIn("--connect", batch_launcher)
        self.assertIn("warmup", batch_launcher)
        self.assertIn("--no-browser", batch_launcher)
        self.assertIn("--offline", batch_launcher)
        self.assertIn("--skip-update", batch_launcher)
        self.assertIn("Run 'pexo update' for full git or auth output.", batch_launcher)
        self.assertIn("requirements-core.txt", batch_launcher)
        self.assertIn("requirements-mcp.txt", batch_launcher)
        self.assertIn("requirements-full.txt", batch_launcher)
        self.assertIn("requirements-vector.txt", batch_launcher)
        self.assertIn(":ensure_venv_pip", batch_launcher)
        self.assertIn(":checkout_detached", batch_launcher)
        self.assertIn("-m app.launcher warmup", batch_launcher)
        self.assertIn("Dependency marker", batch_launcher)
        self.assertIn(".pexo-update-check", batch_launcher)

    def test_windows_wrappers_bypass_execution_policy(self):
        install_wrapper = Path("install.cmd").read_text(encoding="utf-8")
        uninstall_wrapper = Path("uninstall.cmd").read_text(encoding="utf-8")
        bootstrap_wrapper = Path("bootstrap.cmd").read_text(encoding="utf-8")

        self.assertIn("ExecutionPolicy Bypass", install_wrapper)
        self.assertIn("install.ps1", install_wrapper)
        self.assertIn("ExecutionPolicy Bypass", uninstall_wrapper)
        self.assertIn("uninstall.ps1", uninstall_wrapper)
        self.assertIn("ExecutionPolicy Bypass", bootstrap_wrapper)
        self.assertIn("bootstrap.ps1", bootstrap_wrapper)

    def test_bootstrap_scripts_provide_standalone_ai_install_path(self):
        bootstrap_ps = Path("bootstrap.ps1").read_text(encoding="utf-8")
        bootstrap_sh = Path("bootstrap.sh").read_text(encoding="utf-8")

        self.assertIn('[string]$Ref = "v1.0"', bootstrap_ps)
        self.assertIn('throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join \' \')"', bootstrap_ps)
        self.assertNotIn('throw "Command failed with exit code $LASTEXITCODE:', bootstrap_ps)
        self.assertIn('[string]$ConnectClients = "all"', bootstrap_ps)
        self.assertIn('Invoke-External -Percent 20 -Message "Installing packaged Pexo tool" -FilePath "uv"', bootstrap_ps)
        self.assertIn('Invoke-External -Percent 20 -Message "Installing packaged Pexo tool" -FilePath "pipx"', bootstrap_ps)
        self.assertIn('Standalone bootstrap does not support repo-local install', bootstrap_ps)
        self.assertIn('Invoke-DoctorCommand -Percent 92 -CommandPath "pexo"', bootstrap_ps)
        self.assertIn('Invoke-ConnectCommand -Percent 97 -CommandPath "pexo" -ClientTarget $ConnectClients', bootstrap_ps)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", bootstrap_ps)
        self.assertIn('REF="v1.0"', bootstrap_sh)
        self.assertIn('CONNECT_CLIENTS="all"', bootstrap_sh)
        self.assertIn('uv tool install --reinstall "$PACKAGE_SOURCE"', bootstrap_sh)
        self.assertIn('pipx install --force "$PACKAGE_SOURCE"', bootstrap_sh)
        self.assertIn("Standalone bootstrap does not support repo-local install", bootstrap_sh)
        self.assertIn('run_doctor 92 pexo', bootstrap_sh)
        self.assertIn('run_connect 97 "$CONNECT_CLIENTS" pexo', bootstrap_sh)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", bootstrap_sh)

    def test_dependency_profiles_split_core_mcp_and_full_runtime(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        core_requirements = Path("requirements-core.txt").read_text(encoding="utf-8")
        mcp_requirements = Path("requirements-mcp.txt").read_text(encoding="utf-8")
        full_requirements = Path("requirements-full.txt").read_text(encoding="utf-8")
        vector_requirements = Path("requirements-vector.txt").read_text(encoding="utf-8")
        constraints = Path("constraints.txt").read_text(encoding="utf-8")

        self.assertIn("-r requirements-full.txt", requirements)
        self.assertIn("fastapi==0.115.0", core_requirements)
        self.assertIn("pydantic==2.12.5", core_requirements)
        self.assertNotIn("chromadb", core_requirements)
        self.assertIn("mcp==1.27.0", mcp_requirements)
        self.assertNotIn("uvicorn", mcp_requirements)
        self.assertIn("uvicorn==0.32.0", full_requirements)
        self.assertIn("langgraph==0.2.0", full_requirements)
        self.assertNotIn("chromadb", full_requirements)
        self.assertIn("-r requirements-full.txt", vector_requirements)
        self.assertIn("chromadb==0.4.24", vector_requirements)
        self.assertNotIn("sentence-transformers", full_requirements)
        self.assertNotIn("langchain-core", full_requirements)
        self.assertNotIn("psutil", full_requirements)
        self.assertIn("chromadb==0.4.24", constraints)
        self.assertIn("mcp==1.27.0", constraints)

    def test_pyproject_exposes_github_native_console_scripts(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "pexo-agent"', pyproject)
        self.assertIn('pexo = "app.launcher:main"', pyproject)
        self.assertIn('pexo-mcp = "app.launcher:mcp_main"', pyproject)
        self.assertIn('"mcp==1.27.0"', pyproject)
        self.assertIn("[project.optional-dependencies]", pyproject)
        self.assertIn('full = ["uvicorn==0.32.0", "langgraph==0.2.0"]', pyproject)
        self.assertIn('vector = ["uvicorn==0.32.0", "langgraph==0.2.0", "chromadb==0.4.24"]', pyproject)

    def test_release_workflow_builds_and_publishes_package_assets(self):
        workflow = Path(".github/workflows/release-package.yml").read_text(encoding="utf-8")
        bundle_script = Path("scripts/build_release_bundle.py").read_text(encoding="utf-8")
        self.assertIn("Build And Release Package", workflow)
        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn("actions/setup-python@v6", workflow)
        self.assertIn('python -m build', workflow)
        self.assertIn("SHA256SUMS.txt", workflow)
        self.assertIn("python scripts/build_release_bundle.py", workflow)
        self.assertIn("pexo-install-windows.zip", bundle_script)
        self.assertIn("pexo-install-unix.tar.gz", bundle_script)
        self.assertIn("pexo-install-manifest.json", bundle_script)
        self.assertIn("softprops/action-gh-release@v2.6.1", workflow)
        self.assertIn("contents: write", workflow)
        self.assertNotIn("FORCE_JAVASCRIPT_ACTIONS_TO_NODE24", workflow)

    def test_release_bundle_manifest_includes_dependency_fingerprint(self):
        module_path = Path("scripts/build_release_bundle.py").resolve()
        spec = importlib.util.spec_from_file_location("build_release_bundle", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        manifest = module._build_manifest("pexo_agent-1.0-py3-none-any.whl", "abc123")

        self.assertEqual(manifest["wheel"]["sha256"], "abc123")
        self.assertIn("dependency_fingerprint", manifest)
        self.assertEqual(len(manifest["dependency_fingerprint"]), 64)
        self.assertEqual(manifest["bundle_root"], ".")
        self.assertEqual(manifest["commands"]["windows"][-1], ".\\install.cmd")
        self.assertEqual(manifest["commands"]["unix"][-1], "./install.sh")

    def test_readme_documents_packaged_install_and_pexo_mcp(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("gh release download", readme)
        self.assertIn("pexo-install-windows.zip", readme)
        self.assertIn("pexo-install-unix.tar.gz", readme)
        self.assertIn('pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0"', readme)
        self.assertIn("pexo-mcp", readme)
        self.assertIn("PEXO_HOME", readme)
        self.assertIn("pexo doctor", readme)
        self.assertIn("pexo connect all --scope user", readme)
        self.assertIn("pexo --chat", readme)
        self.assertIn("use it automatically", readme)
        self.assertIn("default local brain", readme)
        self.assertIn("Repository-level AI usage rules live in `AGENTS.md`", readme)
        self.assertIn("Existing Git checkouts are protected by default", readme)
        self.assertIn(".\\install.cmd", readme)
        self.assertIn("./install.sh", readme)
        self.assertIn("-AllowRepoInstall", readme)
        self.assertIn("## Install", readme)
        self.assertIn("## Use", readme)
        self.assertNotIn("## Core Architecture", readme)
        self.assertNotIn("## Fleet Quickstart", readme)
        self.assertNotIn("pexo_exchange", readme)

    def test_agents_file_documents_safe_windows_install_path(self):
        agents_doc = Path("AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## Default behavior", agents_doc)
        self.assertIn("gh release download", agents_doc)
        self.assertIn("pexo-install-windows.zip", agents_doc)
        self.assertIn("pexo-install-unix.tar.gz", agents_doc)
        self.assertIn('pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0"', agents_doc)
        self.assertIn("pexo connect all --scope user", agents_doc)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON", agents_doc)
        self.assertIn("Existing Git checkouts are protected by default", agents_doc)
        self.assertIn(".\\install.cmd", agents_doc)
        self.assertIn("./install.sh", agents_doc)
        self.assertIn("-AllowRepoInstall", agents_doc)
        self.assertIn("Do not touch the current repo", agents_doc)
        self.assertIn("Do not execute raw remote scripts", agents_doc)
        self.assertIn("Do not repeat those steps unless the user asks for verification", agents_doc)
        self.assertIn("pexo --chat", agents_doc)
        self.assertIn("## Simple Task Flow", agents_doc)
        self.assertIn('even when the user does not explicitly say "use Pexo"', agents_doc)
        self.assertIn("`pexo`", agents_doc)
        self.assertIn("pexo_exchange", agents_doc)
        self.assertIn("pexo_bootstrap_brain", agents_doc)
        self.assertIn("pexo_start_task", agents_doc)
        self.assertIn("pexo_continue_task", agents_doc)
        self.assertIn("pexo_get_task_status", agents_doc)
        self.assertIn("pexo_recall_context", agents_doc)
        self.assertIn("pexo_remember_context", agents_doc)
        self.assertIn("pexo_attach_context", agents_doc)
        self.assertIn("user_message", agents_doc)

    def test_launcher_help_mentions_terminal_chat_mode(self):
        buffer = StringIO()
        with redirect_stdout(buffer):
            launcher_module.print_help()
        output = buffer.getvalue()
        self.assertIn("pexo --chat", output)
        self.assertIn("Starts a direct terminal chat with Pexo", output)
        self.assertIn("pexo warmup", output)

    def test_terminal_chat_uses_fetch_animation_label(self):
        launcher_text = Path("app/launcher.py").read_text(encoding="utf-8")
        self.assertIn("pexo> fetching answer", launcher_text)

    def test_checkout_wrappers_advertise_terminal_chat_mode(self):
        windows_wrapper = Path("pexo.bat").read_text(encoding="utf-8")
        unix_wrapper = Path("pexo").read_text(encoding="utf-8")
        self.assertIn("pexo --chat", windows_wrapper)
        self.assertIn("python.exe -m app.launcher chat", windows_wrapper)
        self.assertIn("pexo --chat", unix_wrapper)
        self.assertIn('-m app.launcher chat "$@"', unix_wrapper)

    def test_release_bundle_installers_exist_and_emit_summary(self):
        install_ps = Path("release_bundle/install.ps1").read_text(encoding="utf-8")
        install_sh = Path("release_bundle/install.sh").read_text(encoding="utf-8")
        install_cmd = Path("release_bundle/install.cmd").read_text(encoding="utf-8")

        self.assertIn("SHA256SUMS.txt", install_ps)
        self.assertIn("pipx", install_ps)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", install_ps)
        self.assertIn(".pexo-install.json", install_ps)
        self.assertIn("Write-Progress", install_ps)
        self.assertIn("Priming local runtime", install_ps)
        self.assertIn('ArgumentList @("warmup", "--quiet")', install_ps)
        self.assertIn("install.ps1", install_cmd)
        self.assertIn("SHA256SUMS.txt", install_sh)
        self.assertIn("pipx install --force", install_sh)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", install_sh)
        self.assertIn(".pexo-install.json", install_sh)
        self.assertIn("Priming local runtime", install_sh)
        self.assertIn('"$COMMAND_PATH" warmup --quiet', install_sh)
        self.assertIn("Resetting managed runtime environment", install_ps)
        self.assertIn("Resetting managed runtime environment", install_sh)
        self.assertIn(".pexo-deps-profile", install_ps)
        self.assertIn(".pexo-deps-profile", install_sh)
        self.assertIn("pexo --update", install_ps)
        self.assertIn("pexo --update", install_sh)
        self.assertIn("wheel_sha256", install_ps)
        self.assertIn("dependency_fingerprint", install_ps)
        self.assertIn("wheel_sha256", install_sh)
        self.assertIn("dependency_fingerprint", install_sh)

    def test_doctor_report_surfaces_guidance_and_install_health(self):
        report = build_doctor_report()

        self.assertIn(report["install_mode"], {"checkout", "packaged"})
        self.assertIn("update", report["guidance"])
        self.assertIn("uninstall", report["guidance"])
        self.assertIn("mcp", report["guidance"])
        self.assertIn("connect", report["guidance"])
        self.assertIn("vector", report["guidance"])
        self.assertIn("python", report["commands"])
        self.assertIn("codex", report["commands"])
        self.assertIn("claude", report["commands"])
        self.assertIn("gemini", report["commands"])
        self.assertIsInstance(report["issues"], list)
        self.assertIn("install_metadata", report["paths"])

    @patch("app.launcher.running_from_repo_checkout", return_value=False)
    def test_packaged_doctor_guidance_uses_pexo_update(self, _mock_checkout):
        report = launcher_module.build_doctor_report()
        self.assertEqual(report["guidance"]["update"], "pexo --update")

    @patch("app.launcher.resolve_editable_source_root", return_value=Path("C:/CDXCLI/pexo"))
    @patch("app.launcher._editable_install_artifacts_present", return_value=True)
    @patch("app.launcher.running_from_repo_checkout", return_value=False)
    def test_packaged_doctor_reports_editable_checkout_residue(self, _mock_checkout, _mock_editable_residue, _mock_editable_root):
        report = launcher_module.build_doctor_report()

        self.assertEqual(report["install_mode"], "packaged")
        self.assertTrue(report["install_source"]["editable"])
        self.assertTrue(report["install_source"]["editable_residue"])
        self.assertIn("editable checkout", " ".join(report["issues"]).lower())

    @patch("app.launcher.running_from_repo_checkout", return_value=False)
    @patch("app.launcher._exec_update_helper", return_value=0)
    @patch("app.launcher._prepare_packaged_update_helper")
    @patch("app.launcher._build_packaged_update_plan")
    def test_run_update_executes_packaged_update_helper(
        self,
        mock_build_plan,
        mock_prepare,
        mock_exec,
        _mock_checkout,
    ):
        mock_build_plan.return_value = {
            "version": "1.0",
            "release_url": "https://github.com/ParadoxGods/pexo-agent/releases/tag/v1.0",
            "wheel_name": "pexo_agent-1.0-py3-none-any.whl",
            "wheel_url": "https://example.invalid/pexo_agent-1.0-py3-none-any.whl",
            "checksum_url": "https://example.invalid/SHA256SUMS.txt",
            "target_python": sys.executable,
            "install_metadata_path": "C:/Users/dustin/.pexo/.pexo-install.json",
            "update_stamp_path": "C:/Users/dustin/.pexo/.pexo-update-check",
            "operation": "wheel-only",
            "install_label": "Installing update (wheel refresh only)...",
            "pip_args": ["install", "--disable-pip-version-check", "--force-reinstall", "--no-deps"],
            "wheel_sha256": "deadbeef",
            "dependency_fingerprint": "cafebabe",
        }
        mock_prepare.return_value = (Path("C:/temp/pexo_update_helper.py"), Path("C:/temp/update-plan.json"))

        self.assertEqual(launcher_module.run_update(), 0)
        mock_prepare.assert_called_once_with(mock_build_plan.return_value)
        mock_exec.assert_called_once()

    def test_packaged_update_helper_removes_editable_install_artifacts(self):
        self.assertIn("__editable__.pexo_agent-", launcher_module.PACKAGED_UPDATE_HELPER)
        self.assertIn("__editable___pexo_agent_", launcher_module.PACKAGED_UPDATE_HELPER)

    @patch("app.launcher.running_from_repo_checkout", return_value=False)
    @patch("app.launcher._maybe_stop_existing_server_for_update", return_value="stopped")
    @patch("app.launcher._exec_update_helper", return_value=0)
    @patch("app.launcher._prepare_packaged_update_helper")
    @patch("app.launcher._build_packaged_update_plan")
    def test_run_update_stops_running_server_before_packaged_update(
        self,
        mock_build_plan,
        mock_prepare,
        mock_exec,
        mock_stop_server,
        _mock_checkout,
    ):
        mock_build_plan.return_value = {
            "version": "1.0",
            "release_url": "https://github.com/ParadoxGods/pexo-agent/releases/tag/v1.0",
            "wheel_name": "pexo_agent-1.0-py3-none-any.whl",
            "wheel_url": "https://example.invalid/pexo_agent-1.0-py3-none-any.whl",
            "checksum_url": "https://example.invalid/SHA256SUMS.txt",
            "target_python": "C:/Users/dustin/.pexo/venv/Scripts/python.exe",
            "install_metadata_path": "C:/Users/dustin/.pexo/.pexo-install.json",
            "update_stamp_path": "C:/Users/dustin/.pexo/.pexo-update-check",
            "operation": "wheel-only",
            "install_label": "Installing update (wheel refresh only)...",
            "pip_args": ["install", "--disable-pip-version-check", "--force-reinstall", "--no-deps"],
            "wheel_sha256": "deadbeef",
            "dependency_fingerprint": "cafebabe",
        }
        mock_prepare.return_value = (Path("C:/temp/pexo_update_helper.py"), Path("C:/temp/update-plan.json"))

        self.assertEqual(launcher_module.run_update(), 0)
        mock_stop_server.assert_called_once_with("127.0.0.1", 9999)
        mock_exec.assert_called_once()

    @patch("app.launcher.running_from_repo_checkout", return_value=False)
    @patch("app.launcher._maybe_stop_existing_server_for_update", return_value="unavailable")
    @patch("app.launcher._build_packaged_update_plan")
    def test_run_update_refuses_packaged_update_when_server_cannot_be_stopped(
        self,
        mock_build_plan,
        mock_stop_server,
        _mock_checkout,
    ):
        mock_build_plan.return_value = {
            "version": "1.0",
            "operation": "wheel-only",
        }

        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(launcher_module.run_update(), 1)
        self.assertIn("must be stopped before this packaged update can continue", stderr.getvalue())

    @patch("app.launcher.running_from_repo_checkout", return_value=False)
    @patch("app.launcher._local_pexo_http_available", return_value=True)
    @patch("app.launcher._write_update_stamp")
    @patch("app.launcher._prepare_packaged_update_helper")
    @patch("app.launcher._build_packaged_update_plan")
    def test_run_update_skips_when_latest_wheel_is_already_installed(
        self,
        mock_build_plan,
        mock_prepare,
        mock_write_stamp,
        _mock_local_server,
        _mock_checkout,
    ):
        mock_build_plan.return_value = {
            "version": "1.0",
            "operation": "skip",
        }

        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(launcher_module.run_update(), 0)
        mock_prepare.assert_not_called()
        mock_write_stamp.assert_called_once()
        self.assertIn("Restart it", stdout.getvalue())

    @patch("app.launcher._read_install_metadata")
    @patch("app.launcher._editable_install_artifacts_present", return_value=False)
    @patch("app.launcher._fetch_release_manifest")
    @patch("app.launcher._fetch_latest_release")
    def test_build_packaged_update_plan_uses_wheel_only_refresh_when_dependencies_match(
        self,
        mock_fetch_release,
        mock_fetch_manifest,
        _mock_editable_residue,
        mock_read_metadata,
    ):
        mock_fetch_release.return_value = {
            "tag_name": "v1.0",
            "html_url": "https://github.com/ParadoxGods/pexo-agent/releases/tag/v1.0",
            "assets": [
                {"name": "pexo_agent-1.0-py3-none-any.whl", "browser_download_url": "https://example.invalid/pexo_agent-1.0-py3-none-any.whl"},
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/SHA256SUMS.txt"},
                {"name": "pexo-install-manifest.json", "browser_download_url": "https://example.invalid/pexo-install-manifest.json"},
            ],
        }
        mock_fetch_manifest.return_value = {
            "wheel": {"sha256": "new-wheel"},
            "dependency_fingerprint": "same-deps",
        }
        mock_read_metadata.return_value = {
            "wheel_sha256": "old-wheel",
            "dependency_fingerprint": "same-deps",
        }

        plan = launcher_module._build_packaged_update_plan()

        self.assertEqual(plan["operation"], "wheel-only")
        self.assertIn("--no-deps", plan["pip_args"])
        self.assertEqual(plan["wheel_sha256"], "new-wheel")

    @patch("app.launcher._read_install_metadata")
    @patch("app.launcher._editable_install_artifacts_present", return_value=False)
    @patch("app.launcher._fetch_release_manifest")
    @patch("app.launcher._fetch_latest_release")
    def test_build_packaged_update_plan_skips_when_matching_wheel_is_installed(
        self,
        mock_fetch_release,
        mock_fetch_manifest,
        _mock_editable_residue,
        mock_read_metadata,
    ):
        mock_fetch_release.return_value = {
            "tag_name": "v1.0",
            "html_url": "https://github.com/ParadoxGods/pexo-agent/releases/tag/v1.0",
            "assets": [
                {"name": "pexo_agent-1.0-py3-none-any.whl", "browser_download_url": "https://example.invalid/pexo_agent-1.0-py3-none-any.whl"},
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/SHA256SUMS.txt"},
                {"name": "pexo-install-manifest.json", "browser_download_url": "https://example.invalid/pexo-install-manifest.json"},
            ],
        }
        mock_fetch_manifest.return_value = {
            "wheel": {"sha256": "same-wheel"},
            "dependency_fingerprint": "same-deps",
        }
        mock_read_metadata.return_value = {
            "wheel_sha256": "same-wheel",
            "dependency_fingerprint": "same-deps",
        }

        plan = launcher_module._build_packaged_update_plan()

        self.assertEqual(plan["operation"], "skip")
        self.assertEqual(plan["pip_args"], [])

    @patch("app.launcher._read_install_metadata")
    @patch("app.launcher._editable_install_artifacts_present", return_value=True)
    @patch("app.launcher._fetch_release_manifest")
    @patch("app.launcher._fetch_latest_release")
    def test_build_packaged_update_plan_normalizes_editable_residue_even_when_wheel_matches(
        self,
        mock_fetch_release,
        mock_fetch_manifest,
        _mock_editable_residue,
        mock_read_metadata,
    ):
        mock_fetch_release.return_value = {
            "tag_name": "v1.0",
            "html_url": "https://github.com/ParadoxGods/pexo-agent/releases/tag/v1.0",
            "assets": [
                {"name": "pexo_agent-1.0-py3-none-any.whl", "browser_download_url": "https://example.invalid/pexo_agent-1.0-py3-none-any.whl"},
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/SHA256SUMS.txt"},
                {"name": "pexo-install-manifest.json", "browser_download_url": "https://example.invalid/pexo-install-manifest.json"},
            ],
        }
        mock_fetch_manifest.return_value = {
            "wheel": {"sha256": "same-wheel"},
            "dependency_fingerprint": "same-deps",
        }
        mock_read_metadata.return_value = {
            "wheel_sha256": "same-wheel",
            "dependency_fingerprint": "same-deps",
        }

        plan = launcher_module._build_packaged_update_plan()

        self.assertEqual(plan["operation"], "wheel-only")
        self.assertEqual(plan["install_label"], "Normalizing packaged runtime...")
        self.assertTrue(plan["editable_residue"])

    def test_resolve_runtime_python_executable_prefers_venv_python_for_console_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_root = Path(tmpdir) / "venv"
            if launcher_module.os.name == "nt":
                scripts_dir = venv_root / "Scripts"
                console_path = scripts_dir / "pexo.exe"
                python_path = scripts_dir / "python.exe"
            else:
                scripts_dir = venv_root / "bin"
                console_path = scripts_dir / "pexo"
                python_path = scripts_dir / "python"
            scripts_dir.mkdir(parents=True)
            console_path.write_text("", encoding="utf-8")
            python_path.write_text("", encoding="utf-8")

            with patch.object(sys, "executable", str(console_path)), patch.object(sys, "prefix", str(venv_root)):
                resolved = launcher_module._resolve_runtime_python_executable()

            self.assertEqual(
                os.path.normcase(os.path.realpath(resolved)),
                os.path.normcase(os.path.realpath(str(python_path))),
            )

    def test_exec_update_helper_uses_plan_target_python(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            helper_path = root / "pexo_update_helper.py"
            helper_path.write_text("print('ok')", encoding="utf-8")
            plan_path = root / "update-plan.json"
            target_python = root / "python.exe"
            target_python.write_text("", encoding="utf-8")
            plan_path.write_text(json.dumps({"target_python": str(target_python)}), encoding="utf-8")

            with patch("app.launcher.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    [str(target_python), str(helper_path), str(plan_path)],
                    0,
                )
                result = launcher_module._exec_update_helper(helper_path, plan_path)

            self.assertEqual(result, 0)
            mock_run.assert_called_once_with(
                [str(target_python), str(helper_path), str(plan_path)],
                check=False,
            )

    def test_packaged_update_helper_uses_wheel_overlay_sync(self):
        helper = launcher_module.PACKAGED_UPDATE_HELPER
        self.assertIn("_overlay_wheel", helper)
        self.assertIn("_sync_dependencies", helper)
        self.assertIn("_print_progress", helper)
        self.assertIn("_warmup", helper)
        self.assertIn('"-m", "app.launcher", "warmup", "--quiet"', helper)
        self.assertIn("Requires-Dist", helper)
        self.assertIn("zipfile.ZipFile", helper)
        self.assertNotIn('[target_python, "-m", "pip", *plan["pip_args"], str(wheel_path)]', helper)

    @patch("app.launcher.connect_clients", return_value={"status": "partial", "results": []})
    @patch("app.launcher.build_runtime_status", return_value={"active_profile": "mcp"})
    @patch("app.launcher.ensure_db_ready")
    def test_run_warmup_primes_local_state(self, mock_db_ready, _mock_runtime_status, mock_connect_clients):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "state"
            artifacts = root / "artifacts"
            tools = root / "dynamic_tools"
            with patch.object(launcher_module, "PROJECT_ROOT", root), patch.object(launcher_module, "ARTIFACTS_DIR", artifacts), patch.object(launcher_module, "DYNAMIC_TOOLS_DIR", tools):
                self.assertEqual(launcher_module.run_warmup(quiet=True), 0)

        mock_db_ready.assert_called_once()
        _mock_runtime_status.assert_called_once()
        mock_connect_clients.assert_called_once_with(target="all", scope="user", dry_run=True, verify_existing=False)

    @patch("app.direct_chat._run_gemini_turn", return_value="ok")
    @patch("app.direct_chat.build_client_connection_plan")
    def test_run_direct_chat_backend_prefers_fast_model_for_conversation(self, mock_plan, mock_run_gemini):
        mock_plan.return_value = {
            "available": True,
            "invoker": "gemini",
        }

        result = direct_chat_module.run_direct_chat_backend(
            "gemini",
            "hello",
            str(PROJECT_ROOT),
            timeout_seconds=30,
            mode="conversation",
        )

        self.assertEqual(result, "ok")
        mock_run_gemini.assert_called_once()
        self.assertEqual(mock_run_gemini.call_args.kwargs["model_override"], "gemini-2.5-flash")

    @patch("app.direct_chat._run_gemini_turn")
    @patch("app.direct_chat.build_client_connection_plan")
    def test_run_direct_chat_backend_does_not_retry_model_after_timeout(self, mock_plan, mock_run_gemini):
        mock_plan.return_value = {
            "available": True,
            "invoker": "gemini",
        }
        mock_run_gemini.side_effect = RuntimeError("Gemini direct chat timed out after 10 seconds.")

        with self.assertRaises(RuntimeError):
            direct_chat_module.run_direct_chat_backend(
                "gemini",
                "who is the president",
                str(PROJECT_ROOT),
                timeout_seconds=10,
                mode="conversation",
            )

        mock_run_gemini.assert_called_once()

    @patch("app.direct_chat._run_gemini_turn")
    @patch("app.direct_chat.build_client_connection_plan")
    def test_run_direct_chat_backend_retries_without_model_for_invalid_model_error(self, mock_plan, mock_run_gemini):
        mock_plan.return_value = {
            "available": True,
            "invoker": "gemini",
        }
        mock_run_gemini.side_effect = [
            RuntimeError("Unknown model 'gemini-2.5-flash'."),
            "ok",
        ]

        result = direct_chat_module.run_direct_chat_backend(
            "gemini",
            "hello",
            str(PROJECT_ROOT),
            timeout_seconds=10,
            mode="conversation",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(mock_run_gemini.call_count, 2)
        self.assertEqual(mock_run_gemini.call_args_list[0].kwargs["model_override"], "gemini-2.5-flash")
        self.assertIsNone(mock_run_gemini.call_args_list[1].kwargs["model_override"])

    @patch("app.direct_chat.build_client_connection_plan")
    def test_resolve_backend_name_prefers_gemini_for_conversation_and_codex_for_tasks(self, mock_plan):
        def fake_plan(client, scope="user"):
            return {
                "available": client in {"codex", "gemini"},
                "invoker": client,
            }

        mock_plan.side_effect = fake_plan

        self.assertEqual(direct_chat_module._resolve_backend_name("auto", mode="conversation"), "gemini")
        self.assertEqual(direct_chat_module._resolve_backend_name("auto", mode="brain_lookup"), "gemini")
        self.assertEqual(direct_chat_module._resolve_backend_name("auto", mode="task"), "codex")

    @patch("app.direct_chat.build_client_connection_plan")
    def test_resolve_backend_name_prefers_capability_specific_backends(self, mock_plan):
        def fake_plan(client, scope="user"):
            return {
                "available": client in {"codex", "gemini", "claude"},
                "invoker": client,
            }

        mock_plan.side_effect = fake_plan

        self.assertEqual(
            direct_chat_module._resolve_backend_name("auto", mode="conversation", capability="search"),
            "gemini",
        )
        self.assertEqual(
            direct_chat_module._resolve_backend_name("auto", mode="task", capability="code"),
            "codex",
        )
        self.assertEqual(
            direct_chat_module._resolve_backend_name("auto", mode="task", capability="image"),
            "codex",
        )

    @patch("app.direct_chat.build_client_connection_plan")
    def test_resolve_backend_name_falls_back_when_capability_backend_is_missing(self, mock_plan):
        availability = {
            "codex": True,
            "gemini": False,
            "claude": False,
        }

        def fake_plan(client, scope="user"):
            return {
                "available": availability.get(client, False),
                "invoker": client,
            }

        mock_plan.side_effect = fake_plan

        self.assertEqual(
            direct_chat_module._resolve_backend_name("auto", mode="conversation", capability="search"),
            "codex",
        )

        availability["codex"] = False
        availability["gemini"] = True
        self.assertEqual(
            direct_chat_module._resolve_backend_name("auto", mode="task", capability="code"),
            "gemini",
        )

    def test_infer_chat_capability_routes_search_image_and_code_turns(self):
        session = ChatSession(
            id="chat-test",
            title="Test",
            backend="gemini",
            workspace_path=str(PROJECT_ROOT),
            details={"mode": "conversation"},
        )

        self.assertEqual(
            direct_chat_module._infer_chat_capability(
                session,
                "google the latest OpenAI news",
                mode="conversation",
                direct_fact_intent=None,
            ),
            "search",
        )
        self.assertEqual(
            direct_chat_module._infer_chat_capability(
                session,
                "Create a new logo and hero image for my product.",
                mode="task",
                direct_fact_intent=None,
            ),
            "image",
        )
        self.assertEqual(
            direct_chat_module._infer_chat_capability(
                session,
                "Fix the Python API bug in this repo.",
                mode="task",
                direct_fact_intent=None,
            ),
            "code",
        )

    @patch("app.direct_chat.build_client_connection_plan")
    def test_resolve_backend_name_can_adapt_to_observed_backend_performance(self, mock_plan):
        def fake_plan(client, scope="user"):
            return {
                "available": client in {"codex", "gemini"},
                "invoker": client,
            }

        mock_plan.side_effect = fake_plan
        init_db()
        db = SessionLocal()
        try:
            direct_chat_module._record_backend_attempt(db, mode="conversation", backend_name="codex", success=True, latency_ms=450)
            direct_chat_module._record_backend_attempt(db, mode="conversation", backend_name="codex", success=True, latency_ms=420)
            direct_chat_module._record_backend_attempt(
                db,
                mode="conversation",
                backend_name="gemini",
                success=False,
                error="Gemini direct chat timed out after 6 seconds.",
            )
            direct_chat_module._record_backend_attempt(
                db,
                mode="conversation",
                backend_name="gemini",
                success=False,
                error="Gemini direct chat timed out after 6 seconds.",
            )
            db.commit()

            self.assertEqual(direct_chat_module._resolve_backend_name("auto", mode="conversation", db=db), "codex")
        finally:
            db.close()

    @patch("app.direct_chat.build_client_connection_plan")
    def test_resolve_backend_name_deprioritizes_single_timed_out_backend(self, mock_plan):
        def fake_plan(client, scope="user"):
            return {
                "available": client in {"codex", "gemini"},
                "invoker": client,
            }

        mock_plan.side_effect = fake_plan
        init_db()
        db = SessionLocal()
        try:
            direct_chat_module._record_backend_attempt(
                db,
                mode="task",
                backend_name="codex",
                success=False,
                error="Codex direct chat timed out after 25 seconds.",
            )
            db.commit()

            self.assertEqual(direct_chat_module._resolve_backend_name("auto", mode="task", db=db), "gemini")
        finally:
            db.close()

    @patch("app.direct_chat.build_client_connection_plan")
    def test_conversation_backend_candidates_push_recently_timed_out_backend_to_the_end(self, mock_plan):
        def fake_plan(client, scope="user"):
            return {
                "available": client in {"codex", "gemini", "claude"},
                "invoker": client,
            }

        mock_plan.side_effect = fake_plan
        init_db()
        db = SessionLocal()
        try:
            direct_chat_module._record_backend_attempt(
                db,
                mode="conversation",
                backend_name="gemini",
                success=False,
                error="Gemini direct chat timed out after 6 seconds.",
            )
            direct_chat_module._record_backend_attempt(
                db,
                mode="conversation",
                backend_name="gemini",
                success=False,
                error="Gemini direct chat timed out after 6 seconds.",
            )
            direct_chat_module._record_backend_attempt(db, mode="conversation", backend_name="codex", success=True, latency_ms=500)
            db.commit()

            candidates = direct_chat_module._conversation_backend_candidates("gemini", mode="conversation", db=db)

            self.assertEqual(candidates[0], "codex")
            self.assertEqual(candidates[-1], "gemini")
        finally:
            db.close()

    def test_wikipedia_candidate_scoring_prefers_current_officeholder_over_irrelevant_actor_list(self):
        actor_score = direct_chat_module._score_wikipedia_candidate(
            "who is the president",
            "List of actors who have played the president of the United States",
            "This is a list of actors who have played the role of a real or fictitious president of the United States.",
        )
        incumbent_score = direct_chat_module._score_wikipedia_candidate(
            "who is the president",
            "List of presidents of the United States",
            "The incumbent president is Donald Trump, who assumed office in 2025.",
        )

        self.assertGreater(incumbent_score, actor_score)

    def test_relevant_fact_snippet_prefers_incumbent_clause_over_leading_noise(self):
        snippet = (
            "Forces. The first president, George Washington, won a unanimous vote of the Electoral College. "
            "The incumbent president is Donald Trump, who assumed office in 2025."
        )

        answer = direct_chat_module._extract_relevant_fact_snippet(snippet, prefix="According to Wikipedia, ")

        self.assertIn("incumbent president is Donald Trump", answer)

    @patch("app.launcher._port_is_in_use", return_value=False)
    @patch("app.launcher.build_runtime_status", return_value={"installed_profiles": {"full": True}})
    @patch("uvicorn.run")
    def test_run_server_prints_pexo_banner(self, mock_uvicorn_run, _mock_status, _mock_port_in_use):
        original_no_browser = os.environ.get("PEXO_NO_BROWSER")
        try:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(launcher_module.run_server(no_browser=True), 0)
            rendered = output.getvalue()
            self.assertIn("PEXO", rendered)
            self.assertIn("Primary EXecution Operator", rendered)
            self.assertNotIn("\033[", rendered)
            self.assertIn("\n\nPEXO | Primary EXecution Operator | local-first control plane", rendered)
            mock_uvicorn_run.assert_called_once()
            self.assertEqual(mock_uvicorn_run.call_args.kwargs["use_colors"], False)
        finally:
            if original_no_browser is None:
                os.environ.pop("PEXO_NO_BROWSER", None)
            else:
                os.environ["PEXO_NO_BROWSER"] = original_no_browser

    @patch("app.launcher._maybe_restart_existing_server", return_value="unavailable")
    @patch("app.launcher._local_pexo_http_available", return_value=True)
    @patch("app.launcher._port_is_in_use", return_value=True)
    @patch("app.launcher.build_runtime_status", return_value={"installed_profiles": {"full": True}})
    @patch("uvicorn.run")
    def test_run_server_reports_existing_pexo_instance_clearly(self, mock_uvicorn_run, _mock_status, _mock_port_in_use, _mock_pexo_http, _mock_restart_prompt):
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(launcher_module.run_server(no_browser=True), 1)
        self.assertIn("Pexo already appears to be running", stderr.getvalue())
        self.assertIn("If you just updated Pexo", stderr.getvalue())
        mock_uvicorn_run.assert_not_called()

    @patch("app.launcher._maybe_restart_existing_server", return_value="restarted")
    @patch("app.launcher._local_pexo_http_available", return_value=True)
    @patch("app.launcher._port_is_in_use", side_effect=[True, False])
    @patch("app.launcher.build_runtime_status", return_value={"installed_profiles": {"full": True}})
    @patch("uvicorn.run")
    def test_run_server_can_replace_existing_pexo_instance(self, mock_uvicorn_run, _mock_status, _mock_port_in_use, _mock_pexo_http, mock_restart_prompt):
        self.assertEqual(launcher_module.run_server(no_browser=True), 0)
        mock_restart_prompt.assert_called_once_with("127.0.0.1", 9999)
        mock_uvicorn_run.assert_called_once()

    @patch("app.launcher._maybe_restart_existing_server", return_value="declined")
    @patch("app.launcher._local_pexo_http_available", return_value=True)
    @patch("app.launcher._port_is_in_use", return_value=True)
    @patch("app.launcher.build_runtime_status", return_value={"installed_profiles": {"full": True}})
    @patch("uvicorn.run")
    def test_run_server_exits_cleanly_when_user_keeps_existing_instance(self, mock_uvicorn_run, _mock_status, _mock_port_in_use, _mock_pexo_http, _mock_restart_prompt):
        self.assertEqual(launcher_module.run_server(no_browser=True), 0)
        mock_uvicorn_run.assert_not_called()

    @patch("builtins.input", side_effect=EOFError)
    @patch("app.launcher.create_chat_session")
    @patch("app.launcher.ensure_db_ready")
    @patch("app.launcher.build_runtime_status", return_value={"installed_profiles": {"mcp": True}})
    def test_run_chat_mode_bootstraps_database_before_opening_terminal_chat(self, _mock_status, mock_db_ready, mock_create_chat, _mock_input):
        mock_create_chat.return_value = {
            "id": "chat-session-1",
            "backend": "codex",
            "workspace_path": str(PROJECT_ROOT),
        }

        self.assertEqual(launcher_module.run_chat_mode(), 0)
        mock_db_ready.assert_called_once()
        mock_create_chat.assert_called_once()

    @patch("app.launcher.run_warmup", return_value=0)
    @patch("app.launcher._restart_launcher_process", return_value=0)
    @patch("app.launcher.promote_runtime")
    @patch("app.launcher.build_runtime_status", return_value={"installed_profiles": {"full": False}})
    def test_run_server_restarts_after_successful_runtime_promotion(self, _mock_status, mock_promote, mock_restart, mock_warmup):
        mock_promote.return_value = {"status": "success"}

        self.assertEqual(launcher_module.run_server(no_browser=True), 0)
        mock_promote.assert_called_once_with("full")
        mock_warmup.assert_called_once_with(quiet=True)
        mock_restart.assert_called_once()

    @patch("app.launcher.run_warmup", return_value=0)
    @patch("app.launcher._restart_launcher_process", return_value=0)
    @patch("app.launcher.promote_runtime")
    @patch("app.launcher.build_runtime_status", return_value={"installed_profiles": {"mcp": False}})
    def test_run_mcp_restarts_after_successful_runtime_promotion(self, _mock_status, mock_promote, mock_restart, mock_warmup):
        mock_promote.return_value = {"status": "success"}

        self.assertEqual(launcher_module.run_mcp(), 0)
        mock_promote.assert_called_once_with("mcp")
        mock_warmup.assert_called_once_with(quiet=True)
        mock_restart.assert_called_once()

    @patch("app.client_connect.running_from_repo_checkout", return_value=False)
    @patch("app.client_connect.which")
    def test_client_connect_builds_packaged_plans_for_supported_clients(self, mock_which, _mock_checkout):
        mock_which.side_effect = lambda name: f"C:/Tools/{name}.exe"

        codex_plan = build_client_connection_plan("codex", scope="user")
        claude_plan = build_client_connection_plan("claude", scope="user")
        gemini_plan = build_client_connection_plan("gemini", scope="user")

        self.assertEqual(codex_plan["target"]["display"], "pexo-mcp")
        self.assertIn("C:/Tools/codex.exe mcp add pexo -- pexo-mcp", codex_plan["manual_command"])
        self.assertIn("C:/Tools/claude.exe mcp add pexo --scope user -- pexo-mcp", claude_plan["manual_command"])
        self.assertIn("C:/Tools/gemini.exe mcp add --scope user --transport stdio pexo pexo-mcp", gemini_plan["manual_command"])
        self.assertEqual(codex_plan["invoker"], "C:/Tools/codex.exe")

    @patch("app.client_connect.running_from_repo_checkout", return_value=False)
    @patch("app.client_connect.which")
    @patch("app.client_connect._read_install_metadata")
    def test_client_connect_prefers_recorded_mcp_command_for_packaged_installs(self, mock_metadata, mock_which, _mock_checkout):
        mock_which.side_effect = lambda name: f"C:/Tools/{name}.exe"
        mock_metadata.return_value = {"mcp_command": "C:/Users/dustin/.pexo/venv/Scripts/pexo-mcp.exe"}

        plan = build_client_connection_plan("codex", scope="user")

        self.assertEqual(plan["target"]["command"], "C:/Users/dustin/.pexo/venv/Scripts/pexo-mcp.exe")
        self.assertIn("pexo-mcp.exe", plan["manual_command"])

    def test_install_metadata_reads_utf8_bom_safely(self):
        from app.client_connect import _read_install_metadata as read_client_metadata
        from app.launcher import _read_install_metadata as read_launcher_metadata
        from app.paths import INSTALL_METADATA_PATH

        original = INSTALL_METADATA_PATH.read_bytes() if INSTALL_METADATA_PATH.exists() else None
        INSTALL_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        bom_payload = '\ufeff{"mcp_command":"C:/Users/dustin/.pexo/venv/Scripts/pexo-mcp.exe"}'
        INSTALL_METADATA_PATH.write_text(bom_payload, encoding="utf-8")
        try:
            self.assertEqual(read_client_metadata()["mcp_command"], "C:/Users/dustin/.pexo/venv/Scripts/pexo-mcp.exe")
            self.assertEqual(read_launcher_metadata()["mcp_command"], "C:/Users/dustin/.pexo/venv/Scripts/pexo-mcp.exe")
        finally:
            if original is None:
                INSTALL_METADATA_PATH.unlink(missing_ok=True)
            else:
                INSTALL_METADATA_PATH.write_bytes(original)

    @patch("app.client_connect.running_from_repo_checkout", return_value=True)
    @patch("app.client_connect.which")
    def test_client_connect_uses_repo_launcher_when_running_from_checkout(self, mock_which, _mock_checkout):
        mock_which.side_effect = lambda name: f"C:/Tools/{name}.exe"

        plan = build_client_connection_plan("codex", scope="user")

        if os.name == "nt":
            self.assertEqual(plan["target"]["command"], "cmd.exe")
            self.assertIn("pexo.bat", plan["manual_command"])
        else:
            self.assertEqual(plan["target"]["command"], "bash")
            self.assertIn("pexo --mcp", plan["manual_command"])
        self.assertIn("--mcp", plan["manual_command"])

    @patch("app.client_connect.subprocess.run")
    @patch("app.client_connect.running_from_repo_checkout", return_value=False)
    @patch("app.client_connect.which")
    def test_connect_clients_dry_run_and_execution_reporting(self, mock_which, _mock_checkout, mock_run):
        mock_which.side_effect = lambda name: f"C:/Tools/{name}.exe"
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=["codex", "mcp", "get", "pexo"], returncode=0, stdout="enabled", stderr=""),
            subprocess.CompletedProcess(args=["claude", "mcp", "get", "pexo"], returncode=0, stdout="enabled", stderr=""),
            subprocess.CompletedProcess(args=["gemini", "mcp", "list"], returncode=0, stdout="pexo", stderr=""),
            subprocess.CompletedProcess(args=["codex", "mcp", "remove", "pexo"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["codex", "mcp", "add", "pexo"], returncode=0, stdout="added", stderr=""),
            subprocess.CompletedProcess(args=["codex", "mcp", "get", "pexo"], returncode=0, stdout="enabled", stderr=""),
        ]

        dry_run_report = connect_clients(target="all", scope="user", dry_run=True)
        self.assertEqual(dry_run_report["status"], "success")
        self.assertTrue(all(item["status"] == "connected" for item in dry_run_report["results"]))
        self.assertTrue(all(item["configured"] is True for item in dry_run_report["results"]))

        execution_report = connect_clients(target="codex", scope="user", dry_run=False)
        self.assertEqual(execution_report["status"], "success")
        self.assertEqual(execution_report["results"][0]["status"], "connected")
        self.assertIn("enabled", execution_report["results"][0]["verify_output"])

    @patch("app.client_connect.subprocess.run")
    @patch("app.client_connect.running_from_repo_checkout", return_value=False)
    @patch("app.client_connect.which")
    def test_connect_clients_can_skip_verification_for_fast_local_status(self, mock_which, _mock_checkout, mock_run):
        mock_which.side_effect = lambda name: f"C:/Tools/{name}.exe"

        report = connect_clients(target="all", scope="user", dry_run=True, verify_existing=False)

        self.assertEqual(report["status"], "success")
        self.assertTrue(all(item["status"] == "available" for item in report["results"]))
        self.assertTrue(all(item["verification_skipped"] is True for item in report["results"]))
        mock_run.assert_not_called()

    @patch("app.client_connect.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["gemini", "mcp", "list"], timeout=4))
    @patch("app.client_connect.running_from_repo_checkout", return_value=False)
    @patch("app.client_connect.which")
    def test_connect_clients_marks_verification_timeouts_without_hanging(self, mock_which, _mock_checkout, _mock_run):
        mock_which.side_effect = lambda name: f"C:/Tools/{name}.exe" if name == "gemini" else None

        report = connect_clients(target="gemini", scope="user", dry_run=True)

        self.assertEqual(report["status"], "success")
        self.assertEqual(report["results"][0]["status"], "available")
        self.assertTrue(report["results"][0]["verification_timed_out"])
        self.assertIn("timed out", report["results"][0]["message"])

    def test_paths_use_repo_checkout_locally_and_home_for_packaged_mode(self):
        self.assertTrue(looks_like_repo_checkout(CODE_ROOT))
        self.assertEqual(resolve_state_root(code_root=CODE_ROOT, env_override=None), CODE_ROOT)

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_code_root = Path(tmpdir) / "site-packages-pexo"
            fake_code_root.mkdir()
            fake_home = Path(tmpdir) / "home"
            fake_home.mkdir()
            resolved = resolve_state_root(code_root=fake_code_root, env_override=None, home_dir=fake_home)
            self.assertEqual(
                os.path.normcase(os.path.realpath(str(resolved))),
                os.path.normcase(os.path.realpath(str(fake_home / ".pexo"))),
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            override = Path(tmpdir) / "custom-pexo-home"
            resolved = resolve_state_root(code_root=CODE_ROOT, env_override=str(override))
            self.assertEqual(
                os.path.normcase(os.path.realpath(str(resolved))),
                os.path.normcase(os.path.realpath(str(override))),
            )

    def test_paths_prefer_managed_state_root_when_invoked_via_packaged_pexo_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            managed_root = Path(tmpdir) / ".pexo"
            managed_root.mkdir()
            (managed_root / ".pexo-install.json").write_text("{}", encoding="utf-8")
            invoker = managed_root / "venv" / "Scripts" / "pexo.exe"
            invoker.parent.mkdir(parents=True)
            invoker.write_text("", encoding="utf-8")

            detected_root = resolve_managed_runtime_state_root(invoker)
            resolved_root = resolve_state_root(code_root=CODE_ROOT, env_override=None, runtime_invoker=invoker)

            self.assertEqual(
                os.path.normcase(os.path.realpath(str(detected_root))),
                os.path.normcase(os.path.realpath(str(managed_root))),
            )
            self.assertEqual(
                os.path.normcase(os.path.realpath(str(resolved_root))),
                os.path.normcase(os.path.realpath(str(managed_root))),
            )

    def test_gitattributes_enforces_shell_script_line_endings(self):
        content = Path(".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", content)
        self.assertIn("pexo text eol=lf", content)

    def test_gitignore_covers_repo_local_runtime_state(self):
        content = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn(".pexo-deps-profile", content)
        self.assertIn(".pexo-update-check", content)
        self.assertIn("artifacts/", content)
        self.assertIn("dynamic_tools/", content)

    def test_install_runtime_ci_workflow_covers_windows_and_linux(self):
        workflow = Path(".github/workflows/install-runtime-ci.yml").read_text(encoding="utf-8")
        self.assertIn("windows-latest", workflow)
        self.assertIn("ubuntu-latest", workflow)
        self.assertIn("Install Runtime CI", workflow)
        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn("actions/setup-python@v6", workflow)
        self.assertNotIn("FORCE_JAVASCRIPT_ACTIONS_TO_NODE24", workflow)
        self.assertIn(".\\install.cmd -UseCurrentCheckout -AllowRepoInstall", workflow)
        self.assertIn("bash ./install.sh --use-current-checkout --allow-repo-install", workflow)
        self.assertIn("uninstall.ps1", workflow)
        self.assertIn("uninstall.sh", workflow)

    def test_uninstall_scripts_target_their_own_install_directory(self):
        windows_uninstall = Path("uninstall.ps1").read_text(encoding="utf-8")
        shell_uninstall = Path("uninstall.sh").read_text(encoding="utf-8")

        self.assertIn("Split-Path -Path $MyInvocation.MyCommand.Path -Parent", windows_uninstall)
        self.assertIn("cd \"$(dirname \"$0\")\"", shell_uninstall)

    def test_profile_preset_builds_expected_answers(self):
        answers = build_profile_from_preset("efficient_operator")

        self.assertEqual(answers.personality_answers["p1"], "1")
        self.assertEqual(answers.personality_answers["p5"], "3")
        self.assertEqual(answers.scripting_answers["s3"], "1")

    def test_quick_setup_profile_can_be_created_without_backup_path(self):
        init_db()
        db = SessionLocal()
        try:
            profile = upsert_profile(build_profile_from_preset("efficient_operator"), db)
            self.assertIsNone(profile.backup_path)
            self.assertIn("Communication Style: Direct & Concise", profile.personality_prompt)
            self.assertEqual(profile.scripting_preferences["Type Checking"], "Strict typing always")
        finally:
            db.close()

    def test_profile_update_can_clear_backup_path(self):
        init_db()
        db = SessionLocal()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                profile = upsert_profile(
                    build_profile_from_preset("balanced_builder", backup_path=tmpdir),
                    db,
                )
                self.assertEqual(profile.backup_path, str(Path(tmpdir).resolve()))

                cleared = upsert_profile(
                    ProfileAnswers(name="default_user", clear_backup_path=True),
                    db,
                )
                self.assertIsNone(cleared.backup_path)
        finally:
            db.close()

    def test_profile_answers_can_be_derived_for_dashboard_editor(self):
        init_db()
        db = SessionLocal()
        try:
            profile = upsert_profile(build_profile_from_preset("balanced_builder"), db)
            answers = derive_profile_answers(profile)
            self.assertEqual(answers["personality_answers"]["p1"], "2")
            self.assertEqual(answers["scripting_answers"]["s8"], "2")
        finally:
            db.close()

    def test_list_presets_json_contains_efficient_operator(self):
        output = StringIO()
        with redirect_stdout(output):
            exit_code = list_presets(as_json=True)

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(any(preset["id"] == "efficient_operator" for preset in payload))

    def test_headless_setup_creates_profile_without_ui(self):
        output = StringIO()
        with redirect_stdout(output):
            exit_code = headless_setup(preset="strict_engineer", name="cli_user")

        self.assertEqual(exit_code, 0)
        self.assertIn("Headless profile setup complete", output.getvalue())

        init_db()
        db = SessionLocal()
        try:
            profile = db.query(Profile).filter(Profile.name == "cli_user").first()
            self.assertIsNotNone(profile)
            self.assertIn("Communication Style: Direct & Concise", profile.personality_prompt)
        finally:
            db.close()

    @patch("app.routers.memory.get_memory_collection")
    def test_memory_update_and_delete_keep_sql_and_vector_state_in_sync(self, mock_get_memory_collection):
        mock_get_memory_collection.return_value = FakeCollection()
        init_db()
        db = SessionLocal()
        try:
            store_result = store_memory(
                MemoryStoreRequest(
                    session_id="session-1",
                    content="Original memory",
                    task_context="initial context",
                ),
                db,
            )
            memory_id = store_result["memory_id"]
            chroma_id = store_result["chroma_id"]

            update_result = update_memory(
                memory_id,
                MemoryUpdateRequest(
                    content="Updated memory",
                    task_context="refined context",
                    is_compacted=True,
                ),
                db,
            )
            self.assertEqual(update_result["memory"]["content"], "Updated memory")
            self.assertTrue(update_result["memory"]["is_compacted"])

            delete_result = delete_memory(memory_id, db)
            self.assertEqual(delete_result["status"], "success")
            self.assertIsNone(db.query(Memory).filter(Memory.id == memory_id).first())
            self.assertNotIn(chroma_id, mock_get_memory_collection.return_value.records)
        finally:
            db.close()

    @patch("app.routers.memory.get_memory_collection")
    def test_memory_maintenance_compacts_old_context_and_archives_sources(self, mock_get_memory_collection):
        mock_get_memory_collection.return_value = FakeCollection()
        init_db()
        db = SessionLocal()
        try:
            for index in range(7):
                store_memory(
                    MemoryStoreRequest(
                        session_id="session-compact",
                        content=f"memory payload {index}",
                        task_context="task-alpha",
                    ),
                    db,
                )

            result = maintain_memory_health(db, task_context="task-alpha")
            active_raw = db.query(Memory).filter(
                Memory.task_context == "task-alpha",
                Memory.is_archived.is_(False),
                Memory.is_compacted.is_(False),
            ).count()
            archived = db.query(Memory).filter(
                Memory.task_context == "task-alpha",
                Memory.is_archived.is_(True),
            ).count()
            summary = db.query(Memory).filter(
                Memory.task_context == "task-alpha",
                Memory.is_compacted.is_(True),
                Memory.is_archived.is_(False),
            ).first()

            self.assertGreaterEqual(result["compacted_count"], 0)
            self.assertLessEqual(active_raw, 2)
            self.assertGreaterEqual(archived, 1)
            self.assertIsNotNone(summary)
        finally:
            db.close()

    @patch("app.routers.memory.get_memory_collection")
    def test_memory_compaction_reuses_summary_without_recursive_bloat(self, mock_get_memory_collection):
        mock_get_memory_collection.return_value = FakeCollection()
        init_db()
        db = SessionLocal()
        try:
            for index in range(10):
                store_memory(
                    MemoryStoreRequest(
                        session_id="session-compact-2",
                        content=f"memory payload {index}",
                        task_context="task-beta",
                    ),
                    db,
                )

            summary = db.query(Memory).filter(
                Memory.task_context == "task-beta",
                Memory.is_compacted.is_(True),
                Memory.is_archived.is_(False),
            ).first()

            self.assertIsNotNone(summary)
            self.assertEqual(summary.content.count("Compacted memory summary for task-beta."), 1)
            self.assertLessEqual(summary.content.count("- "), 6)
        finally:
            db.close()

    def test_memory_search_falls_back_to_sqlite_when_chromadb_is_unavailable(self):
        init_db()
        db = SessionLocal()
        original_chromadb = memory_router.chromadb
        original_settings = memory_router.Settings
        original_collection = memory_router._memory_collection
        memory_router.chromadb = None
        memory_router.Settings = None
        memory_router._memory_collection = None
        try:
            stored = store_memory(
                MemoryStoreRequest(
                    session_id="session-fallback",
                    content="Use deterministic repo-local MCP setup for Windows.",
                    task_context="install-flow",
                ),
                db,
            )
            self.assertEqual(stored["embedding_mode"], "sqlite_keyword_fallback")
            results = search_memory(MemorySearchRequest(query="repo-local MCP", n_results=3), db)
            self.assertTrue(results["results"])
            self.assertEqual(results["results"][0]["metadata"]["search_mode"], "keyword_fallback")
            self.assertIn("runtime", results)
            self.assertIsNotNone(results.get("promotion_offer"))
        finally:
            memory_router.chromadb = original_chromadb
            memory_router.Settings = original_settings
            memory_router._memory_collection = original_collection
            db.close()

    def test_fallback_graph_can_route_without_langgraph_installed(self):
        init_db()
        fallback_app = FallbackPexoApp()

        initial_state = {
            "session_id": "fallback-session",
            "user_prompt": "Create a repo-local install plan.",
            "clarification_answer": "Use a flat checkout.",
            "tasks": [],
            "completed_tasks": [],
            "current_agent": "Supervisor",
            "current_instruction": "",
            "waiting_for_ai": False,
            "final_response": "",
            "user_profile": "",
            "available_agents": "",
            "available_tools": "",
        }

        supervisor_state = fallback_app.invoke(initial_state)
        self.assertTrue(supervisor_state["waiting_for_ai"])
        self.assertEqual(supervisor_state["current_agent"], "Supervisor")

        supervisor_state["waiting_for_ai"] = False
        supervisor_state["tasks"] = [{"id": "task-1", "description": "Write the install plan", "assigned_agent": "Developer"}]
        developer_state = fallback_app.invoke(supervisor_state)
        self.assertTrue(developer_state["waiting_for_ai"])
        self.assertEqual(developer_state["current_agent"], "Developer")

    def test_admin_telemetry_payload_summarizes_recent_activity(self):
        init_db()
        db = SessionLocal()
        try:
            db.add(
                AgentState(
                    session_id="session-telemetry",
                    agent_name="Developer",
                    status="completed",
                    context_size_tokens=42,
                    data={
                        "user_prompt": "Create a hardening plan for the install flow.",
                        "clarification_question": "Which OS should be prioritized?",
                    },
                )
            )
            db.add(
                AgentState(
                    session_id="session-telemetry",
                    agent_name="orchestrator",
                    status="clarification_pending",
                    context_size_tokens=9,
                    data={
                        "task_id": "task-1",
                        "task_description": "Implement feature",
                    },
                )
            )
            db.add(
                AgentState(
                    session_id="session-telemetry",
                    agent_name="orchestrator",
                    status="session_complete",
                    context_size_tokens=11,
                    data={"completed_task_count": 1},
                )
            )
            db.commit()

            telemetry = build_telemetry_payload(db)
            self.assertEqual(telemetry["summary"]["session_count"], 1)
            self.assertEqual(telemetry["summary"]["action_count"], 3)
            session = next(session for session in telemetry["recent_sessions"] if session["session_id"] == "session-telemetry")
            self.assertEqual(session["status_label"], "Complete")
            self.assertEqual(session["last_agent_label"], "Orchestrator")
            self.assertEqual(session["short_id"], "session-")
            self.assertIn("Create a hardening plan", session["title"])
            self.assertTrue(session["summary"])
            self.assertTrue(any(activity["agent_name"] == "Developer" for activity in telemetry["recent_activity"]))
        finally:
            db.close()

    def test_runtime_status_reports_vector_offer_when_chromadb_missing(self):
        init_db()
        db = SessionLocal()
        original_chromadb = memory_router.chromadb
        original_settings = memory_router.Settings
        original_collection = memory_router._memory_collection
        memory_router.chromadb = None
        memory_router.Settings = None
        memory_router._memory_collection = None
        try:
            status = build_runtime_status(db)
            self.assertFalse(status["vector_embeddings_available"])
            self.assertTrue(status["vector_promotion_offer_pending"])
            self.assertEqual(status["vector_promotion_offer"]["profile"], "vector")
        finally:
            memory_router.chromadb = original_chromadb
            memory_router.Settings = original_settings
            memory_router._memory_collection = original_collection
            db.close()

    def test_runtime_module_availability_handles_missing_optional_parents(self):
        with patch("app.runtime.find_spec", side_effect=ModuleNotFoundError("mcp")):
            self.assertFalse(runtime_module._module_available("mcp.server.fastmcp"))

    def test_runtime_status_reconciles_stale_marker_to_installed_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker_path = Path(tmpdir) / ".pexo-deps-profile"
            marker_path.write_text("full", encoding="utf-8")

            with patch.object(runtime_module, "RUNTIME_MARKER_PATH", marker_path):
                with patch("app.runtime._profile_install_matrix", return_value={
                    "core": True,
                    "mcp": True,
                    "full": False,
                    "vector": False,
                }):
                    status = build_runtime_status()

            self.assertEqual(status["active_profile"], "mcp")
            self.assertEqual(status["marker_profile"], "mcp")
            self.assertEqual(marker_path.read_text(encoding="utf-8"), "mcp")

    @patch("app.routers.runtime.promote_runtime")
    def test_mcp_runtime_tools_expose_status_and_promotion(self, mock_promote_runtime):
        init_db()
        mock_promote_runtime.return_value = {
            "status": "success",
            "profile": "vector",
            "command": ["python", "-m", "pip"],
            "duration_ms": 1,
            "stdout": "ok",
            "stderr": "",
            "returncode": 0,
            "runtime": {"active_profile": "vector"},
        }

        status = pexo_get_runtime_status()
        self.assertIn("active_profile", status)

        promotion = pexo_promote_runtime("vector")
        self.assertEqual(promotion["status"], "success")
        self.assertEqual(promotion["profile"], "vector")

    def test_artifact_text_and_path_registration_round_trip(self):
        init_db()
        db = SessionLocal()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                artifact_dir = Path(tmpdir) / "artifacts"
                source_file = Path(tmpdir) / "note.txt"
                source_file.write_text("Attachment body for artifact indexing.", encoding="utf-8")

                with patch("app.routers.artifacts.ARTIFACTS_DIR", artifact_dir):
                    text_result = register_artifact_text(
                        ArtifactTextRequest(
                            name="summary.md",
                            content="Attached context from local execution.",
                            session_id="artifact-session",
                            task_context="docs",
                        ),
                        db,
                    )
                    path_result = register_artifact_path(
                        ArtifactPathRequest(
                            path=str(source_file),
                            session_id="artifact-session",
                            task_context="docs",
                        ),
                        db,
                    )

                    listing = list_artifacts(limit=10, query="artifact", db=db)
                    self.assertEqual(len(listing["artifacts"]), 2)
                    self.assertTrue(text_result["artifact"]["has_text"])
                    self.assertTrue(path_result["artifact"]["has_text"])

                    artifact = get_artifact(path_result["artifact"]["id"], db)
                    self.assertIn("Attachment body", artifact["extracted_text"])

                    deleted = delete_artifact(text_result["artifact"]["id"], db)
                    self.assertEqual(deleted["status"], "success")
        finally:
            db.close()

    def test_admin_snapshot_includes_runtime_and_artifacts(self):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                artifact_dir = Path(tmpdir) / "artifacts"
                with patch("app.routers.artifacts.ARTIFACTS_DIR", artifact_dir):
                    register_artifact_text(
                        ArtifactTextRequest(
                            name="summary.txt",
                            content="Dashboard artifact.",
                            session_id="artifact-session",
                            task_context="admin",
                        ),
                        db,
                    )
                    snapshot = get_admin_snapshot(memory_limit=5, db=db)
                    self.assertIn("runtime", snapshot)
                    self.assertIn("clients", snapshot)
                    self.assertEqual(snapshot["stats"]["artifact_count"], 1)
                    self.assertEqual(len(snapshot["recent_artifacts"]), 1)
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_service_round_trip(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="gemini", workspace_path=str(PROJECT_ROOT))
            self.assertEqual(session["backend"], "gemini")

            mock_run_backend.side_effect = [
                "Hi. I'm online and ready.",
                "Understood. I will keep it responsive, minimal, and high-contrast.",
            ]

            first_reply = send_chat_message(db, session_id=session["id"], message="This is a test chat.")
            self.assertEqual(first_reply["reply"]["status"], "answered")
            self.assertIn("ready", first_reply["reply"]["user_message"].lower())

            second_reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Keep it responsive, minimal, and high-contrast.",
            )
            self.assertEqual(second_reply["reply"]["status"], "answered")
            self.assertIn("responsive", second_reply["reply"]["user_message"].lower())
            self.assertEqual(mock_run_backend.call_count, 2)

            payload = get_chat_session_payload(db, session["id"])
            self.assertEqual(payload["session"]["status"], "answered")
            self.assertGreaterEqual(len(payload["messages"]), 4)

            self.assertEqual(db.query(ChatSession).count(), 1)
            self.assertGreaterEqual(db.query(ChatMessage).count(), 4)
        finally:
            db.close()

    @patch("app.direct_chat._best_effort_backend_connection")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_session_creation_defers_backend_connection_verification(self, mock_backend_name, mock_best_effort):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            self.assertEqual(session["backend"], "gemini")
            mock_best_effort.assert_not_called()
            self.assertEqual(session["details"]["backend_verified"], False)
            self.assertNotIn("backend_warning", session["details"])
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_routes_simple_messages_to_conversation_mode(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.return_value = "Hi. I'm here and ready."

            reply = send_chat_message(db, session_id=session["id"], message="This is a test chat.")

            mock_run_backend.assert_called_once()
            self.assertEqual(reply["session"]["details"]["mode"], "conversation")
            self.assertEqual(reply["reply"]["status"], "answered")
            self.assertIn("ready", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend", return_value="Noted.")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_learns_explicit_user_preferences(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="I prefer clean, futuristic UI by default.",
            )

            preferences = (
                db.query(Memory)
                .filter(Memory.task_context == direct_chat_module.LEARNED_PREFERENCE_TASK_CONTEXT)
                .all()
            )

            self.assertEqual(len(preferences), 1)
            self.assertTrue(preferences[0].is_pinned)
            self.assertIn("Prefer clean, futuristic UI", preferences[0].content)
            self.assertIn("learned_preference", reply["session"]["details"])
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend", return_value="I'll act as the user-facing Pexo assistant for this session.")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_brain_lookup_surfaces_learned_preferences(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            send_chat_message(
                db,
                session_id=session["id"],
                message="I prefer clean, futuristic UI by default.",
            )

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="what do you know about my preferences",
            )

            self.assertIn("learned preferences", reply["reply"]["user_message"].lower())
            self.assertIn("clean, futuristic ui", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_routes_task_follow_up_after_task_turn(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = [
                "I can design that landing page.",
                "I'll keep it clean and premium.",
            ]

            first = send_chat_message(db, session_id=session["id"], message="Can you help me design a modern landing page for my product?")
            second = send_chat_message(db, session_id=session["id"], message="yes, keep it clean and premium")

            self.assertEqual(first["session"]["details"]["mode"], "task")
            self.assertEqual(second["session"]["details"]["mode"], "task")
            self.assertEqual(second["session"]["details"]["response_path"], "local_direct")
            self.assertIn("keep it clean and premium", second["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_answers_whats_next_from_task_session_context(self, mock_backend_name, mock_connect):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            chat_session = db.query(ChatSession).filter(ChatSession.id == session["id"]).first()
            chat_session.details = {
                "mode": "task",
                "response_path": "backend_retry",
                "last_assistant_message": "I can handle that. I'll start with the structure, visual direction, and first concrete implementation step.",
            }
            db.commit()

            reply = send_chat_message(db, session_id=session["id"], message="what should you do next?")

            self.assertEqual(reply["session"]["details"]["mode"], "conversation")
            self.assertEqual(reply["session"]["details"]["response_path"], "local_direct")
            self.assertIn("next i'll start with", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._fast_web_fact_lookup", return_value={"answer": "According to Wikipedia, the incumbent president is Donald Trump.", "source": "wikipedia_search", "title": "List of presidents of the United States"})
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_answers_general_question_from_fast_web_fact_lookup(self, mock_backend_name, mock_connect, mock_web_fact, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            reply = send_chat_message(db, session_id=session["id"], message="who is the president")

            mock_run_backend.assert_not_called()
            self.assertEqual(reply["session"]["details"]["response_path"], "web_fact")
            self.assertEqual(reply["session"]["details"]["web_fact_source"], "wikipedia_search")
            self.assertIn("president", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch(
        "app.direct_chat._fast_web_fact_lookup",
        return_value={
            "answer": "According to Wikipedia, the incumbent president is Donald Trump.",
            "source": "wikipedia_search",
            "title": "List of presidents of the United States",
        },
    )
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_explains_web_fact_answer_from_session_context(self, mock_backend_name, mock_connect, mock_web_fact, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            first_reply = send_chat_message(db, session_id=session["id"], message="who is the president")
            second_reply = send_chat_message(db, session_id=session["id"], message="how did you get that answer")

            mock_run_backend.assert_not_called()
            self.assertEqual(first_reply["session"]["details"]["response_path"], "web_fact")
            self.assertEqual(second_reply["session"]["details"]["response_path"], "local_direct")
            self.assertIn("wikipedia", second_reply["reply"]["user_message"].lower())
            self.assertIn("list of presidents of the united states", second_reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._best_effort_backend_connection")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_verifies_backend_connection_for_task_mode_when_session_is_unverified(self, mock_backend_name, mock_best_effort, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            mock_best_effort.return_value = None
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.return_value = "I can design that landing page."

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Design a modern landing page for my product.",
            )

            self.assertEqual(mock_best_effort.call_count, 1)
            self.assertEqual(reply["session"]["details"]["mode"], "task")
            self.assertNotIn("backend_warning", reply["session"]["details"])
            self.assertEqual(reply["session"]["details"]["backend_verified"], True)
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._best_effort_backend_connection", return_value="Unable to connect gemini to the Pexo MCP server.")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_surfaces_backend_warning_when_task_mode_verification_fails(self, mock_backend_name, mock_best_effort, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.return_value = "I can design that landing page."

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Design a modern landing page for my product.",
            )

            self.assertEqual(mock_best_effort.call_count, 1)
            self.assertEqual(reply["session"]["details"]["backend_verified"], False)
            self.assertIn("backend_warning", reply["session"]["details"])
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_falls_back_to_local_identity_and_date_when_backend_fails(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = RuntimeError("backend unavailable")

            name_reply = send_chat_message(db, session_id=session["id"], message="What is your name?")
            date_reply = send_chat_message(db, session_id=session["id"], message="what day is it")
            time_reply = send_chat_message(db, session_id=session["id"], message="what time is it")

            mock_run_backend.assert_not_called()
            self.assertEqual(name_reply["reply"]["user_message"], "My name is Pexo.")
            self.assertIn("Today is", date_reply["reply"]["user_message"])
            self.assertIn("It is", time_reply["reply"]["user_message"])
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="codex")
    def test_direct_chat_falls_back_to_local_answer_when_fast_backend_times_out(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = RuntimeError("Codex direct chat timed out after 6 seconds.")

            reply = send_chat_message(db, session_id=session["id"], message="how are you")

            self.assertEqual(mock_run_backend.call_count, 0)
            self.assertIn("ready", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="codex")
    def test_direct_chat_answers_direct_local_facts_without_waiting_on_backend(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            reply = send_chat_message(db, session_id=session["id"], message="what time is it")

            mock_run_backend.assert_not_called()
            self.assertEqual(reply["session"]["details"]["response_path"], "local_direct")
            self.assertIn("It is", reply["reply"]["user_message"])
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._fast_web_fact_lookup", return_value=None)
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_returns_graceful_message_when_general_question_backend_times_out(self, mock_backend_name, mock_connect, mock_web_fact, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = RuntimeError("Gemini direct chat timed out after 18 seconds.")

            reply = send_chat_message(db, session_id=session["id"], message="who is the president")

            self.assertEqual(reply["reply"]["status"], "answered")
            self.assertEqual(reply["session"]["details"]["response_path"], "backend_unavailable")
            self.assertIn("still running", reply["reply"]["user_message"].lower())
            self.assertIn("gemini", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._fast_web_fact_lookup", return_value=None)
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_does_not_try_secondary_backend_for_general_question(self, mock_backend_name, mock_connect, mock_web_fact, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = RuntimeError("Gemini direct chat timed out after 6 seconds.")

            reply = send_chat_message(db, session_id=session["id"], message="who is the president")

            self.assertEqual(mock_run_backend.call_count, 1)
            self.assertEqual(reply["session"]["details"]["response_path"], "backend_unavailable")
            self.assertEqual(reply["session"]["details"]["attempted_backends"], ["gemini"])
            self.assertEqual(reply["session"]["backend"], "gemini")
            first_prompt = mock_run_backend.call_args_list[0].args[1]
            self.assertNotIn("Recent direct chat transcript", first_prompt)
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat.build_client_connection_plan")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="codex")
    def test_direct_chat_task_worker_can_try_secondary_backend_in_auto_mode(self, mock_backend_name, mock_connect, mock_plan, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            mock_plan.side_effect = lambda client, scope="user": {
                "available": client in {"codex", "gemini"},
                "invoker": client,
                "binary": client,
                "target": {"display": "pexo-mcp"},
                "manual_command": f"{client} mcp add pexo",
            }
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            first_reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Design a modern landing page for my product with a clean premium look.",
            )
            self.assertEqual(first_reply["session"]["details"]["pexo_task_status"], "agent_action_required")

            mock_run_backend.side_effect = [
                RuntimeError("Codex direct chat timed out after 25 seconds."),
                RuntimeError("Gemini direct chat timed out after 25 seconds."),
            ]
            db_session = db.query(ChatSession).filter(ChatSession.id == session["id"]).first()
            second_reply = direct_chat_module._advance_direct_chat_task(
                db,
                chat_session=db_session,
                latest_user_message="continue",
                backend_name="codex",
                history_excerpt=direct_chat_module._history_excerpt(db, session["id"]),
                timeout_seconds=300,
                stop_before_external_worker=False,
            )

            self.assertEqual(mock_run_backend.call_count, 2)
            self.assertEqual(second_reply["response_path"], "task_session_blocked")
            self.assertEqual(second_reply["attempted_backends"], ["codex", "gemini"])
            self.assertEqual(second_reply["backend_errors"]["codex"], "Codex direct chat timed out after 25 seconds.")
            self.assertEqual(second_reply["backend_errors"]["gemini"], "Gemini direct chat timed out after 25 seconds.")
        finally:
            db.close()

    @patch("app.direct_chat.threading.Thread.start", return_value=None)
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="codex")
    def test_direct_chat_starts_background_task_worker_on_continue(self, mock_backend_name, mock_connect, _mock_thread_start):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            first_reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Design a modern landing page for my product with a clean premium look.",
            )
            self.assertEqual(first_reply["session"]["details"]["pexo_task_status"], "agent_action_required")

            second_reply = send_chat_message(
                db,
                session_id=session["id"],
                message="continue",
            )

            self.assertEqual(second_reply["session"]["status"], "working")
            self.assertEqual(second_reply["session"]["details"]["response_path"], "task_run_started")
            self.assertEqual(second_reply["session"]["details"]["task_run_status"], "running")
            self.assertIn("step is running", second_reply["reply"]["user_message"].lower())
        finally:
            db.close()

    def test_direct_chat_reports_active_background_run_status(self):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            db_session = db.query(ChatSession).filter(ChatSession.id == session["id"]).first()
            details = dict(db_session.details or {})
            details.update(
                {
                    "mode": "task",
                    "task_run_status": "running",
                    "task_run_role": "Developer",
                    "task_run_backend": "codex",
                    "task_run_started_at": "2026-04-04T10:00:00",
                    "task_run_progress_message": "The Developer step is running.",
                }
            )
            db_session.status = "working"
            db_session.pexo_session_id = "pexo-task-1"
            db_session.details = details
            db.commit()

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="status",
            )

            self.assertEqual(reply["session"]["details"]["response_path"], "local_direct")
            self.assertIn("developer step is running", reply["reply"]["user_message"].lower())
            self.assertIn("via codex", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._fast_web_fact_lookup", return_value=None)
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_rejects_generic_filler_for_general_question(self, mock_backend_name, mock_connect, mock_web_fact, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = [
                "Pexo: What do you need?",
                "How can I help?",
            ]

            reply = send_chat_message(db, session_id=session["id"], message="who is the president")

            self.assertEqual(mock_run_backend.call_count, 2)
            self.assertEqual(reply["session"]["details"]["response_path"], "backend_unavailable")
            self.assertIn("still running", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_falls_back_to_local_smalltalk_and_feedback_when_backend_fails(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = RuntimeError("backend unavailable")

            status_reply = send_chat_message(db, session_id=session["id"], message="how are you")
            preference_reply = send_chat_message(db, session_id=session["id"], message="what is your favorite color")
            feedback_reply = send_chat_message(db, session_id=session["id"], message="this is bad")

            self.assertEqual(mock_run_backend.call_count, 2)
            self.assertIn("ready", status_reply["reply"]["user_message"].lower())
            self.assertIn("don't have personal preferences", preference_reply["reply"]["user_message"].lower())
            self.assertIn("simpler and more direct", feedback_reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_rewrites_generic_backend_filler(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = [
                "Ill speak directly to you as Pexo.",
                "The weather depends on your location, but I can still help with a plan if you want one.",
            ]

            reply = send_chat_message(db, session_id=session["id"], message="talk casually about weather for one sentence")

            self.assertEqual(mock_run_backend.call_count, 2)
            self.assertEqual(reply["session"]["details"]["mode"], "conversation")
            self.assertIn("weather", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_falls_back_to_local_direct_answer_when_backend_misses_the_question(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = [
                "I'm online and ready.",
                "I'm still online and ready.",
            ]

            reply = send_chat_message(db, session_id=session["id"], message="what day is it")

            mock_run_backend.assert_not_called()
            self.assertIn("Today is", reply["reply"]["user_message"])
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_routes_lookup_requests_to_brain_lookup_mode(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            register_artifact_text(
                ArtifactTextRequest(
                    name="README.md",
                    content="Pexo README artifact.",
                    session_id="lookup-session",
                    task_context="docs",
                ),
                db,
            )
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            reply = send_chat_message(db, session_id=session["id"], message="Tell me the readme we have stored.")

            mock_run_backend.assert_not_called()
            self.assertEqual(reply["session"]["details"]["mode"], "brain_lookup")
            self.assertEqual(reply["session"]["details"]["response_path"], "local_direct")
            self.assertIn("README.md", reply["reply"]["user_message"])
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_routes_build_requests_to_task_mode(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Design a modern landing page for my product.",
            )

            mock_run_backend.assert_not_called()
            self.assertEqual(reply["session"]["details"]["mode"], "task")
            self.assertEqual(reply["session"]["details"]["pexo_task_status"], "agent_action_required")
            self.assertEqual(reply["session"]["details"]["pexo_task_role"], "Developer")
            self.assertTrue(reply["reply"]["pexo_session_id"])
            self.assertIn("next developer step", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_help_framed_task_stays_local_first(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="can you help me design a landing page?",
            )

            mock_run_backend.assert_not_called()
            self.assertEqual(reply["session"]["details"]["mode"], "task")
            self.assertEqual(reply["session"]["details"]["response_path"], "local_direct")
            self.assertIn("i can help with that", reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_routes_create_agent_request_to_task_mode(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.return_value = "I can create that frontend agent."

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="create a new frontend design agent for me",
            )

            self.assertEqual(reply["session"]["details"]["mode"], "task")
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_task_mode_rejects_meta_filler(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="gemini", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = [
                "I’ll reply as Pexo from here: direct, natural, and without the internal orchestration unless you ask for it.",
                "Built the landing page structure and hero section.",
                "The landing page is complete and ready for review.",
            ]

            first_reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Design a modern landing page for my product.",
            )
            self.assertEqual(first_reply["session"]["details"]["pexo_task_status"], "agent_action_required")

            db_session = db.query(ChatSession).filter(ChatSession.id == session["id"]).first()
            reply = direct_chat_module._advance_direct_chat_task(
                db,
                chat_session=db_session,
                latest_user_message="continue",
                backend_name="gemini",
                history_excerpt=direct_chat_module._history_excerpt(db, session["id"]),
                timeout_seconds=300,
                stop_before_external_worker=False,
            )

            self.assertEqual(mock_run_backend.call_count, 2)
            self.assertEqual(reply["task_payload"]["status"], "complete")
            self.assertIn("landing page structure and hero section", reply["assistant_text"].lower())
            self.assertNotIn("respond as pexo", reply["assistant_text"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_direct_chat_task_mode_rejects_im_pexo_meta_filler(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="gemini", workspace_path=str(PROJECT_ROOT))
            mock_run_backend.side_effect = [
                "I’m Pexo. What are we working on?",
                "Defined the frontend design agent role and capabilities.",
                "The frontend design agent is ready.",
            ]

            first_reply = send_chat_message(
                db,
                session_id=session["id"],
                message="create a new frontend design agent for me",
            )
            self.assertEqual(first_reply["session"]["details"]["pexo_task_status"], "agent_action_required")

            db_session = db.query(ChatSession).filter(ChatSession.id == session["id"]).first()
            reply = direct_chat_module._advance_direct_chat_task(
                db,
                chat_session=db_session,
                latest_user_message="continue",
                backend_name="gemini",
                history_excerpt=direct_chat_module._history_excerpt(db, session["id"]),
                timeout_seconds=300,
                stop_before_external_worker=False,
            )

            self.assertEqual(mock_run_backend.call_count, 2)
            self.assertEqual(reply["task_payload"]["status"], "complete")
            self.assertIn("frontend design agent role and capabilities", reply["assistant_text"].lower())
        finally:
            db.close()

    def test_start_simple_task_skips_clarification_for_specific_prompt(self):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            payload = start_simple_task(
                PromptRequest(
                    user_id="default_user",
                    prompt="Design a modern landing page for my product with a clean premium look.",
                ),
                db,
            )

            self.assertNotEqual(payload["status"], "clarification_required")
            self.assertEqual(payload["status"], "agent_action_required")
            self.assertEqual(payload["role"], "Supervisor")
        finally:
            db.close()

    def test_start_simple_task_requires_clarification_for_vague_prompt(self):
        self.assertTrue(should_require_clarification("Fix it."))
        self.assertFalse(should_require_clarification("Design a modern landing page for my product with a clean premium look."))

    def test_submit_task_result_uses_manager_output_as_final_response(self):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            started = start_simple_task(
                PromptRequest(
                    user_id="default_user",
                    prompt="Design a modern landing page for my product with a clean premium look.",
                ),
                db,
            )
            session_id = started["session_id"]

            after_supervisor = continue_simple_task(
                SimpleContinueRequest(
                    session_id=session_id,
                    result_data=[{"id": "task-1", "description": "Build the page", "assigned_agent": "Developer"}],
                ),
                db,
            )
            self.assertEqual(after_supervisor["status"], "agent_action_required")
            self.assertEqual(after_supervisor["role"], "Developer")

            after_developer = continue_simple_task(
                SimpleContinueRequest(
                    session_id=session_id,
                    result_data="Built the landing page structure.",
                ),
                db,
            )
            self.assertEqual(after_developer["status"], "agent_action_required")
            self.assertEqual(after_developer["role"], "Code Organization Manager")

            completed = continue_simple_task(
                SimpleContinueRequest(
                    session_id=session_id,
                    result_data="The landing page is complete and ready for review.",
                ),
                db,
            )
            self.assertEqual(completed["status"], "complete")
            self.assertIn("landing page is complete", completed["final_response"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="codex")
    def test_direct_chat_promotes_concrete_task_into_real_pexo_session(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Design a modern landing page for my product with a clean premium look.",
            )

            self.assertEqual(reply["session"]["details"]["mode"], "task")
            self.assertTrue(reply["reply"]["pexo_session_id"])
            self.assertEqual(reply["session"]["details"]["pexo_task_status"], "agent_action_required")
            self.assertEqual(reply["session"]["details"]["pexo_task_role"], "Developer")
            self.assertIn("next developer step", reply["reply"]["user_message"].lower())
            mock_run_backend.assert_not_called()
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="codex")
    def test_direct_chat_uses_real_task_session_for_clarification_follow_up(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        db = SessionLocal()
        try:
            session = create_chat_session(db, backend="auto", workspace_path=str(PROJECT_ROOT))

            first_reply = send_chat_message(
                db,
                session_id=session["id"],
                message="Fix it.",
            )
            self.assertEqual(first_reply["session"]["details"]["pexo_task_status"], "clarification_required")
            self.assertTrue(first_reply["reply"]["pexo_session_id"])

            mock_run_backend.side_effect = [
                "Adjusted the landing page layout to fix the issue.",
            ]

            second_reply = send_chat_message(
                db,
                session_id=session["id"],
                message="The landing page layout is broken on desktop.",
            )

            self.assertEqual(second_reply["session"]["details"]["pexo_task_status"], "agent_action_required")
            self.assertEqual(second_reply["session"]["details"]["pexo_task_role"], "Developer")
            self.assertEqual(
                first_reply["reply"]["pexo_session_id"],
                second_reply["reply"]["pexo_session_id"],
            )
            self.assertIn("next developer step", second_reply["reply"]["user_message"].lower())
        finally:
            db.close()

    @patch("app.direct_chat.run_direct_chat_backend")
    @patch("app.direct_chat._ensure_backend_connected")
    @patch("app.direct_chat._resolve_backend_name", return_value="gemini")
    def test_chat_api_and_snapshot_include_direct_chat(self, mock_backend_name, mock_connect, mock_run_backend):
        os.environ["PEXO_NO_BROWSER"] = "1"
        init_db()
        mock_run_backend.side_effect = [
            "Hi. Pexo is online and ready.",
            "I can keep the plan local-first and simple.",
        ]

        client = TestClient(app)
        create_response = client.post("/chat/sessions", json={"backend": "auto", "workspace_path": str(PROJECT_ROOT)})
        self.assertEqual(create_response.status_code, 200)
        session_id = create_response.json()["id"]

        first_message = client.post(f"/chat/sessions/{session_id}/messages", json={"message": "This is a direct chat smoke test."})
        self.assertEqual(first_message.status_code, 200)
        self.assertEqual(first_message.json()["reply"]["status"], "answered")

        second_message = client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"message": "Keep it local-first and very simple."},
        )
        self.assertEqual(second_message.status_code, 200)
        self.assertEqual(second_message.json()["reply"]["status"], "answered")
        self.assertEqual(mock_run_backend.call_count, 2)

        snapshot = client.get("/admin/snapshot")
        self.assertEqual(snapshot.status_code, 200)
        payload = snapshot.json()
        self.assertGreaterEqual(payload["stats"]["chat_count"], 1)
        self.assertGreaterEqual(len(payload["recent_chats"]), 1)

    def test_direct_chat_defaults_workspace_away_from_windows_system_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            windir = Path(tmpdir) / "Windows"
            system32 = windir / "System32"
            fake_home = Path(tmpdir) / "home"
            system32.mkdir(parents=True)
            fake_home.mkdir()
            with patch.dict(os.environ, {"WINDIR": str(windir)}), patch("app.direct_chat.Path.cwd", return_value=system32), patch("app.direct_chat.Path.home", return_value=fake_home):
                from app.direct_chat import _default_workspace_path

                self.assertEqual(
                    os.path.normcase(os.path.realpath(_default_workspace_path())),
                    os.path.normcase(os.path.realpath(str(fake_home))),
                )

    def test_artifact_upload_endpoint_accepts_file_content(self):
        os.environ["PEXO_NO_BROWSER"] = "1"
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifacts"
            with patch("app.routers.artifacts.ARTIFACTS_DIR", artifact_dir):
                client = TestClient(app)
                response = client.post(
                    "/artifacts/upload",
                    data={"session_id": "upload-session", "task_context": "uploads"},
                    files={"file": ("trace.log", b"upload artifact body", "text/plain")},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["status"], "success")
                self.assertTrue(payload["artifact"]["has_text"])
                self.assertIn("upload artifact body", payload["artifact"]["extracted_text"])

    def test_tool_execution_runs_in_subprocess_and_captures_output(self):
        init_db()
        db = SessionLocal()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tool_dir = Path(tmpdir)
                with patch("app.routers.tools.DYNAMIC_TOOLS_DIR", tool_dir):
                    register_tool(
                        ToolRegistrationRequest(
                            name="cwd_echo",
                            description="Echoes cwd and prints stdout.",
                            python_code=(
                                "from pathlib import Path\n"
                                "def run(**kwargs):\n"
                                "    print(f\"printed:{kwargs['value']}\")\n"
                                "    return {'cwd': str(Path.cwd()), 'value': kwargs['value']}\n"
                            ),
                        ),
                        db,
                    )
                    result = execute_tool(
                        "cwd_echo",
                        ToolExecutionRequest(
                            kwargs={"value": "hello"},
                            session_id="tool-session",
                            working_directory=str(PROJECT_ROOT),
                        ),
                        db,
                    )
                    self.assertEqual(result["status"], "success")
                    self.assertEqual(result["execution_mode"], "subprocess")
                    self.assertIn("printed:hello", result["stdout"])
                    self.assertEqual(result["result"]["value"], "hello")
                    self.assertEqual(Path(result["result"]["cwd"]), PROJECT_ROOT)

                    log_entry = db.query(AgentState).filter(AgentState.session_id == "tool-session").first()
                    self.assertIsNotNone(log_entry)
                    self.assertEqual(log_entry.agent_name, "Genesis:cwd_echo")
        finally:
            db.close()

    def test_tool_execution_rejects_outside_working_directory_by_default(self):
        init_db()
        db = SessionLocal()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tool_dir = Path(tmpdir) / "tools"
                outside_dir = Path(tmpdir) / "outside"
                tool_dir.mkdir()
                outside_dir.mkdir()
                with patch("app.routers.tools.DYNAMIC_TOOLS_DIR", tool_dir):
                    register_tool(
                        ToolRegistrationRequest(
                            name="safe_tool",
                            description="No-op",
                            python_code="def run(**kwargs):\n    return kwargs\n",
                        ),
                        db,
                    )
                    with self.assertRaises(HTTPException) as ctx:
                        execute_tool(
                            "safe_tool",
                            ToolExecutionRequest(
                                kwargs={},
                                session_id="tool-session",
                                working_directory=str(outside_dir),
                            ),
                            db,
                        )
                    self.assertEqual(ctx.exception.status_code, 400)
        finally:
            db.close()

    def test_mcp_profile_and_agent_tools_expose_structured_control_plane(self):
        init_db()

        questions = pexo_get_profile_questions()
        self.assertIn("personality", questions)
        self.assertIn("scripting", questions)

        presets = pexo_list_profile_presets()
        self.assertTrue(any(preset["id"] == "efficient_operator" for preset in presets))

        setup = pexo_quick_setup_profile("efficient_operator")
        self.assertEqual(setup["status"], "success")
        self.assertEqual(setup["profile"]["name"], "default_user")

        updated_profile = pexo_update_profile(personality_answers={"p1": "2"})
        self.assertEqual(updated_profile["profile_answers"]["personality_answers"]["p1"], "2")

        profile = pexo_get_profile()
        self.assertEqual(profile["profile"]["name"], "default_user")
        self.assertEqual(profile["profile_answers"]["personality_answers"]["p1"], "2")

        profile_bundle = pexo_read_profile()
        self.assertIn("profile", profile_bundle)
        self.assertIn("agents", profile_bundle)

        created_agent = pexo_create_agent(
            name="Reviewer",
            role="Code Reviewer",
            system_prompt="Review all code for correctness.",
            capabilities=["review", "analyze"],
        )
        self.assertEqual(created_agent["name"], "Reviewer")

        updated_agent = pexo_update_agent(
            agent_name="Reviewer",
            system_prompt="Review all code for correctness and regressions.",
            capabilities=["review"],
        )
        self.assertEqual(updated_agent["status"], "success")
        self.assertEqual(updated_agent["agent"]["capabilities"], ["review"])

        agents = pexo_list_agents()
        self.assertTrue(any(agent["name"] == "Reviewer" for agent in agents))

        deleted_agent = pexo_delete_agent(agent_name="Reviewer")
        self.assertEqual(deleted_agent["status"], "success")

    @patch("app.routers.memory.get_memory_collection")
    def test_mcp_memory_admin_and_session_tools(self, mock_get_memory_collection):
        mock_get_memory_collection.return_value = FakeCollection()
        init_db()

        stored = pexo_store_memory(
            content="Implemented a deterministic smoke path.",
            task_context="task-mcp",
            session_id="session-mcp",
        )
        memory_id = stored["memory_id"]
        self.assertIn("maintenance", stored)

        recent = pexo_list_recent_memories(limit=5, include_archived=True)
        self.assertTrue(any(memory["id"] == memory_id for memory in recent["memories"]))

        memory = pexo_get_memory(memory_id)
        self.assertEqual(memory["task_context"], "task-mcp")

        updated = pexo_update_memory(memory_id, content="Updated deterministic smoke path.", is_pinned=True)
        self.assertEqual(updated["status"], "success")
        self.assertTrue(updated["memory"]["is_pinned"])

        maintenance = pexo_run_memory_maintenance("task-mcp")
        self.assertEqual(maintenance["status"], "success")

        intake = pexo_intake_prompt("Create a test plan for local execution.")
        self.assertIn("clarification_question", intake)

        execution = pexo_execute_plan(intake["session_id"], "Prefer a flat workspace layout.")
        self.assertEqual(execution["session_id"], intake["session_id"])

        next_step = pexo_get_next_task(intake["session_id"])
        self.assertEqual(next_step["status"], "pending_action")
        self.assertEqual(next_step["role"], "Supervisor")

        submit = pexo_submit_task_result(
            intake["session_id"],
            [{"id": "task-1", "description": "Write the plan", "assigned_agent": "Developer"}],
        )
        self.assertEqual(submit["status"], "Result accepted. Graph advanced.")

        sessions = pexo_list_sessions()
        self.assertTrue(any(session["session_id"] == intake["session_id"] for session in sessions))

        activity = pexo_get_session_activity(intake["session_id"])
        self.assertTrue(any(item["agent_name"] == "orchestrator" for item in activity))

        telemetry = pexo_get_telemetry()
        self.assertGreaterEqual(telemetry["summary"]["session_count"], 1)

        snapshot = pexo_get_admin_snapshot(memory_limit=5)
        self.assertIn("telemetry", snapshot)
        self.assertIn("recent_memories", snapshot)

        simple_start = pexo_start_task("Help me with this plan.")
        self.assertEqual(simple_start["status"], "clarification_required")
        self.assertIn("user_message", simple_start)

        simple_continue = pexo_continue_task(
            simple_start["session_id"],
            clarification_answer="Keep it lightweight and local-first.",
        )
        self.assertEqual(simple_continue["status"], "agent_action_required")
        self.assertEqual(simple_continue["role"], "Supervisor")
        self.assertIn("user_message", simple_continue)
        self.assertIn("agent_instruction", simple_continue)

        simple_status = pexo_get_task_status(simple_start["session_id"])
        self.assertEqual(simple_status["status"], "agent_action_required")
        self.assertIn("user_message", simple_status)

        simple_submit = pexo_continue_task(
            simple_start["session_id"],
            result_data=[{"id": "task-1", "description": "Write the plan", "assigned_agent": "Developer"}],
        )
        self.assertIn(simple_submit["status"], {"agent_action_required", "complete"})

        deleted = pexo_delete_memory(memory_id)
        self.assertEqual(deleted["status"], "success")

    def test_mcp_brain_surface_bootstraps_context_and_exposes_resources(self):
        init_db()
        pexo_quick_setup_profile("efficient_operator")

        remembered = pexo_remember_context(
            "Brain bootstrap note for later recall.",
            task_context="brain-test",
            session_id="brain-session",
        )
        self.assertEqual(remembered["status"], "success")
        self.assertEqual(remembered["memory"]["task_context"], "brain-test")

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write("# Brain artifact\n\nStored for bootstrap testing.\n")
            artifact_path = handle.name

        try:
            attached = pexo_attach_context(
                artifact_path,
                session_id="brain-session",
                task_context="brain-test",
            )
            self.assertEqual(attached["status"], "success")
            self.assertTrue(attached["artifact"]["has_text"])
        finally:
            Path(artifact_path).unlink(missing_ok=True)

        attached_text = pexo_attach_text_context(
            name="brain-note.txt",
            content="Saved inline context for bootstrap.",
            session_id="brain-session",
            task_context="brain-test",
        )
        self.assertEqual(attached_text["status"], "success")

        recall = pexo_recall_context("brain", memory_results=5, artifact_results=5)
        self.assertTrue(any("Brain bootstrap note" in item["content"] for item in recall["memory"]["results"]))
        self.assertGreaterEqual(len(recall["artifacts"]["results"]), 1)

        auto_exchange = pexo(
            message="Summarize the current local brain state.",
            remember="Auto exchange recorded this bootstrap check.",
            task_context="brain-test",
        )
        self.assertEqual(auto_exchange["mode"], "exchange")
        self.assertEqual(auto_exchange["status"], "clarification_required")
        self.assertEqual(auto_exchange["next_action"], "ask_user")

        exchange = pexo_exchange(
            message="Summarize the current local brain state.",
            remember="Exchange recorded this bootstrap check.",
            task_context="brain-test",
            attach_text="Saved exchange note.",
            attach_name="exchange-note.txt",
        )
        self.assertEqual(exchange["mode"], "exchange")
        self.assertEqual(exchange["status"], "clarification_required")
        self.assertEqual(exchange["next_action"], "ask_user")
        self.assertIn("brain", exchange)
        self.assertIn("writes", exchange)
        self.assertEqual(exchange["writes"]["memory"]["task_context"], "brain-test")
        self.assertEqual(len(exchange["writes"]["artifacts"]), 1)

        exchange_continue = pexo_exchange(
            session_id=exchange["session_id"],
            message="Keep it lightweight and local-first.",
        )
        self.assertEqual(exchange_continue["status"], "agent_action_required")
        self.assertEqual(exchange_continue["next_action"], "perform_agent_work")
        self.assertEqual(exchange_continue["role"], "Supervisor")

        exchange_submit = pexo_exchange(
            session_id=exchange["session_id"],
            agent_result=[{"id": "task-1", "description": "Write the plan", "assigned_agent": "Developer"}],
        )
        self.assertIn(exchange_submit["status"], {"agent_action_required", "complete"})

        bootstrap = pexo_bootstrap_brain(
            prompt="Summarize the current local brain state.",
            query="brain",
        )
        self.assertEqual(bootstrap["mode"], "brain")
        self.assertIn("operating_contract", bootstrap)
        self.assertIn("pexo", " ".join(bootstrap["operating_contract"]))
        self.assertEqual(bootstrap["task"]["status"], "clarification_required")
        self.assertGreaterEqual(len(bootstrap["memory"]["results"]), 1)
        self.assertGreaterEqual(len(bootstrap["artifacts"]["results"]), 1)

        prompts = mcp.list_prompts()
        if asyncio.iscoroutine(prompts):
            prompts = asyncio.run(prompts)
        self.assertTrue(any(prompt.name == "pexo_default_task_flow" for prompt in prompts))

        resource_items = mcp.read_resource("pexo://brain-guide")
        if asyncio.iscoroutine(resource_items):
            resource_items = asyncio.run(resource_items)
        resource_items = list(resource_items)
        resource_text = "\n".join(getattr(item, "text", str(item)) for item in resource_items)
        self.assertIn("`pexo`", resource_text)
        self.assertIn("pexo_recall_context", resource_text)

    def test_mcp_genesis_tool_lifecycle(self):
        init_db()
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_tools_dir = Path(tmpdir)
            with patch("app.routers.tools.DYNAMIC_TOOLS_DIR", temp_tools_dir):
                created = pexo_register_tool(
                    name="echo_tool",
                    description="Echoes keyword arguments.",
                    python_code="def run(**kwargs):\n    return {'echo': kwargs.get('message', ''), 'count': kwargs.get('count', 0)}\n",
                )
                self.assertEqual(created["status"], "Success. Genesis Engine has assimilated the new tool.")

                tool = pexo_get_tool("echo_tool")
                self.assertEqual(tool["name"], "echo_tool")
                self.assertIn("def run", tool["python_code"])

                tools = pexo_list_tools()
                self.assertTrue(any(entry["name"] == "echo_tool" for entry in tools))

                updated = pexo_update_tool(
                    "echo_tool",
                    description="Echoes keyword arguments in uppercase.",
                    python_code="def run(**kwargs):\n    return {'echo': str(kwargs.get('message', '')).upper()}\n",
                )
                self.assertEqual(updated["status"], "success")
                self.assertIn("uppercase", updated["tool"]["description"])

                executed = pexo_execute_tool("echo_tool", {"message": "hello"})
                self.assertEqual(executed["status"], "success")
                self.assertEqual(executed["result"]["echo"], "HELLO")

                deleted = pexo_delete_tool("echo_tool")
                self.assertEqual(deleted["status"], "success")
                self.assertFalse((temp_tools_dir / "echo_tool.py").exists())

    def test_mcp_artifact_tools_round_trip(self):
        init_db()
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifacts"
            source_path = Path(tmpdir) / "readme.txt"
            source_path.write_text("Artifact MCP content.", encoding="utf-8")
            with patch("app.routers.artifacts.ARTIFACTS_DIR", artifact_dir):
                created_text = pexo_register_artifact_text(
                    name="memo.txt",
                    content="Artifact note body.",
                    session_id="artifact-session",
                    task_context="mcp",
                )
                created_path = pexo_register_artifact_path(
                    path=str(source_path),
                    session_id="artifact-session",
                    task_context="mcp",
                )
                listing = pexo_list_artifacts(limit=10, query="Artifact")
                self.assertEqual(len(listing["artifacts"]), 2)
                fetched = pexo_get_artifact(created_path["artifact"]["id"])
                self.assertIn("Artifact MCP content.", fetched["extracted_text"])
                deleted = pexo_delete_artifact(created_text["artifact"]["id"])
                self.assertEqual(deleted["status"], "success")


if __name__ == "__main__":
    unittest.main()
