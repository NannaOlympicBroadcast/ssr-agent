"""WeChat channel integration using iLink Bot API."""

from __future__ import annotations

import json
import logging
import time
import httpx
from pathlib import Path
from ssr.config import Settings
from ssr.channels.base import AbstractChannel
from ssr.agent.core import SSRAgent
from ssr.channels.slash_im import handle_slash_im
from ssr.channels.message_parser import parse_and_process_message
import threading

logger = logging.getLogger(__name__)

class WeChatChannel(AbstractChannel):
    name = "wechat"

    def __init__(self):
        self.bot_token = ""
        self.uin = ""
        self.get_updates_buf = ""
        self.default_cwd = ""
        self.config_file = None

    def configure(self, settings: Settings) -> None:
        self.config_file = settings.home / "wechat.json"
        print("\n配置微信机器人 (WeChat iLink Bot)\n" + "-" * 36)
        
        confirm = input("是否立即扫码登录微信机器人? (Y/n): ").strip().lower()
        if confirm != "n":
            self.run_login_flow(settings)
        else:
            default_cwd = input(f"输入默认运行目录 [{settings.project_dir}]: ").strip() or str(settings.project_dir)
            cfg = {
                "bot_token": "",
                "uin": "",
                "get_updates_buf": "",
                "default_cwd": default_cwd
            }
            self.config_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"配置已保存到 {self.config_file} (未登录)")

    def run_login_flow(self, settings: Settings):
        self.config_file = settings.home / "wechat.json"
        default_cwd = input(f"输入默认运行目录 [{settings.project_dir}]: ").strip() or str(settings.project_dir)
        
        print("\n正在向微信 iLink 服务器请求登录二维码...")
        headers = {
            "Content-Type": "application/json",
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": "1"
        }
        try:
            resp = httpx.get(
                "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode",
                params={"bot_type": 3},
                headers=headers,
                timeout=15.0
            )
            data = resp.json()
            ret_code = data.get("ret") or data.get("errcode") or 0
            if ret_code != 0:
                print(f"获取二维码失败: {data.get('errmsg', '未知错误')} (错误码: {ret_code})")
                return
                
            qrcode = data.get("qrcode")
            qrcode_url = data.get("qrcode_img_content") or data.get("qrcode_url") or f"https://open.weixin.qq.com/connect/confirm?uuid={qrcode}"
            
            print(f"\n请在浏览器中打开此链接扫描二维码进行登录:\n{qrcode_url}\n")
            
            print("等待扫码确认...")
            while True:
                try:
                    status_resp = httpx.get(
                        "https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status",
                        params={"qrcode": qrcode},
                        headers=headers,
                        timeout=15.0
                    )
                    status_data = status_resp.json()
                except httpx.TimeoutException:
                    continue
                except httpx.RequestError:
                    time.sleep(2)
                    continue
                
                ret_code = status_data.get("ret") or status_data.get("errcode") or 0
                if ret_code != 0:
                    errmsg = status_data.get("errmsg", "未知错误")
                    if ret_code == -14:
                        print(f"\n二维码已过期 (错误码: {ret_code})，请重新运行。")
                    else:
                        print(f"\n登录失败: {errmsg} (错误码: {ret_code})")
                    break

                status = status_data.get("status")
                if isinstance(status, str):
                    status = status.lower()

                if status in (1, "wait", "timeout"):
                    time.sleep(2)
                elif status in (2, "scaned", "scanned"):
                    print("已扫码，等待在手机上确认...")
                    time.sleep(2)
                elif status in (3, "confirmed"):
                    self.bot_token = status_data.get("bot_token")
                    self.uin = status_data.get("uin")
                    self.default_cwd = default_cwd
                    self.get_updates_buf = ""
                    
                    cfg = {
                        "bot_token": self.bot_token,
                        "uin": self.uin,
                        "get_updates_buf": self.get_updates_buf,
                        "default_cwd": self.default_cwd
                    }
                    self.config_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
                    print("\n登录成功！配置已保存。")
                    break
                elif status in ("expired", "expire"):
                    print("\n二维码已过期，请重新运行。")
                    break
                elif status in ("canceled", "cancel"):
                    print("\n登录已取消。")
                    break
                else:
                    print(f"\n登录失败，未知状态: {status}，请重新运行。")
                    break
        except KeyboardInterrupt:
            print("\n登录已取消。")
        except Exception as e:
            print(f"登录过程中发生错误: {e}")

    def load_config(self, settings: Settings) -> bool:
        self.config_file = settings.home / "wechat.json"
        if not self.config_file.exists():
            return False
        try:
            cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            self.bot_token = cfg.get("bot_token", "")
            self.uin = cfg.get("uin", "")
            self.get_updates_buf = cfg.get("get_updates_buf", "")
            self.default_cwd = cfg.get("default_cwd", "")
            return bool(self.bot_token)
        except Exception:
            return False

    def save_state(self) -> None:
        if self.config_file:
            try:
                cfg = {
                    "bot_token": self.bot_token,
                    "uin": self.uin,
                    "get_updates_buf": self.get_updates_buf,
                    "default_cwd": self.default_cwd
                }
                self.config_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    def _get_headers(self) -> dict[str, str]:
        import random
        import base64
        val = random.randint(0, 4294967295)
        wechat_uin = base64.b64encode(str(val).encode("utf-8")).decode("utf-8")
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.bot_token}",
            "X-WECHAT-UIN": wechat_uin,
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": "1"
        }

    def serve(self, settings: Settings) -> None:
        if not self.load_config(settings):
            raise SystemExit("微信机器人未配置或未登录。请先运行: ssr channel config wechat")

        settings.project_dir = Path(self.default_cwd).expanduser()
        agent = SSRAgent(settings)

        def send_reply(target_user: str, text: str) -> None:
            # 修改点：在此处增加 XML 标签解析
            clean_text = parse_and_process_message(self, target_user, text)
            if clean_text:
                if ":" in target_user:
                    to_user_id, context_token = target_user.split(":", 1)
                else:
                    to_user_id = target_user
                    context_token = ""
                self._send_raw_message(to_user_id, clean_text, context_token)

        from ssr.approval import IMApprovalHandler
        agent.toolkit.approval_handler = IMApprovalHandler(
            send_fn=send_reply
        )

        print(f"SSR WeChat channel serving... (cwd={self.default_cwd})")

        while True:
            try:
                payload = {
                    "get_updates_buf": self.get_updates_buf,
                    "base_info": {
                        "channel_version": "1.0.0"
                    }
                }
                resp = httpx.post(
                    "https://ilinkai.weixin.qq.com/ilink/bot/getupdates",
                    json=payload,
                    headers=self._get_headers(),
                    timeout=45.0
                )
                if resp.status_code == 401:
                    print("\n微信登录凭证已过期，请重新运行: ssr channel config wechat 进行登录。")
                    break
                    
                data = resp.json()
                errcode = data.get("errcode", 0)
                errmsg = data.get("errmsg", "")
                if errcode != 0:
                    if errcode == -14 or "session timeout" in errmsg.lower():
                        print(f"\n微信登录凭证已过期 (错误码: {errcode})，请重新运行: ssr channel config wechat 进行登录。")
                        break
                    logger.error(f"[wechat] getupdates error: {errmsg} (错误码: {errcode})")
                    time.sleep(5)
                    continue
                    
                self.get_updates_buf = data.get("get_updates_buf", "")
                self.save_state()
                
                msgs = data.get("msgs", [])
                for msg in msgs:
                    to_user_id = msg.get("from_user_id")
                    context_token = msg.get("context_token")
                    target_repr = f"{to_user_id}:{context_token}"
                    
                    items = msg.get("item_list", [])
                    downloaded_parts = []
                    image_error = None
                    
                    for item in items:
                        itype = item.get("type")
                        if itype == 1:
                            text_val = item.get("text_item", {}).get("text", "")
                            if text_val:
                                downloaded_parts.append({"type": "text", "text": text_val})
                        elif itype == 2:
                            try:
                                image_item = item.get("image_item", {})
                                media = image_item.get("media", {})
                                url = image_item.get("url")
                                if not url and media:
                                    param = media.get("encrypt_query_param")
                                    if param:
                                        from urllib.parse import quote
                                        url = f"https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param={quote(param)}"
                                
                                if not url:
                                    raise ValueError("图片下载URL缺失且无CDNMedia参数")
                                    
                                # Download the encrypted bytes
                                download_resp = httpx.get(url, timeout=25.0)
                                if download_resp.status_code != 200:
                                    raise ValueError(f"下载失败 (HTTP status: {download_resp.status_code})")
                                encrypted_bytes = download_resp.content
                                
                                # Decode the AES key
                                media_aes_key = media.get("aes_key")
                                item_aes_key = image_item.get("aeskey")
                                aes_key = None
                                
                                if item_aes_key and len(item_aes_key) == 32:
                                    try:
                                        aes_key = bytes.fromhex(item_aes_key)
                                    except Exception:
                                        pass
                                        
                                if not aes_key and media_aes_key:
                                    try:
                                        import base64
                                        decoded = base64.b64decode(media_aes_key)
                                        if len(decoded) == 16:
                                            aes_key = decoded
                                        elif len(decoded) == 32:
                                            aes_key = bytes.fromhex(decoded.decode("utf-8", errors="ignore"))
                                    except Exception:
                                        pass
                                
                                if not aes_key:
                                    if encrypted_bytes.startswith(b"\x89PNG") or encrypted_bytes.startswith(b"\xff\xd8\xff"):
                                        downloaded_parts.append({
                                            "type": "image",
                                            "mime_type": "image/png",
                                            "data": encrypted_bytes
                                        })
                                        continue
                                    raise ValueError("缺少有效的 AES 解密 Key")
                                
                                # Decrypt the bytes
                                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                                from cryptography.hazmat.primitives import padding
                                
                                cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
                                decryptor = cipher.decryptor()
                                padded_data = decryptor.update(encrypted_bytes) + decryptor.finalize()
                                
                                unpadder = padding.PKCS7(128).unpadder()
                                plain_bytes = unpadder.update(padded_data) + unpadder.finalize()
                                
                                downloaded_parts.append({
                                    "type": "image",
                                    "mime_type": "image/png",
                                    "data": plain_bytes
                                })
                            except Exception as e:
                                image_error = e
                                break
                        elif itype == 3: # VOICE
                            voice_item = item.get("voice_item", {})
                            text = voice_item.get("text", "")
                            if text:
                                downloaded_parts.append({"type": "text", "text": f"[语音转文字: {text}]"})
                        elif itype == 4: # FILE
                            file_item = item.get("file_item", {})
                            file_name = file_item.get("file_name", "unknown_file")
                            media = file_item.get("media", {})
                            
                            file_downloaded = False
                            try:
                                url = file_item.get("url")
                                if not url and media:
                                    param = media.get("encrypt_query_param")
                                    if param:
                                        from urllib.parse import quote
                                        url = f"https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param={quote(param)}"
                                
                                if url:
                                    download_resp = httpx.get(url, timeout=45.0)
                                    if download_resp.status_code == 200:
                                        encrypted_bytes = download_resp.content
                                        
                                        # Decode AES key
                                        media_aes_key = media.get("aes_key")
                                        item_aes_key = file_item.get("aeskey")
                                        aes_key = None
                                        
                                        if item_aes_key and len(item_aes_key) == 32:
                                            try:
                                                aes_key = bytes.fromhex(item_aes_key)
                                            except Exception:
                                                pass
                                                
                                        if not aes_key and media_aes_key:
                                            try:
                                                import base64
                                                decoded = base64.b64decode(media_aes_key)
                                                if len(decoded) == 16:
                                                    aes_key = decoded
                                                elif len(decoded) == 32:
                                                    aes_key = bytes.fromhex(decoded.decode("utf-8", errors="ignore"))
                                            except Exception:
                                                pass
                                                
                                        if aes_key:
                                            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                                            from cryptography.hazmat.primitives import padding
                                            
                                            cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
                                            decryptor = cipher.decryptor()
                                            padded_data = decryptor.update(encrypted_bytes) + decryptor.finalize()
                                            
                                            unpadder = padding.PKCS7(128).unpadder()
                                            plain_bytes = unpadder.update(padded_data) + unpadder.finalize()
                                            
                                            # Save to project directory
                                            dest_path = settings.project_dir / file_name
                                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                                            dest_path.write_bytes(plain_bytes)
                                            downloaded_parts.append({
                                                "type": "text", 
                                                "text": f"[用户上传了文件并在本地保存为: {dest_path.absolute()}]"
                                            })
                                            file_downloaded = True
                            except Exception as e:
                                logger.error(f"[wechat] failed to download/decrypt file {file_name}: {e}")
                                
                            if not file_downloaded:
                                downloaded_parts.append({"type": "text", "text": f"[文件接收: {file_name}]"})
    
                                
                    if image_error:
                        send_reply(target_repr, f"无法接收图片消息: 图片下载或解密失败: {image_error}，请重试。")
                        continue
                        
                    if not downloaded_parts:
                        continue

                    # Check for slash commands in first text part
                    first_text = ""
                    for p in downloaded_parts:
                        if p["type"] == "text":
                            first_text = p["text"]
                            break
                            
                    if first_text.startswith("/"):
                        try:
                            handle_slash_im(first_text, agent, settings, target_repr, send_reply)
                        except Exception as e:
                            send_reply(target_repr, f"Error executing slash command: {e}")
                        continue

                    def worker(target_user=target_repr, parts_list=downloaded_parts):
                        try:
                            self.send_typing(target_user.split(":")[0])
                            agent.active_im_context = ("wechat", target_user)
                            reply = agent.run_parts(parts_list)
                            self.stop_typing(target_user.split(":")[0])
                        except Exception as e:
                            reply = f"[ssr error] {e}"
                        send_reply(target_user, reply)
                        
                    threading.Thread(target=worker, daemon=True).start()

            except httpx.ReadTimeout:
                continue
            except Exception as e:
                logger.error(f"[wechat] polling error: {e}")
                time.sleep(5)

    def _send_raw_message(self, to_user_id: str, text: str, context_token: str) -> None:
        import uuid
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": str(uuid.uuid4()),
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [
                    {
                        "type": 1,
                        "text_item": {"text": text}
                    }
                ]
            },
            "base_info": {
                "channel_version": "1.0.0"
            }
        }
        logger.info(f"[wechat] sending raw message payload: {json.dumps(payload, ensure_ascii=False)}")
        try:
            resp = httpx.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
                json=payload,
                headers=self._get_headers(),
                timeout=15.0
            )
            data = resp.json()
            logger.info(f"[wechat] sendmessage raw response: {json.dumps(data, ensure_ascii=False)}")
            ret_code = data.get("ret") or data.get("errcode") or 0
            if ret_code != 0:
                logger.error(f"[wechat] sendmessage failed: {data.get('errmsg', '未知错误')} (错误码: {ret_code})")
        except Exception as e:
            logger.error(f"[wechat] sendmessage exception: {e}")

    def send_message(self, target: str, text: str) -> None:
        # 这里无需额外处理，因为所有消息都通过 send_reply 走过了 parser
        if ":" in target:
            to_user, ctx = target.split(":", 1)
        else:
            to_user, ctx = target, ""
        self._send_raw_message(to_user, text, ctx)

    def send_typing(self, target: str) -> None:
        pass

    def stop_typing(self, target: str) -> None:
        pass

    def _upload_media(self, to_user_id: str, path_or_bytes: str | bytes, media_type: int) -> tuple[str, bytes, str, int, int]:
        import os
        import uuid
        import hashlib
        import base64
        from urllib.parse import quote
        
        if isinstance(path_or_bytes, bytes):
            plaintext = path_or_bytes
        else:
            p = Path(path_or_bytes)
            plaintext = p.read_bytes()
            
        raw_size = len(plaintext)
        raw_md5 = hashlib.md5(plaintext).hexdigest()
        
        # 1. Generate AES key
        aes_key = os.urandom(16)
        aes_key_hex = aes_key.hex()
        
        # 2. Pad & encrypt plaintext
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(plaintext) + padder.finalize()
        
        cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        ciphertext_size = len(ciphertext)
        
        # 3. Request upload parameters
        filekey = uuid.uuid4().hex[:32]
        payload = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": raw_md5,
            "filesize": ciphertext_size,
            "no_need_thumb": True,
            "aeskey": aes_key_hex,
            "base_info": {
                "channel_version": "1.0.0"
            }
        }
        
        resp = httpx.post(
            "https://ilinkai.weixin.qq.com/ilink/bot/getuploadurl",
            json=payload,
            headers=self._get_headers(),
            timeout=25.0
        )
        resp_data = resp.json()
        if resp.status_code != 200 or "upload_param" not in resp_data:
            raise RuntimeError(f"获取上传URL失败: {resp_data.get('errmsg', '未知错误')} (errcode={resp_data.get('errcode')})")
            
        upload_param = resp_data["upload_param"]
        
        # 4. Upload ciphertext to CDN
        cdn_url = f"https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param={quote(upload_param)}&filekey={filekey}"
        cdn_headers = {
            "Content-Type": "application/octet-stream"
        }
        
        cdn_resp = httpx.post(
            cdn_url,
            content=ciphertext,
            headers=cdn_headers,
            timeout=60.0
        )
        if cdn_resp.status_code != 200:
            raise RuntimeError(f"CDN 上传失败 (HTTP {cdn_resp.status_code})")
            
        encrypt_query_param = cdn_resp.headers.get("x-encrypted-param")
        if not encrypt_query_param:
            raise RuntimeError("CDN 上传响应中缺少 x-encrypted-param")
            
        return encrypt_query_param, aes_key, raw_md5, ciphertext_size, raw_size

    def send_file(self, target: str, path: str, mime_type: str) -> None:
        if ":" in target:
            to_user, ctx = target.split(":", 1)
        else:
            to_user, ctx = target, ""
            
        file_path = Path(path)
        if not file_path.exists():
            logger.error(f"[wechat] file not found: {path}")
            return
            
        try:
            encrypt_query_param, aes_key_bytes, raw_md5, ciphertext_size, raw_size = self._upload_media(
                to_user, path, media_type=3
            )
            
            # For files, aes_key should be base64(hex string)
            import base64
            aes_key_hex = aes_key_bytes.hex()
            aes_key_base64 = base64.b64encode(aes_key_hex.encode("utf-8")).decode("utf-8")
            
            import uuid
            payload = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user,
                    "client_id": str(uuid.uuid4()),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": ctx,
                    "item_list": [
                        {
                            "type": 4,
                            "file_item": {
                                "media": {
                                    "encrypt_query_param": encrypt_query_param,
                                    "aes_key": aes_key_base64,
                                    "encrypt_type": 1
                                },
                                "file_name": file_path.name,
                                "md5": raw_md5,
                                "len": str(raw_size)
                            }
                        }
                    ]
                },
                "base_info": {
                    "channel_version": "1.0.0"
                }
            }
            
            logger.info(f"[wechat] sending file payload: {json.dumps(payload, ensure_ascii=False)}")
            resp = httpx.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
                json=payload,
                headers=self._get_headers(),
                timeout=15.0
            )
            data = resp.json()
            logger.info(f"[wechat] send_file response: {json.dumps(data, ensure_ascii=False)}")
            ret_code = data.get("ret") or data.get("errcode") or 0
            if ret_code != 0:
                logger.error(f"[wechat] send_file message failed: {data.get('errmsg', '未知错误')} (错误码: {ret_code})")
        except Exception as e:
            logger.error(f"[wechat] send_file exception: {e}")

    def send_image(self, target: str, path_or_bytes: str | bytes) -> None:
        if ":" in target:
            to_user, ctx = target.split(":", 1)
        else:
            to_user, ctx = target, ""
            
        try:
            encrypt_query_param, aes_key_bytes, raw_md5, ciphertext_size, raw_size = self._upload_media(
                to_user, path_or_bytes, media_type=1
            )
            
            # For images, aes_key should be base64(raw 16 bytes)
            import base64
            aes_key_base64 = base64.b64encode(aes_key_bytes).decode("utf-8")
            
            import uuid
            payload = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user,
                    "client_id": str(uuid.uuid4()),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": ctx,
                    "item_list": [
                        {
                            "type": 2,
                            "image_item": {
                                "media": {
                                    "encrypt_query_param": encrypt_query_param,
                                    "aes_key": aes_key_base64,
                                    "encrypt_type": 1
                                },
                                "aeskey": aes_key_bytes.hex(),
                                "mid_size": ciphertext_size
                            }
                        }
                    ]
                },
                "base_info": {
                    "channel_version": "1.0.0"
                }
            }
            
            logger.info(f"[wechat] sending image payload: {json.dumps(payload, ensure_ascii=False)}")
            resp = httpx.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
                json=payload,
                headers=self._get_headers(),
                timeout=15.0
            )
            data = resp.json()
            logger.info(f"[wechat] send_image response: {json.dumps(data, ensure_ascii=False)}")
            ret_code = data.get("ret") or data.get("errcode") or 0
            if ret_code != 0:
                logger.error(f"[wechat] send_image message failed: {data.get('errmsg', '未知错误')} (错误码: {ret_code})")
        except Exception as e:
            logger.error(f"[wechat] send_image exception: {e}")
