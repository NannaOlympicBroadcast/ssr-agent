"""Agent tools for the Xiaomi **Miloco** home integration.

These are thin, model-facing wrappers over :mod:`ssr.integrations.miloco`. They
return short human-readable strings (the agent reads them), degrade gracefully
when Miloco is offline, and surface the Windows→Docker guidance instead of a raw
traceback when run on an unsupported platform.
"""

from __future__ import annotations

import json

from ..config import Settings
from ..integrations import miloco as ml


def _client_or_msg(settings: Settings):
    """Return a ``(client, None)`` or ``(None, error_message)`` pair."""
    try:
        return ml.MilocoClient(settings=settings), None
    except ml.MilocoUnavailable as e:
        return None, f"Miloco 不可用：{e}"


def _dump(records: list[dict], empty: str, limit: int = 50) -> str:
    if not records:
        return empty
    shown = records[:limit]
    body = json.dumps(shown, ensure_ascii=False, indent=2)
    suffix = f"\n… (+{len(records) - limit} more)" if len(records) > limit else ""
    return f"{len(records)} 条：\n{body}{suffix}"


def miloco_status(settings: Settings) -> str:
    """Check whether the Miloco home service is reachable and the Mi account is bound."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    if not client.health():
        return (f"Miloco 未响应（{client.cfg.base_url}）。请确认 Miloco 服务已启动："
                "miloco-cli dashboard，或在 Docker 中 `docker compose up -d miloco`。")
    bind = client.bind_status()
    return f"Miloco 在线（{client.cfg.base_url}）。账号绑定状态：{json.dumps(bind, ensure_ascii=False)}"


def miloco_devices(settings: Settings) -> str:
    """List Mi Home devices known to Miloco (id/did, name, room, online state)."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    return _dump(client.devices(), "未发现设备（账号未绑定或 Miloco 未同步）。")


def miloco_device_control(settings: Settings, did: str, action_json: str) -> str:
    """Control a Mi Home device via Miloco.

    Args:
        did: The device id (``did``) from ``miloco_devices``.
        action_json: JSON body for the control call, e.g.
            ``{"siid": 2, "piid": 1, "value": true}`` (per the device spec).
    """
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    try:
        action = json.loads(action_json) if action_json else {}
    except json.JSONDecodeError as e:
        return f"action_json 不是合法 JSON：{e}"
    result = client.control_device(did, action)
    if result is None:
        return f"控制设备 {did} 失败（Miloco 未响应或参数无效）。"
    return f"已下发控制到 {did}：{json.dumps(result, ensure_ascii=False)}"


def miloco_family(settings: Settings) -> str:
    """List recognised family members / persons (Miloco identity library)."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    return _dump(client.members(), "暂无已登记的家庭成员（人脸/身份库为空）。")


def miloco_activities(settings: Settings, limit: int = 20) -> str:
    """List recent meaningful home events/activities from Miloco (newest first)."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    return _dump(client.activities(limit=limit), "暂无最近事件。", limit=limit)


def miloco_automations(settings: Settings) -> str:
    """List Miloco automation rules (triggers/conditions/actions)."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    return _dump(client.automations(), "暂无自动化规则。")


def miloco_sync(settings: Settings) -> str:
    """Refresh the cached Miloco context snapshot (devices/family/events/automations).

    The snapshot feeds the persistent context pool, so call this after the home
    changes (new device, new family member) to make the agent aware of it.
    """
    snap = ml.sync_snapshot(settings)
    if snap.get("error"):
        return f"同步失败：{snap['error']}"
    counts = {k: len(snap.get(k) or []) for k in ("homes", "devices", "members", "automations", "activities")}
    return f"已同步 Miloco 上下文快照：{json.dumps(counts, ensure_ascii=False)} → {ml.snapshot_path(settings)}"
