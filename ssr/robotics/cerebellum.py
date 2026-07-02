"""The cerebellum (小脑): a realtime VLX-Flow grasp loop for the OpenArm.

Grasping follows a big-brain / cerebellum split. The SSR agent (big brain) only
crops the target object out of the camera frame and publishes an
``arm.grasp.request`` bus event; this module is the cerebellum that actually
performs the grasp, built on the Om-Agent **VLX-Flow** streaming vision model
(https://api.om-agent.cn — docs: apifox 46c7cae8…/8994275m0):

1. **Camera stream up** — publish ``arm.stream.start``; the Isaac bridge starts
   pushing its camera as an RTSP stream to the configured push address and
   confirms with ``arm.stream.started``.
2. **Target to VLX** — upload the brain's target crop (PNG) to the platform
   (``POST {endpoint}/os/uploadFile`` → ``data.fileUrl``) and reference it from
   the system prompt.
3. **Realtime loop** — ``POST {endpoint}/os/v2/vlx/stream`` with the configured
   RTSP *pull* URL (``source: {path, type: "1"}``) and a system prompt that makes
   VLX-Flow output one compact arm correction per analysed frame:
   ``{"dx":…,"dy":…,"dz":…,"g":"o|c|h"}`` (metres, root frame), or ``DONE`` /
   ``FAIL:reason``. Each correction is executed on the arm as the env's ``servo``
   skill via ``arm.action.execute`` and the loop waits for its
   ``arm.action.completed`` before applying the next one.
4. **Callback** — when VLX-Flow reports DONE/FAIL (or the loop times out), the
   VLX task is stopped (``POST {endpoint}/os/v2/vlx/stop`` — mandatory), the
   camera stream is stopped (``arm.stream.stop``) and the brain is notified with
   ``arm.grasp.result`` (which wakes its ``arm_await_grasp`` checker turn).

The cerebellum is registered by the **openarm plugin** as a ``python`` bus
handler on ``arm.grasp.request`` (see ``ssr/builtin_plugins/openarm``), so any
SSR agent with that plugin enabled hosts a cerebellum on its own bus.

Configuration lives in ``~/.ssr/vlx.json`` with ``VLX_*`` / ``OM_API_KEY`` env
overrides — see :class:`VlxConfig`.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import protocol as P

DEFAULT_ENDPOINT = "https://api.om-agent.cn"

# How the overhead camera's image axes map to the robot root frame. Matches the
# Isaac-Manip-OpenArm-v0 tiled_camera (straight-down, OpenGL identity rotation:
# image right = +x, image up = +y). Override per deployment via vlx.json
# ("axes_hint") / VLX_AXES_HINT when the camera is mounted differently.
DEFAULT_AXES_HINT = (
    "画面右方为 +x（远离机械臂底座），画面上方为 +y，z 轴垂直桌面向上为正。"
    "夹爪（机械臂末端）在画面中可见。"
)

# System prompt template that turns VLX-Flow into the arm's realtime motion
# corrector. Kept as a module constant so a deployment can review/tune it; the
# {target_url}/{label}/{bbox}/{axes_hint}/{max_step} slots are filled per grasp.
SYSTEM_PROMPT_TEMPLATE = """你是一台机械臂的小脑（实时运动控制器）。视频流是俯视机械臂工作台的相机画面。
抓取目标：{label}。目标物体的参考截图：{target_url} （截取自任务开始时画面中的像素框 {bbox}）。{instruction}
坐标系（机器人根坐标系，单位：米）：{axes_hint}
每次分析当前画面后，只输出一条指令，不要输出任何解释：
- 修正指令（JSON 一行）：{{"dx":0.02,"dy":0.00,"dz":-0.03,"g":"h"}}
  dx/dy/dz 是末端执行器的位移增量，每个分量不超过 {max_step} 米；g 是夹爪动作："o"=张开，"c"=闭合，"h"=保持。
