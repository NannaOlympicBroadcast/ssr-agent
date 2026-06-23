"""Feishu channel integration using Lark WS client."""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from ssr.config import Settings
from ssr.channels.base import AbstractChannel
from ssr.integrations.feishu import FeishuConfig, load_config, save_config, _import_lark, _extract_text, _build_parts
from ssr.channels.message_parser import parse_and_process_message

logger = logging.getLogger(__name__)

class FeishuChannel(AbstractChannel):
    name = "feishu"

    def __init__(self):
        self.api = None

    def configure(self, settings: Settings) -> None:
        from ssr.integrations.feishu import configure_interactive
        configure_interactive(settings)

    def serve(self, settings: Settings) -> None:
        lark = _import_lark()
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        from ssr.agent.core import SSRAgent
        from ssr.channels.slash_im import handle_slash_im
        import threading

        cfg = load_config(settings)
        if cfg is None or not cfg.app_id or not cfg.app_secret:
            raise SystemExit("Feishu not configured. Run: ssr channel config feishu")

        # default_cwd is only a *starting* directory, not a hard restriction:
        # the agent may switch with /project and run commands elsewhere.
        if cfg.default_cwd:
            settings.project_dir = Path(cfg.default_cwd).expanduser()
        self.api = lark.Client.builder().app_id(cfg.app_id).app_secret(cfg.app_secret).build()
        agent = SSRAgent(settings)

        def send_reply(chat_id: str, text: str) -> None:
            clean_text = parse_and_process_message(self, chat_id, text)
            if clean_text:
                self.send_message(chat_id, clean_text)

        def fetch_resource(message_id: str, file_key: str, rtype: str):
            try:
                req = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(file_key)
                    .type(rtype)
                    .build()
                )
                resp = self.api.im.v1.message_resource.get(req)
                if not resp.success():
                    return None
                data = getattr(resp, "file", None)
                if data is None:
                    return getattr(resp, "raw", None) and resp.raw.content
                return data.read() if hasattr(data, "read") else bytes(data)
            except Exception:
                return None

        # Bind approval handler to IM
        from ssr.approval import IMApprovalHandler
        agent.toolkit.approval_handler = IMApprovalHandler(
            send_fn=lambda target, text: send_reply(target, text)
        )

        seen_ids = set()
        lock = threading.Lock()

        def on_message(data: P2ImMessageReceiveV1) -> None:
            msg = data.event.message
            message_id = msg.message_id
            with lock:
                if message_id in seen_ids:
                    return
                seen_ids.add(message_id)

            text = _extract_text(msg.content)
            if text.startswith("/"):
                try:
                    handle_slash_im(text, agent, settings, msg.chat_id, send_reply)
                except Exception as e:
                    send_reply(msg.chat_id, f"Error executing slash command: {e}")
                return

            parts = _build_parts(msg, fetch_resource)
            if not parts:
                return

            def worker():
                try:
                    agent.active_im_context = ("feishu", msg.chat_id)
                    reply = agent.run_parts(parts)
                except Exception as e:
                    reply = f"[ssr error] {e}"
                send_reply(msg.chat_id, reply)

            threading.Thread(target=worker, daemon=True).start()

        def on_message_read(data) -> None:
            # Read receipts (im.message.message_read_v1) need no action; register a
            # no-op so the dispatcher doesn't log "processor not found".
            return None

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_im_message_message_read_v1(on_message_read)
            .build()
        )

        ws = lark.ws.Client(
            cfg.app_id,
            cfg.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        print(f"SSR Feishu channel serving... (cwd={cfg.default_cwd})")
        ws.start()

    def send_message(self, target: str, text: str) -> None:
        lark = _import_lark()
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        if not self.api:
            logger.error("[feishu] client not initialized")
            return
            
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(target)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.api.im.v1.message.create(req)
        if not resp.success():
            logger.error(f"[feishu] send failed: {resp.code} {resp.msg}")

    def send_file(self, target: str, path: str, mime_type: str) -> None:
        lark = _import_lark()
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        if not self.api:
            logger.error("[feishu] client not initialized")
            return
            
        file_path = Path(path)
        if not file_path.exists():
            logger.error(f"[feishu] file not found: {path}")
            return

        try:
            with open(file_path, "rb") as f:
                req = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type("stream")
                        .file_name(file_path.name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                resp = self.api.im.v1.file.create(req)
            if not resp.success():
                logger.error(f"[feishu] upload file failed: {resp.code} {resp.msg}")
                return
            
            file_key = json.loads(resp.raw.content.decode("utf-8"))["data"]["file_key"]
            
            msg_req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(target)
                    .msg_type("file")
                    .content(json.dumps({"file_key": file_key}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            self.api.im.v1.message.create(msg_req)
        except Exception as e:
            logger.error(f"[feishu] send file exception: {e}")

    def send_image(self, target: str, path_or_bytes: str | bytes) -> None:
        lark = _import_lark()
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        if not self.api:
            logger.error("[feishu] client not initialized")
            return
            
        try:
            import io
            if isinstance(path_or_bytes, bytes):
                img_file = io.BytesIO(path_or_bytes)
            else:
                img_path = Path(path_or_bytes)
                if not img_path.exists():
                    logger.error(f"[feishu] image not found: {path_or_bytes}")
                    return
                img_file = open(img_path, "rb")
                
            try:
                req = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(img_file)
                        .build()
                    )
                    .build()
                )
                resp = self.api.im.v1.image.create(req)
            finally:
                if not isinstance(path_or_bytes, bytes):
                    img_file.close()

            if not resp.success():
                logger.error(f"[feishu] upload image failed: {resp.code} {resp.msg}")
                return
                
            image_key = json.loads(resp.raw.content.decode("utf-8"))["data"]["image_key"]
            
            msg_req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(target)
                    .msg_type("image")
                    .content(json.dumps({"image_key": image_key}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            self.api.im.v1.message.create(msg_req)
        except Exception as e:
            logger.error(f"[feishu] send image exception: {e}")
