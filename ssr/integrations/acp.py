"""Agent Client Protocol (ACP) server.

Exposes the SSR agent over ACP (https://agentclientprotocol.com) so ACP-capable
clients (e.g. Zed) can drive it. Invoked via ``ssr --experimental-acp``.

This is a minimal newline-delimited JSON-RPC 2.0 implementation over stdio
covering the core handshake: ``initialize``, ``session/new``, ``session/prompt``
and ``session/cancel``, emitting ``session/update`` notifications for output.
"""

from __future__ import annotations

import json
import sys
import uuid

from ..config import Settings

PROTOCOL_VERSION = 1


class ACPServer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions: dict[str, object] = {}
        # Bind to the *real* stdout, then redirect the process-wide stdout to
        # stderr so any stray library prints (model2vec, genai, etc.) during a
        # session never corrupt the JSON-RPC stream.
        self._out = sys.stdout
        sys.stdout = sys.stderr

    # --- framing -----------------------------------------------------------
    def _send(self, obj: dict) -> None:
        self._out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._out.flush()

    def _result(self, rid, result) -> None:
        self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def _error(self, rid, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # --- handlers ----------------------------------------------------------
    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            # Echo a protocol version we support that is <= the client's, per spec.
            client_version = params.get("protocolVersion", PROTOCOL_VERSION)
            try:
                negotiated = min(int(client_version), PROTOCOL_VERSION)
            except (TypeError, ValueError):
                negotiated = PROTOCOL_VERSION
            self._result(
                rid,
                {
                    "protocolVersion": negotiated,
                    "agentCapabilities": {
                        "loadSession": False,
                        "promptCapabilities": {"image": True, "audio": True, "embeddedContext": True},
                    },
                    "authMethods": [],
                    "serverInfo": {"name": "ssr-agent", "version": "0.1.0"},
                },
            )
        elif method == "session/new":
            from ..agent.core import SSRAgent

            sid = str(uuid.uuid4())
            self.sessions[sid] = SSRAgent(self.settings)
            self._result(rid, {"sessionId": sid})
        elif method == "session/prompt":
            sid = params.get("sessionId")
            agent = self.sessions.get(sid)
            if agent is None:
                return self._error(rid, -32602, f"unknown session {sid}")
            parts = _extract_parts(params.get("prompt", []))
            reply = agent.run_parts(parts)  # type: ignore[attr-defined]
            self._notify(
                "session/update",
                {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": reply},
                    },
                },
            )
            self._result(rid, {"stopReason": "end_turn"})
        elif method == "session/cancel":
            self._result(rid, {})
        elif method is None:
            return  # response/ack we don't track
        else:
            if rid is not None:
                self._error(rid, -32601, f"method not found: {method}")

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self.handle(msg)
            except Exception as e:  # keep the loop alive
                if msg.get("id") is not None:
                    self._error(msg["id"], -32603, str(e))


def _extract_parts(prompt_blocks: list) -> list[dict]:
    """Normalise ACP prompt content blocks into agent input parts.

    Handles ACP ``text``, ``image`` and ``audio`` blocks (base64 ``data`` with a
    ``mimeType``), plus ``resource_link`` / embedded ``resource`` references.
    """
    import base64

    parts: list[dict] = []
    for block in prompt_blocks:
        if isinstance(block, str):
            parts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype in ("image", "audio"):
            data = block.get("data")
            try:
                raw = base64.b64decode(data) if data else None
            except Exception:
                raw = None
            if raw:
                parts.append(
                    {
                        "type": btype,
                        "mime_type": block.get("mimeType") or block.get("mime_type"),
                        "data": raw,
                    }
                )
        elif btype in ("resource_link", "resource"):
            res = block.get("resource") or {}
            uri = block.get("uri") or res.get("uri", "")
            if res.get("text"):  # embedded text resource
                parts.append({"type": "text", "text": res["text"]})
            elif uri:
                parts.append({"type": "text", "text": f"[resource] {uri}"})
    return parts


def run_acp(settings: Settings) -> None:
    ACPServer(settings).serve()
