"""Bus event protocol for capability-driven arm control.

The brain (SSR agent) and the robot (Isaac Lab env runner) communicate *only*
through :class:`~ssr.bus.events.BusEvent` objects, so the two sides stay
decoupled and either can run in a different process / machine.

Crucially, **action types are not hardcoded in the brain**. The robot *advertises*
its capabilities — the low-level action space *and* the set of higher-level
skills it actually implements — on ``arm.capabilities``. The agent reads them and
plans using whatever the robot says it supports, invoking any advertised skill
generically (or sending raw action vectors). Adding/removing a skill on the robot
side is automatically reflected to the agent; no brain change is needed.

Topics
------
``arm.capabilities.request`` (brain → robot) : ask for the capability descriptor.
``arm.capabilities``         (robot → brain) : action space + skills + objects + camera.
``arm.action.execute``       (brain → robot) : invoke a skill (or raw actions).
``arm.grasp.completed``      (robot → brain) : a grasping skill finished settling.
``arm.action.completed``     (robot → brain) : any other skill finished.
``arm.task.success``         (brain → brain) : the agent judged the instruction done.
``arm.reset`` / ``arm.state.request`` / ``arm.state`` : episode + scene snapshot.

Capability descriptor (example)::

    {
      "action_space": {
        "type": "JointPositionAction+BinaryGripper",
        "dof": 8, "joint_names": ["openarm_joint1", ..., "gripper"],
        "scale": 0.5, "use_default_offset": true,
        "gripper": {"open": 1.0, "close": -1.0},
        "low": [...], "high": [...]
      },
      "skills": [
        {"name": "pick", "args": {"object": "str"}, "grasping": true,
         "desc": "grasp a named object and lift it"},
        {"name": "place_on", "args": {"object": "str"}, "desc": "..."},
        {"name": "place_at", "args": {"x": "float", "y": "float"}},
        {"name": "move_above", "args": {"object": "str?", "x": "float?", "y": "float?"}},
        {"name": "raw", "args": {"actions": "list[8 floats]"}}
      ],
      "objects": {"apple": [x,y,z], "orange": [x,y,z]},
      "camera": {"width": 320, "height": 240}
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- topic names
TOPIC_CAPS_REQUEST = "arm.capabilities.request"
TOPIC_CAPS = "arm.capabilities"
TOPIC_ACTION_EXECUTE = "arm.action.execute"
TOPIC_GRASP_COMPLETED = "arm.grasp.completed"
TOPIC_ACTION_COMPLETED = "arm.action.completed"
TOPIC_TASK_SUCCESS = "arm.task.success"
TOPIC_RESET = "arm.reset"
TOPIC_STATE_REQUEST = "arm.state.request"
TOPIC_STATE = "arm.state"

# Topic pattern matching ANY skill completion.
PATTERN_COMPLETED = "arm.*.completed"

# Conventional gripper values for the raw action vector (only meaningful when the
# robot's advertised action_space includes a binary gripper).
GRIPPER_OPEN = 1.0
GRIPPER_CLOSE = -1.0


@dataclass
class ArmActionRequest:
    """A generic request to invoke a robot-advertised skill (or raw actions).

    ``command`` is a skill name from the robot's advertised capabilities, or
    ``"raw"`` to replay low-level action vectors. ``args`` carries that skill's
    arguments verbatim; the robot validates them. The brain never hardcodes the
    skill set — it forwards whatever skill the agent chose.
    """

    seq_id: str
    episode: int
    command: str
    args: dict[str, Any] = field(default_factory=dict)
    actions: list[list[float]] = field(default_factory=list)  # for command == "raw"
    label: str = ""

    def to_payload(self) -> dict:
        return {
            "seq_id": self.seq_id, "episode": self.episode, "command": self.command,
            "args": self.args, "actions": self.actions, "label": self.label,
        }

    @classmethod
    def from_payload(cls, p: dict) -> "ArmActionRequest":
        return cls(
            seq_id=str(p.get("seq_id") or ""),
            episode=int(p.get("episode") or 0),
            command=str(p.get("command") or ""),
            args=dict(p.get("args") or {}),
            actions=list(p.get("actions") or []),
            label=str(p.get("label") or ""),
        )
