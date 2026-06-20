# SSR Memory

- (2026-06-19 11:25) 在飞书（Feishu）和微信（WeChat）的对话回复中，可以通过 XML 标签携带文件和图片。
- `<ssr_reply_image>文件地址</ssr_reply_image>`：发送指定路径的图片。
- `<ssr_reply_files>文件地址</ssr_reply_files>`：发送指定路径的文件。
系统在发送回复前会自动解析这些标签，并触发相应的媒体发送逻辑，同时移除原始消息中的这些标签，仅保留纯文本部分。
- (2026-06-20 06:32) 飞书 push_notification 的 target 参数必须使用 feishu.json 中的 session_id（即 chat_id，格式 oc_xxx），不能传 "user" 等非合法值。feishu_channel.py 的 send_message 硬编码了 receive_id_type("chat_id")。当前正确的 chat_id 是 oc_b68818a6a73246064deeec4aa3e63957。
- (2026-06-20 06:34) tools_push.py 已修复：push_notification_impl 现在会先调用 parse_and_process_message 解析 <ssr_reply_files>/<ssr_reply_image> 标签并上传文件/图片，再发送剩余纯文本。之前缺少此步骤导致标签被当作纯文本发送。
- (2026-06-20 06:36) message_parser.py 的 parse_and_process_message 新增了 base_dir 参数用于解析相对路径。tools_push.py 调用时传入 settings.project_dir 作为 base_dir。之前相对路径直接用 Path(exists) 判断，agent CWD 不是项目目录导致文件被静默跳过。
