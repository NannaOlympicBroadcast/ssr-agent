"""Gateway — install a channel-bound SSR instance as a system service.

A *gateway* is a long-running SSR instance that serves one (or all) messaging
channels (Feishu / WeChat / XiaoAI) as a background **system service** so it
survives logout/reboot and restarts on failure. The same gateway definition is
installed using the platform's native service manager:

* **Linux**  — a systemd *user* unit (``systemctl --user``).
* **macOS**  — a launchd LaunchAgent plist (``launchctl``).
* **Windows** — **pm2**, with pm2 itself installed as a Windows boot service so
  the gateway survives reboot and restarts on failure. The process is described
  by a pm2 ecosystem file and persisted with ``pm2 save``; ``pm2-windows-startup``
  registers pm2 to resurrect the saved processes on boot. If pm2 is not on PATH
  this falls back to a Scheduled Task that runs at logon (``schtasks``).

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
def _redirect_headless_output(settings: Settings, name: str) -> None:
    """Point stdout/stderr at the gateway log when launched without a console.

    On Windows the service runs under ``pythonw.exe`` (windowless) via a hidden
    VBScript launcher, so ``sys.stdout`` / ``sys.stderr`` are ``None``. Channel
    logging writes to ``sys.stdout``; redirect both to the log file so nothing is
    lost and no console window is needed.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log = logs_dir(settings) / f"gateway-{name}.log"
        stream = open(log, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def run_gateway(settings: Settings, name: str) -> int:
    """Foreground entrypoint executed by the system service: serve the channel."""
    import threading
    import time

    gw = get_gateway(settings, name)
    if gw is None:
        raise SystemExit(
            f"未找到网关 '{name}'。请先安装： ssr gateway install {name} --channel <feishu|wechat|xiaomi|all>"
        )

    # When launched headless (pythonw, no console) capture output to the log.
    _redirect_headless_output(settings, name)

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


def format_bytes(n: int | None) -> str:
    """Human-readable byte size (e.g. ``142.3 MB``); ``-`` for unknown."""
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _rss_from_proc(pid: int) -> int | None:
    """Read VmRSS (bytes) for *pid* from ``/proc`` (Linux)."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text("utf-8").splitlines():
            if line.startswith("VmRSS:"):
                kb = int(line.split()[1])
                return kb * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _empty_stats() -> dict:
    return {"running": False, "pid": None, "rss": None, "threads": None,
            "cpu_percent": None, "source": None}


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

    def stats(self, settings: Settings, name: str) -> dict:
        """Return live resource usage: running / pid / rss(bytes) / source."""
        return _empty_stats()


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


_PM2_PATH_CACHE: list = []  # memoized [path|None] for this process


def _find_pm2() -> str | None:
    """Locate the ``pm2`` executable, even when npm's global bin is off PATH.

    Tries PATH first, then ``%APPDATA%\\npm`` and ``npm config get prefix`` —
    so a service or otherwise stripped environment still finds the pm2 that the
    user installed, instead of silently falling back to a Scheduled Task.
    """
    if _PM2_PATH_CACHE:
        return _PM2_PATH_CACHE[0]
    found = shutil.which("pm2")
    if not found:
        candidates = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "pm2.cmd")
        try:
            r = subprocess.run(
                ["npm", "config", "get", "prefix"],
                capture_output=True, text=True, shell=True, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip():
                candidates.append(Path(r.stdout.strip()) / "pm2.cmd")
        except Exception:
            pass
        for c in candidates:
            try:
                if c.exists():
                    found = str(c)
                    break
            except OSError:
                pass
    _PM2_PATH_CACHE.append(found)
    return found


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

    def stats(self, settings: Settings, name: str) -> dict:
        st = _empty_stats()
        sid = f"{service_id(name)}.service"
        res = _run(["systemctl", "--user", "show", sid,
                    "-p", "MainPID", "-p", "MemoryCurrent", "-p", "ActiveState"])
        props: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v.strip()
        pid = int(props.get("MainPID", "0") or 0)
        st["running"] = props.get("ActiveState") == "active" and pid > 0
        st["pid"] = pid or None
        mc = props.get("MemoryCurrent", "")
        if mc.isdigit() and int(mc) < (1 << 63):  # systemd sentinel = 2**64-1
            st["rss"] = int(mc)
            st["source"] = "systemd MemoryCurrent (cgroup)"
        elif pid:
            st["rss"] = _rss_from_proc(pid)
            st["source"] = "/proc VmRSS"
        return st


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

    def stats(self, settings: Settings, name: str) -> dict:
        st = _empty_stats()
        res = _run(["launchctl", "list", self._label(name)])
        if res.returncode != 0:
            return st
        pid = None
        for line in res.stdout.splitlines():
            s = line.strip()
            if s.startswith('"PID"') and "=" in s:
                try:
                    pid = int(s.split("=", 1)[1].strip().rstrip(";").strip())
                except ValueError:
                    pid = None
        st["pid"] = pid
        st["running"] = pid is not None
        if pid:
            r = _run(["ps", "-o", "rss=", "-p", str(pid)])
            out = r.stdout.strip()
            if out.isdigit():
                st["rss"] = int(out) * 1024  # ps reports KB
                st["source"] = "ps rss"
        return st


class WindowsTaskManager(ServiceManager):
    backend = "Scheduled Task (schtasks)"

    def available(self) -> bool:
        return shutil.which("schtasks") is not None

    def _task_name(self, name: str) -> str:
        return service_id(name)

    def _interpreter(self) -> str:
        """Prefer ``pythonw.exe`` (no console window) over ``python.exe``."""
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        return str(pyw) if pyw.exists() else sys.executable

    def _wrapper_path(self, settings: Settings, name: str) -> Path:
        d = settings.home / "gateways"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}.cmd"

    def _write_wrapper(self, settings: Settings, gw: Gateway) -> Path:
        """Write the ``.cmd`` the scheduled task runs.

        Starts with ``@echo off`` to keep the console clean, sets SSR_HOME /
        extra env / cwd, then uses ``start "" /b`` to launch the **windowless**
        ``pythonw.exe`` and let ``cmd`` exit immediately — so only a brief
        console flash appears at logon instead of a persistent window. Output is
        redirected to the gateway log (``run_gateway`` also redirects when
        running under pythonw, which has no console).
        """
        path = self._wrapper_path(settings, gw.name)
        exe = self._interpreter()
        log = logs_dir(settings) / f"gateway-{gw.name}.log"
        lines = ["@echo off", f'set "SSR_HOME={settings.home}"']
        for k, v in (gw.env or {}).items():
            lines.append(f'set "{k}={v}"')
        if gw.cwd:
            lines.append(f'cd /d "{gw.cwd}"')
        lines.append(f'start "" /b "{exe}" -m ssr gateway run {gw.name} >> "{log}" 2>&1')
        path.write_text("\r\n".join(lines) + "\r\n", "utf-8")
        return path

    def install(self, settings: Settings, gw: Gateway, start: bool = True) -> str:
        wrapper = self._write_wrapper(settings, gw)
        tn = self._task_name(gw.name)
        res = _run([
            "schtasks", "/Create", "/TN", tn, "/TR", f'cmd /c "{wrapper}"',
            "/SC", "ONLOGON", "/RL", "HIGHEST", "/F",
        ], shell=True)
        if res.returncode != 0:
            return f"[!] 创建计划任务失败：{res.stderr.strip() or res.stdout.strip()}"
        msg = (
            f"已创建 Windows 计划任务 {tn}（登录时自动启动）。\n"
            f"包装脚本(.cmd，@echo off + pythonw + start /b，仅登录时一闪而过)： {wrapper}\n"
            f"日志： {logs_dir(settings)}\\gateway-{gw.name}.log"
        )
        if start:
            self.start(settings, gw.name)
            msg += "\n已立即启动。"
        return msg

    def uninstall(self, settings: Settings, name: str) -> str:
        res = _run(["schtasks", "/Delete", "/TN", self._task_name(name), "/F"], shell=True)
        wrapper = self._wrapper_path(settings, name)
        if wrapper.exists():
            wrapper.unlink()
        # Clean up any VBScript launcher left by an older version.
        legacy = wrapper.with_suffix(".vbs")
        if legacy.exists():
            legacy.unlink()
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

    def _query_processes(self, name: str) -> list[tuple[int, int]]:
        """Return ``(pid, working_set_bytes)`` for the process(es) serving *name*.

        Matched by command line so it works even though ``start /b`` detaches the
        process from the scheduled task (which then reports "Ready", not the live
        state). Concatenated — not f-string — so PowerShell's ``{ }`` stay intact.
        """
        needle = "gateway run " + name
        script = (
            "Get-CimInstance Win32_Process "
            "-Filter \"Name='pythonw.exe' or Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*" + needle + "*' } | "
            "ForEach-Object { \"$($_.ProcessId) $($_.WorkingSetSize)\" }"
        )
        res = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], shell=True)
        procs: list[tuple[int, int]] = []
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                procs.append((int(parts[0]), int(parts[1])))
        return procs

    def status(self, settings: Settings, name: str) -> str:
        if self._query_processes(name):
            return "running"
        res = _run(["schtasks", "/Query", "/TN", self._task_name(name)], shell=True)
        return "stopped" if res.returncode == 0 else "not installed"

    def stats(self, settings: Settings, name: str) -> dict:
        st = _empty_stats()
        # Locate the (pythonw/python) process actually serving this gateway and
        # sum its WorkingSetSize (bytes).
        procs = self._query_processes(name)
        if procs:
            st["pid"] = procs[0][0]
            st["rss"] = sum(ws for _, ws in procs)
            st["running"] = True
            st["source"] = "WorkingSetSize"
        return st


class Pm2WindowsManager(ServiceManager):
    """Run the gateway under **pm2** on Windows, with pm2 as a boot service.

    pm2 manages the long-running ``ssr gateway run <name>`` process (described by
    a per-gateway ecosystem file). ``pm2 save`` snapshots the process list and
    ``pm2-windows-startup`` registers pm2 to resurrect it on boot — i.e. pm2 acts
    as the Windows service that keeps the gateway alive across reboots.
    """

    backend = "pm2 (Windows boot service)"

    def available(self) -> bool:
        return _find_pm2() is not None

    def _pm2(self, args: list[str]) -> subprocess.CompletedProcess:
        # pm2 on Windows is a ``pm2.cmd`` shim, so it needs the shell. Resolve its
        # full path because the npm global bin is often missing from a service's
        # (or otherwise stripped) PATH, which would silently fall back to schtasks.
        return _run([_find_pm2() or "pm2", *args], shell=True)

    def _app_name(self, name: str) -> str:
        return service_id(name)

    def _ecosystem_path(self, settings: Settings, name: str) -> Path:
        d = settings.home / "gateways"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}.pm2.json"

    def _write_ecosystem(self, settings: Settings, gw: Gateway) -> Path:
        """Write the pm2 ecosystem file describing the gateway process."""
        log = str(logs_dir(settings) / f"gateway-{gw.name}.log")
        env = {"SSR_HOME": str(settings.home), **(gw.env or {})}
        app = {
            "name": self._app_name(gw.name),
            # Run ``<python> -m ssr gateway run <name>`` directly (no node wrapper).
            "script": sys.executable,
            "args": ["-m", "ssr", "gateway", "run", gw.name],
            "interpreter": "none",
            "cwd": gw.cwd or str(Path.home()),
            "env": env,
            # Auto-recover from genuine crashes, but guard against a tight restart
            # loop (pm2 "forking infinite processes") when the gateway exits or
            # crashes immediately: a start only counts as healthy after 15s, rapid
            # restarts are capped, and pm2 backs off exponentially between them.
            "autorestart": True,
            "min_uptime": "15s",
            "max_restarts": 5,
            "exp_backoff_restart_delay": 200,
            "instances": 1,
            "exec_mode": "fork",
            "out_file": log,
            "error_file": log,
            "merge_logs": True,
        }
        path = self._ecosystem_path(settings, gw.name)
        path.write_text(json.dumps({"apps": [app]}, ensure_ascii=False, indent=2), "utf-8")
        return path

    def _ensure_boot_service(self) -> str:
        """Make pm2 start on Windows boot (install pm2-windows-startup if needed).

        Every step is time-bounded so a slow/offline ``npm`` can never hang
        ``ssr gateway install`` — it just degrades to printed instructions.
        """
        def _try(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
            try:
                return _run(cmd, shell=True, timeout=timeout)
            except Exception:
                return None

        if shutil.which("pm2-startup"):
            res = _try(["pm2-startup", "install"], 60)
            if res is not None and res.returncode == 0:
                return "已配置 pm2 开机自启（pm2-windows-startup）。"
            return "[!] pm2-startup install 失败/超时，请手动执行： pm2-startup install"
        if shutil.which("npm"):
            ins = _try(["npm", "install", "-g", "pm2-windows-startup"], 180)
            if ins is not None and ins.returncode == 0 and shutil.which("pm2-startup"):
                res = _try(["pm2-startup", "install"], 60)
                if res is not None and res.returncode == 0:
                    return "已安装并配置 pm2-windows-startup（开机自启）。"
        return (
            "[!] 未能自动把 pm2 安装为 Windows 开机服务，请手动执行其一：\n"
            "    npm install -g pm2-windows-startup && pm2-startup install\n"
            "    （或用 https://github.com/jessety/pm2-installer 安装为系统服务）"
        )

    def install(self, settings: Settings, gw: Gateway, start: bool = True) -> str:
        eco = self._write_ecosystem(settings, gw)
        # Re-add cleanly so repeated installs don't error on a duplicate name.
        self._pm2(["delete", self._app_name(gw.name)])
        res = self._pm2(["start", str(eco)])
        if res.returncode != 0:
            return f"[!] pm2 start 失败：{res.stderr.strip() or res.stdout.strip()}"
        if not start:
            self._pm2(["stop", self._app_name(gw.name)])
        self._pm2(["save"])  # persist so pm2 resurrects it on boot
        boot_msg = self._ensure_boot_service()
        msg = (
            f"已通过 pm2 注册网关 {self._app_name(gw.name)}（ecosystem： {eco}）。\n"
            f"已执行 pm2 save，开机后由 pm2 自动恢复。\n"
            f"{boot_msg}\n"
            f"日志： {logs_dir(settings)}\\gateway-{gw.name}.log\n"
            f"查看日志： pm2 logs {self._app_name(gw.name)}"
        )
        if start:
            msg += "\n已立即启动。"
        return msg

    def uninstall(self, settings: Settings, name: str) -> str:
        self._pm2(["delete", self._app_name(name)])
        self._pm2(["save"])
        eco = self._ecosystem_path(settings, name)
        if eco.exists():
            eco.unlink()
        # Clean up any legacy scheduled-task wrapper from the schtasks backend.
        legacy = settings.home / "gateways" / f"{name}.cmd"
        if legacy.exists():
            legacy.unlink()
        return f"已从 pm2 删除 {self._app_name(name)} 并更新 pm2 save。"

    def start(self, settings: Settings, name: str) -> str:
        res = self._pm2(["start", self._app_name(name)])
        self._pm2(["save"])
        return res.stdout.strip() or res.stderr.strip() or f"{self._app_name(name)} 已启动"

    def stop(self, settings: Settings, name: str) -> str:
        res = self._pm2(["stop", self._app_name(name)])
        self._pm2(["save"])
        return res.stdout.strip() or res.stderr.strip() or f"{self._app_name(name)} 已停止"

    def restart(self, settings: Settings, name: str) -> str:
        res = self._pm2(["restart", self._app_name(name)])
        return res.stdout.strip() or res.stderr.strip() or f"{self._app_name(name)} 已重启"

    def _find(self, name: str) -> dict | None:
        res = self._pm2(["jlist"])
        try:
            apps = json.loads(res.stdout or "[]")
        except (json.JSONDecodeError, ValueError):
            return None
        target = self._app_name(name)
        for app in apps if isinstance(apps, list) else []:
            if app.get("name") == target:
                return app
        return None

    def status(self, settings: Settings, name: str) -> str:
        app = self._find(name)
        if app is None:
            return "not installed"
        return (app.get("pm2_env") or {}).get("status") or "unknown"

    def stats(self, settings: Settings, name: str) -> dict:
        st = _empty_stats()
        app = self._find(name)
        if app:
            env = app.get("pm2_env") or {}
            monit = app.get("monit") or {}
            st["pid"] = app.get("pid") or None
            st["running"] = env.get("status") == "online"
            st["rss"] = monit.get("memory")
            if st["rss"] is not None:
                st["source"] = "pm2 monit.memory"
        return st


def gather_stats(settings: Settings, name: str, with_cpu: bool = False) -> dict:
    """Normalized live stats for a gateway, enriched with psutil when available.

    Native managers provide running/pid/rss; if ``psutil`` is installed we add
    thread count (and CPU%% on demand) and backfill rss when the native probe
    couldn't read it.
    """
    st = get_manager().stats(settings, name)
    pid = st.get("pid")
    if pid:
        try:
            import psutil  # optional dependency

            proc = psutil.Process(pid)
            if st.get("rss") is None:
                st["rss"] = proc.memory_info().rss
                st["source"] = "psutil rss"
            st["threads"] = proc.num_threads()
            if with_cpu:
                st["cpu_percent"] = proc.cpu_percent(interval=0.3)
        except Exception:
            pass
    return st


def get_manager() -> ServiceManager:
    system = platform.system()
    mgr: ServiceManager
    if system == "Linux":
        mgr = SystemdManager()
    elif system == "Darwin":
        mgr = LaunchdManager()
    elif system == "Windows":
        # Prefer pm2 (as requested); fall back to a Scheduled Task when pm2 is
        # not installed so the gateway still works out of the box.
        pm2_mgr = Pm2WindowsManager()
        mgr = pm2_mgr if pm2_mgr.available() else WindowsTaskManager()
    else:
        mgr = _NullManager()
    return mgr if mgr.available() else _NullManager()
