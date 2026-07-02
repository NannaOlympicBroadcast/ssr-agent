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

The brain is **never handed object positions**. The robot advertises its skills and
camera, and the agent perceives objects by looking at the camera frame and naming a
target by image **pixel** (px, py), which the robot back-projects through the
camera. There is no scene-snapshot of object coordinates.

Topics
------
``arm.capabilities.request`` (brain → robot) : ask for the capability descriptor.
``arm.capabilities``         (robot → brain) : action space + skills + obstacles + camera.
``arm.action.execute``       (brain → robot) : invoke a skill (or raw actions).
``arm.grasp.completed``      (robot → brain) : a grasping skill finished settling.
``arm.action.completed``     (robot → brain) : any other skill finished.
``arm.task.success``         (brain → brain) : the agent judged the instruction done.
``arm.reset``                (brain → robot) : reset the episode.
``arm.camera.request`` / ``arm.camera`` : fetch a fresh camera frame (+ the arm's
    own proprioception: held? / gripper width). NO object positions.
``arm.record.start`` / ``arm.record.stop`` (brain → robot) : record any advertised
    camera; ``arm.record.started`` acks, ``arm.record.saved`` (robot → brain)
    returns the encoded video for the brain to save and review.

Capability descriptor (example)::

    {
      "action_space": {
        "type": "DifferentialInverseKinematicsAction+BinaryGripper",
        "dof": 8, "ee_body": "openarm_hand",
        "gripper": {"open": 1.0, "close": -1.0},
        "pose_format": "[px,py,pz,qw,qx,qy,qz] in robot root frame"
      },
      "skills": [
        {"name": "pick", "args": {"px": "float", "py": "float"}, "grasping": true,
         "desc": "grasp the object at image pixel (px, py) and lift it"},
        {"name": "place_at", "args": {"px": "float", "py": "float"}},
        {"name": "move_above", "args": {"px": "float", "py": "float"}},
        {"name": "raw", "args": {"actions": "list[8 floats]"}}
      ],
      "obstacles": [{"name": "stand", "aabb": [x0,y0,z0, x1,y1,z1]}],
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
# Fetch a fresh camera frame (+ proprioception). Replaces the old arm.state* scene
# snapshot, which exposed object positions — the agent now perceives objects only by
# looking at the returned frame, never from coordinates handed to it.
TOPIC_CAMERA_REQUEST = "arm.camera.request"
TOPIC_CAMERA = "arm.camera"
# Camera recording: start/stop buffering frames on the robot side from any of the
# advertised cameras; the robot replies with arm.record.started (ack/error) and, on
# stop, arm.record.saved carrying the encoded video (video_b64 + format/fps/frames)
# so the brain can save it and REVIEW the motion — reflecting on a recording of the
# whole step is far more informative than a single after-the-fact frame.
TOPIC_RECORD_START = "arm.record.start"
TOPIC_RECORD_STOP = "arm.record.stop"
TOPIC_RECORD_STARTED = "arm.record.started"
TOPIC_RECORD_SAVED = "arm.record.saved"

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
