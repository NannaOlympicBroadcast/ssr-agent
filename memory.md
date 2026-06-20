# SSR Memory

- (2026-06-19 11:25) 在飞书（Feishu）和微信（WeChat）的对话回复中，可以通过 XML 标签携带文件和图片。
- `<ssr_reply_image>文件地址</ssr_reply_image>`：发送指定路径的图片。
- `<ssr_reply_files>文件地址</ssr_reply_files>`：发送指定路径的文件。
系统在发送回复前会自动解析这些标签，并触发相应的媒体发送逻辑，同时移除原始消息中的这些标签，仅保留纯文本部分。
