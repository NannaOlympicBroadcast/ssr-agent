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

Grasping follows a **big-brain / cerebellum (大脑/小脑)** split. The SSR agent is
the big brain: it only *perceives and delegates* — it crops the target object out
of the camera frame and publishes a grasp request on the bus. The **cerebellum**
(:mod:`ssr.robotics.cerebellum`, driven by the Om-Agent **VLX-Flow** streaming
vision model) owns the realtime grasp loop: it has the robot push the camera as an
RTSP stream, feeds that stream + the target crop to VLX-Flow, translates each
streamed reply into a low-level ``servo`` correction on the arm, and finally
notifies the brain with a grasp result callback.

Topics
------
``arm.capabilities.request`` (brain → robot) : ask for the capability descriptor.
``arm.capabilities``         (robot → brain) : action space + skills + objects + camera.
``arm.action.execute``       (brain/cerebellum → robot) : invoke a skill (or raw actions).
``arm.action.completed``     (robot → brain/cerebellum) : a skill finished settling.
``arm.grasp.request``        (brain → cerebellum) : grasp this cropped target object.
``arm.grasp.result``         (cerebellum → brain) : grasp finished (ok / failed) — callback.
``arm.stream.start``         (cerebellum → robot) : start pushing the camera RTSP stream.
``arm.stream.started``       (robot → cerebellum) : the camera stream is up (or errored).
``arm.stream.stop``          (cerebellum → robot) : stop the camera stream.
``arm.stream.stopped``       (robot → cerebellum) : the camera stream was stopped.
``arm.task.success``         (brain → brain) : the agent judged the instruction done.
``arm.reset`` / ``arm.state.request`` / ``arm.state`` : episode + scene snapshot.

Capability descriptor (example)::

    {
      "action_space": {
        "type": "DifferentialInverseKinematicsAction + BinaryJointPositionAction",
        "dof": 8, "ee_body": "openarm_hand",
        "gripper": {"open": 1.0, "close": -1.0},
        "pose_format": "[px,py,pz,qw,qx,qy,qz] in robot root frame"
      },
      "skills": [
        {"name": "servo", "args": {"dx": "float", "dy": "float", "dz": "float",
                                   "grip": "open|close|hold"},
         "desc": "small realtime end-effector correction (cerebellum grasp loop)"},
        {"name": "place_at", "args": {"x": "float", "y": "float"}},
        {"name": "move_above", "args": {"x": "float", "y": "float"}},
        {"name": "raw", "args": {"actions": "list[8 floats]"}}
      ],
      "objects": {"obj0": [x,y,z], "obj1": [x,y,z]},
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
TOPIC_ACTION_COMPLETED = "arm.action.completed"
TOPIC_GRASP_REQUEST = "arm.grasp.request"
TOPIC_GRASP_RESULT = "arm.grasp.result"
TOPIC_STREAM_START = "arm.stream.start"
TOPIC_STREAM_STARTED = "arm.stream.started"
TOPIC_STREAM_STOP = "arm.stream.stop"
TOPIC_STREAM_STOPPED = "arm.stream.stopped"
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


@dataclass
class ArmGraspRequest:
    """A grasp delegation from the big brain to the cerebellum.

    The brain crops the target object out of the camera frame (``target_b64``,
    PNG) and hands the whole grasp over. ``bbox`` is the crop's pixel box
    ``[x0, y0, x1, y1]`` in the full frame (``frame_width`` x ``frame_height``),
    kept so the cerebellum can tell VLX-Flow where the target started out.
    ``instruction`` carries any extra natural-language context for the grasp.
    """

    seq_id: str
    label: str
    target_b64: str
    bbox: list[float] = field(default_factory=list)
    frame_width: int = 0
    frame_height: int = 0
    instruction: str = ""

    def to_payload(self) -> dict:
        return {
            "seq_id": self.seq_id, "label": self.label, "target_b64": self.target_b64,
            "bbox": self.bbox, "frame_width": self.frame_width,
            "frame_height": self.frame_height, "instruction": self.instruction,
        }

    @classmethod
    def from_payload(cls, p: dict) -> "ArmGraspRequest":
        return cls(
            seq_id=str(p.get("seq_id") or ""),
            label=str(p.get("label") or ""),
            target_b64=str(p.get("target_b64") or ""),
            bbox=list(p.get("bbox") or []),
            frame_width=int(p.get("frame_width") or 0),
            frame_height=int(p.get("frame_height") or 0),
            instruction=str(p.get("instruction") or ""),
        )
