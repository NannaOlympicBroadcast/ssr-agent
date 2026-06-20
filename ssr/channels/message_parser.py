import re
from pathlib import Path
from typing import Tuple

def parse_and_process_message(channel: any, target: str, message: str) -> str:
    """
    解析消息中的 <ssr_reply_image> 和 <ssr_reply_files> 标签并进行处理。
    
    返回处理后剩余的纯文本部分。
    """
    # 处理图片
    image_pattern = re.compile(r'<ssr_reply_image>(.*?)</ssr_reply_image>')
    for match in image_pattern.finditer(message):
        image_path = match.group(1).strip()
        if Path(image_path).exists():
            channel.send_image(target, image_path)
    
    # 处理文件
    file_pattern = re.compile(r'<ssr_reply_files>(.*?)</ssr_reply_files>')
    for match in file_pattern.finditer(message):
        file_path = match.group(1).strip()
        if Path(file_path).exists():
            import mimetypes
            mime, _ = mimetypes.guess_type(file_path)
            channel.send_file(target, file_path, mime or "application/octet-stream")
            
    # 移除标签，返回纯文本
    clean_message = re.sub(r'<(ssr_reply_image|ssr_reply_files)>.*?</\1>', '', message).strip()
    return clean_message
