# SSR Robotics — natural-language OpenArm control (brain side)

Lets an SSR agent control an Isaac Lab **OpenArm** robot from **natural-language
instructions** (e.g. *“帮我把苹果放到橘子上”*) using an **asynchronous,
multi-agent, event-bus** paradigm. The agent never touches the simulator; it only
publishes/consumes [`ssr.bus`](../bus) events. The simulator side is the Isaac Lab
bridge (`ssr-robotics` + `openarm_isaac_lab/scripts/ssr_bridge`).

## Capability-driven — nothing is hardcoded

The robot **advertises its capabilities** on `arm.capabilities`: the low-level
action space (introspected from the live `action_manager`) *and* the skills it
implements. The arm's real action types — grounded in the OpenArm repo
(`lift_env_cfg.py`) — are **end-effector pose** (`DifferentialInverseKinematicsAction`
on `openarm_hand`) and a **binary gripper** (`openarm_finger_joint.*`). On top of
those the bridge exposes skills (`pick`, `place_on`, `place_at`, `move_above`,
`raw`). The agent calls `arm_describe` to learn them and plans only with what is
advertised — add a skill on the robot and the agent can use it with no code change.

## The interaction paradigm

**Perception is camera-only.** The agent is never handed object positions. It
looks at the camera frame (`arm_get_camera`), finds the target in the image, and
names it by **pixel** `(px, py)`; the bridge back-projects that pixel through the
camera. `arm_get_camera` attaches the actual frame to the agent's next turn as a
real inline image (via `SSRAgent.queue_tool_image`/`take_tool_images`, drained by
every provider's tool loop) — a tool's return value is otherwise plain text, so
without this the agent would only ever read a saved-path string and could never
actually see what it's picking. For each step the agent plans:

0. **Look** — `arm_get_camera` fetches a fresh frame; the agent reads the target's
   pixel off the image.
1. **Dispatch** — `arm_invoke('pick', {px, py})` (or `arm_act(actions)`) publishes
   `arm.action.execute` (non-blocking).
2. **Register & suspend** — `arm_await_completion` registers a one-shot bus
   handler on `arm.*.completed`, then the agent **ends its turn** → the session is
   suspended and resources freed (no blocking wait).
3. **Robot executes** — the bridge runs the skill, then publishes
   `arm.grasp.completed` / `arm.action.completed` (grasp/proprioception result +
   camera frame; no object coordinates).
4. **Wake a checker agent** — the handler fires a **fresh agent turn** that calls
   `arm_check_result` (+ `arm_get_camera`) to judge the step.
5. **Branch** — failed → fix & re-dispatch; succeeded but unfinished → next step;
   done → `arm_report_done` (publishes `arm.task.success`).

Built on existing SSR primitives: `MessageBus`, `SSRAgent.create_bus_handler`
(wakes a new turn per matching event) and the append-only `SessionStore`.

## Tools (`tools.py`)

`arm_describe`, `arm_reset`, `arm_invoke`, `arm_act`, `arm_await_completion`,
`arm_check_result`, `arm_get_camera`, `arm_record_start`, `arm_record_stop`,
`arm_report_done` — merged into the main `ToolKit`. (There is no `arm_get_scene`:
object positions are not exposed; the agent perceives via `arm_get_camera` and
targets by pixel.)

### Recording & reflection

Any camera the robot advertises (`arm_describe → recording.cameras`) can be
recorded: `arm_record_start(camera)` before dispatching a skill,
`arm_record_stop()` after its completion — the robot encodes the buffered frames
(MP4 when ffmpeg is available, else GIF) and ships them back over the bus; the
tool saves the file locally and returns its path. **Reviewing the recording of
the whole motion — not just an end frame — is how the agent reflects**: was the
approach centred, did the gripper actually close on the object, did the arm clip
an obstacle? Feeding that back into the next attempt substantially improves task
accuracy.

## Run

The Isaac bridge must be running and connected to the same bus server (see
`ssr-robotics` / `openarm_isaac_lab/scripts/ssr_bridge`). Then on the brain
machine (needs `GEMINI_API_KEY`):

```bash
ssr arm do "帮我把苹果放到橘子上" --bus-url ws://<gpu-host>:8765
```

Brain-side unit tests (no simulator):

```bash
python -m pytest tests/test_robotics.py -q
```
