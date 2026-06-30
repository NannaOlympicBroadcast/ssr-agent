"""Capability-driven agent tools for the OpenArm robot.

The action types are **not** baked into these tools. The robot advertises its
capabilities (low-level action space + the skills it implements); the agent reads
them with ``arm_describe`` and then invokes *any* advertised skill generically
with ``arm_invoke`` (or sends raw action vectors with ``arm_act``). The brain
adapts to whatever the robot supports — adding a skill on the robot needs no
brain change.

The agent is **never handed object positions**: it perceives objects by looking at
the live camera frame (``arm_get_camera``) and names a target by the image pixel it
sees there — the robot back-projects that pixel through the camera. There is no
``get_scene`` that returns coordinates.

Typical flow for an arbitrary instruction:
``arm_describe`` (learn skills) → ``arm_get_camera`` (look) → read the apple's pixel
off the image → ``arm_invoke('pick', {px:.., py:..})`` → ``arm_await_completion``
then END THE TURN → [woken] ``arm_check_result`` (+ ``arm_get_camera``) → place it
→ … → ``arm_report_done``.
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
        arm_invoke (with their argument schemas), any "obstacles" (collision
        bodies — table/support AABBs in the robot root frame — to route the arm
        around) and camera info (width/height for reading pixels). It does NOT
        list object positions: perceive objects by looking at the camera frame
        (arm_get_camera) and target them by image pixel. Plan only with skills
        listed here; do not assume any.
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

    def arm_invoke(self, skill: str, args_json: str = "") -> str:
        """Invoke one robot-advertised skill (non-blocking).

        Use a skill name and argument schema exactly as returned by arm_describe.
        Targets are named by the image **pixel** you read off the camera frame
        (arm_get_camera), e.g. skill='pick', args_json='{"px":171,"py":96}' — the
        robot back-projects the pixel through the camera. After this, call
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

    def arm_await_completion(self, handler_prompt: str = "", timeout_s: float = 90.0) -> str:
        """Register a one-shot handler that wakes a new turn when the step finishes.

        Core of the asynchronous paradigm: after invoking a skill/action you call
        this and then END YOUR TURN (suspending the session to free resources).
        When the robot publishes its completion event, the bus fires a fresh agent
        turn running ``handler_prompt`` — there you call arm_check_result to judge
        the step and decide the next action.

        Args:
            handler_prompt: instructions for the woken checker turn (optional).
            timeout_s: watchdog timeout. If no completion event arrives within this
                many seconds, a timer wakes a checker turn anyway, so a missed or
                lost completion event can't strand the session forever.
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
        # Watchdog: if the completion event never arrives (lost/missed wake, or the
        # robot stalled), wake a checker turn anyway after `timeout_s` so the session
        # can never be stranded suspended forever.
        self._start_watchdog(agent, hid, prompt, timeout_s)
        return (f"Registered completion handler {hid} (watchdog {timeout_s:.0f}s). "
                "END YOUR TURN now to suspend the session; the robot's completion "
                "event — or the watchdog — will wake the checker.")

    def _start_watchdog(self, agent, hid: str, prompt: str, timeout_s: float) -> None:
        """Wake a checker turn if the completion handler ``hid`` hasn't fired within
        ``timeout_s``. A one-shot bus handler removes itself from
        ``agent._bus_notify_listeners`` when it fires, so its continued presence
        there means the completion never arrived and we must wake the agent."""
        import threading
        import time as _time

        from ..bus.events import BusEvent

        delay = max(1.0, float(timeout_s))

        def _watch() -> None:
            _time.sleep(delay)
            listeners = getattr(agent, "_bus_notify_listeners", {})
            if hid not in listeners:
                return  # the real completion already fired the handler — nothing to do
            agent.remove_bus_handler(hid)
            wd_prompt = (
                f"{prompt}\n\n[WATCHDOG] No completion event arrived within "
                f"{delay:.0f}s. The robot may have finished without notifying, or the "
                "step may have stalled. Call arm_check_result and arm_get_camera to see "
                "the current state: if the step actually completed, continue with the "
                "next step; if it stalled, arm_reset and retry; if you cannot make "
                "progress, call arm_report_done with what is stuck."
            )
            ev = BusEvent(topic="arm.watchdog.timeout", payload={"handler": hid},
                          source="arm-watchdog")
            try:
                agent._run_bus_handler(ev, wd_prompt, True)
            except Exception:
                pass

        threading.Thread(target=_watch, daemon=True, name="arm-watchdog").start()

    def arm_check_result(self) -> str:
        """Return the latest step result to judge the step.

        Reports the command, ok/error and the arm's own proprioception (held? /
        gripper width) — NOT object positions. To see where things are, look at the
        camera frame with arm_get_camera.
        """
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
            "grasp": snap.get("grasp"),
            "seq_id": snap.get("seq_id"),
            "episode": snap.get("episode"),
        }
        return json.dumps(view, ensure_ascii=False)

    def arm_get_camera(self, path: str = "") -> str:
        """Look through the robot's camera: fetch a FRESH frame, save it to a PNG and
        return its path.

        This is how you perceive the scene — read object locations straight off this
        image (you are never handed object coordinates). Identify your target in the
        picture and pass its pixel (px, py) to pick/place_at/move_above.

        Args:
            path: optional output path; defaults to the project dir.
        """
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        # Actively request a fresh frame (the old arm_get_scene did this); the camera
        # event carries only the frame + proprioception, never object positions.
        ctrl.request_camera()
        import time
        time.sleep(0.4)
        snap = ctrl.latest()
        b64 = (snap or {}).get("frame_b64")
        if not b64:
            return "(no camera frame available — is the Isaac bridge connected?)"
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
            self.arm_invoke,
            self.arm_act,
            self.arm_await_completion,
            self.arm_check_result,
            self.arm_get_camera,
            self.arm_report_done,
        ]