- 抓取策略：先水平移动使夹爪对准目标正上方（只调 dx/dy），对准后下降（dz<0）至目标高度，然后输出 g:"c" 闭合夹爪，最后 dz>0 提起目标。
- 已经抓稳并提起目标后，只输出：DONE
- 判断无法完成抓取时，只输出：FAIL:原因"""

USER_PROMPT_TEMPLATE = "根据当前画面，输出抓取『{label}』的下一条指令。"


@dataclass
class VlxConfig:
    """VLX-Flow (Om-Agent 开发者平台) + grasp-loop configuration.

    File: ``~/.ssr/vlx.json`` (same keys). Env overrides: ``VLX_ENDPOINT`` /
    ``OM_API_ENDPOINT``, ``VLX_API_KEY`` / ``OM_API_KEY``, ``VLX_RTSP_PULL_URL``,
    ``VLX_MODEL``, ``VLX_INTERVAL_SECONDS``, ``VLX_FRAME_PREEMPT``,
    ``VLX_TEMPERATURE``, ``VLX_MAX_TOKENS``, ``VLX_MAX_STEP_M``,
    ``VLX_SERVO_TIMEOUT_S``, ``VLX_STREAM_START_TIMEOUT_S``,
    ``VLX_MAX_DURATION_S``, ``VLX_AXES_HINT``.
    """

    endpoint: str = DEFAULT_ENDPOINT
    api_key: str = ""
    model: str = "vlx-flow"
    # The RTSP address the VLX platform pulls the live camera from. The robot
    # side pushes to its own configured address (SSR_ARM_STREAM_URL on the
    # bridge); with a media server (e.g. mediamtx) both are usually the same URL.
    rtsp_pull_url: str = ""
    interval_seconds: float = 1.0
    # `preempt` analyses the newest frame instead of queueing stale ones — the
    # right mode for closed-loop control (docs: queue / preempt / qa).
    frame_preempt: str = "preempt"
    temperature: float = 0.1
    # The docs recommend ~20 tokens; a one-line servo JSON needs a little more.
    max_tokens: int = 64
    max_step_m: float = 0.05
    servo_timeout_s: float = 30.0
    stream_start_timeout_s: float = 20.0
    # Hard wall for one grasp; when exceeded the task is stopped and reported
    # failed so the brain's checker can re-plan.
    max_duration_s: float = 240.0
    axes_hint: str = DEFAULT_AXES_HINT

    @classmethod
    def from_dict(cls, d: dict) -> "VlxConfig":
        cfg = cls()
        for f in ("endpoint", "api_key", "model", "rtsp_pull_url", "frame_preempt",
                  "axes_hint"):
            if d.get(f):
                setattr(cfg, f, str(d[f]))
        for f in ("interval_seconds", "temperature", "max_step_m", "servo_timeout_s",
                  "stream_start_timeout_s", "max_duration_s"):
            if d.get(f) is not None:
                try:
                    setattr(cfg, f, float(d[f]))
                except (TypeError, ValueError):
                    pass
        if d.get("max_tokens") is not None:
            try:
                cfg.max_tokens = int(d["max_tokens"])
            except (TypeError, ValueError):
                pass
        return cfg


def load_vlx_config(settings=None) -> VlxConfig:
    """Load :class:`VlxConfig` from ``~/.ssr/vlx.json`` + env overrides."""
    import os

    home = Path(getattr(settings, "home", None) or Path.home() / ".ssr")
    cfg = VlxConfig()
    path = home / "vlx.json"
    try:
        if path.exists():
            cfg = VlxConfig.from_dict(json.loads(path.read_text("utf-8")))
    except Exception:
        pass  # a malformed file must not kill the handler; env can still configure
    env = os.environ.get
    cfg.endpoint = env("VLX_ENDPOINT") or env("OM_API_ENDPOINT") or cfg.endpoint
    cfg.api_key = env("VLX_API_KEY") or env("OM_API_KEY") or cfg.api_key
    cfg.rtsp_pull_url = env("VLX_RTSP_PULL_URL") or cfg.rtsp_pull_url
    cfg.model = env("VLX_MODEL") or cfg.model
    cfg.frame_preempt = env("VLX_FRAME_PREEMPT") or cfg.frame_preempt
    cfg.axes_hint = env("VLX_AXES_HINT") or cfg.axes_hint
    for name, attr in (("VLX_INTERVAL_SECONDS", "interval_seconds"),
                       ("VLX_TEMPERATURE", "temperature"),
                       ("VLX_MAX_STEP_M", "max_step_m"),
                       ("VLX_SERVO_TIMEOUT_S", "servo_timeout_s"),
                       ("VLX_STREAM_START_TIMEOUT_S", "stream_start_timeout_s"),
                       ("VLX_MAX_DURATION_S", "max_duration_s")):
        raw = env(name, "")
        if raw.strip():
            try:
                setattr(cfg, attr, float(raw))
            except ValueError:
                pass
    raw = env("VLX_MAX_TOKENS", "")
    if raw.strip():
        try:
            cfg.max_tokens = int(raw)
        except ValueError:
            pass
    cfg.endpoint = cfg.endpoint.rstrip("/")
    return cfg


# --------------------------------------------------------------- VLX client
class VlxFlowClient:
    """Minimal client for the Om-Agent VLX-Flow endpoints used by the grasp loop.

    Endpoints (all Bearer-authenticated with the platform access token):

    * ``POST {endpoint}/os/uploadFile`` — multipart upload, returns
      ``data.fileUrl``.
    * ``POST {endpoint}/os/v2/vlx/stream`` — the model call; replies as
      ``text/event-stream`` SSE (``data: {json}`` lines, ``[DONE]`` terminator).
    * ``POST {endpoint}/os/v2/vlx/stop`` — stops a running task by ``taskId``
      (mandatory after every run).
    """

    def __init__(self, cfg: VlxConfig):
        self.cfg = cfg

    def _headers(self, **extra: str) -> dict:
        return {"Authorization": f"Bearer {self.cfg.api_key}", **extra}

    def upload_bytes(self, data: bytes, filename: str = "target.png",
                     content_type: str = "image/png") -> str:
        """Upload a file to the platform; return the hosted ``fileUrl``."""
        import httpx

        resp = httpx.post(
            f"{self.cfg.endpoint}/os/uploadFile",
            headers=self._headers(),
            files={"file": (filename, data, content_type)},
            data={"type": content_type},
            timeout=60.0,
        )
        resp.raise_for_status()
        body = resp.json()
        url = ((body.get("data") or {}).get("fileUrl")
               if isinstance(body.get("data"), dict) else None)
        if not url:
            raise RuntimeError(f"uploadFile returned no data.fileUrl: {body}")
        return str(url)

    def stream(self, prompt: str, system_prompt: str):
        """Open the VLX-Flow SSE stream; returns ``(response, events)`` where
        ``events`` iterates parsed ``data:`` JSON dicts until ``[DONE]``/EOF.

        The caller is responsible for closing ``response`` (``response.close()``)
        and for calling :meth:`stop` with the task id observed in the events.
        """
        import httpx

        body = {
            "model": self.cfg.model,
            "prompt": prompt,
            "system_prompt": system_prompt,
            "interval_seconds": self.cfg.interval_seconds,
            "frame_preempt": self.cfg.frame_preempt,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "source": {"path": self.cfg.rtsp_pull_url, "type": "1"},
        }
        client = httpx.Client(timeout=httpx.Timeout(30.0, read=None))
        req = client.build_request(
            "POST", f"{self.cfg.endpoint}/os/v2/vlx/stream",
            headers=self._headers(**{"Content-Type": "application/json",
                                     "Accept": "text/event-stream"}),
            json=body,
        )
        resp = client.send(req, stream=True)
        try:
            resp.raise_for_status()
        except Exception:
            try:
                resp.read()
                detail = resp.text[:500]
            except Exception:
                detail = ""
            resp.close()
            client.close()
            raise RuntimeError(
                f"vlx/stream HTTP {resp.status_code}: {detail}") from None

        def _events():
            try:
                yield from iter_sse_events(resp.iter_lines())
            finally:
                resp.close()
                client.close()

        return resp, _events()

    def stop(self, task_id: str) -> None:
        """Stop a running VLX-Flow task (must be called after every run)."""
        import httpx

        resp = httpx.post(
            f"{self.cfg.endpoint}/os/v2/vlx/stop",
            headers=self._headers(**{"Content-Type": "application/json"}),
            json={"taskId": task_id},
            timeout=30.0,
        )
        resp.raise_for_status()


def iter_sse_events(lines):
    """Parse an SSE line iterator into JSON event dicts.

    Yields each ``data: {...}`` payload as a dict; a bare ``data`` string that is
    not JSON is yielded as ``{"content": <str>}``. Stops on the ``[DONE]``
    terminator or end of stream.
    """
    for raw in lines:
        if raw is None:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            obj = {"content": data}
        if isinstance(obj, dict):
            yield obj


def find_task_id(event: dict) -> str | None:
    """Pull a task id out of an SSE event; the docs only promise it exists
    ('具体字段位置以接口实际返回为准'), so look in the likely places."""
    for holder in (event, event.get("data") if isinstance(event.get("data"), dict) else {}):
        for key in ("taskId", "task_id", "taskID"):
            v = holder.get(key)
            if v:
                return str(v)
    return None


_JSON_RE = re.compile(r"\{[^{}]*\}")


def parse_frame_text(text: str, max_step: float) -> dict | None:
    """Interpret one analysed frame's model output.

    Returns ``{"kind": "done"}``, ``{"kind": "fail", "reason": …}``, or
    ``{"kind": "servo", "dx": …, "dy": …, "dz": …, "grip": …}`` with the deltas
    clamped to ``±max_step``; ``None`` when the text contains no actionable
    command (it is skipped — the next frame brings a fresh one).
    """
    t = (text or "").strip()
    if not t:
        return None
    upper = t.upper()
    m = re.search(r"FAIL\s*[:：]?\s*(.*)", upper)
    if m:
        # Recover the reason from the original (possibly non-ASCII) text.
        idx = upper.find("FAIL")
        reason = t[idx + 4:].lstrip(" :：").strip() or "unspecified"
        return {"kind": "fail", "reason": reason}
    if re.search(r"\bDONE\b", upper):
        return {"kind": "done"}
    m = _JSON_RE.search(t)
    if not m:
        return None
    try:
        cmd = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(cmd, dict):
        return None
    def _delta(key: str) -> float:
        try:
            v = float(cmd.get(key) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        return max(-max_step, min(max_step, v))
    grip_raw = str(cmd.get("g") or cmd.get("grip") or "h").strip().lower()
    grip = {"o": "open", "open": "open", "c": "close", "close": "close"}.get(
        grip_raw, "hold")
    out = {"kind": "servo", "dx": _delta("dx"), "dy": _delta("dy"),
           "dz": _delta("dz"), "grip": grip}
    if out["dx"] == 0.0 and out["dy"] == 0.0 and out["dz"] == 0.0 and grip == "hold":
        return None  # a no-op correction — nothing to execute
    return out


# ------------------------------------------------------------- grasp session
class GraspSession:
    """One delegated grasp, end to end: stream handshake → VLX loop → callback."""

    def __init__(self, bus, cfg: VlxConfig, req: P.ArmGraspRequest,
                 source: str = "ssr-cerebellum"):
        self.bus = bus
        self.cfg = cfg
        self.req = req
        self.source = source
        self.client = VlxFlowClient(cfg)
        self.task_id: str | None = None
        self.frames = 0
        self.commands = 0
        self._last_completion: dict = {}

    # ------------------------------------------------------------ bus glue
    def _publish(self, topic: str, payload: dict) -> None:
        try:
            self.bus.publish(topic, payload, source=self.source)
        except TypeError:
            # A remote BusClient has no `source` kwarg (it stamps its own).
            self.bus.publish(topic, payload)

    # ------------------------------------------------------------ the loop
    def run(self) -> dict:
        started = time.monotonic()
        outcome, reason = "failed", ""
        stream_started = False
        try:
            self._start_camera_stream()
            stream_started = True
            target_url = self.client.upload_bytes(
                base64.b64decode(self.req.target_b64), filename="target.png")
            outcome, reason = self._vlx_loop(target_url, started)
        except Exception as e:
            reason = str(e) or type(e).__name__
        finally:
            if self.task_id:
                try:
                    self.client.stop(self.task_id)
                except Exception as e:
                    print(f"[cerebellum] vlx stop failed: {e}")
            if stream_started:
                self._publish(P.TOPIC_STREAM_STOP, {"seq_id": self.req.seq_id})
        result = {
            "seq_id": self.req.seq_id,
            "label": self.req.label,
            "ok": outcome == "grasped",
            "outcome": outcome,
            "reason": reason,
            "holding": self._last_completion.get("holding"),
            "grasp": self._last_completion.get("grasp"),
            "task_id": self.task_id,
            "frames": self.frames,
            "commands": self.commands,
            "duration_s": round(time.monotonic() - started, 1),
        }
        self._publish(P.TOPIC_GRASP_RESULT, result)
        print(f"[cerebellum] grasp '{self.req.label}' -> {outcome} "
              f"({reason or 'ok'}; {self.commands} servo cmds, "
              f"{self.frames} frames, task={self.task_id})")
        return result

    def _start_camera_stream(self) -> None:
        """Ask the robot side to push its camera to the configured RTSP address
        and wait for the confirmation event."""
        seq = self.req.seq_id
        done = threading.Event()
        box: dict = {}

        def _cb(ev) -> None:
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            if not payload.get("seq_id") or payload.get("seq_id") == seq:
                box.setdefault("payload", payload)
                done.set()

        sid = self.bus.subscribe(P.TOPIC_STREAM_STARTED, _cb)
        try:
            self._publish(P.TOPIC_STREAM_START, {"seq_id": seq})
            if not done.wait(self.cfg.stream_start_timeout_s):
                raise RuntimeError(
                    "camera stream did not start: no arm.stream.started within "
                    f"{self.cfg.stream_start_timeout_s:.0f}s — is the Isaac bridge "
                    "running with --stream-url configured?")
        finally:
            try:
                self.bus.unsubscribe(sid)
            except Exception:
                pass
        payload = box.get("payload") or {}
        if payload.get("ok") is False:
            raise RuntimeError(f"camera stream failed to start: "
                               f"{payload.get('error') or 'unknown error'}")

    def _vlx_loop(self, target_url: str, started: float) -> tuple[str, str]:
        """Drive the realtime correction loop until DONE / FAIL / timeout."""
        bbox = "[" + ", ".join(str(int(round(c))) for c in (self.req.bbox or [])) + "]"
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            label=self.req.label,
            target_url=target_url,
            bbox=f"{bbox} (画面 {self.req.frame_width}x{self.req.frame_height})",
            instruction=(self.req.instruction or "").strip(),
            axes_hint=self.cfg.axes_hint,
            max_step=self.cfg.max_step_m,
        )
        prompt = USER_PROMPT_TEMPLATE.format(label=self.req.label)
        resp, events = self.client.stream(prompt, system_prompt)
        frame_no: object = None
        buffer: list[str] = []
        try:
            for ev in events:
                if self.task_id is None:
                    self.task_id = find_task_id(ev)
                if time.monotonic() - started > self.cfg.max_duration_s:
                    return "failed", (f"grasp exceeded max_duration_s="
                                      f"{self.cfg.max_duration_s:.0f}")
                ev_frame = ev.get("frameNo")
                ev_kind = str(ev.get("event") or "")
                content = ev.get("content")
                if ev_frame is not None and ev_frame != frame_no and buffer:
                    # A new frame's tokens started — the previous frame is complete.
                    verdict = self._handle_frame("".join(buffer))
                    buffer.clear()
                    if verdict is not None:
                        return verdict
                if ev_frame is not None:
                    frame_no = ev_frame
                if isinstance(content, str) and content:
                    buffer.append(content)
                if ev_kind == "done":
                    verdict = self._handle_frame("".join(buffer))
                    buffer.clear()
                    if verdict is not None:
                        return verdict
            # Flush whatever the stream ended on.
            if buffer:
                verdict = self._handle_frame("".join(buffer))
                if verdict is not None:
                    return verdict
            return "failed", "vlx stream ended without DONE"
        finally:
            resp.close()

    def _handle_frame(self, text: str) -> tuple[str, str] | None:
        """Act on one analysed frame's output; return a final verdict or None."""
        cmd = parse_frame_text(text, self.cfg.max_step_m)
        self.frames += 1
        if cmd is None:
            return None
        if cmd["kind"] == "fail":
            return "failed", f"vlx: {cmd['reason']}"
        if cmd["kind"] == "done":
            holding = self._last_completion.get("holding")
            if holding is False:
                return "failed", "vlx reported DONE but the gripper holds nothing"
            return "grasped", ""
        self._servo(cmd)
        return None

    def _servo(self, cmd: dict) -> None:
        """Execute one correction on the arm and wait for its completion."""
        seq = uuid.uuid4().hex[:10]
        done = threading.Event()
        box: dict = {}

        def _cb(ev) -> None:
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            if payload.get("seq_id") == seq:
                box.setdefault("payload", payload)
                done.set()

        sid = self.bus.subscribe(P.TOPIC_ACTION_COMPLETED, _cb)
        try:
            req = P.ArmActionRequest(
                seq_id=seq, episode=0, command="servo",
                args={"dx": cmd["dx"], "dy": cmd["dy"], "dz": cmd["dz"],
                      "grip": cmd["grip"]},
                label=f"cerebellum servo {self.req.label}",
            )
            self._publish(P.TOPIC_ACTION_EXECUTE, req.to_payload())
            self.commands += 1
            if not done.wait(self.cfg.servo_timeout_s):
                raise RuntimeError(
                    f"servo step {seq} got no arm.action.completed within "
                    f"{self.cfg.servo_timeout_s:.0f}s")
        finally:
            try:
                self.bus.unsubscribe(sid)
            except Exception:
                pass
        payload = box.get("payload") or {}
        self._last_completion = payload
        if payload.get("ok") is False:
            raise RuntimeError(f"servo step failed on the robot: "
                               f"{payload.get('error') or 'unknown error'}")


