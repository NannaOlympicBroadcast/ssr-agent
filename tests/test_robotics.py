"""Unit tests for the brain-side robotics components.

These cover the generic (capability-driven) bus protocol, the
:class:`ArmController` caching of capabilities / completions / state / grasp
results, and the cerebellum's pure pieces (VLX-Flow SSE parsing + servo command
parsing) using the real in-process :class:`~ssr.bus.core.MessageBus` (no
simulator, no network). End-to-end manipulation is an integration test against
the Isaac Lab bridge on the robot machine — see ssr/robotics/README.md.
"""

from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

from ssr.bus.core import MessageBus
from ssr.robotics import cerebellum as C
from ssr.robotics import protocol as P
from ssr.robotics.controller import ArmController
from ssr.robotics.tools import ArmTools


def test_action_request_payload_roundtrip():
    req = P.ArmActionRequest(seq_id="", episode=0, command="servo",
                             args={"dx": 0.02, "grip": "close"}, label="servo")
    req.seq_id, req.episode = "abc123", 3
    back = P.ArmActionRequest.from_payload(req.to_payload())
    assert back.command == "servo" and back.args == {"dx": 0.02, "grip": "close"}
    assert back.seq_id == "abc123" and back.episode == 3


def test_grasp_request_payload_roundtrip():
    req = P.ArmGraspRequest(seq_id="g1", label="apple", target_b64="cGln",
                            bbox=[10, 20, 60, 80], frame_width=320,
                            frame_height=240, instruction="小心")
    back = P.ArmGraspRequest.from_payload(req.to_payload())
    assert back.label == "apple" and back.target_b64 == "cGln"
    assert back.bbox == [10, 20, 60, 80]
    assert (back.frame_width, back.frame_height) == (320, 240)
    assert back.instruction == "小心"


def test_raw_request_carries_actions():
    req = P.ArmActionRequest(seq_id="s", episode=1, command="raw",
                             actions=[[0.0] * 8, [0.1] * 8])
    back = P.ArmActionRequest.from_payload(req.to_payload())
    assert back.command == "raw" and len(back.actions) == 2


def test_controller_publishes_skill_invocation():
    bus = MessageBus(name="t", source="t")
    seen = []
    bus.subscribe(P.TOPIC_ACTION_EXECUTE, lambda ev: seen.append(ev))
    ctrl = ArmController(bus)
    ctrl.reset()
    seq = ctrl.execute(P.ArmActionRequest(seq_id="", episode=0, command="move_above",
                                          args={"x": 0.5, "y": -0.1}))
    assert len(seen) == 1
    assert seen[0].payload["command"] == "move_above"
    assert seen[0].payload["args"] == {"x": 0.5, "y": -0.1}
    assert seen[0].payload["seq_id"] == seq


def test_controller_caches_capabilities():
    bus = MessageBus(name="t", source="t")
    ctrl = ArmController(bus)
    caps = {"action_space": {"dof": 8}, "skills": [{"name": "servo"}]}
    bus.publish(P.TOPIC_CAPS, caps)
    got = ctrl.capabilities()
    assert got and got["skills"][0]["name"] == "servo"


def test_controller_caches_completion_events():
    bus = MessageBus(name="t", source="t")
    ctrl = ArmController(bus)
    bus.publish(P.TOPIC_ACTION_COMPLETED, {
        "seq_id": "s1", "episode": 1, "command": "servo", "ok": True,
        "holding": True, "objects": {"obj0": [0.4, 0.0, 0.3]},
    })
    snap = ctrl.last_completion()
    assert snap and snap["holding"] is True
    assert ctrl.latest()["objects"]["obj0"] == [0.4, 0.0, 0.3]


