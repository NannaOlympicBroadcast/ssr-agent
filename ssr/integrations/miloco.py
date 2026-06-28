"""Xiaomi **Miloco** integration — the Mi Home bridge for SSR.

This module replaces the legacy bundled ``miot`` MCP plugin. Instead of driving
the Mi Home cloud through a third-party MIoT MCP server, SSR now talks to a
locally running `Xiaomi Miloco <https://github.com/XiaoMi/xiaomi-miloco>`_
service — Xiaomi's official open-source "perceptive home" gateway. Miloco binds
the user's Mi account, enumerates devices / homes / family members, records
**activities** (events) and runs **automations**, exposing all of it over a
local HTTP API (its dashboard listens on ``http://127.0.0.1:1810`` by default).

What this gives SSR:

* **Device control & queries** — list/control Mi Home devices via Miloco.
* **Persistent context** — devices, family members, recent activities and
  automations are snapshotted to ``~/.ssr/miloco/`` and surfaced as one of the
  context-pool categories (see :func:`ssr.context_pool.loaders.load_miloco_context`).
* **Bus event source** — :class:`MilocoActivityBridge` polls Miloco activities
  and republishes each as a ``miloco.activity.<type>`` event on the SSR bus, so
  a bus handler agent can react to what happens at home (a person arrives, a
  sensor trips, a hazard is detected …).

Platform note
-------------
Miloco only runs natively on **macOS / Linux** (it needs camera streaming and a
POSIX networking stack). On **Windows** it must be run inside Docker — see
:func:`ensure_native_supported`. Accordingly the SSR gateway defaults to a Docker
backend on Windows (see :mod:`ssr.integrations.gateway`).

The HTTP endpoint paths are version-dependent, so they are configurable in
``~/.ssr/miloco.json`` (``endpoints`` map); the defaults below target a current
Miloco backend. Everything degrades gracefully when Miloco is not running.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Settings

logger = logging.getLogger("ssr.miloco")

DEFAULT_BASE_URL = "http://127.0.0.1:1810"

# Logical name -> HTTP path. ``{did}`` is substituted for device control. These
# mirror the current Miloco backend (``backend/miloco`` — all business routers
# are mounted under ``/api``; ``/health`` is the unauthenticated probe) and are
# overridable per-install in ``~/.ssr/miloco.json`` so a new backend version can
# be pointed at without a code change.
DEFAULT_ENDPOINTS: dict[str, str] = {
    "health": "/health",                               # GET, unauthenticated probe
    "bind_status": "/api/miot/status",                 # GET, Mi account bind state
    "devices": "/api/miot/device_list",                # GET, Mi Home devices
    "device_control": "/api/miot/devices/{did}/control",  # POST, control a device
    "device_status": "/api/miot/devices/{did}/status",    # GET, current properties
    "homes": "/api/miot/home",                         # GET, homes/rooms
    "scenes": "/api/miot/scenes",                       # GET, Mi Home scenes
    "members": "/api/identity/persons",                # GET, recognised family persons
    "activities": "/api/events",                       # GET, meaningful home events
    "automations": "/api/rules",                       # GET, Miloco automation rules
}


# --------------------------------------------------------------------- config
@dataclass
class MilocoConfig:
    enabled: bool = True
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""              # optional bearer token for the Miloco API
    timeout: float = 10.0
    poll_interval: float = 5.0     # activity-bridge poll cadence (seconds)
    activity_limit: int = 50       # max activities fetched per poll
    home_id: str = ""              # optional: restrict to one home
    bus_topic_prefix: str = "miloco.activity"
    endpoints: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ENDPOINTS))

    @classmethod
    def from_dict(cls, data: dict) -> "MilocoConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        cfg = cls(**{k: v for k, v in (data or {}).items() if k in known})
        # Merge user endpoint overrides onto the defaults so a partial map still
        # resolves every logical endpoint.
        merged = dict(DEFAULT_ENDPOINTS)
        merged.update(cfg.endpoints or {})
        cfg.endpoints = merged
        return cfg


def config_path(settings: Settings) -> Path:
    return settings.home / "miloco.json"


def data_dir(settings: Settings) -> Path:
    d = settings.home / "miloco"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_path(settings: Settings) -> Path:
    return data_dir(settings) / "snapshot.json"


def _bridge_state_path(settings: Settings) -> Path:
    return data_dir(settings) / "bridge-state.json"


def load_config(settings: Settings) -> MilocoConfig:
    p = config_path(settings)
    if not p.exists():
        # Environment overrides let a Docker deployment configure Miloco without
        # writing a file into the volume.
        env_url = os.environ.get("MILOCO_BASE_URL")
        cfg = MilocoConfig()
        if env_url:
            cfg.base_url = env_url
        if os.environ.get("MILOCO_API_KEY"):
            cfg.api_key = os.environ["MILOCO_API_KEY"]
        return cfg
    try:
        return MilocoConfig.from_dict(json.loads(p.read_text("utf-8")))
    except (OSError, json.JSONDecodeError):
        return MilocoConfig()


def save_config(settings: Settings, cfg: MilocoConfig) -> Path:
    p = config_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), "utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


# ------------------------------------------------------------- platform guard
class MilocoUnavailable(RuntimeError):
    """Raised when Miloco cannot be used (unsupported platform, deps, network)."""


def running_in_docker() -> bool:
    """Best-effort detection of running inside a Linux container."""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text("utf-8")
        return "docker" in cgroup or "containerd" in cgroup or "kubepods" in cgroup
    except OSError:
        return False


def is_native_supported() -> bool:
    """Whether Miloco can run natively on this host (macOS / Linux, not Windows).

    Inside a Linux container ``platform.system()`` is ``Linux`` even when the
    Docker host is Windows, so this naturally returns True there — which is
    exactly the supported "Windows → Docker" path.
    """
    return platform.system() != "Windows"


WINDOWS_DOCKER_HINT = (
    "Miloco 无法在 Windows 上原生运行（需要 POSIX 网络/摄像头串流）。\n"
    "请通过 Docker 运行 Miloco 与 ssr-miloco 网关：\n"
    "    docker compose up -d miloco ssr\n"
    "（Windows 上的 ssr 网关也默认以 Docker 方式运行，见 `ssr gateway install`）。"
)


def ensure_native_supported() -> None:
    """Raise :class:`MilocoUnavailable` on Windows (unless inside a container)."""
    if not is_native_supported() and not running_in_docker():
        raise MilocoUnavailable(WINDOWS_DOCKER_HINT)


# ------------------------------------------------------------------- client
class MilocoClient:
    """Thin synchronous HTTP client for the local Miloco service.

    Read methods degrade gracefully: on a connection error (Miloco not running)
    they return an empty list / ``None`` and log at debug level, rather than
    raising — so the agent keeps working when the home gateway is offline. The
    Windows platform guard, however, raises :class:`MilocoUnavailable` so the
    user is told to use Docker.
    """

    def __init__(self, cfg: MilocoConfig | None = None, *, settings: Settings | None = None):
        if cfg is None:
            if settings is None:
                raise ValueError("MilocoClient needs a config or settings")
            cfg = load_config(settings)
        self.cfg = cfg
        ensure_native_supported()

    # -- low level
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def _url(self, name: str, **fmt) -> str:
        path = self.cfg.endpoints.get(name, DEFAULT_ENDPOINTS.get(name, ""))
        if fmt:
            path = path.format(**fmt)
        return self.cfg.base_url.rstrip("/") + path

    def _request(self, method: str, name: str, *, params=None, json_body=None, raw=False, **fmt):
        import httpx

        url = self._url(name, **fmt)
        try:
            resp = httpx.request(
                method, url, params=params, json=json_body,
                headers=self._headers(), timeout=self.cfg.timeout,
            )
        except Exception as e:  # connection refused / DNS / timeout
            logger.debug("miloco %s %s failed: %s", method, url, e)
            return False if raw else None
        if raw:
            return resp.status_code < 400
        if resp.status_code >= 400:
            logger.debug("miloco %s %s -> HTTP %s: %s", method, url, resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return None

    # Keys under which Miloco's ``NormalResponse.data`` carries a list.
    _LIST_KEYS = (
        "events", "devices", "persons", "rules", "homes", "scenes", "entries",
        "items", "result", "results", "list", "data",
    )

    @classmethod
    def _as_list(cls, body) -> list:
        """Normalise a Miloco ``NormalResponse`` envelope to a list of records.

        Responses look like ``{"code":0,"message":"ok","data": {...}|[...]}}``;
        the payload list lives either directly in ``data`` or under a named key
        inside it (``data.events``, ``data.devices`` …).
        """
        if body is None:
            return []
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            # Unwrap the NormalResponse ``data`` envelope first.
            inner = body.get("data", body)
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict):
                for key in cls._LIST_KEYS:
                    v = inner.get(key)
                    if isinstance(v, list):
                        return v
                if inner and all(isinstance(v, dict) for v in inner.values()):
                    return list(inner.values())
        return []

    # -- high level
    def health(self) -> bool:
        """True if the Miloco service answers its ``/health`` probe."""
        return bool(self._request("GET", "health", raw=True))

    def bind_status(self) -> dict | None:
        """Mi-account bind status (``data`` of ``/api/miot/status``)."""
        body = self._request("GET", "bind_status")
        return body.get("data") if isinstance(body, dict) else None

    def devices(self) -> list[dict]:
        return self._as_list(self._request("GET", "devices"))

    def homes(self) -> list[dict]:
        return self._as_list(self._request("GET", "homes"))

    def scenes(self) -> list[dict]:
        return self._as_list(self._request("GET", "scenes"))

    def members(self) -> list[dict]:
        return self._as_list(self._request("GET", "members"))

    def automations(self) -> list[dict]:
        return self._as_list(self._request("GET", "automations"))

    def activities(self, since_ms: int | None = None, limit: int | None = None) -> list[dict]:
        """Recent meaningful home events (``/api/events``).

        ``since_ms`` is an inclusive Unix-millisecond lower bound (the API's own
        ``since`` semantics); results are returned newest-first.
        """
        params: dict = {"limit": min(limit or self.cfg.activity_limit, 200)}
        if since_ms:
            params["since"] = int(since_ms)
        return self._as_list(self._request("GET", "activities", params=params))

    def control_device(self, did: str, action: dict) -> dict | None:
        return self._request("POST", "device_control", json_body=action, did=did)


# --------------------------------------------------------- activity bus bridge
def _activity_id(act: dict) -> str:
    for key in ("id", "activity_id", "event_id", "uuid"):
        v = act.get(key)
        if v:
            return str(v)
    # Fall back to a content hash so de-dup still works without an explicit id.
    return str(act.get("timestamp") or act.get("time") or "") + "|" + str(act.get("type") or act.get("name") or "")


def _activity_type(act: dict) -> str:
    t = act.get("type") or act.get("event_type") or act.get("category") or act.get("name") or "event"
    return str(t).strip().replace(" ", "_").replace(".", "_") or "event"


PublishFn = Callable[[str, dict], None]


class MilocoActivityBridge:
    """Poll Miloco activities and republish each as an SSR bus event.

    ``publish(topic, payload)`` is supplied by the caller so the same bridge
    works against an in-process :class:`~ssr.bus.core.MessageBus` or a remote
    :class:`~ssr.bus.client.BusClient`. Events are de-duplicated by activity id
    (persisted across restarts in ``~/.ssr/miloco/bridge-state.json``) so a
    restart does not replay the whole backlog.
    """

    def __init__(
        self,
        settings: Settings,
        publish: PublishFn,
        cfg: MilocoConfig | None = None,
    ):
        self.settings = settings
        self.cfg = cfg or load_config(settings)
        self.client = MilocoClient(self.cfg)
        self._publish = publish
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ts: float = 0.0
        self._load_state()

    # -- de-dup state
    def _load_state(self) -> None:
        try:
            st = json.loads(_bridge_state_path(self.settings).read_text("utf-8"))
            self._seen = set(st.get("seen", [])[-1000:])
            self._last_ts = float(st.get("last_ts") or 0.0)
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            _bridge_state_path(self.settings).write_text(
                json.dumps({"seen": list(self._seen)[-1000:], "last_ts": self._last_ts}),
                "utf-8",
            )
        except OSError:
            pass

    # -- polling
    def poll_once(self) -> int:
        """Fetch activities and publish any not seen before. Returns new count.

        ``_last_ts`` is tracked in Unix milliseconds (Miloco's event timestamp
        unit) and used as the ``since`` lower bound so each poll only fetches the
        new tail rather than the whole backlog.
        """
        acts = self.client.activities(since_ms=int(self._last_ts) or None)
        published = 0
        for act in acts:
            aid = _activity_id(act)
            if aid in self._seen:
                continue
            self._seen.add(aid)
            ts = float(act.get("timestamp") or act.get("time") or 0.0) or time.time() * 1000
            self._last_ts = max(self._last_ts, ts)
            topic = f"{self.cfg.bus_topic_prefix}.{_activity_type(act)}"
            payload = {"activity": act, "activity_id": aid, "ts": ts}
            try:
                self._publish(topic, payload)
                published += 1
            except Exception:
                logger.exception("publishing miloco activity %s failed", aid)
        if published:
            self._save_state()
            logger.info("miloco bridge published %d new activity event(s)", published)
        return published

    def run_forever(self) -> None:
        logger.info(
            "miloco activity bridge polling %s every %.1fs (topic prefix %s.*)",
            self.cfg.base_url, self.cfg.poll_interval, self.cfg.bus_topic_prefix,
        )
        while not self._stop.is_set():
            try:
                self.poll_once()
            except MilocoUnavailable:
                raise
            except Exception:
                logger.exception("miloco bridge poll error (continuing)")
            self._stop.wait(self.cfg.poll_interval)

    def start(self) -> "MilocoActivityBridge":
        """Start polling in a daemon thread (non-blocking)."""
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, daemon=True, name="miloco-bridge")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


def start_bus_bridge(agent, settings: Settings | None = None) -> MilocoActivityBridge | None:
    """Attach a Miloco activity bridge to a running ``SSRAgent``'s bus.

    Returns the started bridge, or ``None`` when Miloco is disabled / unsupported
    on this platform (so the agent keeps running without a home gateway).
    """
    settings = settings or agent.settings
    cfg = load_config(settings)
    if not cfg.enabled:
        return None
    try:
        ensure_native_supported()
    except MilocoUnavailable as e:
        logger.info("miloco bridge not started: %s", e)
        return None

    def publish(topic: str, payload: dict) -> None:
        agent.bus.publish(topic, payload, source="miloco")

    bridge = MilocoActivityBridge(settings, publish, cfg)
    return bridge.start()


def start_bridge_via_busclient(settings: Settings) -> MilocoActivityBridge | None:
    """Run the activity bridge publishing to the (embedded/remote) bus server.

    Used by the gateway and the ``ssr miloco bridge`` CLI: it connects a
    :class:`~ssr.bus.client.BusClient` to the bus server (``SSR_BUS_URL`` or the
    embedded ``ws://host:port``) so every agent / bus-handler in the process tree
    receives ``miloco.activity.*`` events. Returns ``None`` (logging why) when
    Miloco is disabled, unsupported, or the bus is unreachable.
    """
    cfg = load_config(settings)
    if not cfg.enabled:
        return None
    try:
        ensure_native_supported()
    except MilocoUnavailable as e:
        logger.info("miloco bridge not started: %s", e)
        return None

    url = getattr(settings, "bus_url", None) or (
        f"ws://{getattr(settings, 'bus_host', '127.0.0.1')}:{getattr(settings, 'bus_port', 8765)}"
    )
    try:
        from ..bus import BusClient

        client = BusClient(url, source="miloco", api_key=getattr(settings, "bus_api_key", None)).connect()
    except Exception as e:
        logger.info("miloco bridge: bus connect to %s failed: %s", url, e)
        return None

    def publish(topic: str, payload: dict) -> None:
        client.publish(topic, payload)

    bridge = MilocoActivityBridge(settings, publish, cfg)
    return bridge.start()


# --------------------------------------------------------- context snapshot
def sync_snapshot(settings: Settings, cfg: MilocoConfig | None = None) -> dict:
    """Fetch devices / homes / members / activities / automations and cache them.

    Written to ``~/.ssr/miloco/snapshot.json`` and consumed by the context-pool
    loader. Returns the snapshot dict (with an ``error`` key if Miloco is
    unreachable, so callers can report it).
    """
    cfg = cfg or load_config(settings)
    snap: dict = {"synced_at": time.time(), "base_url": cfg.base_url}
    try:
        client = MilocoClient(cfg)
    except MilocoUnavailable as e:
        snap["error"] = str(e)
        return snap
    snap["homes"] = client.homes()
    snap["devices"] = client.devices()
    snap["members"] = client.members()
    snap["automations"] = client.automations()
    snap["activities"] = client.activities(limit=cfg.activity_limit)
    if not client.health() and not any(
        snap.get(k) for k in ("homes", "devices", "members", "automations", "activities")
    ):
        snap["error"] = f"Miloco 未响应（{cfg.base_url}）。请确认 Miloco 服务已启动。"
    try:
        snapshot_path(settings).write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), "utf-8"
        )
    except OSError:
        pass
    return snap


def load_snapshot(settings: Settings) -> dict:
    p = snapshot_path(settings)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
