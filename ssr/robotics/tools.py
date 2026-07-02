"""Capability-driven agent tools for the OpenArm robot.

The action types are **not** baked into these tools. The robot advertises its
capabilities (low-level action space + the skills it implements); the agent reads
them with ``arm_describe`` and then invokes *any* advertised skill generically
with ``arm_invoke`` (or sends raw action vectors with ``arm_act``).

**Grasping is delegated, not planned here.** The agent is the big brain (大脑):
it looks at the camera frame, crops the target object out of it (``arm_grasp``
with a pixel bbox) and hands the grasp to the **cerebellum** (小脑,
:mod:`ssr.robotics.cerebellum`) over the bus. The cerebellum closes the realtime
loop with the Om-Agent VLX-Flow streaming vision model (camera RTSP stream in,
servo corrections out) and calls back with ``arm.grasp.result``.

Typical flow for an arbitrary instruction:
``arm_describe`` (learn skills + objects) → ``arm_get_camera`` (look at the
frame) → ``arm_grasp('apple', '[x0,y0,x1,y1]')`` → ``arm_await_grasp`` then END
THE TURN → [woken] ``arm_check_grasp`` → ``arm_invoke('place_at', {...})`` →
``arm_await_completion`` → … → ``arm_report_done``.
"""

from __future__ import annotations

