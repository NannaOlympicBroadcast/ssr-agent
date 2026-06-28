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
    bus = MessageBus(name="t", source="t")
    ctrl = ArmController(bus)
    bus.publish(P.TOPIC_GRASP_COMPLETED, {
        "seq_id": "s1", "episode": 1, "command": "pick", "ok": True,
        "holding": "apple", "objects": {"apple": [0.4, 0.0, 0.3]},
    })
    snap = ctrl.last_completion()
    assert snap and snap["holding"] == "apple"
    assert ctrl.latest()["objects"]["apple"] == [0.4, 0.0, 0.3]


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
    return SimpleNamespace(
        bus=bus, agent_id="t",
        create_bus_handler=lambda *a, **k: handlers.append((a, k)) or "h1",
        remove_bus_handler=lambda hid: handlers.append(("removed", hid)) or True,
    )


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
    msg = tools.arm_await_completion()  # no completion yet → normal suspend path

    assert "END YOUR TURN" in msg
    assert len(handlers) == 1 and handlers[0][0][0] == P.PATTERN_COMPLETED
