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


def _normalize_control(action: dict) -> dict:
    """Coerce a control payload into Miloco's ``DeviceControlRequest`` shape.

    Miloco expects ``{"type": "set_property"|"set_properties"|"call_action",
    "iid": "prop.{siid}.{piid}"|"action.{siid}.{aiid}", "value"/"properties"/
    "params": …}``. To be forgiving, accept these shorthands and normalise:

    * ``{"siid": 2, "piid": 1, "value": true}``        → set_property prop.2.1
    * ``{"iid": "prop.2.1", "value": true}``           → set_property (as-is iid)
    * ``{"siid": 2, "aiid": 1, "params": [..]}``        → call_action action.2.1
    * anything already carrying ``type``                → passed through unchanged
    """
    if not isinstance(action, dict) or action.get("type"):
        return action
    if "siid" in action and "piid" in action:
        return {"type": "set_property", "iid": f"prop.{action['siid']}.{action['piid']}",
                "value": action.get("value")}
    if "siid" in action and "aiid" in action:
        return {"type": "call_action", "iid": f"action.{action['siid']}.{action['aiid']}",
                "params": action.get("params", [])}
    if "iid" in action and "value" in action:
        return {"type": "set_property", "iid": action["iid"], "value": action["value"]}
    return action


def miloco_device_control(settings: Settings, did: str, action_json: str) -> str:
    """Control a Mi Home device via Miloco.

    Args:
        did: The device id (``did``) from ``miloco_devices``.
        action_json: JSON control body. Use Miloco's ``DeviceControlRequest`` shape:
            set a property — ``{"type":"set_property","iid":"prop.2.1","value":true}``
            (``iid`` is ``prop.{siid}.{piid}``; get siid/piid from
            ``miloco_device_spec``); set many —
            ``{"type":"set_properties","properties":[{"iid":"prop.2.1","value":true}]}``;
            run an action — ``{"type":"call_action","iid":"action.2.1","params":[]}``.
            A shorthand ``{"siid":2,"piid":1,"value":true}`` is also accepted.
    """
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    try:
        action = json.loads(action_json) if action_json else {}
    except json.JSONDecodeError as e:
        return f"action_json 不是合法 JSON：{e}"
    body = _normalize_control(action)
    if not body.get("type"):
        return ("控制参数缺少 type。请用 DeviceControlRequest 形式，例如 "
                '{"type":"set_property","iid":"prop.2.1","value":true}；'
                "iid 为 prop.{siid}.{piid}，可先用 miloco_device_spec 查 siid/piid。")
    result = client.control_device(did, body)
    if result is None:
        return (f"控制设备 {did} 失败（Miloco 未响应、鉴权失败或参数无效）。"
                f"已下发 body：{json.dumps(body, ensure_ascii=False)}。"
                "用 miloco_device_spec 确认 iid，用 miloco_status 确认连通/鉴权。")
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


def miloco_device_status(settings: Settings, did: str) -> str:
    """Read a Mi Home device's current property values (on/off, temp, battery…).

    Args:
        did: The device id (``did``) from ``miloco_devices``.
    """
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    data = client.device_status(did)
    return json.dumps(data, ensure_ascii=False, indent=2) if data is not None else f"读取设备 {did} 状态失败。"


def miloco_device_spec(settings: Settings, did: str) -> str:
    """Get a device's MIoT spec (the siid/piid/aiid map needed for control).

    Args:
        did: The device id (``did``) from ``miloco_devices``.
    """
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    data = client.device_spec(did)
    return json.dumps(data, ensure_ascii=False, indent=2) if data is not None else f"读取设备 {did} spec 失败。"


def miloco_trigger_scene(settings: Settings, scene_id: str) -> str:
    """Trigger a Mi Home manual scene (e.g. 回家/离家/睡眠) by its scene id.

    Args:
        scene_id: The Mi Home scene id to run.
    """
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    res = client.trigger_scene(scene_id)
    return f"已触发场景 {scene_id}：{json.dumps(res, ensure_ascii=False)}" if res is not None else f"触发场景 {scene_id} 失败。"


def miloco_cameras(settings: Settings) -> str:
    """List Mi Home cameras known to Miloco (id, name, online/connected state)."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    return _dump(client.cameras(), "未发现摄像头。")


def miloco_tasks(settings: Settings) -> str:
    """List Miloco persistent home tasks (reminders / automations / habit stats)."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    return _dump(client.tasks(), "暂无家庭任务。")


def miloco_home_profile(settings: Settings) -> str:
    """Read the home memory/profile — family preferences, habits, routines, rules."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    data = client.home_profile()
    if not data:
        return "家庭档案为空（尚未积累偏好/习惯记录）。"
    return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)


def miloco_scope(settings: Settings) -> str:
    """Show Miloco's perception scope — which homes and cameras it perceives."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    homes = client.scope_homes()
    cams = client.scope_cameras()
    return ("感知范围\n家庭(homes):\n" + json.dumps(homes, ensure_ascii=False, indent=2)
            + "\n摄像头(cameras):\n" + json.dumps(cams, ensure_ascii=False, indent=2))


def miloco_notify(settings: Settings, message: str) -> str:
    """Send a proactive home notification via Miloco (speaker TTS / IM / Mi push).

    Args:
        message: The notification text to deliver (Miloco decides the channel).
    """
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    if not (message or "").strip():
        return "通知内容为空。"
    res = client.send_notify(message)
    return "通知已发送。" if res is not None else "发送通知失败（Miloco 未响应或鉴权失败）。"