# --------------------------------------------------- plugin handler entry point
_ACTIVE = threading.Lock()
_HANDLED: set[str] = set()
_HANDLED_LOCK = threading.Lock()


def handle_grasp_event(event, bus, settings=None) -> dict | None:
    """Entry point for the openarm plugin's ``arm.grasp.request`` bus handler.

    Runs one :class:`GraspSession` synchronously (the plugin dispatches python
    handlers on their own daemon thread). De-duplicates by ``seq_id`` (bridged
    buses can deliver an event to several handler instances) and rejects
    concurrent grasps — there is only one arm.
    """
    payload = event.payload if isinstance(event.payload, dict) else {}
    req = P.ArmGraspRequest.from_payload(payload)
    if not req.seq_id:
        req.seq_id = uuid.uuid4().hex[:10]
    with _HANDLED_LOCK:
        if req.seq_id in _HANDLED:
            return None
        _HANDLED.add(req.seq_id)
        if len(_HANDLED) > 512:
            _HANDLED.clear()
            _HANDLED.add(req.seq_id)

    def _fail(reason: str) -> dict:
        result = {"seq_id": req.seq_id, "label": req.label, "ok": False,
                  "outcome": "failed", "reason": reason}
        try:
            bus.publish(P.TOPIC_GRASP_RESULT, result, source="ssr-cerebellum")
        except TypeError:
            bus.publish(P.TOPIC_GRASP_RESULT, result)
        print(f"[cerebellum] grasp '{req.label}' rejected: {reason}")
        return result

    cfg = load_vlx_config(settings)
    if not cfg.api_key:
        return _fail("VLX api key not configured — set OM_API_KEY/VLX_API_KEY or "
                     "api_key in ~/.ssr/vlx.json")
    if not cfg.rtsp_pull_url:
        return _fail("RTSP pull url not configured — set VLX_RTSP_PULL_URL or "
                     "rtsp_pull_url in ~/.ssr/vlx.json")
    if not req.target_b64:
        return _fail("grasp request carries no target crop (target_b64)")
    if not _ACTIVE.acquire(blocking=False):
        return _fail("cerebellum busy with another grasp")
    try:
        return GraspSession(bus, cfg, req).run()
    finally:
        _ACTIVE.release()
