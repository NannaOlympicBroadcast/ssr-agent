"""Gateway — install a channel-bound SSR instance as a system service.

A *gateway* is a long-running SSR instance that serves one (or all) messaging
channels (Feishu / WeChat / XiaoAI) as a background **system service** so it
survives logout/reboot and restarts on failure. The same gateway definition is
installed using the platform's native service manager:

* **Linux**  — a systemd *user* unit (``systemctl --user``).
* **macOS**  — a launchd LaunchAgent plist (``launchctl``).
* **Windows** — a Scheduled Task that runs at logon (``schtasks``).

Gateway definitions live in ``~/.ssr/gateways.json``; the service simply runs
``ssr gateway run <name>``, which loads the record and serves its channel. When
no native manager is available the gateway is still saved and run-instructions
are printed, so it degrades gracefully (and can be driven by pm2 or Docker).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Settings

VALID_CHANNELS = ("feishu", "wechat", "xiaomi", "all")


# --------------------------------------------------------------------- records
@dataclass
class Gateway:
    name: str
    channel: str            # feishu | wechat | xiaomi | all
    cwd: str = ""           # starting working directory (optional)
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Gateway":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def gateways_file(settings: Settings) -> Path:
    return settings.home / "gateways.json"


def logs_dir(settings: Settings) -> Path:
    d = settings.home / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_gateways(settings: Settings) -> dict[str, Gateway]:
    f = gateways_file(settings)
    if not f.exists():
        return {}
    try:
        raw = json.loads(f.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {name: Gateway.from_dict(rec) for name, rec in raw.items()}


def save_gateways(settings: Settings, gateways: dict[str, Gateway]) -> None:
    data = {name: asdict(gw) for name, gw in gateways.items()}
    gateways_file(settings).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def get_gateway(settings: Settings, name: str) -> Gateway | None:
    return load_gateways(settings).get(name)


def add_gateway(settings: Settings, gw: Gateway) -> None:
    gateways = load_gateways(settings)
    gateways[gw.name] = gw
    save_gateways(settings, gateways)


def remove_gateway(settings: Settings, name: str) -> bool:
    gateways = load_gateways(settings)
    if name not in gateways:
        return False
    del gateways[name]
    save_gateways(settings, gateways)
    return True


def service_id(name: str) -> str:
    return f"ssr-gateway-{name}"


# ----------------------------------------------------------------- run gateway
def run_gateway(settings: Settings, name: str) -> int:
    """Foreground entrypoint executed by the system service: serve the channel."""
    import threading
    import time

    gw = get_gateway(settings, name)
    if gw is None:
        raise SystemExit(
            f"未找到网关 '{name}'。请先安装： ssr gateway install {name} --channel <feishu|wechat|xiaomi|all>"
        )

    import ssr.channels  # noqa: F401 — trigger channel registration
    from ssr.channels.registry import registry

    for k, v in (gw.env or {}).items():
        os.environ.setdefault(k, v)
    if gw.cwd:
        settings.project_dir = Path(gw.cwd).expanduser()

    if gw.channel == "all":
        threads = []
        for ch in registry.list_channels():
            t = threading.Thread(target=ch.serve, args=(settings,), daemon=True)
            t.start()
            threads.append(t)
        if not threads:
            raise SystemExit("没有已注册的频道可服务。")
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            return 0
    channel = registry.get(gw.channel)
    if channel is None:
        raise SystemExit(f"频道 '{gw.channel}' 未找到。")
    channel.serve(settings)
    return 0


# ------------------------------------------------------------ service managers
def _exec_args(name: str) -> list[str]:
    """Command the service runs: this Python's ``-m ssr gateway run <name>``."""
    return [sys.executable, "-m", "ssr", "gateway", "run", name]


