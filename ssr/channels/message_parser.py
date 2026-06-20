import re
from pathlib import Path
from typing import Tuple, Optional

def parse_and_process_message(channel: any, target: str, message: str, base_dir: Optional[str | Path] = None) -> str:
    """
    解析消息中的 <ssr_reply_image> 和 <ssr_reply_files> 标签并进行处理。
    
    返回处理后剩余的纯文本部分。
    
    Args:
        channel: The channel instance (feishu, wechat, etc.).
        target: The target chat ID or user ID.
        message: The message text potentially containing tags.
        base_dir: Base directory for resolving relative file paths. 
                  If None, paths are resolved against CWD.
    """
    base = Path(base_dir) if base_dir else None

    def _resolve(path_str: str) -> Path:
        p = Path(path_str.strip())
        if not p.is_absolute() and base is not None:
            p = base / p
        return p

    # 处理图片
    image_pattern = re.compile(r'<ssr_reply_image>(.*?)</ssr_reply_image>')
    for match in image_pattern.finditer(message):
        image_path = _resolve(match.group(1))
        if image_path.exists():
            channel.send_image(target, str(image_path))
    
    # 处理文件
    file_pattern = re.compile(r'<ssr_reply_files>(.*?)</ssr_reply_files>')
    for match in file_pattern.finditer(message):
        file_path = _resolve(match.group(1))
        if file_path.exists():
            import mimetypes
            mime, _ = mimetypes.guess_type(str(file_path))
            channel.send_file(target, str(file_path), mime or "application/octet-stream")
            
    # 移除标签，返回纯文本
    clean_message = re.sub(r'<(ssr_reply_image|ssr_reply_files)>.*?</\1>', '', message).strip()
    return clean_message
