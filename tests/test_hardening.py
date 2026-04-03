import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import inspect

from app.database import engine, init_db
from app.paths import PEXO_DB_PATH
from app.routers.backup import create_backup_archive
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


if __name__ == "__main__":
    unittest.main()