class ServiceManager:
    backend = "none"

    def available(self) -> bool:
        return False

    def install(self, settings: Settings, gw: Gateway, start: bool = True) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def uninstall(self, settings: Settings, name: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def start(self, settings: Settings, name: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def stop(self, settings: Settings, name: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def restart(self, settings: Settings, name: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def status(self, settings: Settings, name: str) -> str:  # pragma: no cover
        raise NotImplementedError


class _NullManager(ServiceManager):
    """Used when no native service manager is available — degrade gracefully."""

    def available(self) -> bool:
        return False

    def install(self, settings: Settings, gw: Gateway, start: bool = True) -> str:
        cmd = " ".join(_exec_args(gw.name))
        return (
            "未检测到受支持的系统服务管理器（systemd / launchd / schtasks）。\n"
            f"网关 '{gw.name}' 已保存，可手动前台运行：\n    {cmd}\n"
            "或使用 pm2 守护： pm2 start " + sys.executable
            + f' --name {service_id(gw.name)} -- -m ssr gateway run {gw.name}'
        )

    def uninstall(self, settings: Settings, name: str) -> str:
        return f"网关 '{name}' 记录已删除（无系统服务需要移除）。"

    def start(self, settings: Settings, name: str) -> str:
        return "无系统服务管理器，请手动运行： " + " ".join(_exec_args(name))

    stop = start  # type: ignore[assignment]
    restart = start  # type: ignore[assignment]

    def status(self, settings: Settings, name: str) -> str:
        return "unknown (no service manager)"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


class SystemdManager(ServiceManager):
    backend = "systemd (--user)"

    def available(self) -> bool:
        return shutil.which("systemctl") is not None

    def _unit_path(self, name: str) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "systemd" / "user"
        return base / f"{service_id(name)}.service"

    def _unit_text(self, settings: Settings, gw: Gateway) -> str:
        workdir = gw.cwd or str(Path.home())
        env_lines = [f"Environment=SSR_HOME={settings.home}"]
        for k, v in (gw.env or {}).items():
            env_lines.append(f"Environment={k}={v}")
        exec_start = " ".join(_exec_args(gw.name))
        return (
            "[Unit]\n"
            f"Description=SSR Gateway ({gw.name}) — channel {gw.channel}\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={workdir}\n"
            + "\n".join(env_lines) + "\n"
            f"ExecStart={exec_start}\n"
            "Restart=always\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def install(self, settings: Settings, gw: Gateway, start: bool = True) -> str:
        path = self._unit_path(gw.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._unit_text(settings, gw), "utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        action = "enable --now" if start else "enable"
        res = _run(["systemctl", "--user", *action.split(), f"{service_id(gw.name)}.service"])
        msg = f"已安装 systemd 用户服务： {path}"
        if res.returncode != 0:
            return msg + f"\n[!] 启用失败：{res.stderr.strip()}\n请确认已登录用户会话；可执行 `loginctl enable-linger $USER` 让服务在未登录时也运行。"
        return (
            msg + f"\n已启用并启动 {service_id(gw.name)}。\n"
            "提示：若希望注销后仍运行， 执行： loginctl enable-linger $USER\n"
            f"查看日志： journalctl --user -u {service_id(gw.name)} -f"
        )

    def uninstall(self, settings: Settings, name: str) -> str:
        sid = f"{service_id(name)}.service"
        _run(["systemctl", "--user", "disable", "--now", sid])
        path = self._unit_path(name)
        existed = path.exists()
        if existed:
            path.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
        return f"已移除 systemd 用户服务 {sid}" + ("" if existed else "（单元文件原本不存在）")

    def start(self, settings: Settings, name: str) -> str:
        res = _run(["systemctl", "--user", "start", f"{service_id(name)}.service"])
        return res.stderr.strip() or f"{service_id(name)} 已启动"

    def stop(self, settings: Settings, name: str) -> str:
        res = _run(["systemctl", "--user", "stop", f"{service_id(name)}.service"])
        return res.stderr.strip() or f"{service_id(name)} 已停止"

    def restart(self, settings: Settings, name: str) -> str:
        res = _run(["systemctl", "--user", "restart", f"{service_id(name)}.service"])
        return res.stderr.strip() or f"{service_id(name)} 已重启"

    def status(self, settings: Settings, name: str) -> str:
        res = _run(["systemctl", "--user", "is-active", f"{service_id(name)}.service"])
        return (res.stdout or res.stderr).strip() or "unknown"


class LaunchdManager(ServiceManager):
    backend = "launchd"

    def available(self) -> bool:
        return shutil.which("launchctl") is not None

    def _label(self, name: str) -> str:
        return f"com.ssr.gateway.{name}"

    def _plist_path(self, name: str) -> Path:
        return Path("~/Library/LaunchAgents").expanduser() / f"{self._label(name)}.plist"

    def _plist_text(self, settings: Settings, gw: Gateway) -> str:
        args = "".join(f"\n    <string>{a}</string>" for a in _exec_args(gw.name))
        env = {"SSR_HOME": str(settings.home), **(gw.env or {})}
        env_xml = "".join(f"\n    <key>{k}</key><string>{v}</string>" for k, v in env.items())
        logs = logs_dir(settings)
        workdir = gw.cwd or str(Path.home())
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f"  <key>Label</key><string>{self._label(gw.name)}</string>\n"
            f"  <key>ProgramArguments</key>\n  <array>{args}\n  </array>\n"
            f"  <key>WorkingDirectory</key><string>{workdir}</string>\n"
            f"  <key>EnvironmentVariables</key>\n  <dict>{env_xml}\n  </dict>\n"
            "  <key>RunAtLoad</key><true/>\n"
            "  <key>KeepAlive</key><true/>\n"
            f"  <key>StandardOutPath</key><string>{logs}/gateway-{gw.name}.log</string>\n"
            f"  <key>StandardErrorPath</key><string>{logs}/gateway-{gw.name}.log</string>\n"
            "</dict>\n</plist>\n"
        )

    def install(self, settings: Settings, gw: Gateway, start: bool = True) -> str:
        path = self._plist_path(gw.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Reload cleanly if it already exists.
        _run(["launchctl", "unload", str(path)])
        path.write_text(self._plist_text(settings, gw), "utf-8")
        msg = f"已安装 launchd LaunchAgent： {path}"
        if start:
            res = _run(["launchctl", "load", "-w", str(path)])
            if res.returncode != 0:
                return msg + f"\n[!] 加载失败：{res.stderr.strip()}"
            return msg + f"\n已加载并启动 {self._label(gw.name)}。\n日志： {logs_dir(settings)}/gateway-{gw.name}.log"
        return msg

    def uninstall(self, settings: Settings, name: str) -> str:
        path = self._plist_path(name)
        _run(["launchctl", "unload", str(path)])
        existed = path.exists()
        if existed:
            path.unlink()
        return f"已移除 LaunchAgent {self._label(name)}" + ("" if existed else "（plist 原本不存在）")

    def start(self, settings: Settings, name: str) -> str:
        res = _run(["launchctl", "start", self._label(name)])
        return res.stderr.strip() or f"{self._label(name)} 已启动"

    def stop(self, settings: Settings, name: str) -> str:
        res = _run(["launchctl", "stop", self._label(name)])
        return res.stderr.strip() or f"{self._label(name)} 已停止"

    def restart(self, settings: Settings, name: str) -> str:
        self.stop(settings, name)
        return self.start(settings, name)

    def status(self, settings: Settings, name: str) -> str:
        res = _run(["launchctl", "list", self._label(name)])
        if res.returncode != 0:
            return "not loaded"
        return "loaded (running)" if '"PID"' in res.stdout else "loaded"


class WindowsTaskManager(ServiceManager):
    backend = "Scheduled Task (schtasks)"

    def available(self) -> bool:
        return shutil.which("schtasks") is not None

    def _task_name(self, name: str) -> str:
        return service_id(name)

    def _wrapper_path(self, settings: Settings, name: str) -> Path:
        d = settings.home / "gateways"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}.cmd"

    def _write_wrapper(self, settings: Settings, gw: Gateway) -> Path:
        path = self._wrapper_path(settings, gw.name)
        log = logs_dir(settings) / f"gateway-{gw.name}.log"
        lines = ["@echo off", f'set "SSR_HOME={settings.home}"']
        for k, v in (gw.env or {}).items():
            lines.append(f'set "{k}={v}"')
        if gw.cwd:
            lines.append(f'cd /d "{gw.cwd}"')
        exec_args = " ".join(f'"{a}"' if " " in a else a for a in _exec_args(gw.name))
        lines.append(f'{exec_args} >> "{log}" 2>&1')
        path.write_text("\r\n".join(lines) + "\r\n", "utf-8")
        return path

    def install(self, settings: Settings, gw: Gateway, start: bool = True) -> str:
        wrapper = self._write_wrapper(settings, gw)
        tn = self._task_name(gw.name)
        res = _run([
            "schtasks", "/Create", "/TN", tn, "/TR", str(wrapper),
            "/SC", "ONLOGON", "/RL", "HIGHEST", "/F",
        ], shell=True)
        if res.returncode != 0:
            return f"[!] 创建计划任务失败：{res.stderr.strip() or res.stdout.strip()}"
        msg = f"已创建 Windows 计划任务 {tn}（登录时自动启动）。\n包装脚本： {wrapper}"
        if start:
            self.start(settings, gw.name)
            msg += "\n已立即启动。"
        return msg

    def uninstall(self, settings: Settings, name: str) -> str:
        res = _run(["schtasks", "/Delete", "/TN", self._task_name(name), "/F"], shell=True)
        wrapper = self._wrapper_path(settings, name)
        if wrapper.exists():
            wrapper.unlink()
        return res.stdout.strip() or res.stderr.strip() or f"已删除计划任务 {self._task_name(name)}"

    def start(self, settings: Settings, name: str) -> str:
        res = _run(["schtasks", "/Run", "/TN", self._task_name(name)], shell=True)
        return res.stdout.strip() or res.stderr.strip() or "已请求启动"

    def stop(self, settings: Settings, name: str) -> str:
        res = _run(["schtasks", "/End", "/TN", self._task_name(name)], shell=True)
        return res.stdout.strip() or res.stderr.strip() or "已请求停止"

    def restart(self, settings: Settings, name: str) -> str:
        self.stop(settings, name)
        return self.start(settings, name)

    def status(self, settings: Settings, name: str) -> str:
        res = _run(["schtasks", "/Query", "/TN", self._task_name(name), "/FO", "LIST"], shell=True)
        if res.returncode != 0:
            return "not installed"
        for line in res.stdout.splitlines():
            if line.lower().startswith("status:"):
                return line.split(":", 1)[1].strip() or "unknown"
        return "installed"


def get_manager() -> ServiceManager:
    system = platform.system()
    mgr: ServiceManager
    if system == "Linux":
        mgr = SystemdManager()
    elif system == "Darwin":
        mgr = LaunchdManager()
    elif system == "Windows":
        mgr = WindowsTaskManager()
    else:
        mgr = _NullManager()
    return mgr if mgr.available() else _NullManager()
