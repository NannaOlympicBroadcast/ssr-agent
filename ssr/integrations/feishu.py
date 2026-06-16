"""Feishu (Lark) bot integration.

Two layers:

* Interactive configuration (``ssr feishu configure``) — stores the bot's default
  working directory, session id, app id (ak) and app secret (sk) into
  ``~/.ssr/feishu.json``.
* A scaffold for the Vercel Chat SDK official Lark adapter
  (https://chat-sdk.dev/adapters/vendor-official/lark) so inbound Feishu messages
  can trigger the agent. ``ssr feishu serve`` runs a minimal pure-python webhook
  receiver as a fallback when the Node app isn't deployed.
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


def serve_fallback(settings: Settings, host: str = "0.0.0.0", port: int = 8848) -> None:
    """Minimal pure-python webhook receiver (fallback to the Node adapter)."""
    import http.server
    import socketserver

    from ..agent.core import SSRAgent

    cfg = load_config(settings)
    if cfg is None:
        raise SystemExit("Feishu not configured. Run: ssr feishu configure")
    settings.project_dir = Path(cfg.default_cwd).expanduser()
    agent = SSRAgent(settings)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if "challenge" in payload:  # Feishu URL verification
                return self._json({"challenge": payload["challenge"]})
            text = (
                payload.get("event", {})
                .get("message", {})
                .get("content", "")
            )
            reply = agent.run(text or json.dumps(payload))
            self._json({"reply": reply})

        def _json(self, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # quiet
            pass

    with socketserver.TCPServer((host, port), Handler) as httpd:
        print(f"SSR Feishu webhook listening on http://{host}:{port}")
        httpd.serve_forever()