def miloco_refresh(settings: Settings) -> str:
    """Refresh Miloco's device/scene/user caches from the Mi cloud."""
    client, msg = _client_or_msg(settings)
    if client is None:
        return msg
    res = client.refresh()
    return "已刷新 Miloco 设备缓存。" if res is not None else "刷新失败（Miloco 未响应）。"


def miloco_sync(settings: Settings) -> str:
    """Refresh the cached Miloco context snapshot (devices/family/events/automations).

    The snapshot feeds the persistent context pool, so call this after the home
    changes (new device, new family member) to make the agent aware of it.
    """
    snap = ml.sync_snapshot(settings)
    if snap.get("error"):
        return f"同步失败：{snap['error']}"
    keys = ("homes", "devices", "cameras", "members", "automations", "tasks", "activities")
    counts = {k: len(snap.get(k) or []) for k in keys}
    counts["home_profile"] = bool(snap.get("home_profile"))
    return f"已同步 Miloco 上下文快照：{json.dumps(counts, ensure_ascii=False)} → {ml.snapshot_path(settings)}"


class MilocoTools:
    """Agent tools for the Xiaomi Miloco (Mi Home) integration, bound to a
    :class:`~ssr.agent.tools.ToolKit`.

    Contributed in-process by the bundled ``miloco`` plugin (``agent_tools``) — the
    same mechanism the ``openarm`` plugin uses — rather than being hardcoded into the
    core ToolKit. Each tool is a thin wrapper over the module functions above.
    """

    def __init__(self, toolkit):
        self.toolkit = toolkit

    @property
    def _settings(self) -> Settings:
        return self.toolkit.settings

    def miloco_status(self) -> str:
        """Check the Xiaomi Miloco home service: reachable? Mi account bound?"""
        return miloco_status(self._settings)

    def miloco_devices(self) -> str:
        """List Mi Home devices known to Miloco (id/did, name, room, online)."""
        return miloco_devices(self._settings)

    def miloco_device_control(self, did: str, action_json: str) -> str:
        """Control a Mi Home device via Miloco.

        Args:
            did: Device id (``did``) from ``miloco_devices``.
            action_json: Miloco DeviceControlRequest JSON, e.g.
                ``{"type":"set_property","iid":"prop.2.1","value":true}`` (iid is
                ``prop.{siid}.{piid}`` — get siid/piid from ``miloco_device_spec``);
                or ``{"type":"call_action","iid":"action.2.1","params":[]}``.
        """
        return miloco_device_control(self._settings, did, action_json)

    def miloco_device_status(self, did: str) -> str:
        """Read a Mi Home device's current property values (on/off, temp, battery…).

        Args:
            did: Device id (``did``) from ``miloco_devices``.
        """
        return miloco_device_status(self._settings, did)

    def miloco_device_spec(self, did: str) -> str:
        """Get a device's MIoT spec (siid/piid/aiid map) needed for control.

        Args:
            did: Device id (``did``) from ``miloco_devices``.
        """
        return miloco_device_spec(self._settings, did)

    def miloco_trigger_scene(self, scene_id: str) -> str:
        """Trigger a Mi Home manual scene (回家/离家/睡眠…) by its scene id.

        Args:
            scene_id: The Mi Home scene id to run.
        """
        return miloco_trigger_scene(self._settings, scene_id)

    def miloco_cameras(self) -> str:
        """List Mi Home cameras known to Miloco (online/connected state)."""
        return miloco_cameras(self._settings)

    def miloco_family(self) -> str:
        """List recognised family members / persons in Miloco's identity library."""
        return miloco_family(self._settings)

    def miloco_activities(self, limit: int = 20) -> str:
        """List recent meaningful home events/activities from Miloco (newest first).

        Args:
            limit: Max number of events to return (1–200).
        """
        return miloco_activities(self._settings, limit)

    def miloco_automations(self) -> str:
        """List Miloco automation rules (triggers / conditions / actions)."""
        return miloco_automations(self._settings)

    def miloco_tasks(self) -> str:
        """List Miloco persistent home tasks (reminders / automations / habit stats)."""
        return miloco_tasks(self._settings)

    def miloco_home_profile(self) -> str:
        """Read the home memory/profile — family preferences, habits, routines, rules."""
        return miloco_home_profile(self._settings)

    def miloco_scope(self) -> str:
        """Show Miloco's perception scope — which homes and cameras it perceives."""
        return miloco_scope(self._settings)

    def miloco_notify(self, message: str) -> str:
        """Send a proactive home notification via Miloco (speaker TTS / IM / Mi push).

        Args:
            message: The notification text to deliver.
        """
        return miloco_notify(self._settings, message)

    def miloco_refresh(self) -> str:
        """Refresh Miloco's device/scene/user caches from the Mi cloud."""
        return miloco_refresh(self._settings)

    def miloco_sync(self) -> str:
        """Refresh the cached Miloco context snapshot (devices/family/events/rules)."""
        return miloco_sync(self._settings)

    def callables(self) -> list:
        return [
            self.miloco_status,
            self.miloco_devices,
            self.miloco_device_control,
            self.miloco_device_status,
            self.miloco_device_spec,
            self.miloco_trigger_scene,
            self.miloco_cameras,
            self.miloco_family,
            self.miloco_activities,
            self.miloco_automations,
            self.miloco_tasks,
            self.miloco_home_profile,
            self.miloco_scope,
            self.miloco_notify,
            self.miloco_refresh,
            self.miloco_sync,
        ]
