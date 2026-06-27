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

For each step the agent plans:

1. **Dispatch** — `arm_invoke(skill, args)` (or `arm_act(actions)`) publishes
   `arm.action.execute` (non-blocking).
2. **Register & suspend** — `arm_await_completion` registers a one-shot bus
   handler on `arm.*.completed`, then the agent **ends its turn** → the session is
   suspended and resources freed (no blocking wait).
3. **Robot executes** — the bridge runs the skill, then publishes
   `arm.grasp.completed` / `arm.action.completed` (result + objects + camera frame).
4. **Wake a checker agent** — the handler fires a **fresh agent turn** that calls
   `arm_check_result` (+ `arm_get_camera`) to judge the step.
5. **Branch** — failed → fix & re-dispatch; succeeded but unfinished → next step;
   done → `arm_report_done` (publishes `arm.task.success`).

Built on existing SSR primitives: `MessageBus`, `SSRAgent.create_bus_handler`
(wakes a new turn per matching event) and the append-only `SessionStore`.

## Tools (`tools.py`)

`arm_describe`, `arm_reset`, `arm_get_scene`, `arm_invoke`, `arm_act`,
`arm_await_completion`, `arm_check_result`, `arm_get_camera`, `arm_report_done` —
merged into the main `ToolKit`.

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
