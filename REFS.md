# REFS — 引用资料目录

`REFS.md` 定义了一种新的上下文类型 **refs**（引用资料）。它以 markdown 表格的形式
记录项目用到的引用资料，被 SSR Agent 装载进 `refs` 上下文类别，可通过
`search_context category=refs ...` 检索，并以摘要形式注入系统提示。

每一行描述一个引用资料：

- **名称**：简短的资料名称。
- **位置**：可以是一个 web URL、一段文字、或一个本地文件 / 目录的路径。
- **内容**：对该资料内容的简要说明。

放在项目根目录的 `REFS.md`（项目级）会与 `~/.ssr/REFS.md`（全局级）一起被装载。

| 名称 | 位置 | 内容 |
| --- | --- | --- |
| 项目指南 | ./CLAUDE.md | SSR Agent 的架构、约定与常用命令说明。 |
| 自述文档 | ./README.md | 安装、用法与功能概览。 |
| Google ADK | https://google.github.io/adk-docs/ | 智能体运行时框架文档；`SSRAgent` 基于它构建。 |
| Gemini API | https://ai.google.dev/gemini-api/docs | 默认模型 `gemini-3.1-flash-lite` 的接口与能力参考。 |
| 小爱音箱接入参考 | https://github.com/ZhengXieGang/Xiaoai-Claw-Addon | 小爱音箱语音拦截/播报的实现参考，用于 xiaomi channel。 |
| Chrome DevTools MCP | https://github.com/ChromeDevTools/chrome-devtools-mcp | chrome-devtools 内置插件对应的 MCP server。 |