def test_controller_publishes_grasp_request_and_caches_result():
    bus = MessageBus(name="t", source="t")
    seen = []
    bus.subscribe(P.TOPIC_GRASP_REQUEST, lambda ev: seen.append(ev))
    ctrl = ArmController(bus)
    seq = ctrl.grasp(P.ArmGraspRequest(seq_id="", label="apple", target_b64="cGln"))
    assert len(seen) == 1 and seen[0].payload["label"] == "apple"
    assert seen[0].payload["seq_id"] == seq
    assert ctrl.grasp_result_ready() is False
    # A result for a different grasp must not count.
    bus.publish(P.TOPIC_GRASP_RESULT, {"seq_id": "other", "ok": True})
    assert ctrl.grasp_result_ready() is False
    bus.publish(P.TOPIC_GRASP_RESULT,
                {"seq_id": seq, "ok": True, "outcome": "grasped"})
    assert ctrl.grasp_result_ready() is True
    assert ctrl.last_grasp_result()["outcome"] == "grasped"


def test_request_capabilities_publishes_request():
    bus = MessageBus(name="t", source="t")
    seen = []
    bus.subscribe(P.TOPIC_CAPS_REQUEST, lambda ev: seen.append(ev))
    ctrl = ArmController(bus)
    ctrl.request_capabilities(wait=0.0)
    assert len(seen) == 1


def test_arm_invoke_routes_raw_actions_to_dedicated_field():
    # arm_invoke('raw', '{"actions": [...]}') must land the waypoints in
    # ArmActionRequest.actions (what the env reads), not in .args — putting them
    # in .args silently no-ops the env's "raw" skill (it never calls env.step()).
    bus = MessageBus(name="t", source="t")
    seen = []
    bus.subscribe(P.TOPIC_ACTION_EXECUTE, lambda ev: seen.append(ev))
    toolkit = SimpleNamespace(agent_instance=SimpleNamespace(bus=bus, agent_id="t"))
    tools = ArmTools(toolkit)

    tools.arm_invoke(
        "raw",
        '{"actions": [[0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0], '
        '[0.1, 0.1, 0.1, 0.0, 1.0, 0.0, 0.0, 1.0]]}',
    )

    assert len(seen) == 1
    assert seen[0].payload["actions"] == [
        [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0],
        [0.1, 0.1, 0.1, 0.0, 1.0, 0.0, 0.0, 1.0],
    ]
    assert seen[0].payload["args"] == {}


def test_arm_invoke_non_raw_skill_keeps_args_unchanged():
    bus = MessageBus(name="t", source="t")
    seen = []
    bus.subscribe(P.TOPIC_ACTION_EXECUTE, lambda ev: seen.append(ev))
    toolkit = SimpleNamespace(agent_instance=SimpleNamespace(bus=bus, agent_id="t"))
    tools = ArmTools(toolkit)

    tools.arm_invoke("servo", '{"dx": 0.02, "grip": "close"}')

    assert len(seen) == 1
    assert seen[0].payload["args"] == {"dx": 0.02, "grip": "close"}
    assert seen[0].payload["actions"] == []


def test_completion_ready_tracks_pending_seq():
    bus = MessageBus(name="t", source="t")
    ctrl = ArmController(bus)
    seq = ctrl.execute(P.ArmActionRequest(seq_id="", episode=0, command="servo",
                                          args={"dx": 0.01}))
    assert ctrl.completion_ready() is False
    # A completion for a different step must not count.
    bus.publish(P.TOPIC_ACTION_COMPLETED, {"seq_id": "other", "ok": True})
    assert ctrl.completion_ready() is False
    # The matching completion does.
    bus.publish(P.TOPIC_ACTION_COMPLETED, {"seq_id": seq, "ok": True})
    assert ctrl.completion_ready() is True
    # Dispatching the next step clears the previous result.
    ctrl.execute(P.ArmActionRequest(seq_id="", episode=0, command="servo"))
    assert ctrl.completion_ready() is False


