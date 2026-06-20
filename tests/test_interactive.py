import threading
import time
from unittest.mock import MagicMock, patch
import pytest

from ssr.config import Settings
from ssr.agent.tools import ToolKit
from ssr.approval import TUIApprovalHandler, ApprovalDecision, get_active_approval, set_active_approval


def test_tui_approval_handler_fallback(tmp_path):
    # Tests the fallback behavior (blocking input()) when agent._is_polling_stdin is False
    settings = Settings(home=tmp_path / "home", project_dir=tmp_path / "proj")
    settings.ensure_dirs()

    # Mock tool kit and agent
    toolkit = MagicMock(spec=ToolKit)
    toolkit.settings = settings
    agent = MagicMock()
    agent._is_polling_stdin = False
    toolkit.agent_instance = agent

    handler = TUIApprovalHandler()
    handler.toolkit = toolkit

    # Mock inputs:
    # First choice is invalid
    # Second choice is "1" (ALLOW_ONCE)
    with patch("builtins.input", side_effect=["invalid", "1"]):
        decision = handler.request_approval("some_command")
        assert decision == ApprovalDecision.ALLOW_ONCE

    # Test "2" (ALWAYS_ALLOW)
    with patch("builtins.input", return_value="2"):
        decision = handler.request_approval("some_command")
        assert decision == ApprovalDecision.ALWAYS_ALLOW

    # Test "3" (DENY) with reason
    with patch("builtins.input", side_effect=["3", "custom reason"]):
        decision = handler.request_approval("some_command")
        assert decision == ApprovalDecision.DENY
        assert handler.denial_reason == "custom reason"

    # Test slash commands like "/approve"
    with patch("builtins.input", return_value="/approve"):
        decision = handler.request_approval("some_command")
        assert decision == ApprovalDecision.ALLOW_ONCE

    # Test slash commands like "/alwaysallow"
    with patch("builtins.input", return_value="/alwaysallow"):
        decision = handler.request_approval("some_command")
        assert decision == ApprovalDecision.ALWAYS_ALLOW

    # Test slash commands like "/disallow custom reason"
    with patch("builtins.input", return_value="/disallow custom reason"):
        decision = handler.request_approval("some_command")
        assert decision == ApprovalDecision.DENY
        assert handler.denial_reason == "custom reason"


def test_tui_approval_handler_polling(tmp_path):
    # Tests the event-driven behavior when agent._is_polling_stdin is True
    settings = Settings(home=tmp_path / "home", project_dir=tmp_path / "proj")
    settings.ensure_dirs()

    toolkit = MagicMock(spec=ToolKit)
    toolkit.settings = settings
    agent = MagicMock()
    agent._is_polling_stdin = True
    toolkit.agent_instance = agent

    handler = TUIApprovalHandler()
    handler.toolkit = toolkit

    # Create a background thread to approve after a tiny sleep
    def approve_later():
        time.sleep(0.1)
        approval = get_active_approval()
        assert approval is not None
        assert approval.command == "some_command"
        approval.decision = ApprovalDecision.ALLOW_ONCE
        approval.event.set()

    t = threading.Thread(target=approve_later)
    t.start()

    decision = handler.request_approval("some_command")
    t.join()

    assert decision == ApprovalDecision.ALLOW_ONCE
    assert get_active_approval() is None