import base64
import io
import json
import time

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
        arm_invoke (with their argument schemas), plus the current objects,
        any "obstacles" (collision bodies — table/support AABBs in the robot
        root frame — to route the arm around) and camera info. Plan only with
        skills listed here; do not assume any.
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

    def arm_grasp(self, label: str, bbox_json: str, instruction: str = "") -> str:
        """Delegate grasping ONE object to the cerebellum (VLX-Flow realtime loop).

        This is how grasping works now — do NOT try to plan a grasp yourself.
        As the big brain your only vision job is: look at the latest camera
        frame (arm_get_camera) and give the pixel bounding box of the target.
        This tool crops that box out of the frame and publishes it on the bus
        (arm.grasp.request); the cerebellum streams the live camera to the
        VLX-Flow model and servo-drives the arm in realtime until the grasp
        succeeds or fails, then calls back on arm.grasp.result. After this,
        call arm_await_grasp and END YOUR TURN to suspend.

        Args:
            label: short name of the target object (e.g. 'apple' / '苹果').
            bbox_json: JSON array [x0, y0, x1, y1] — the target's pixel box in
                the latest camera frame (origin top-left, x right, y down).
            instruction: optional extra natural-language context for the grasp.
        """
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        try:
            box = json.loads(bbox_json)
            assert isinstance(box, list) and len(box) == 4
            x0, y0, x1, y1 = (float(v) for v in box)
        except Exception:
            return "ERROR: bbox_json must be a JSON array [x0, y0, x1, y1]"
        if x1 <= x0 or y1 <= y0:
            return "ERROR: bbox must satisfy x0 < x1 and y0 < y1"
        snap = ctrl.latest()
        if not (snap or {}).get("frame_b64"):
            ctrl.request_state()
            time.sleep(0.6)
            snap = ctrl.latest()
        b64 = (snap or {}).get("frame_b64")
        if not b64:
            return ("ERROR: no camera frame available — is the Isaac bridge "
                    "connected? Try arm_get_scene first.")
        try:
            from PIL import Image
        except ImportError:
            return "ERROR: pillow is required for cropping (pip install pillow)"
        try:
            img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            w, h = img.size
            crop_box = (max(0, int(x0)), max(0, int(y0)),
                        min(w, int(round(x1))), min(h, int(round(y1))))
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                return f"ERROR: bbox {box} lies outside the {w}x{h} camera frame"
            crop = img.crop(crop_box)
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            crop_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            return f"ERROR: could not crop the camera frame: {e}"
        req = P.ArmGraspRequest(
            seq_id="", label=label, target_b64=crop_b64,
            bbox=[float(c) for c in crop_box], frame_width=w, frame_height=h,
            instruction=instruction,
        )
        seq = ctrl.grasp(req)
        return (f"Grasp of '{label}' delegated to the cerebellum "
                f"(seq_id={seq}, target crop {crop.size[0]}x{crop.size[1]}px). "
                "Now call arm_await_grasp and END YOUR TURN to suspend.")

    def arm_invoke(self, skill: str, args_json: str = "") -> str:
        """Invoke one robot-advertised skill (non-blocking).

        Use a skill name and argument schema exactly as returned by arm_describe
        (e.g. skill='move_above', args_json='{"x":0.5,"y":-0.1}'). Do NOT use
        this for grasping — grasping goes through arm_grasp (the cerebellum).
        After this, call arm_await_completion and END YOUR TURN to suspend.

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
                "step may have stalled. Call arm_check_result and arm_get_scene to see "
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

    def arm_await_grasp(self, handler_prompt: str = "", timeout_s: float = 300.0) -> str:
        """Register a one-shot handler that wakes a new turn when the cerebellum
        finishes the delegated grasp (arm.grasp.result).

        Same asynchronous paradigm as arm_await_completion: call this right after
        arm_grasp, then END YOUR TURN. The cerebellum's VLX-Flow loop can take a
        while (it servo-drives the arm frame by frame), hence the larger default
        watchdog timeout.

        Args:
            handler_prompt: instructions for the woken checker turn (optional).
            timeout_s: watchdog timeout — a timer wakes a checker turn anyway if
                no grasp result arrives within this many seconds.
        """
        agent = getattr(self.toolkit, "agent_instance", None)
        if agent is None or not hasattr(agent, "create_bus_handler"):
            return "ERROR: arm bus not available in this context"
        ctrl = self._controller()
        prompt = handler_prompt or (
            "The cerebellum just finished the delegated grasp. Call arm_check_grasp "
            "(and arm_get_camera if useful) to judge the outcome against the user's "
            "instruction. If the grasp failed, fix (e.g. a better bbox via arm_grasp) "
            "and arm_await_grasp again. If it succeeded but the instruction isn't "
            "finished, invoke the next step (arm_invoke + arm_await_completion). If "
            "the whole instruction is done, call arm_report_done."
        )
        already = ("The cerebellum already reported the grasp result before the "
                   "session could suspend. Do NOT end your turn — call "
                   "arm_check_grasp now to judge it, then continue with the next "
                   "step or arm_report_done.")
        # Race guard: the grasp can fail fast (e.g. cerebellum unconfigured) and
        # publish its result before this handler registers — same no-replay hazard
        # as arm_await_completion.
        if ctrl is not None and ctrl.grasp_result_ready():
            return already
        hid = agent.create_bus_handler(
            P.TOPIC_GRASP_RESULT, prompt, once=True, inherit_session=True,
            description="cerebellum grasp checker",
        )
        if ctrl is not None and ctrl.grasp_result_ready():
            agent.remove_bus_handler(hid)
            return already
        self._start_watchdog(agent, hid, prompt, timeout_s)
        return (f"Registered grasp-result handler {hid} (watchdog {timeout_s:.0f}s). "
                "END YOUR TURN now to suspend the session; the cerebellum's "
                "arm.grasp.result — or the watchdog — will wake the checker.")

    def arm_check_grasp(self) -> str:
        """Return the cerebellum's latest grasp result (arm.grasp.result payload)."""
        ctrl = self._controller()
        if ctrl is None:
            return "ERROR: arm bus not available in this context"
        res = ctrl.last_grasp_result()
        if not res:
            return "(no grasp result yet — the cerebellum has not reported back)"
        return json.dumps(res, ensure_ascii=False)

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
            self.arm_grasp,
            self.arm_await_grasp,
            self.arm_check_grasp,
            self.arm_invoke,
            self.arm_act,
            self.arm_await_completion,
            self.arm_check_result,
            self.arm_get_camera,
            self.arm_report_done,
        ]
