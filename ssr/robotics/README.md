# SSR Robotics — natural-language OpenArm control (brain + cerebellum)

Lets an SSR agent control an Isaac Lab **OpenArm** robot from **natural-language
instructions** (e.g. *“帮我把苹果放到橘子上”*) using an **asynchronous,
multi-agent, event-bus** paradigm. The agent never touches the simulator; it only
publishes/consumes [`ssr.bus`](../bus) events. The simulator side is the Isaac Lab
bridge (`ssr-robotics` + `openarm_isaac_lab/scripts/ssr_bridge`).

## 大脑 / 小脑 — the big-brain / cerebellum split

Grasping is split across two brains:

* **Big brain (the SSR agent)** — plans the instruction and *perceives only*:
  it looks at the camera frame, finds the target object's pixel bounding box,
  crops it out (`arm_grasp`) and publishes the crop on ``arm.grasp.request``.
  It never plans grasp trajectories.
* **Cerebellum ([`cerebellum.py`](cerebellum.py))** — hosted by the **openarm
  plugin** as a bus handler on ``arm.grasp.request``. It drives the realtime
  grasp loop with the Om-Agent **VLX-Flow** streaming vision model
  (`https://api.om-agent.cn`):
  1. asks the robot to push its camera as an **RTSP stream**
     (``arm.stream.start`` → the bridge runs ffmpeg → the configured push URL);
  2. uploads the target crop (``POST /os/uploadFile``) and opens the VLX-Flow
     SSE stream (``POST /os/v2/vlx/stream``) with the configured **RTSP pull
     URL** as the video source and a system prompt that makes the model emit
     one compact correction per analysed frame:
     ``{"dx":…,"dy":…,"dz":…,"g":"o|c|h"}`` / ``DONE`` / ``FAIL:reason``;
  3. executes each correction on the arm as the env's ``servo`` skill and waits
     for its ``arm.action.completed``;
  4. on DONE / FAIL / timeout it stops the VLX task (``POST /os/v2/vlx/stop`` —
     mandatory), stops the camera stream, and calls the brain back with
     ``arm.grasp.result``.

Configure the cerebellum in `~/.ssr/vlx.json` (or env — see
`cerebellum.VlxConfig`):

```json
{
  "endpoint": "https://api.om-agent.cn",
  "api_key": "<OM_API_KEY>",
  "rtsp_pull_url": "rtsp://<media-host>:8554/openarm"
}
```

The robot side needs `--stream-url rtsp://<media-host>:8554/openarm` (push) and
`ffmpeg` on PATH; with a media server such as **mediamtx**, push and pull can be
the same URL. Bus frames carry the images (crops + camera frames) as base64 —
raise `SSR_BUS_MAX_MSG_MB` (default 8) for higher-resolution cameras.

## Capability-driven — nothing is hardcoded

The robot **advertises its capabilities** on `arm.capabilities`: the low-level
action space (introspected from the live `action_manager`) *and* the skills it
implements. The arm's real action types — grounded in the OpenArm repo
(`lift_env_cfg.py`) — are **end-effector pose** (`DifferentialInverseKinematicsAction`
on `openarm_hand`) and a **binary gripper** (`openarm_finger_joint.*`). On top of
those the bridge exposes skills (`servo`, `place_at`, `move_above`, `raw`). The
agent calls `arm_describe` to learn them and plans only with what is advertised.

## The interaction paradigm

For each step the agent plans:

1. **Dispatch** — grasping: `arm_grasp(label, bbox)` publishes
   `arm.grasp.request`; other motion: `arm_invoke(skill, args)` (or
   `arm_act(actions)`) publishes `arm.action.execute`. Both are non-blocking.
2. **Register & suspend** — `arm_await_grasp` / `arm_await_completion` registers
   a one-shot bus handler, then the agent **ends its turn** → the session is
   suspended and resources freed (no blocking wait). A watchdog timer guards
   against lost events.
3. **Execute** — the cerebellum runs its VLX-Flow loop (grasp) or the bridge runs
   the skill (other motion), then publishes `arm.grasp.result` /
   `arm.action.completed`.
4. **Wake a checker agent** — the handler fires a **fresh agent turn** that calls
   `arm_check_grasp` / `arm_check_result` (+ `arm_get_camera`) to judge the step.
5. **Branch** — failed → fix & re-dispatch; succeeded but unfinished → next step;
   done → `arm_report_done` (publishes `arm.task.success`).

Built on existing SSR primitives: `MessageBus`, `SSRAgent.create_bus_handler`
(wakes a new turn per matching event) and the append-only `SessionStore`.

## Tools (`tools.py`, contributed by the openarm plugin)

`arm_describe`, `arm_reset`, `arm_get_scene`, `arm_grasp`, `arm_await_grasp`,
`arm_check_grasp`, `arm_invoke`, `arm_act`, `arm_await_completion`,
`arm_check_result`, `arm_get_camera`, `arm_report_done` — merged into the main
`ToolKit`.

## Run

The Isaac bridge must be running and connected to the same bus server (see
`ssr-robotics` / `openarm_isaac_lab/scripts/ssr_bridge`), with `--stream-url`
pointing at the RTSP server. Then on the brain machine (needs `GEMINI_API_KEY` +
`~/.ssr/vlx.json`):

```bash
ssr ask --keep-alive 120 "帮我把苹果放到橘子上"
```

Brain-side unit tests (no simulator, no network):

```bash
python -m pytest tests/test_robotics.py -q
```
