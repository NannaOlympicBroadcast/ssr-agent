import time
import sys
from pathlib import Path
import tempfile
from ssr.config import Settings
from ssr.agent.tools import ToolKit
from ssr.agent.async_terminal import AsyncTerminal

def test_async_terminal_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        
        toolkit = ToolKit(settings, retriever=None, memory=None)
        toolkit.permission_manager.turbo_mode = True
        
        # Test spawn
        tid = toolkit.spawn_terminal("echo hello")
        assert tid is not None
        
        # Test list_terminals
        terms = toolkit.terminal.list_terminals()
        assert len(terms) == 1
        assert terms[0]["id"] == tid
        
        # Wait a bit for output
        time.sleep(2)
        
        # Test check_terminal
        out = toolkit.check_terminal(tid)
        assert "hello" in out.lower()
        
        # Clean up
        toolkit.terminal.cleanup()
        assert len(toolkit.terminal.list_terminals()) == 0

def test_async_terminal_send_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path,
        )
        settings.ensure_dirs()
        
        # Write a simple helper script to avoid cmd quoting issues on Windows
        script_path = tmp_path / "test_stdin.py"
        script_path.write_text(
            "import sys\n"
            "sys.stdout.write('prompt\\n')\n"
            "sys.stdout.flush()\n"
            "line = sys.stdin.readline()\n"
            "sys.stdout.write('echo:' + line)\n"
            "sys.stdout.flush()\n",
            encoding="utf-8"
        )
        
        toolkit = ToolKit(settings, retriever=None, memory=None)
        toolkit.permission_manager.turbo_mode = True
        
        # Start the script using current python executable
        py_path = sys.executable
        cmd = f'"{py_path}" -u "{script_path}"'
        tid = toolkit.spawn_terminal(cmd)
        
        time.sleep(2)
        out = toolkit.check_terminal(tid)
        assert "prompt" in out
        
        toolkit.send_to_terminal(tid, "hello input")
        time.sleep(2)
        
        out2 = toolkit.check_terminal(tid)
        assert "echo:hello input" in out2
        
        toolkit.kill_terminal(tid)
        toolkit.terminal.cleanup()
        
        # Wait a tiny bit for the OS to release the processes
        time.sleep(1)

def test_terminal_completion_wakeup():
    from ssr.agent.core import SSRAgent
    from ssr.config import Settings
    from unittest.mock import MagicMock, patch
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        
        # Instantiate agent
        agent = SSRAgent(settings)
        agent.session_id = "test-session"
        
        # Set active IM context before spawning terminal
        agent.active_im_context = ("feishu", "mock_chat_id")
        
        # Mock agent.run
        mock_reply = "Done processing terminal task!"
        agent.run = MagicMock(return_value=mock_reply)
        
        # Spy on push_notification_impl
        with patch("ssr.agent.tools_push.push_notification_impl") as mock_push:
            # Manually register the context to test tools integration
            tid = agent.toolkit.spawn_terminal("echo hello")
            assert tid in agent.terminal_contexts
            assert agent.terminal_contexts[tid] == ("feishu", "mock_chat_id")
            
            # Wait for terminal to finish and trigger callback
            time.sleep(2)
            
            # Since the callback starts a background thread with time.sleep(0.5) before running,
            # we wait for it to complete.
            time.sleep(1)
            
            # Assert agent.run was called with correct system prompt
            agent.run.assert_called_once()
            call_args = agent.run.call_args[0][0]
            assert f"terminal {tid} completed" in call_args.lower()
            
            # Assert push_notification_impl was called to push reply to feishu
            mock_push.assert_called_once_with(agent.settings, "feishu", "mock_chat_id", mock_reply)
            
            agent.close()
            agent.toolkit.terminal.cleanup()
