"""Unit tests for the brain-side robotics components.

These cover the generic (capability-driven) bus protocol and the
:class:`ArmController` caching of capabilities / completions / state using the
real in-process :class:`~ssr.bus.core.MessageBus` (no simulator). End-to-end
manipulation is an integration test against the Isaac Lab bridge on the robot
machine — see ssr/robotics/README.md.
"""

from __future__ import annotations

from ssr.bus.core import MessageBus
from ssr.robotics import protocol as P
from ssr.robotics.controller import ArmController


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
