"""Feishu (Lark) bot integration.

Two layers:

* Interactive configuration (``ssr feishu configure``) — stores the bot's default
  working directory, session id, app id (ak) and app secret (sk) into
  ``~/.ssr/feishu.json``.
* A **WebSocket long-connection** event client (``ssr feishu serve``). Using the
  official ``lark-oapi`` SDK's ``lark.ws.Client``, SSR opens an *outbound*
  WebSocket to Feishu's servers and receives ``im.message.receive_v1`` events —
  no public webhook URL or inbound HTTP endpoint is required. This matches the
  transport used by the Vercel Chat SDK official Lark adapter (WS-only). To use
  it, set the app's event subscription method to "长连接 / persistent connection"
  in the Feishu Developer Console.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config import Settings

_INSTALL_HINT = (
    "Feishu long-connection support needs the lark-oapi SDK (now a core "
    "dependency of ssr-agent), but it could not be imported.\n"
    "  Reinstall ssr-agent:  pip install -e .   (or: pip install --upgrade ssr-agent)\n"
    "  Or install it directly:  pip install lark-oapi"
)


def _import_lark():
    """Import lark-oapi, raising a friendly SystemExit if it is missing.

    Distinguishes "lark-oapi itself is absent" from "lark-oapi is installed but
    one of its dependencies is missing" / a different interpreter, so the user
    gets the real cause instead of a misleading 'not installed' message.
    """
    import sys

    try:
        import lark_oapi as lark  # noqa: F401

        return lark
    except ModuleNotFoundError as e:
        missing = getattr(e, "name", "") or ""
        if missing == "lark_oapi" or missing.startswith("lark_oapi"):
            raise SystemExit(
                f"{_INSTALL_HINT}\n\n"
                f"  Active interpreter: {sys.executable}\n"
                "  If you already installed it, you likely installed into a "
                "different environment than the one running `ssr`.\n"
                f"  Install into THIS interpreter:  {sys.executable} -m pip install lark-oapi"
            )
        # lark_oapi is present but a transitive dependency failed to import.
        raise SystemExit(
            f"lark-oapi is installed, but importing it failed: missing module "
            f"'{missing}'.\n  Reinstall its dependencies:  "
            f"{sys.executable} -m pip install --upgrade --force-reinstall lark-oapi\n"
            f"  Original error: {e}"
        )


@dataclass
class FeishuConfig:
    default_cwd: str
    session_id: str
    app_id: str       # ak
    app_secret: str   # sk
    verification_token: str = ""
    encrypt_key: str = ""


def config_path(settings: Settings) -> Path:
    return settings.feishu_config


def load_config(settings: Settings) -> FeishuConfig | None:
    p = config_path(settings)
    if not p.exists():
        return None
    try:
        return FeishuConfig(**json.loads(p.read_text("utf-8")))
    except Exception:
        return None


def save_config(settings: Settings, cfg: FeishuConfig) -> Path:
    p = config_path(settings)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), "utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def configure_interactive(settings: Settings) -> Path:
    """Prompt the user for Feishu bot settings and persist them."""
    existing = load_config(settings)

    def ask(label: str, current: str = "", secret: bool = False) -> str:
        suffix = f" [{'•' * 6 if secret and current else current}]" if current else ""
        val = input(f"{label}{suffix}: ").strip()
        return val or current

    print("\n配置飞书机器人 (Feishu / Lark bot)\n" + "-" * 36)
    cfg = FeishuConfig(
        default_cwd=ask("默认运行目录 default working dir", existing.default_cwd if existing else str(settings.project_dir)),
        session_id=ask("会话 ID session id", existing.session_id if existing else ""),
        app_id=ask("App ID (ak)", existing.app_id if existing else ""),
        app_secret=ask("App Secret (sk)", existing.app_secret if existing else "", secret=True),
        verification_token=ask("Verification token (可选)", existing.verification_token if existing else ""),
        encrypt_key=ask("Encrypt key (可选)", existing.encrypt_key if existing else ""),
    )
    path = save_config(settings, cfg)
    _write_node_adapter(settings)
    print(f"\n已保存到 {path}")
    print(f"Node 适配器脚手架: {settings.home / 'feishu-adapter'}")
    return path


def _write_node_adapter(settings: Settings) -> None:
    """Emit a minimal Vercel Chat SDK Lark adapter scaffold."""
    root = settings.home / "feishu-adapter"
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "ssr-feishu-adapter",
                "private": True,
                "type": "module",
                "dependencies": {
                    "@ai-sdk/lark": "latest",
                    "ai": "latest",
                },
                "scripts": {"start": "node server.js"},
            },
            indent=2,
        ),
        "utf-8",
    )
    (root / "server.js").write_text(_NODE_ADAPTER_TEMPLATE, "utf-8")


_NODE_ADAPTER_TEMPLATE = """\
// SSR Agent — Feishu (Lark) adapter using the Vercel Chat SDK official Lark adapter.
// https://chat-sdk.dev/adapters/vendor-official/lark
//
// On an inbound message it shells out to `ssr task-run` so the Python agent
// handles the request in the configured working directory.
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const cfg = JSON.parse(readFileSync(join(homedir(), '.ssr', 'feishu.json'), 'utf-8'));

export function handleMessage(text) {
  const res = spawnSync('ssr', ['ask', '--cwd', cfg.default_cwd, text], { encoding: 'utf-8' });
  return res.stdout || res.stderr;
}

