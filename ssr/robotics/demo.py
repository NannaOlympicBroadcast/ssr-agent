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
    agent.bus.subscribe(P.TOPIC_STATE, _bump)

    prompt = (
        f"用户指令：{instruction}\n\n"
        "你可以操作一台 Isaac Lab 中的 OpenArm 机械臂来完成它。流程要求：\n"
        "1) 先调用 arm_describe 了解这台机械臂支持哪些动作/技能（skills）及其参数，"
        "再调用 arm_reset、arm_get_scene 观察场景中的物体及位置（必要时看相机图）。\n"
        "2) 只使用 arm_describe 返回的技能来规划：用 arm_invoke(skill, args) 调用某个技能，"
        "或用 arm_act 发送底层动作向量；不要假设任何未被通告的动作类型或物体名称。\n"
        "3) 每下发一步后，调用 arm_await_completion 注册回调，然后结束本回合以挂起会话、释放资源。\n"
        "4) 被总线事件唤醒后，用 arm_check_result（必要时 arm_get_camera）判断这一步是否成功：\n"
        "   失败就修正并重发该步；成功但指令未完成就下发下一步；都完成后调用 arm_report_done。"
    )
    if console:
        console.print(f"[bold]User:[/bold] {instruction}")
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
