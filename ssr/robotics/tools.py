"""Capability-driven agent tools for the OpenArm robot.

The action types are **not** baked into these tools. The robot advertises its
capabilities (low-level action space + the skills it implements); the agent reads
them with ``arm_describe`` and then invokes *any* advertised skill generically
with ``arm_invoke`` (or sends raw action vectors with ``arm_act``). The brain
adapts to whatever the robot supports — adding a skill on the robot needs no
brain change.

Typical flow for an arbitrary instruction:
``arm_describe`` (learn skills + objects) → ``arm_invoke('pick', {object:'apple'})``
→ ``arm_await_completion`` then END THE TURN → [woken] ``arm_check_result`` →
``arm_invoke('place_on', {object:'orange'})`` → … → ``arm_report_done``.
"""

from __future__ import annotations

import base64
import json

from . import protocol as P
from .controller import ArmController


class ArmTools:
    """Capability-driven arm-control tools bound to a :class:`ToolKit`."""

    def __init__(self, toolkit):
        self.toolkit = toolkit

    # --------------------------------------------------------- internal glue
    def _controller(self) -> ArmController | None:
        agent = getattr(self.toolkit, "agent_instance", None)
        bus = getattr(agent, "bus", None) if agent is not None else None
        if agent is None or bus is None:
            return None
        ctrl = getattr(agent, "_arm_controller", None)
        if ctrl is None:
            ctrl = ArmController(bus, source=getattr(agent, "agent_id", "ssr-brain"))
            agent._arm_controller = ctrl
        return ctrl

    # ------------------------------------------------------------- the tools
    def arm_describe(self) -> str:
        """Discover what the arm can do: action space + supported skills + objects.

        ALWAYS call this first. It returns the robot's capability descriptor —
        the low-level action space and the list of skills you may pass to
        arm_invoke (with their argument schemas), plus the current objects and
        camera info. Plan only with skills listed here; do not assume any.
        """
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        caps = ctrl.request_capabilities(wait=2.0)
        if not caps:
            return ("(no capabilities received — is the Isaac bridge connected to "
                    "this bus?)")
        return json.dumps(caps, ensure_ascii=False)

    def arm_reset(self) -> str:
        """Reset the robot episode (objects return to their start poses)."""
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        return f"Arm reset ({ctrl.reset()})."

    def arm_get_scene(self) -> str:
        """Perceive the current scene: object poses, what's held, camera frame."""
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        ctrl.request_state()
        import time
        time.sleep(0.4)
        snap = ctrl.latest()
        if not snap:
            return "(no scene snapshot yet — is the Isaac bridge connected?)"
        view = {
            "objects": snap.get("objects"),
            "holding": snap.get("holding"),
            "gripper_width": (snap.get("grasp") or {}).get("gripper_width"),
            "has_camera_frame": bool(snap.get("frame_b64")),
        }
        out = json.dumps(view, ensure_ascii=False)
        if snap.get("frame_b64"):
            out += "\n" + self.arm_get_camera()
        return out

    def arm_invoke(self, skill: str, args_json: str = "") -> str:
        """Invoke one robot-advertised skill (non-blocking).

        Use a skill name and argument schema exactly as returned by arm_describe
        (e.g. skill='pick', args_json='{"object":"apple"}'). After this, call
        arm_await_completion and END YOUR TURN to suspend the session.

        Args:
            skill: the skill name from arm_describe's "skills" list.
            args_json: JSON object of that skill's arguments.
        """
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        try:
            args = json.loads(args_json) if args_json.strip() else {}
            if not isinstance(args, dict):
                return "ERROR: args_json must be a JSON object"
        except json.JSONDecodeError as e:
            return f"ERROR: args_json is not valid JSON: {e}"
        # Soft check against advertised skills (the robot is the source of truth).
        caps = ctrl.capabilities()
        if caps:
            names = [s.get("name") for s in caps.get("skills", [])]
            if names and skill not in names:
                return (f"ERROR: '{skill}' is not advertised. Available skills: "
                        f"{', '.join(n for n in names if n)}")
        # The "raw" skill's waypoints are advertised under args["actions"] (see
        # capabilities()'s skills list) but the env reads them off the request's
        # dedicated `actions` field, not `args` — route them there or the env sees
        # an empty action list and silently no-ops without ever moving the arm.
        actions = args.pop("actions", []) if skill == "raw" else []
        req = P.ArmActionRequest(seq_id="", episode=0, command=skill, args=args,
                                 actions=actions, label=f"{skill} {args}")
        seq = ctrl.execute(req)
        return (f"Invoked '{skill}' seq_id={seq} (episode {ctrl.episode}). "
                "Now call arm_await_completion and END YOUR TURN to suspend.")

    def arm_act(self, actions_json: str, label: str = "") -> str:
        """Send raw low-level action vectors, per the advertised action_space.

        Args:
            actions_json: JSON array of action vectors; each vector matches the
                advertised action_space dof (e.g. [j1..j7, gripper], gripper
                +1 open / -1 close).
            label: optional human-readable label.
        """
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        try:
            actions = json.loads(actions_json)
            assert isinstance(actions, list)
        except Exception as e:
            return f"ERROR: actions_json must be a JSON array of vectors: {e}"
        req = P.ArmActionRequest(seq_id="", episode=0, command="raw",
                                 actions=actions, label=label or "raw")
        seq = ctrl.execute(req)
        return (f"Sent raw action sequence seq_id={seq} ({len(actions)} steps). "
                "Now call arm_await_completion and END YOUR TURN to suspend.")

    def arm_await_completion(self, handler_prompt: str = "") -> str:
        """Register a one-shot handler that wakes a new turn when the step finishes.

        Core of the asynchronous paradigm: after invoking a skill/action you call
        this and then END YOUR TURN (suspending the session to free resources).
        When the robot publishes its completion event, the bus fires a fresh agent
        turn running ``handler_prompt`` — there you call arm_check_result to judge
        the step and decide the next action.

        Args:
            handler_prompt: instructions for the woken checker turn (optional).
        """
        agent = getattr(self.toolkit, "agent_instance", None)
        if agent is None or not hasattr(agent, "create_bus_handler"):
            return "ERROR: arm bus not available in this context"
        # Race guard: a fast (or no-op) step can publish its completion before this
        # handler is registered. The bus has no replay, so that wake would be lost
        # and the session would hang until the outer timeout. The controller caches
        # every completion, so if the step is already done, don't suspend — tell the
        # agent to judge it right now in this same turn.
        ctrl = self._controller()
        if ctrl is not None and ctrl.completion_ready():
            return ("The robot step already completed before the session could "
                    "suspend. Do NOT end your turn — call arm_check_result now to "
                    "judge it, then invoke the next step (and arm_await_completion "
                    "again) or arm_report_done if the instruction is finished.")
        prompt = handler_prompt or (
            "A robot step just completed. Call arm_check_result (and arm_get_camera "
            "if useful) to judge it against the user's instruction. If it failed, "
            "fix and re-invoke that step then arm_await_completion again. If it "
            "succeeded but the instruction isn't finished, invoke the next step and "
            "arm_await_completion again. If the whole instruction is done, call "
            "arm_report_done."
        )
        hid = agent.create_bus_handler(
            P.PATTERN_COMPLETED, prompt, once=True, inherit_session=True,
            description="arm step checker",
        )
        # Re-check after registering: if the completion landed in the tiny window
        # between the guard above and the subscribe, the handler missed it too —
        # drop it and have the agent check now rather than wait forever.
        if ctrl is not None and ctrl.completion_ready():
            agent.remove_bus_handler(hid)
            return ("The robot step already completed before the session could "
                    "suspend. Do NOT end your turn — call arm_check_result now to "
                    "judge it, then invoke the next step (and arm_await_completion "
                    "again) or arm_report_done if the instruction is finished.")
        return (f"Registered completion handler {hid}. END YOUR TURN now to suspend "
                "the session; the robot's completion event will wake the checker.")

    def arm_check_result(self) -> str:
        """Return the latest step result + scene snapshot to judge the step."""
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        snap = ctrl.last_completion()
        if not snap:
            return "(no completion event yet — the robot has not reported back)"
        view = {
            "command": snap.get("command"),
            "ok": snap.get("ok"),
            "error": snap.get("error"),
            "holding": snap.get("holding"),
            "objects": snap.get("objects"),
            "grasp": snap.get("grasp"),
            "seq_id": snap.get("seq_id"),
            "episode": snap.get("episode"),
        }
        return json.dumps(view, ensure_ascii=False)

    def arm_get_camera(self, path: str = "") -> str:
        """Save the latest camera RGB frame (if any) to a PNG and return its path.

        Args:
            path: optional output path; defaults to the project dir.
        """
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        snap = ctrl.latest()
        b64 = (snap or {}).get("frame_b64")
        if not b64:
            return "(no camera frame available in the latest snapshot)"
        out = self.toolkit._resolve(path or "arm_frame.png")
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(b64))
        except Exception as e:
            return f"ERROR: could not write frame: {e}"
        meta = snap.get("camera") or {}
        return f"Saved camera frame to {out} ({meta.get('width', '?')}x{meta.get('height', '?')})."

    def arm_report_done(self, summary: str = "") -> str:
        """Signal the user's instruction is complete (publishes arm.task.success).

        Args:
            summary: short description of what was accomplished.
        """
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        ctrl.bus.publish(P.TOPIC_TASK_SUCCESS, {"summary": summary},
                         source=getattr(ctrl, "source", "ssr-brain"))
        return f"Task reported complete: {summary or '(done)'}"

    # ------------------------------------------------------------- collection
    def callables(self) -> list:
        return [
            self.arm_describe,
            self.arm_reset,
            self.arm_get_scene,
            self.arm_invoke,
            self.arm_act,
            self.arm_await_completion,
            self.arm_check_result,
            self.arm_get_camera,
            self.arm_report_done,
        ]
