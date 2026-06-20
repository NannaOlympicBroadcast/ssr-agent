
import re

def fix_wechat_channel():
    path = "ssr/channels/wechat_channel.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    new_logic = """
                        elif itype == 3: # VOICE
                            voice_item = item.get("voice_item", {})
                            text = voice_item.get("text", "")
                            if text:
                                downloaded_parts.append({"type": "text", "text": f"[语音转文字: {text}]"})
                        elif itype == 4: # FILE
                            file_item = item.get("file_item", {})
                            file_name = file_item.get("file_name", "unknown_file")
                            downloaded_parts.append({"type": "text", "text": f"[文件接收: {file_name}]"})
    """

    # Look for the block
    pattern = r'(elif itype == 2:.*?image_error = e\n                                break)'
    
    # We add the new logic after the image block
    new_content = re.sub(pattern, r'\1' + new_logic, content, flags=re.DOTALL)
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print("Fixed wechat_channel.py")

fix_wechat_channel()
