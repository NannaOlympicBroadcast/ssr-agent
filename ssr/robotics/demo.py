"""Brain-side entry point: drive the real OpenArm by a natural-language instruction.

There is no simulator here — the SSR agent only speaks :mod:`ssr.bus` events. The
environment side is the Isaac Lab bridge (``ssr-robotics`` /
``openarm_isaac_lab/scripts/ssr_bridge``) which must be connected to the same bus
server. A real :class:`~ssr.agent.core.SSRAgent` (the LLM) reads the instruction,
perceives the scene via the arm tools, and composes pick / place / move steps —
each run through the suspend → handler → wake → verify paradigm — until done.
"""

from __future__ import annotations

import time

from . import protocol as P


def _bridge_to_bus(agent, bus_url: str | None):
    url = bus_url or getattr(agent.settings, "bus_url", None)
    if url:
        agent.connect_bus(url)
    return getattr(agent.settings, "bus_url", None)


def run_instruction(settings, instruction: str, bus_url: str | None = None,
                    timeout: float = 180.0, console=None) -> int:
    """Drive the real Isaac bridge from a free-form instruction (needs GEMINI_API_KEY)."""
    from ..agent.core import SSRAgent
    from ..approval import AutoApprovalHandler

    agent = SSRAgent(settings)
    # `ssr arm do` drives the agent headlessly (agent.run() with no stdin-polling
    # loop), so the default TUIApprovalHandler would block forever on input() the
    # moment any command needs approval — in the main turn *and* in every
    # bus-woken checker turn — stalling the whole instruction. There is no usable
    # approval UX here (same situation as the xiaomi speaker channel), so
    # auto-approve every command.
    agent.toolkit.approval_handler = AutoApprovalHandler()
    url = _bridge_to_bus(agent, bus_url)
    done = {"ok": False, "summary": ""}
    # `timeout` is an *idle* timeout, not a hard wall: any robot activity (a step
    # completing, a fresh scene snapshot) pushes the deadline out. A task that keeps
    # making progress therefore never times out — only one that goes silent for the
    # full window does. Combined with arm_await_completion's watchdog (which revives
    # a stalled session, producing activity), this stops "still times out" on long
    # multi-step instructions.
    last_activity = [time.time()]

    def _bump(ev):
        last_activity[0] = time.time()

    def _on_success(ev):
        done["ok"] = True
        done["summary"] = (ev.payload or {}).get("summary", "")

    agent.bus.subscribe(P.TOPIC_TASK_SUCCESS, _on_success)
    agent.bus.subscribe(P.TOPIC_ACTION_COMPLETED, _bump)
    agent.bus.subscribe(P.TOPIC_GRASP_COMPLETED, _bump)
    agent.bus.subscribe(P.TOPIC_CAMERA, _bump)

    prompt = (
        f"用户指令：{instruction}\n\n"
        "你可以操作一台 Isaac Lab 中的 OpenArm 机械臂来完成它。流程要求：\n"
        "1) 先调用 arm_describe 了解这台机械臂支持哪些动作/技能（skills）及其参数。"
        "系统不会直接告诉你物体的位置——你要靠相机看：调用 arm_get_camera 拍一张当前相机画面，"
        "从图像里自己找出目标物体在画面中的像素位置（px, py）。\n"
        "2) 只使用 arm_describe 返回的技能来规划：用 arm_invoke(skill, args) 调用某个技能"
        "（用你从相机图里读出的像素坐标 px, py 指定目标，例如 pick {px, py}；机械臂会把该像素"
        "通过相机反投影到三维空间），或用 arm_act 发送底层动作向量；不要假设任何未被通告的动作类型。\n"
        "3) 注意避障：arm_describe 返回的 obstacles 列出了桌面/支撑台等障碍物（root 坐标系下的 "
        "AABB 包围盒）。把它们当作碰撞体——不要让机械臂或夹爪的运动路径穿过这些区域。规划时"
        "先抬到物体正上方再下降抓取，移动到别处前先抬升到障碍物之上，避免在低空横向直线穿过桌面/台子。\n"
        "4) 像素只需大致落在目标上即可：机械臂会用相机视觉（俯视相机，若配置了腕部相机还会做局部居中修正）"
        "精确定位抓取点；到达航点也有一定容差范围，不必追求绝对精确。\n"
        "5) 每下发一步后，调用 arm_await_completion 注册回调，然后结束本回合以挂起会话、释放资源。\n"
        "6) 被总线事件唤醒后，用 arm_check_result 和 arm_get_camera（重新看相机）判断这一步是否成功：\n"
        "   失败就修正并重发该步；成功但指令未完成就下发下一步；都完成后调用 arm_report_done。"
    )
    if console:
        console.print(f"[bold]User:[/bold] {instruction}")
        # Build marker — if you don't see this line, the installed ssr-agent is an
        # OLD build (reinstall it): the idle-timeout + completion watchdog below
        # only exist here.
        console.print(f"[dim]arm driver: auto-approve on, idle-timeout={timeout:.0f}s, "
                      f"completion watchdog active[/dim]")
        console.print(f"[dim]bus={url or 'embedded'} — awaiting the Isaac bridge…[/dim]")
    reply = agent.run(prompt)
    if console:
        console.print(f"[bold]Agent:[/bold] {reply}\n[dim]session suspended; awaiting bus callbacks…[/dim]")

    while not done["ok"] and (time.time() - last_activity[0]) < timeout:
        time.sleep(0.5)
    agent.close()
    if console:
        if done["ok"]:
            console.print(f"[green]✓ instruction complete[/green] {done['summary']}")
        else:
            console.print(f"[yellow]instruction not confirmed complete — "
                          f"no robot activity for {timeout:.0f}s[/yellow]")
    return 0 if done["ok"] else 1
