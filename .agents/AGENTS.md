# Workspace Rules

- **信息检索优先规则**：当遇到未确定或复杂的系统设计、第三方 API 变更、安全验证逻辑或其它开发决策时，绝对不能根据自身内置的知识库进行主观猜测或编造设计。必须优先通过 `search grounding`（Web 搜索）查找最新的社区公开讨论（如 GitHub Issues 讨论），或者阅读本地项目里的 `references.md` / `REFS.md` 文件，基于可信、真实的信息做出开发决策。

- **小米安全验证链接拼接规则**：当小米账号登录返回 `notificationUrl` 需要进行安全验证时，如果是以 `https://account.xiaomi.com/` 开头的完整 URL，必须直接使用原 URL（即 `https://account.xiaomi.com/fe/service/identity/authStart?...`），绝对不能拼接或替换为 `https://account.xiaomi.com/pass/serviceLoginAuth2/` 前缀，否则在浏览器中访问时会遇到 404 错误。如果是以 `/` 开头的相对路径，必须使用 `https://account.xiaomi.com` 作为前缀进行拼接。这样可以确保用户能够正常在浏览器中打开页面并完成安全验证。