def _fake_agent(bus, handlers):
    agent = SimpleNamespace(bus=bus, agent_id="t", _bus_notify_listeners={}, woke=[])

    def _create(*a, **k):
        handlers.append((a, k))
        hid = "h%d" % len(handlers)
        agent._bus_notify_listeners[hid] = {"prompt": a[1] if len(a) > 1 else ""}
        return hid

    def _remove(hid):
        agent._bus_notify_listeners.pop(hid, None)
        return True

    def _run(ev, prompt, inherit):  # mirrors SSRAgent._run_bus_handler signature
        agent.woke.append((ev.topic, prompt))

    agent.create_bus_handler = _create
    agent.remove_bus_handler = _remove
    agent._run_bus_handler = _run
    return agent


def test_await_completion_does_not_suspend_when_step_already_done():
    # The completion can land before the handler registers (fast/no-op skills);
    # the bus has no replay, so suspending would hang. arm_await_completion must
    # detect the already-cached completion and keep the turn going instead.
    bus = MessageBus(name="t", source="t")
    handlers = []
    agent = _fake_agent(bus, handlers)
    tools = ArmTools(SimpleNamespace(agent_instance=agent))

    tools.arm_invoke("move_above", '{"x": 0.5, "y": 0.0}')
    ctrl = agent._arm_controller
    bus.publish(P.TOPIC_ACTION_COMPLETED,
                {"seq_id": ctrl._pending_seq, "command": "move_above", "ok": True})

    msg = tools.arm_await_completion()
    assert "Do NOT end your turn" in msg
    assert handlers == []  # no handler registered for an event that already fired


def test_await_completion_suspends_when_step_not_yet_done():
    bus = MessageBus(name="t", source="t")
    handlers = []
    agent = _fake_agent(bus, handlers)
    tools = ArmTools(SimpleNamespace(agent_instance=agent))

    tools.arm_invoke("move_above", '{"x": 0.5, "y": 0.0}')
    msg = tools.arm_await_completion(timeout_s=30)  # no completion yet → suspend path

    assert "END YOUR TURN" in msg
    assert len(handlers) == 1 and handlers[0][0][0] == P.PATTERN_COMPLETED


def test_watchdog_wakes_checker_when_no_completion_arrives():
    # The safety net: if the completion event is lost/missed, a timer still wakes a
    # checker turn so the session can't be stranded suspended forever.
    import time

    bus = MessageBus(name="t", source="t")
    handlers = []
    agent = _fake_agent(bus, handlers)
    tools = ArmTools(SimpleNamespace(agent_instance=agent))

    tools.arm_invoke("move_above", '{"x": 0.5, "y": 0.0}')
    msg = tools.arm_await_completion(timeout_s=1.0)  # 1.0s is the watchdog floor
    assert "watchdog" in msg.lower() and len(handlers) == 1

    time.sleep(1.4)  # let the watchdog fire (no completion was published)
    assert agent.woke and agent.woke[0][0] == "arm.watchdog.timeout"
    assert not agent._bus_notify_listeners  # watchdog dropped the stale handler


def test_watchdog_does_not_double_wake_when_completion_fired():
    import time

    bus = MessageBus(name="t", source="t")
    handlers = []
    agent = _fake_agent(bus, handlers)
    tools = ArmTools(SimpleNamespace(agent_instance=agent))

    tools.arm_invoke("move_above", '{"x": 0.5, "y": 0.0}')
    tools.arm_await_completion(timeout_s=1.0)
    # The real once-handler firing removes itself from the listener registry.
    agent._bus_notify_listeners.clear()

    time.sleep(1.4)
    assert agent.woke == []  # watchdog must stay quiet — the completion handled it


