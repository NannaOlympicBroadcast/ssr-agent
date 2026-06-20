import json
import tempfile
import sys
from pathlib import Path
from ssr.config import Settings
from ssr.hooks import load_hooks, run_hooks


def test_load_hooks_merging():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()

        # Write hooks config to global home
        global_hooks = {
            "hooks": {
                "UserPromptSubmit": [
                    {"command": "echo global_submit", "timeout": 5}
                ]
            }
        }
        (settings.home / "hooks.json").write_text(json.dumps(global_hooks), encoding="utf-8")

        # Write hooks config to project state dir
        settings.project_state_dir.mkdir(parents=True, exist_ok=True)
        project_hooks = {
            "hooks": {
                "UserPromptSubmit": [
                    {"command": "echo project_submit"}
                ],
                "Stop": [
                    {"command": "echo project_stop"}
                ]
            }
        }
        (settings.project_state_dir / "hooks.json").write_text(json.dumps(project_hooks), encoding="utf-8")

        # Load hooks
        merged = load_hooks(settings)
        
        # Verify merged hooks
        assert "UserPromptSubmit" in merged
        assert len(merged["UserPromptSubmit"]) == 2
        assert merged["UserPromptSubmit"][0]["command"] == "echo global_submit"
        assert merged["UserPromptSubmit"][1]["command"] == "echo project_submit"
        
        assert "Stop" in merged
        assert len(merged["Stop"]) == 1
        assert merged["Stop"][0]["command"] == "echo project_stop"


def test_run_hooks_stdin_and_output():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        settings.project_state_dir.mkdir(parents=True, exist_ok=True)

        # Config a hook that echoes back the stdin using python
        python_executable = sys.executable.replace("\\", "/")
        cmd = f'"{python_executable}" -c "import sys, json; data = json.load(sys.stdin); print(data[\'val\'].upper())"'
        hooks = {
            "hooks": {
                "UserPromptSubmit": [
                    {"command": cmd}
                ]
            }
        }
        (settings.project_state_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

        # Run hooks
        payload = {"val": "hello world"}
        results = run_hooks(settings, "UserPromptSubmit", payload)

        assert len(results) == 1
        assert results[0]["exit_code"] == 0
        assert "HELLO WORLD" in results[0]["stdout"]


def test_run_hooks_timeout_and_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        settings.project_state_dir.mkdir(parents=True, exist_ok=True)

        python_executable = sys.executable.replace("\\", "/")
        # Hook that sleeps for a long time
        cmd_timeout = f'"{python_executable}" -c "import time; time.sleep(10)"'
        # Hook that fails immediately
        cmd_fail = f'"{python_executable}" -c "import sys; sys.exit(42)"'

        hooks = {
            "hooks": {
                "UserPromptSubmit": [
                    {"command": cmd_timeout, "timeout": 1},
                    {"command": cmd_fail}
                ]
            }
        }
        (settings.project_state_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

        results = run_hooks(settings, "UserPromptSubmit", {})
        assert len(results) == 2

        # First one should error due to timeout
        assert "error" in results[0]
        # Second one should have exit code 42
        assert results[1]["exit_code"] == 42
