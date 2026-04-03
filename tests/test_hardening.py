import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import inspect

from app.database import SessionLocal, engine, init_db
from app.paths import PEXO_DB_PATH
from app.routers.backup import create_backup_archive
from app.routers.profile import ProfileAnswers, build_profile_from_preset, upsert_profile
from app.routers.tools import resolve_tool_path


class HardeningTests(unittest.TestCase):
    def tearDown(self):
        engine.dispose()
        PEXO_DB_PATH.unlink(missing_ok=True)

    def test_init_db_creates_all_tables_without_preimporting_models(self):
        engine.dispose()
        PEXO_DB_PATH.unlink(missing_ok=True)

        init_db()

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        self.assertTrue(
            {"profiles", "agent_profiles", "memories", "dynamic_tools", "agent_states", "workspaces"}.issubset(table_names)
        )

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

    def test_install_scripts_report_progress_percentages(self):
        powershell_installer = Path("install.ps1").read_text(encoding="utf-8")
        shell_installer = Path("install.sh").read_text(encoding="utf-8")

        self.assertIn("Show-InstallProgress", powershell_installer)
        self.assertIn("Installing Python dependencies... still working", powershell_installer)
        self.assertIn("print_progress 100", shell_installer)
        self.assertIn("Installing Python dependencies... still working", shell_installer)

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


if __name__ == "__main__":
    unittest.main()
