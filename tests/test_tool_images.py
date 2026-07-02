"""Tests for the tool-image side channel.

A tool's return value is always a plain string — the provider loop only ever
wraps it in a text-only ``function_response``, which carries no image bytes to
the model. Concretely this broke ``arm_get_camera``: it saved a camera frame to
disk and returned a path string, so the agent could never actually SEE the
frame it was told to read pixel coordinates off of ("机器人无法读取视觉图像").

``SSRAgent.queue_tool_image`` / ``take_tool_images`` is the fix: a tool queues
the raw bytes it produced, and every provider's tool-dispatch loop drains that
queue right after running the tool and attaches the bytes as a real inline
image part on the next model turn — the same mechanism ``run_parts`` already
uses for images attached to the start of a turn.

These tests cover the queue itself, ``arm_get_camera`` wiring it up, and each
of the three providers (Gemini / Anthropic / OpenAI) actually delivering the
queued bytes to the underlying SDK on the following request.
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from ssr.bus.core import MessageBus
from ssr.config import Settings
from ssr.models import ModelEntry
from ssr.robotics import protocol as P
from ssr.robotics.tools import ArmTools

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return Settings(home=home, project_dir=tmp_path)


# ------------------------------------------------------------- queue itself
def test_queue_and_take_tool_images_roundtrip(tmp_path):
    from ssr.agent.core import SSRAgent

    agent = SSRAgent(_settings(tmp_path))
    assert agent.take_tool_images() == []  # empty until something queues

    agent.queue_tool_image(b"\x89PNG-a", mime_type="image/png", label="a")
    agent.queue_tool_image(b"\x89PNG-b", mime_type="image/jpeg", label="b")

    imgs = agent.take_tool_images()
    assert [i["label"] for i in imgs] == ["a", "b"]
    assert imgs[0]["data"] == b"\x89PNG-a" and imgs[0]["mime_type"] == "image/png"
    assert imgs[1]["mime_type"] == "image/jpeg"

    # Draining clears the queue.
    assert agent.take_tool_images() == []


def test_queue_tool_image_defaults_mime_and_label(tmp_path):
    from ssr.agent.core import SSRAgent

    agent = SSRAgent(_settings(tmp_path))
    agent.queue_tool_image(b"raw")
    img = agent.take_tool_images()[0]
    assert img["mime_type"] == "image/png" and img["label"] == ""


# --------------------------------------------------------- arm_get_camera
def test_arm_get_camera_queues_image_and_saves_file(tmp_path):
    bus = MessageBus(name="t", source="t")
    queued = []
    agent = SimpleNamespace(
        bus=bus, agent_id="t",
        queue_tool_image=lambda data, mime_type="image/png", label="": queued.append(
            {"data": data, "mime_type": mime_type, "label": label}),
    )
    toolkit = SimpleNamespace(agent_instance=agent, _resolve=lambda p: tmp_path / p)
    tools = ArmTools(toolkit)

    def _reply(ev):
        import threading
        threading.Timer(0.05, lambda: bus.publish(P.TOPIC_CAMERA, {
            "holding": False,
            "camera": {"width": 320, "height": 240},
            "frame_b64": base64.b64encode(PNG_BYTES).decode("ascii"),
        })).start()

    bus.subscribe(P.TOPIC_CAMERA_REQUEST, _reply)

    msg = tools.arm_get_camera()

    assert "attached to this turn" in msg
    assert (tmp_path / "arm_frame.png").read_bytes() == PNG_BYTES
    assert len(queued) == 1
    assert queued[0]["data"] == PNG_BYTES and queued[0]["mime_type"] == "image/png"
    assert queued[0]["label"]


def test_arm_get_camera_without_queue_tool_image_support_still_saves(tmp_path):
    # An agent stub that doesn't implement queue_tool_image (e.g. some future
    # lightweight harness) must not crash arm_get_camera — the file save still
    # happens, it just can't attach the live image this turn.
    bus = MessageBus(name="t", source="t")
    agent = SimpleNamespace(bus=bus, agent_id="t")  # no queue_tool_image
    toolkit = SimpleNamespace(agent_instance=agent, _resolve=lambda p: tmp_path / p)
    tools = ArmTools(toolkit)

    def _reply(ev):
        import threading
        threading.Timer(0.05, lambda: bus.publish(P.TOPIC_CAMERA, {
            "camera": {"width": 8, "height": 4},
            "frame_b64": base64.b64encode(PNG_BYTES).decode("ascii"),
        })).start()

    bus.subscribe(P.TOPIC_CAMERA_REQUEST, _reply)
    msg = tools.arm_get_camera()
    assert "ERROR" not in msg
    assert (tmp_path / "arm_frame.png").read_bytes() == PNG_BYTES


# -------------------------------------------------------------- providers
def _fake_toolkit(tool_fn):
    tk = SimpleNamespace()
    tk.callables = lambda: [tool_fn]
    return tk


@pytest.fixture
def gemini_agent_and_tool(tmp_path, monkeypatch):
    import google.genai  # noqa: F401 -- ensure real types are used
    from ssr.agent.core import SSRAgent

    agent = SSRAgent(_settings(tmp_path))

    def look() -> str:
        """Look through the camera."""
        agent.queue_tool_image(PNG_BYTES, mime_type="image/png", label="frame")
        return "Camera frame captured."

    return agent, look


def test_gemini_provider_attaches_queued_image_to_next_request(gemini_agent_and_tool, monkeypatch):
    from google.genai import types
    from ssr.providers.gemini import GeminiProvider

    agent, look = gemini_agent_and_tool
    entry = ModelEntry(id="g", provider="gemini", model="gemini-3.1-flash-lite",
                       api_key_env="GEMINI_API_KEY", api_key="k")
    provider = GeminiProvider(agent.settings, entry, agent_instance=agent)

    call_log = []

    def fake_generate_content(model, contents, config):
        call_log.append([c for c in contents])
        if len(call_log) == 1:
            content = types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(name="look", args={}))])
        else:
            content = types.Content(role="model", parts=[types.Part(text="done")])
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)])

    monkeypatch.setattr(provider.client.models, "generate_content", fake_generate_content)

    contents = [types.Content(role="user", parts=[types.Part(text="look and report")])]
    result = provider.complete(contents, "sys", _fake_toolkit(look), max_iters=5)

    assert result == "done"
    assert len(call_log) == 2
    # The SECOND request (after the tool ran) must carry the image bytes.
    second_request_contents = call_log[1]
    image_parts = [p for c in second_request_contents for p in (c.parts or [])
                   if getattr(p, "inline_data", None) is not None]
    assert len(image_parts) == 1
    assert image_parts[0].inline_data.data == PNG_BYTES
    assert image_parts[0].inline_data.mime_type == "image/png"
    # The queue must be drained — a second unrelated turn shouldn't resend it.
    assert agent.take_tool_images() == []


def test_anthropic_provider_merges_image_into_tool_result_message(tmp_path, monkeypatch):
    from google.genai import types
    from ssr.agent.core import SSRAgent
    from ssr.providers.anthropic_provider import AnthropicProvider

    agent = SSRAgent(_settings(tmp_path))

    def look() -> str:
        """Look through the camera."""
        agent.queue_tool_image(PNG_BYTES, mime_type="image/png", label="frame")
        return "Camera frame captured."

    entry = ModelEntry(id="a", provider="anthropic", model="claude-x",
                       api_key_env="ANTHROPIC_API_KEY", api_key="k")
    provider = AnthropicProvider(agent.settings, entry, agent_instance=agent)

    call_log = []

    def fake_create(model, system, messages, tools, max_tokens):
        call_log.append(messages)
        if len(call_log) == 1:
            block = SimpleNamespace(type="tool_use", name="look", input={})
            return SimpleNamespace(content=[block])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")])

    monkeypatch.setattr(provider.client.messages, "create", fake_create)

    contents = [types.Content(role="user", parts=[types.Part(text="look and report")])]
    result = provider.complete(contents, "sys", _fake_toolkit(look), max_iters=5)

    assert result == "done"
    assert len(call_log) == 2
    # The message with the tool_result must be a SINGLE "user" message that
    # also carries the image block (Anthropic requires strict user/assistant
    # alternation, so it cannot be a separate trailing message).
    second_request_messages = call_log[1]
    tool_result_msgs = [m for m in second_request_messages
                        if any(b.get("type") == "tool_result" for b in m["content"])]
    assert len(tool_result_msgs) == 1
    msg = tool_result_msgs[0]
    assert msg["role"] == "user"
    image_blocks = [b for b in msg["content"] if b.get("type") == "image"]
    assert len(image_blocks) == 1
    decoded = base64.b64decode(image_blocks[0]["source"]["data"])
    assert decoded == PNG_BYTES
    assert image_blocks[0]["source"]["media_type"] == "image/png"
    # No consecutive duplicate "user" role messages (would violate alternation).
    roles = [m["role"] for m in second_request_messages]
    assert not any(a == b == "user" for a, b in zip(roles, roles[1:]))


def test_openai_provider_sends_image_as_follow_up_user_message(tmp_path, monkeypatch):
    from google.genai import types
    from ssr.agent.core import SSRAgent
    from ssr.providers.openai_provider import OpenAIProvider

    agent = SSRAgent(_settings(tmp_path))

    def look() -> str:
        """Look through the camera."""
        agent.queue_tool_image(PNG_BYTES, mime_type="image/png", label="frame")
        return "Camera frame captured."

    entry = ModelEntry(id="o", provider="openai", model="gpt-x",
                       api_key_env="OPENAI_API_KEY", api_key="k")
    provider = OpenAIProvider(agent.settings, entry, agent_instance=agent)

    call_log = []

    def fake_create(model, messages, tools, tool_choice):
        call_log.append(messages)
        if len(call_log) == 1:
            tc = SimpleNamespace(function=SimpleNamespace(name="look", arguments="{}"))
            message = SimpleNamespace(content=None, tool_calls=[tc])
        else:
            message = SimpleNamespace(content="done", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(provider.client.chat.completions, "create", fake_create)

    contents = [types.Content(role="user", parts=[types.Part(text="look and report")])]
    result = provider.complete(contents, "sys", _fake_toolkit(look), max_iters=5)

    assert result == "done"
    assert len(call_log) == 2
    second_request_messages = call_log[1]
    tool_msgs = [m for m in second_request_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1  # tool result itself stays a plain string message
    image_user_msgs = [
        m for m in second_request_messages
        if m.get("role") == "user" and isinstance(m.get("content"), list)
        and any(b.get("type") == "image_url" for b in m["content"])
    ]
    assert len(image_user_msgs) == 1
    url = image_user_msgs[0]["content"][0]["image_url"]["url"] if \
        image_user_msgs[0]["content"][0]["type"] == "image_url" else \
        image_user_msgs[0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(url.split(",", 1)[1])
    assert decoded == PNG_BYTES
