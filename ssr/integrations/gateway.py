"""Gateway — install a channel-bound SSR instance as a system service.

A *gateway* is a long-running SSR instance that serves one (or all) messaging
channels (Feishu / WeChat / XiaoAI) as a background **system service** so it
survives logout/reboot and restarts on failure. The same gateway definition is
installed using the platform's native service manager:

* **Linux**  — a systemd *user* unit (``systemctl --user``).
* **macOS**  — a launchd LaunchAgent plist (``launchctl``).
* **Windows** — **nssm** (the Non-Sucking Service Manager), which wraps
  ``ssr gateway run <name>`` in a genuine Windows service. It starts at boot
  (``SERVICE_AUTO_START``), survives logout, and is restarted on failure by
  nssm's own supervisor — no Node/pm2 toolchain required. If nssm is not on
  PATH this falls back to a Scheduled Task that runs at logon (``schtasks``).

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


# Proxy vars a system service must inherit. A background service does NOT pick up
# the user's shell proxy, but the model provider (e.g. Gemini, blocked in some
# regions) may need it to reach the API — without it the agent silently fails
# every turn. Captured at install time so the gateway can phone home.
_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def _inherited_proxy_env() -> dict[str, str]:
    """The proxy-related variables currently set in this process's environment."""
    return {k: os.environ[k] for k in _PROXY_ENV_KEYS if os.environ.get(k)}


def service_env(settings: Settings, gw: "Gateway") -> dict[str, str]:
    """Full environment for the gateway service.

    ``SSR_HOME`` + inherited proxy vars + the gateway's own ``env`` (which wins on
    conflict, so a user can override the captured proxy).
    """
    env = {"SSR_HOME": str(settings.home)}
    env.update(_inherited_proxy_env())
    env.update(gw.env or {})
    return env


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
            "未检测到受支持的系统服务管理器（systemd / launchd / nssm / schtasks）。\n"
            f"网关 '{gw.name}' 已保存，可手动前台运行：\n    {cmd}\n"
            "或安装 nssm 后注册为 Windows 服务： nssm install "
            + f'{service_id(gw.name)} "{sys.executable}" -m ssr gateway run {gw.name}'
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
    # Force UTF-8 decoding with replacement: on a non-UTF-8 locale (e.g. GBK on
    # Chinese Windows) the default codec chokes on nssm's UTF-16/non-locale bytes
    # and the subprocess reader thread dies, leaving stdout/stderr as None.
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _out(res: subprocess.CompletedProcess) -> str:
    """First non-empty of stdout/stderr — NUL-stripped, trimmed, None-safe.

    nssm may emit UTF-16 console text (interleaved NULs) and, on a decode error,
    a stream can be ``None``; normalize both here so callers never crash.
    """
    out = (res.stdout or "").replace("\x00", "").strip()
    err = (res.stderr or "").replace("\x00", "").strip()
    return out or err


_NSSM_PATH_CACHE: list = []  # memoized [path|None] for this process


def _find_nssm() -> str | None:
    """Locate the ``nssm`` executable, even when it is off PATH.

    Tries PATH first, then the common Chocolatey / Scoop install locations — so
    a service or otherwise stripped environment still finds the nssm the user
    installed, instead of silently falling back to a Scheduled Task.
    """
    if _NSSM_PATH_CACHE:
        return _NSSM_PATH_CACHE[0]
    found = shutil.which("nssm")
    if not found:
        candidates = []
        programdata = os.environ.get("ProgramData")
        if programdata:
            candidates.append(Path(programdata) / "chocolatey" / "bin" / "nssm.exe")
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            candidates.append(Path(userprofile) / "scoop" / "shims" / "nssm.exe")
        for c in candidates:
            try:
                if c.exists():
                    found = str(c)
                    break
            except OSError:
                pass
    _NSSM_PATH_CACHE.append(found)
    return found


