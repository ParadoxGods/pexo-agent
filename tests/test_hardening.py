import json
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import inspect

from app.cli import headless_setup, list_presets
from app.database import SessionLocal, engine, init_db
from app.mcp_server import (
    pexo_create_agent,
    pexo_delete_agent,
    pexo_delete_memory,
    pexo_delete_tool,
    pexo_execute_plan,
    pexo_execute_tool,
    pexo_get_admin_snapshot,
    pexo_get_memory,
    pexo_get_next_task,
    pexo_get_profile,
    pexo_get_profile_questions,
    pexo_get_session_activity,
    pexo_get_telemetry,
    pexo_get_tool,
    pexo_intake_prompt,
    pexo_list_agents,
    pexo_list_profile_presets,
    pexo_list_recent_memories,
    pexo_list_sessions,
    pexo_list_tools,
    pexo_quick_setup_profile,
    pexo_read_profile,
    pexo_register_tool,
    pexo_run_memory_maintenance,
    pexo_store_memory,
    pexo_submit_task_result,
    pexo_update_agent,
    pexo_update_memory,
    pexo_update_profile,
    pexo_update_tool,
)
from app.models import AgentProfile, AgentState, Memory, Profile
from app.paths import CHROMA_DB_DIR, PEXO_DB_PATH
from app.routers.admin import build_telemetry_payload
from app.routers.backup import create_backup_archive
from app.routers.memory import (
    MemoryStoreRequest,
    MemoryUpdateRequest,
    delete_memory,
    maintain_memory_health,
    store_memory,
    update_memory,
)
from app.routers.profile import ProfileAnswers, build_profile_from_preset, derive_profile_answers, upsert_profile
from app.routers.tools import resolve_tool_path


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

    def test_init_db_creates_all_tables_without_preimporting_models(self):
        engine.dispose()
        PEXO_DB_PATH.unlink(missing_ok=True)

        init_db()

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        self.assertTrue(
            {"profiles", "agent_profiles", "memories", "dynamic_tools", "agent_states", "workspaces"}.issubset(table_names)
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
        self.assertIn("/admin/snapshot", html)

    def test_install_scripts_report_progress_percentages(self):
        powershell_installer = Path("install.ps1").read_text(encoding="utf-8")
        shell_installer = Path("install.sh").read_text(encoding="utf-8")

        self.assertIn("Show-InstallProgress", powershell_installer)
        self.assertIn("HeadlessSetup", powershell_installer)
        self.assertIn("Installing Python dependencies... still working", powershell_installer)
        self.assertIn("--headless-setup", shell_installer)
        self.assertIn("print_progress 100", shell_installer)
        self.assertIn("Installing Python dependencies... still working", shell_installer)

    def test_launchers_expose_headless_setup_commands(self):
        shell_launcher = Path("pexo").read_text(encoding="utf-8")
        batch_launcher = Path("pexo.bat").read_text(encoding="utf-8")

        self.assertIn("--list-presets", shell_launcher)
        self.assertIn("--headless-setup", shell_launcher)
        self.assertIn("--update", shell_launcher)
        self.assertIn("--no-browser", shell_launcher)
        self.assertIn(".pexo-update-check", shell_launcher)
        self.assertIn("--list-presets", batch_launcher)
        self.assertIn("--headless-setup", batch_launcher)
        self.assertIn("--update", batch_launcher)
        self.assertIn("--no-browser", batch_launcher)
        self.assertIn(".pexo-update-check", batch_launcher)

    def test_gitattributes_enforces_shell_script_line_endings(self):
        content = Path(".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", content)
        self.assertIn("pexo text eol=lf", content)

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


if __name__ == "__main__":
    unittest.main()