// Wire `handleMessage` into the Lark adapter's onMessage callback. See the
// chat-sdk Lark adapter docs for the deployment wrapper.
console.log('SSR Feishu adapter ready. session:', cfg.session_id);
"""


def _extract_text(content: str) -> str:
    """Pull plain text out of a Feishu message ``content`` JSON string."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content or ""
    if "text" in data:  # text message
        return data["text"]
    # rich text / post: flatten any nested "text" fields
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                chunks.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return " ".join(chunks).strip() or content


def _build_parts(msg, fetch_resource) -> list[dict]:
    """Turn a Feishu message into agent input parts (text / image / audio).

    ``fetch_resource(message_id, file_key, rtype)`` downloads media bytes, where
    ``rtype`` is "image" or "file"; it may be None when downloads are unavailable.
    """
    mtype = getattr(msg, "message_type", "text")
    try:
        content = json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        content = {}

    if mtype == "image":
        key = content.get("image_key")
        raw = fetch_resource(msg.message_id, key, "image") if (fetch_resource and key) else None
        if raw:
            return [{"type": "image", "mime_type": "image/png", "data": raw}]
        return [{"type": "text", "text": "[image received but could not be downloaded]"}]

    if mtype in ("audio", "media", "file"):
        key = content.get("file_key")
        raw = fetch_resource(msg.message_id, key, "file") if (fetch_resource and key) else None
        if raw:
            kind = "audio" if mtype == "audio" else "image"  # 'media' often a video; treat unknown as audio
            mime = "audio/ogg" if mtype == "audio" else (content.get("file_name", "") or "application/octet-stream")
            if mtype == "audio":
                return [{"type": "audio", "mime_type": "audio/ogg", "data": raw}]
            # non-audio files: hand the agent a note (it can't ingest arbitrary bytes)
        return [{"type": "text", "text": f"[{mtype} message received]"}]

    return [{"type": "text", "text": _extract_text(msg.content)}]


def build_event_handler(settings: Settings, send_reply, fetch_resource=None):
    """Build a lark EventDispatcherHandler bound to the SSR agent.

    ``send_reply(message_id, chat_id, text)`` posts the agent's answer back.
    ``fetch_resource(message_id, file_key, rtype)`` optionally downloads image /
    audio bytes so the multimodal agent can see them. Each inbound message runs
    the agent in a worker thread so the long connection's receive loop stays
    responsive.
    """
    import threading

    lark = _import_lark()
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    from ..agent.core import SSRAgent

    cfg = load_config(settings)
    if cfg is None:
        raise SystemExit("Feishu not configured. Run: ssr feishu configure")
    settings.project_dir = Path(cfg.default_cwd).expanduser()
    agent = SSRAgent(settings)
    seen_ids: set[str] = set()
    lock = threading.Lock()

    def on_message(data: P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        message_id = msg.message_id
        with lock:
            if message_id in seen_ids:  # Feishu may redeliver; dedupe.
                return
            seen_ids.add(message_id)
        parts = _build_parts(msg, fetch_resource)
        if not parts:
            return

        def worker():
            try:
                reply = agent.run_parts(parts)
            except Exception as e:  # never crash the connection
                reply = f"[ssr error] {e}"
            send_reply(message_id, msg.chat_id, reply)

        threading.Thread(target=worker, daemon=True).start()

    def on_message_read(data) -> None:
        # Read receipts (im.message.message_read_v1) carry no actionable payload;
        # register a no-op so the dispatcher doesn't log "processor not found".
        return None

    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_im_message_message_read_v1(on_message_read)
        .build()
    )


def serve_long_connection(settings: Settings) -> None:
    """Run the Feishu bot over a WebSocket long connection (no webhook URL)."""
    import json as _json

    lark = _import_lark()
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    cfg = load_config(settings)
    if cfg is None:
        raise SystemExit("Feishu not configured. Run: ssr feishu configure")
    if not cfg.app_id or not cfg.app_secret:
        raise SystemExit("Feishu app_id / app_secret missing. Run: ssr feishu configure")

    # API client used to send replies back to the chat.
    api = lark.Client.builder().app_id(cfg.app_id).app_secret(cfg.app_secret).build()

    def send_reply(message_id: str, chat_id: str, text: str) -> None:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(_json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = api.im.v1.message.create(req)
        if not resp.success():
            print(f"[feishu] send failed: {resp.code} {resp.msg}")

    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    def fetch_resource(message_id: str, file_key: str, rtype: str):
        """Download image/audio bytes attached to a message."""
        try:
            req = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(rtype)
                .build()
            )
            resp = api.im.v1.message_resource.get(req)
            if not resp.success():
                print(f"[feishu] resource download failed: {resp.code} {resp.msg}")
                return None
            data = getattr(resp, "file", None)
            if data is None:
                return getattr(resp, "raw", None) and resp.raw.content
            return data.read() if hasattr(data, "read") else bytes(data)
        except Exception as e:
            print(f"[feishu] resource download error: {e}")
            return None

    handler = build_event_handler(settings, send_reply, fetch_resource=fetch_resource)

    ws = lark.ws.Client(
        cfg.app_id,
        cfg.app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    print(
        f"SSR Feishu long-connection starting (cwd={cfg.default_cwd}). "
        "Set event subscription to 长连接/persistent connection in the console."
    )
    ws.start()  # opens the outbound WebSocket and blocks, auto-reconnecting
