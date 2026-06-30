"""Unit tests for the brain-side robotics components.

These cover the generic (capability-driven) bus protocol and the
:class:`ArmController` caching of capabilities / completions / state using the
real in-process :class:`~ssr.bus.core.MessageBus` (no simulator). End-to-end
manipulation is an integration test against the Isaac Lab bridge on the robot
machine — see ssr/robotics/README.md.
"""

from __future__ import annotations

from types import SimpleNamespace

from ssr.bus.core import MessageBus
from ssr.robotics import protocol as P
from ssr.robotics.controller import ArmController
from ssr.robotics.tools import ArmTools


def test_action_request_payload_roundtrip():
    req = P.ArmActionRequest(seq_id="", episode=0, command="pick",
                             args={"object": "apple"}, label="pick apple")
    req.seq_id, req.episode = "abc123", 3
    back = P.ArmActionRequest.from_payload(req.to_payload())
    assert back.command == "pick" and back.args == {"object": "apple"}
    assert back.seq_id == "abc123" and back.episode == 3


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
    seq = ctrl.execute(P.ArmActionRequest(seq_id="", episode=0, command="pick",
                                          args={"object": "apple"}))
    assert len(seen) == 1
    assert seen[0].payload["command"] == "pick"
    assert seen[0].payload["args"] == {"object": "apple"}
    assert seen[0].payload["seq_id"] == seq


def test_controller_caches_capabilities():
    bus = MessageBus(name="t", source="t")
    ctrl = ArmController(bus)
    caps = {"action_space": {"dof": 8}, "skills": [{"name": "pick"}]}
    bus.publish(P.TOPIC_CAPS, caps)
    got = ctrl.capabilities()
    assert got and got["skills"][0]["name"] == "pick"


def test_controller_caches_completion_events():
    # Completion payloads carry proprioception + a camera frame, never object
    # positions; the controller caches the latest one for the tools to read.
    bus = MessageBus(name="t", source="t")
    ctrl = ArmController(bus)
    bus.publish(P.TOPIC_GRASP_COMPLETED, {
        "seq_id": "s1", "episode": 1, "command": "pick", "ok": True,
        "holding": True, "grasp": {"gripper_width": 0.03}, "frame_b64": "AAAA",
    })
    snap = ctrl.last_completion()
    assert snap and snap["holding"] is True
    assert ctrl.latest()["frame_b64"] == "AAAA"


def test_controller_caches_camera_snapshot():
    bus = MessageBus(name="t", source="t")
    ctrl = ArmController(bus)
    bus.publish(P.TOPIC_CAMERA, {"holding": False, "frame_b64": "ZZZZ"})
    assert ctrl.last_camera()["frame_b64"] == "ZZZZ"
    assert ctrl.latest()["frame_b64"] == "ZZZZ"


def test_request_camera_publishes_request():
    bus = MessageBus(name="t", source="t")
    seen = []
    bus.subscribe(P.TOPIC_CAMERA_REQUEST, lambda ev: seen.append(ev))
    ArmController(bus).request_camera()
    assert len(seen) == 1


def test_no_get_scene_tool_and_check_result_hides_object_positions():
    # Object positions must not be exposed to the agent: there is no arm_get_scene,
    # and arm_check_result reports proprioception only — no "objects" coordinates.
    tools = ArmTools(SimpleNamespace(agent_instance=None))
    names = {fn.__name__ for fn in tools.callables()}
    assert "arm_get_scene" not in names
    assert not hasattr(tools, "arm_get_scene")
    assert "arm_get_camera" in names

    bus = MessageBus(name="t", source="t")
    agent = SimpleNamespace(bus=bus, agent_id="t")
    tools = ArmTools(SimpleNamespace(agent_instance=agent))
    tools._controller()  # instantiate so it subscribes before the event is published
    bus.publish(P.TOPIC_GRASP_COMPLETED, {
        "seq_id": "s1", "command": "pick", "ok": True, "holding": True,
        "grasp": {"gripper_width": 0.03}, "objects": {"apple": [0.4, 0.0, 0.3]},
    })
    view = tools.arm_check_result()
    assert "objects" not in view and "apple" not in view
    assert '"holding": true' in view


def test_arm_invoke_routes_pixel_target_args():
    # The agent points at a target by the image pixel it read off the camera frame;
    # those args ride through to the env verbatim (in .args, not .actions).
    bus = MessageBus(name="t", source="t")
    seen = []
    bus.subscribe(P.TOPIC_ACTION_EXECUTE, lambda ev: seen.append(ev))
    tools = ArmTools(SimpleNamespace(agent_instance=SimpleNamespace(bus=bus, agent_id="t")))

    tools.arm_invoke("pick", '{"px": 171, "py": 96}')

    assert len(seen) == 1
    assert seen[0].payload["args"] == {"px": 171, "py": 96}
    assert seen[0].payload["actions"] == []


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

    tools.arm_invoke("pick", '{"object": "apple"}')

    assert len(seen) == 1
    assert seen[0].payload["args"] == {"object": "apple"}
    assert seen[0].payload["actions"] == []


def test_completion_ready_tracks_pending_seq():
    bus = MessageBus(name="t", source="t")
    ctrl = ArmController(bus)
    seq = ctrl.execute(P.ArmActionRequest(seq_id="", episode=0, command="pick",
                                          args={"object": "apple"}))
    assert ctrl.completion_ready() is False
    # A completion for a different step must not count.
    bus.publish(P.TOPIC_ACTION_COMPLETED, {"seq_id": "other", "ok": True})
    assert ctrl.completion_ready() is False
    # The matching completion does.
    bus.publish(P.TOPIC_ACTION_COMPLETED, {"seq_id": seq, "ok": True})
    assert ctrl.completion_ready() is True
    # Dispatching the next step clears the previous result.
    ctrl.execute(P.ArmActionRequest(seq_id="", episode=0, command="pick"))
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

    tools.arm_invoke("pick", '{"object": "apple"}')
    ctrl = agent._arm_controller
    bus.publish(P.TOPIC_ACTION_COMPLETED,
                {"seq_id": ctrl._pending_seq, "command": "pick", "ok": True})

    msg = tools.arm_await_completion()
    assert "Do NOT end your turn" in msg
    assert handlers == []  # no handler registered for an event that already fired


def test_await_completion_suspends_when_step_not_yet_done():
    bus = MessageBus(name="t", source="t")
    handlers = []
    agent = _fake_agent(bus, handlers)
    tools = ArmTools(SimpleNamespace(agent_instance=agent))

    tools.arm_invoke("pick", '{"object": "apple"}')
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

    tools.arm_invoke("pick", '{"object": "apple"}')
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

    tools.arm_invoke("pick", '{"object": "apple"}')
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