def test_toolkit_auto_approves_under_env_flag(tmp_path, monkeypatch):
    # `ssr arm` sets SSR_AUTO_APPROVE so every command runs without a prompt; the
    # interactive default must stay the prompting TUI handler.
    from ssr.agent.tools import ToolKit
    from ssr.approval import AutoApprovalHandler, TUIApprovalHandler
    from ssr.config import Settings

    settings = Settings(home=tmp_path / "home", project_dir=tmp_path / "proj")
    settings.ensure_dirs()

    monkeypatch.delenv("SSR_AUTO_APPROVE", raising=False)
    tk = ToolKit(settings, retriever=None, memory=None)
    assert isinstance(tk.approval_handler, TUIApprovalHandler)

    for val in ("1", "true", "on", "YES"):
        monkeypatch.setenv("SSR_AUTO_APPROVE", val)
        tk_auto = ToolKit(settings, retriever=None, memory=None)
        assert isinstance(tk_auto.approval_handler, AutoApprovalHandler), val


# ----------------------------------------------------- big brain: arm_grasp
def _tiny_frame_b64(w: int = 32, h: int = 24) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 40, 40)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_arm_grasp_crops_frame_and_publishes_request():
    bus = MessageBus(name="t", source="t")
    seen = []
    bus.subscribe(P.TOPIC_GRASP_REQUEST, lambda ev: seen.append(ev))
    toolkit = SimpleNamespace(agent_instance=SimpleNamespace(bus=bus, agent_id="t"))
    tools = ArmTools(toolkit)
    # The controller subscribes on first use; create it before the state event
    # so the camera frame gets cached (in real flows arm_describe does this).
    assert tools._controller() is not None
    bus.publish(P.TOPIC_STATE, {"objects": {}, "frame_b64": _tiny_frame_b64()})

    msg = tools.arm_grasp("apple", "[4, 4, 20, 16]")

    assert "delegated to the cerebellum" in msg
    assert len(seen) == 1
    payload = seen[0].payload
    assert payload["label"] == "apple"
    assert payload["bbox"] == [4.0, 4.0, 20.0, 16.0]
    assert (payload["frame_width"], payload["frame_height"]) == (32, 24)
    from PIL import Image

    crop = Image.open(io.BytesIO(base64.b64decode(payload["target_b64"])))
    assert crop.size == (16, 12)


def test_arm_grasp_rejects_bad_bbox():
    bus = MessageBus(name="t", source="t")
    toolkit = SimpleNamespace(agent_instance=SimpleNamespace(bus=bus, agent_id="t"))
    tools = ArmTools(toolkit)
    assert "ERROR" in tools.arm_grasp("apple", "not json")
    assert "ERROR" in tools.arm_grasp("apple", "[10, 10, 5, 20]")


def test_await_grasp_suspends_and_detects_early_result():
    bus = MessageBus(name="t", source="t")
    handlers = []
    agent = _fake_agent(bus, handlers)
    tools = ArmTools(SimpleNamespace(agent_instance=agent))
    assert tools._controller() is not None
    bus.publish(P.TOPIC_STATE, {"objects": {}, "frame_b64": _tiny_frame_b64()})
    tools.arm_grasp("apple", "[4, 4, 20, 16]")

    # No result yet → the suspend path registers a handler on the result topic.
    msg = tools.arm_await_grasp(timeout_s=30)
    assert "END YOUR TURN" in msg
    assert len(handlers) == 1 and handlers[0][0][0] == P.TOPIC_GRASP_RESULT

    # A fast failure result that lands before await must keep the turn going.
    ctrl = agent._arm_controller
    bus.publish(P.TOPIC_GRASP_RESULT,
                {"seq_id": ctrl._pending_grasp_seq, "ok": False, "outcome": "failed"})
    msg = tools.arm_await_grasp(timeout_s=30)
    assert "Do NOT end your turn" in msg
    assert len(handlers) == 1  # no second handler for a result that already fired
    assert "failed" in tools.arm_check_grasp()


