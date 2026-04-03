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
from app.models import AgentProfile, Memory, Profile
from app.paths import CHROMA_DB_DIR, PEXO_DB_PATH
from app.routers.backup import create_backup_archive
from app.routers.memory import (
    MemoryStoreRequest,
    MemoryUpdateRequest,
    delete_memory,
    store_memory,
    update_memory,
)
from app.routers.profile import ProfileAnswers, build_profile_from_preset, upsert_profile
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


if __name__ == "__main__":
    unittest.main()
