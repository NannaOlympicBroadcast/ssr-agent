from pathlib import Path
import tempfile
import shutil
from ssr.config import Settings
from ssr.rules import load_rules
from ssr.agent.core import SSRAgent

def test_load_rules_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        assert load_rules(settings) == ""

def test_load_rules_with_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        
        settings.rules_file.write_text("rule 1", encoding="utf-8")
        settings.project_rules_file.write_text("rule 2", encoding="utf-8")
        
        rules = load_rules(settings)
        assert "Global Rules" in rules
        assert "rule 1" in rules
        assert "Project Rules" in rules
        assert "rule 2" in rules