# ------------------------------------------------- cerebellum: pure parsing
def test_iter_sse_events_parses_data_lines_until_done():
    lines = [
        b"",
        b'data: {"content":"{","event":"delta","frameNo":1,"taskId":"t-1"}',
        b'data: {"content":"\\"dx\\":0.02}","event":"delta","frameNo":1}',
        b"data: [DONE]",
        b'data: {"content":"never","frameNo":2}',
    ]
    events = list(C.iter_sse_events(iter(lines)))
    assert len(events) == 2
    assert events[0]["frameNo"] == 1 and C.find_task_id(events[0]) == "t-1"


def test_parse_frame_text_servo_command_clamped():
    cmd = C.parse_frame_text('{"dx": 0.2, "dy": -0.01, "dz": -0.9, "g": "c"}', 0.05)
    assert cmd == {"kind": "servo", "dx": 0.05, "dy": -0.01, "dz": -0.05,
                   "grip": "close"}


def test_parse_frame_text_accepts_long_grip_names_and_noise():
    cmd = C.parse_frame_text('指令：{"dx":0,"dy":0,"dz":-0.03,"grip":"open"} 完毕', 0.05)
    assert cmd["grip"] == "open" and cmd["dz"] == -0.03


def test_parse_frame_text_done_fail_and_garbage():
    assert C.parse_frame_text("DONE", 0.05) == {"kind": "done"}
    fail = C.parse_frame_text("FAIL:目标被遮挡", 0.05)
    assert fail["kind"] == "fail" and "遮挡" in fail["reason"]
    assert C.parse_frame_text("正在分析画面……", 0.05) is None
    # A no-op correction (all zero, hold) is not worth a servo round-trip.
    assert C.parse_frame_text('{"dx":0,"dy":0,"dz":0,"g":"h"}', 0.05) is None


def test_load_vlx_config_env_overrides(monkeypatch, tmp_path):
    (tmp_path / "vlx.json").write_text(json.dumps({
        "api_key": "file-key", "rtsp_pull_url": "rtsp://file/pull",
        "max_tokens": 32,
    }), encoding="utf-8")
    settings = SimpleNamespace(home=tmp_path)
    monkeypatch.setenv("OM_API_KEY", "env-key")
    monkeypatch.setenv("VLX_MAX_STEP_M", "0.03")
    monkeypatch.delenv("VLX_RTSP_PULL_URL", raising=False)
    cfg = C.load_vlx_config(settings)
    assert cfg.endpoint == "https://api.om-agent.cn"
    assert cfg.api_key == "env-key"          # env beats the file
    assert cfg.rtsp_pull_url == "rtsp://file/pull"
    assert cfg.max_tokens == 32
    assert cfg.max_step_m == 0.03
    assert cfg.model == "vlx-flow"


def test_handle_grasp_event_unconfigured_publishes_failed_result(tmp_path):
    # Without an API key the cerebellum must still call back (the brain's
    # arm_await_grasp checker would otherwise hang until its watchdog).
    bus = MessageBus(name="t", source="t")
    results = []
    bus.subscribe(P.TOPIC_GRASP_RESULT, lambda ev: results.append(ev.payload))
    req = P.ArmGraspRequest(seq_id="gseq1", label="apple", target_b64="cGln")
    ev = SimpleNamespace(topic=P.TOPIC_GRASP_REQUEST, payload=req.to_payload(),
                         source="t")
    import os
    old = {k: os.environ.pop(k, None) for k in
           ("OM_API_KEY", "VLX_API_KEY", "VLX_RTSP_PULL_URL", "VLX_ENDPOINT")}
    try:
        out = C.handle_grasp_event(ev, bus, SimpleNamespace(home=tmp_path))
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v
    assert out is not None and out["ok"] is False
    assert results and results[0]["seq_id"] == "gseq1"
    assert "api key" in results[0]["reason"].lower()
    # De-dup: the same seq must not be handled twice (bridged buses re-deliver).
    assert C.handle_grasp_event(ev, bus, SimpleNamespace(home=tmp_path)) is None