def _query_gateway_processes(name: str) -> list[tuple[int, int]]:
    """Return ``(pid, working_set_bytes)`` for the process(es) serving *name*.

    Matched by command line (``gateway run <name>``) so it works regardless of
    which supervisor (nssm service / scheduled task) launched the interpreter.
    Concatenated — not f-string — so PowerShell's ``{ }`` stay intact.
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


class SystemdManager(ServiceManager):
    backend = "systemd (--user)"

    def available(self) -> bool:
        return shutil.which("systemctl") is not None

    def _unit_path(self, name: str) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "systemd" / "user"
        return base / f"{service_id(name)}.service"

    def _unit_text(self, settings: Settings, gw: Gateway) -> str:
        workdir = gw.cwd or str(Path.home())
        env_lines = [f"Environment={k}={v}" for k, v in service_env(settings, gw).items()]
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
        env = service_env(settings, gw)
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
        lines = ["@echo off"]
        for k, v in service_env(settings, gw).items():
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
        """Process(es) serving *name*, matched by command line.

        ``start /b`` detaches the process from the scheduled task (which then
        reports "Ready", not the live state), so match on the command line.
        """
        return _query_gateway_processes(name)

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


class NssmWindowsManager(ServiceManager):
    """Run the gateway as a real Windows service via **nssm**.

    nssm (the Non-Sucking Service Manager) wraps ``ssr gateway run <name>`` in a
    genuine Windows service: it starts at boot (``SERVICE_AUTO_START``), survives
    logout, and is restarted on failure by nssm's own supervisor — no Node/pm2
    toolchain required. The program, arguments, working directory, environment,
    log files and restart throttle are applied with ``nssm set``. Installing or
    removing a service requires Administrator rights.
    """

    backend = "nssm (Windows service)"

    def available(self) -> bool:
        return _find_nssm() is not None

    def _nssm(self, args: list[str]) -> subprocess.CompletedProcess:
        # nssm is a real .exe (resolved to a full path), so no shell is needed.
        return _run([_find_nssm() or "nssm", *args])

    def _service(self, name: str) -> str:
        return service_id(name)

    def _set(self, svc: str, param: str, *values: str) -> None:
        self._nssm(["set", svc, param, *values])

    def install(self, settings: Settings, gw: Gateway, start: bool = True) -> str:
        svc = self._service(gw.name)
        log = str(logs_dir(settings) / f"gateway-{gw.name}.log")
        # Re-install cleanly so repeated installs don't error on a duplicate name.
        self._nssm(["stop", svc])
        self._nssm(["remove", svc, "confirm"])
        res = self._nssm(["install", svc, sys.executable, "-m", "ssr", "gateway", "run", gw.name])
        if res.returncode != 0:
            err = _out(res)
            hint = ""
            if "denied" in err.lower() or "administrator" in err.lower():
                hint = "\n（注册 Windows 服务需要管理员权限，请在管理员终端中重试。）"
            return f"[!] nssm install 失败：{err}{hint}"
        self._set(svc, "AppDirectory", gw.cwd or str(Path.home()))
        # AppEnvironmentExtra takes one KEY=VALUE per argument.
        env = service_env(settings, gw)
        self._set(svc, "AppEnvironmentExtra", *[f"{k}={v}" for k, v in env.items()])
        self._set(svc, "AppStdout", log)
        self._set(svc, "AppStderr", log)
        # Restart on exit, but throttle to avoid a tight crash loop: only count a
        # start as healthy after 15s, and back off 2s between restarts.
        self._set(svc, "AppExit", "Default", "Restart")
        self._set(svc, "AppThrottle", "15000")
        self._set(svc, "AppRestartDelay", "2000")
        self._set(svc, "Start", "SERVICE_AUTO_START")
        self._set(svc, "DisplayName", svc)
        self._set(svc, "Description", f"SSR Gateway ({gw.name}) — channel {gw.channel}")
        msg = (
            f"已通过 nssm 注册 Windows 服务 {svc}（开机自启，崩溃自动重启）。\n"
            f"日志： {log}\n"
            f"查看状态： nssm status {svc}    图形化管理： nssm edit {svc}"
        )
        if start:
            r = self._nssm(["start", svc])
            if r.returncode != 0:
                msg += f"\n[!] 启动失败：{_out(r)}"
            else:
                msg += "\n已立即启动。"
        return msg

    def uninstall(self, settings: Settings, name: str) -> str:
        svc = self._service(name)
        self._nssm(["stop", svc])
        res = self._nssm(["remove", svc, "confirm"])
        # Clean up artifacts left by older Windows backends (schtasks / pm2).
        for legacy in (settings.home / "gateways" / f"{name}.cmd",
                       settings.home / "gateways" / f"{name}.pm2.json"):
            if legacy.exists():
                legacy.unlink()
        return _out(res) or f"已移除 Windows 服务 {svc}。"

    def start(self, settings: Settings, name: str) -> str:
        res = self._nssm(["start", self._service(name)])
        return _out(res) or f"{self._service(name)} 已启动"

    def stop(self, settings: Settings, name: str) -> str:
        res = self._nssm(["stop", self._service(name)])
        return _out(res) or f"{self._service(name)} 已停止"

    def restart(self, settings: Settings, name: str) -> str:
        res = self._nssm(["restart", self._service(name)])
        return _out(res) or f"{self._service(name)} 已重启"

    def status(self, settings: Settings, name: str) -> str:
        res = self._nssm(["status", self._service(name)])
        # nssm may emit UTF-16 console text; strip embedded nulls before matching.
        out = _out(res)
        if res.returncode != 0 or not out:
            return "not installed"
        if "RUNNING" in out:
            return "running"
        if "STOPPED" in out:
            return "stopped"
        if "PAUSED" in out:
            return "paused"
        if "PENDING" in out:
            return "pending"
        return out or "unknown"

    def stats(self, settings: Settings, name: str) -> dict:
        st = _empty_stats()
        # nssm doesn't report pid/memory, so locate the python process actually
        # serving this gateway and sum its WorkingSetSize (bytes).
        procs = _query_gateway_processes(name)
        if procs:
            st["pid"] = procs[0][0]
            st["rss"] = sum(ws for _, ws in procs)
            st["running"] = True
            st["source"] = "WorkingSetSize"
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
        # Prefer nssm (a real Windows service); fall back to a Scheduled Task
        # when nssm is not installed so the gateway still works out of the box.
        nssm_mgr = NssmWindowsManager()
        mgr = nssm_mgr if nssm_mgr.available() else WindowsTaskManager()
    else:
        mgr = _NullManager()
    return mgr if mgr.available() else _NullManager()
