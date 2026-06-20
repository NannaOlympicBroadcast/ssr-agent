# -*- coding: utf-8 -*-
# Copyright (C) 2026 Yang Bingyi (SNH48 Team X) / AI Assistant
# Simple OAuth2 login helper for Xiaomi Home MCP.

import asyncio
import os
import sys
import webbrowser
from urllib.parse import urlparse, parse_qs

# 将当前目录和 miot_kit 挂载进环境变量
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
sys.path.append(os.path.join(current_dir, "miot_kit"))

# Windows 平台动态链接库不存在的 Monkey Patch
import platform
if platform.system().lower() == "windows":
    class FakeLib:
        def __getattr__(self, name):
            if name == "miot_camera_version":
                return lambda *args, **kwargs: b"0.0.0-fake"
            return lambda *args, **kwargs: 0
            
    # 先导入 miot.camera，把 _load_dynamic_lib 拦截替换掉
    import miot.camera
    miot.camera._load_dynamic_lib = lambda: FakeLib()

from miot.client import MIoTClient

async def main():
    cache_path = os.path.join(os.path.expanduser("~"), ".miot_cache")
    os.makedirs(cache_path, exist_ok=True)
    
    # 生成随机 uuid 并使用
    import uuid
    local_uuid = uuid.uuid4().hex
    
    # 实例化一个临时客户端用于执行 OAuth2 流程
    client = MIoTClient(
        uuid=local_uuid,
        redirect_uri="http://127.0.0.1",
        cache_path=cache_path,
        cloud_server="cn"
    )
    await client.init_async()
    
    # 1. 生成并展示登录 URL
    auth_url = await client.gen_oauth_url_async()
    print("==================================================")
    print("         小米米家账号 OAuth 快捷登录助手")
    print("==================================================")
    print("正在尝试在浏览器中打开授权链接，若无响应请手动复制以下网址登录：")
    print(auth_url)
    print("==================================================")
    
    # 尝试自动打开
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
        
    # 2. 引导输入重定向 URL 或 Code
    print("\n请在浏览器完成小米账号登录。")
    print("登录成功后，页面会跳转到一个无法访问的 127.0.0.1 页面（或你配置的跳转页）。")
    print("请直接复制浏览器地址栏重定向后的完整 URL（包含 code=xxx & state=xxx）：")
    print("--------------------------------------------------")
    
    input_str = input("请输入重定向后的完整 URL: ").strip()
    
    code = ""
    state = ""
    if "code=" in input_str:
        try:
            parsed = urlparse(input_str)
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            state = params.get("state", [""])[0]
        except Exception as e:
            print(f"[-] URL 解析失败: {e}")
    else:
        code = input_str
        state = input("请输入 state 参数: ").strip()
        
    if not code or not state:
        print("[-] 错误：无法提取有效的 code 与 state！")
        await client.deinit_async()
        return
        
    # 3. 交换并保存凭证
    try:
        print("[+] 正在向云端交换登录凭证(Token)...")
        oauth_info = await client.get_access_token_async(code=code, state=state)
        
        # 写入本地 Storage 缓存
        await client.storage.save_async(
            domain="cloud",
            name="oauth_info",
            data=oauth_info.model_dump()
        )
        await client.storage.save_async(
            domain="cloud",
            name="uuid",
            data=local_uuid
        )
        print("==================================================")
        print("   登录成功！已成功将登录凭证写入您的本地缓存。")
        print(f"凭证目录: {cache_path}")
        print("现在您可以重启 Claude Desktop，畅快地控制米家设备了！")
        print("==================================================")
    except Exception as e:
        print(f"[-] 登录交换失败: {e}")
    finally:
        await client.deinit_async()

if __name__ == "__main__":
    asyncio.run(main())
