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


def build_event_handler(settings: Settings, send_reply):
    """Build a lark EventDispatcherHandler bound to the SSR agent.

    ``send_reply(message_id, chat_id, text)`` posts the agent's answer back.
    Each inbound message runs the agent in a worker thread so the long
    connection's receive loop stays responsive.
    """
    import threading

    import lark_oapi as lark
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
        text = _extract_text(msg.content)
        if not text.strip():
            return

        def worker():
            try:
                reply = agent.run(text)
            except Exception as e:  # never crash the connection
                reply = f"[ssr error] {e}"
            send_reply(message_id, msg.chat_id, reply)

        threading.Thread(target=worker, daemon=True).start()

    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )


def serve_long_connection(settings: Settings) -> None:
    """Run the Feishu bot over a WebSocket long connection (no webhook URL)."""
    import json as _json

    import lark_oapi as lark
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

    handler = build_event_handler(settings, send_reply)

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
