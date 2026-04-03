import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect

import app.routers.memory as memory_router
import app.runtime as runtime_module
from app.client_connect import build_client_connection_plan, connect_clients
from app.cli import headless_setup, list_presets
from app.agents.graph import FallbackPexoApp
from app.main import app
from app.database import SessionLocal, engine, init_db
from app.mcp_server import (
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
    pexo_read_profile,
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
from app.models import AgentProfile, AgentState, Memory, Profile
from app.paths import ARTIFACTS_DIR, CHROMA_DB_DIR, CODE_ROOT, PEXO_DB_PATH, PROJECT_ROOT, looks_like_repo_checkout, resolve_state_root
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
        PEXO_DB_PATH.unlink(missing_ok=True)
        shutil.rmtree(CHROMA_DB_DIR, ignore_errors=True)
        shutil.rmtree(ARTIFACTS_DIR, ignore_errors=True)

    def test_init_db_creates_all_tables_without_preimporting_models(self):
        engine.dispose()
        PEXO_DB_PATH.unlink(missing_ok=True)

        init_db()

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        self.assertTrue(
            {"profiles", "agent_profiles", "memories", "dynamic_tools", "agent_states", "workspaces", "artifacts", "system_settings"}.issubset(table_names)
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
        self.assertIn("editAgent(", html)
        self.assertIn("saveMemory()", html)
        self.assertIn("deleteMemory(", html)
        self.assertIn("saveProfileSettings()", html)
        self.assertIn("runMemoryMaintenance()", html)
        self.assertIn("telemetry-summary", html)
        self.assertIn("artifact-list", html)
        self.assertIn("promoteRuntime(", html)
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
        self.assertIn("Ready-to-paste Windows MCP config", powershell_installer)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", powershell_installer)
        self.assertIn("--headless-setup", shell_installer)
        self.assertIn("--skip-update", shell_installer)
        self.assertIn("gh auth status -h github.com", shell_installer)
        self.assertIn("ensurepip --upgrade", shell_installer)
        self.assertIn("git_checkout_detached_at", shell_installer)
        self.assertIn("Protected checkout left untouched", shell_installer)
        self.assertIn("Same-shell PATH activation verified", shell_installer)
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
        self.assertIn("--no-browser", shell_launcher)
        self.assertIn("--offline", shell_launcher)
        self.assertIn("--skip-update", shell_launcher)
        self.assertIn("requirements-core.txt", shell_launcher)
        self.assertIn("requirements-mcp.txt", shell_launcher)
        self.assertIn("requirements-full.txt", shell_launcher)
        self.assertIn("requirements-vector.txt", shell_launcher)
        self.assertIn("ensurepip --upgrade", shell_launcher)
        self.assertIn("git_checkout_detached", shell_launcher)
        self.assertIn("Dependency marker", shell_launcher)
        self.assertIn(".pexo-update-check", shell_launcher)
        self.assertIn("--list-presets", batch_launcher)
        self.assertIn("--headless-setup", batch_launcher)
        self.assertIn("--promote", batch_launcher)
        self.assertIn("--update", batch_launcher)
        self.assertIn("--doctor", batch_launcher)
        self.assertIn("--connect", batch_launcher)
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

        self.assertIn('[string]$Ref = "v1.1"', bootstrap_ps)
        self.assertIn('throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join \' \')"', bootstrap_ps)
        self.assertNotIn('throw "Command failed with exit code $LASTEXITCODE:', bootstrap_ps)
        self.assertIn('[string]$ConnectClients = "all"', bootstrap_ps)
        self.assertIn('Invoke-External -Percent 20 -Message "Installing packaged Pexo tool" -FilePath "uv"', bootstrap_ps)
        self.assertIn('Invoke-External -Percent 20 -Message "Installing packaged Pexo tool" -FilePath "pipx"', bootstrap_ps)
        self.assertIn('Standalone bootstrap does not support repo-local install', bootstrap_ps)
        self.assertIn('Invoke-DoctorCommand -Percent 92 -CommandPath "pexo"', bootstrap_ps)
        self.assertIn('Invoke-ConnectCommand -Percent 97 -CommandPath "pexo" -ClientTarget $ConnectClients', bootstrap_ps)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", bootstrap_ps)
        self.assertIn('REF="v1.1"', bootstrap_sh)
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
        self.assertIn('python -m build', workflow)
        self.assertIn("SHA256SUMS.txt", workflow)
        self.assertIn("python scripts/build_release_bundle.py", workflow)
        self.assertIn("pexo-install-windows.zip", bundle_script)
        self.assertIn("pexo-install-unix.tar.gz", bundle_script)
        self.assertIn("pexo-install-manifest.json", bundle_script)
        self.assertIn("softprops/action-gh-release", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("FORCE_JAVASCRIPT_ACTIONS_TO_NODE24", workflow)

    def test_readme_documents_packaged_install_and_pexo_mcp(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("gh release download", readme)
        self.assertIn("pexo-install-windows.zip", readme)
        self.assertIn("pexo-install-unix.tar.gz", readme)
        self.assertIn('uv tool install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.1"', readme)
        self.assertIn('pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.1"', readme)
        self.assertIn("pexo-mcp", readme)
        self.assertIn("PEXO_HOME", readme)
        self.assertIn("pexo doctor", readme)
        self.assertIn("pexo connect all --scope user", readme)
        self.assertIn("pexo_start_task", readme)
        self.assertIn("pexo_continue_task", readme)
        self.assertIn("pexo_get_task_status", readme)
        self.assertIn("user_message", readme)
        self.assertIn("Existing Git checkouts are protected by default", readme)
        self.assertIn(".\\install.cmd", readme)
        self.assertIn("-AllowRepoInstall", readme)
        self.assertIn("## Quick Start", readme)
        self.assertNotIn("## Core Architecture", readme)
        self.assertNotIn("## Fleet Quickstart", readme)
        self.assertIn("Legacy raw bootstrap scripts still exist, but they are fallback-only", readme)

    def test_agents_file_documents_safe_windows_install_path(self):
        agents_doc = Path("AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## Default behavior", agents_doc)
        self.assertIn("gh release download", agents_doc)
        self.assertIn("pexo-install-windows.zip", agents_doc)
        self.assertIn("pexo-install-unix.tar.gz", agents_doc)
        self.assertIn('pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.1"', agents_doc)
        self.assertIn("pexo connect all --scope user", agents_doc)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON", agents_doc)
        self.assertIn("Existing Git checkouts are protected by default", agents_doc)
        self.assertIn(".\\install.cmd", agents_doc)
        self.assertIn("-AllowRepoInstall", agents_doc)
        self.assertIn("Do not touch the current repo", agents_doc)
        self.assertIn("Do not execute raw remote scripts", agents_doc)
        self.assertIn("## Simple Task Flow", agents_doc)
        self.assertIn("pexo_start_task", agents_doc)
        self.assertIn("pexo_continue_task", agents_doc)
        self.assertIn("pexo_get_task_status", agents_doc)
        self.assertIn("user_message", agents_doc)

    def test_release_bundle_installers_exist_and_emit_summary(self):
        install_ps = Path("release_bundle/install.ps1").read_text(encoding="utf-8")
        install_sh = Path("release_bundle/install.sh").read_text(encoding="utf-8")
        install_cmd = Path("release_bundle/install.cmd").read_text(encoding="utf-8")

        self.assertIn("SHA256SUMS.txt", install_ps)
        self.assertIn("pipx", install_ps)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", install_ps)
        self.assertIn(".pexo-install.json", install_ps)
        self.assertIn("install.ps1", install_cmd)
        self.assertIn("SHA256SUMS.txt", install_sh)
        self.assertIn("pipx install --force", install_sh)
        self.assertIn("PEXO_INSTALL_SUMMARY_JSON=", install_sh)
        self.assertIn(".pexo-install.json", install_sh)
        self.assertIn("Resetting managed runtime environment", install_ps)
        self.assertIn("Resetting managed runtime environment", install_sh)
        self.assertIn(".pexo-deps-profile", install_ps)
        self.assertIn(".pexo-deps-profile", install_sh)

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
        self.assertIn("FORCE_JAVASCRIPT_ACTIONS_TO_NODE24", workflow)
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
                        "task_id": "task-1",
                        "task_description": "Implement feature",
                        "output_preview": "done",
                        "result_type": "dict",
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
            self.assertEqual(telemetry["summary"]["action_count"], 2)
            self.assertTrue(any(session["session_id"] == "session-telemetry" for session in telemetry["recent_sessions"]))
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

        simple_start = pexo_start_task("Create a simple plan for local execution.")
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
