---
name: miloco-overview
description: Xiaomi Miloco 米家智能家居能力总览与适配说明。当用户提到米家/小米设备、家庭场景、家人身份、家庭事件/告警、自动化/任务、家庭档案/习惯、感知范围、主动通知时加载本 skill，了解 Miloco 全部能力以及它们在 SSR 中对应的 miloco_* 工具。
metadata:
  author: ssr-miloco
  version: "1.0"
---

# Miloco 能力总览（SSR 适配）

SSR 通过原生集成 [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) 接入米家。
本仓库已把 Miloco 官方 `plugins/skills` 下的全部技能文档纳入知识库（`miloco-*`
skills），它们描述了 Miloco 的完整能力与判断逻辑。

**重要适配说明**：官方技能是为 OpenClaw 写的，正文里大量使用 `miloco-cli ...`
命令行。在 SSR 中：

- 优先使用下面的 **`miloco_*` 工具**（直接走 Miloco 的本地 REST API，无需 miloco-cli）。
- 仅当环境里确实安装了 `miloco-cli`（例如与 Miloco 同机）时，才用 `run_command`
  执行官方技能里的 `miloco-cli` 命令；在 ssr-miloco 的 Docker 部署里 ssr 容器**没有**
  miloco-cli，请一律用 `miloco_*` 工具。
- 把官方技能当作「能力说明书 + 判断准则」来读，把其中的 `miloco-cli <子命令>`
  映射成对应的 `miloco_*` 工具调用。

## 能力 → SSR 工具映射

| Miloco 能力（官方 skill） | SSR 工具 |
| --- | --- |
| 连通性 / 账号绑定 / 运维（miloco-miot-admin） | `miloco_status`、`miloco_refresh` |
| 设备查询/控制（miloco-devices） | `miloco_devices`、`miloco_device_status`、`miloco_device_spec`、`miloco_device_control` |
| 触发场景（miloco-devices 场景部分） | `miloco_trigger_scene` |
| 摄像头列表 | `miloco_cameras` |
| 家庭成员档案 CRUD（miloco-miot-identity） | `miloco_family`（读取；增删改名走 Miloco 端） |
| 身份样本注册/录脸（miloco-miot-identity-register） | 需上传图像，走 Miloco 端/ miloco-cli（SSR 暂只读 `miloco_family`） |
| 家庭事件/感知回顾（miloco-perception, events） | `miloco_activities`，并订阅总线 `miloco.activity.**` |
| 自动化规则（rule） | `miloco_automations` |
| 持续家庭任务（miloco-create-task, terminate-task） | `miloco_tasks`（读取；增删走 Miloco 端） |
| 家庭档案/长期记忆（miloco-home-profile, habit-suggest） | `miloco_home_profile` |
| 感知范围：家庭/摄像头（miloco-miot-scope） | `miloco_scope` |
| 主动通知 TTS/IM/米家推送（miloco-notify） | `miloco_notify`（也可用 SSR 自带 `push_notification`） |
| 一次性快照进上下文 | `miloco_sync`（设备/摄像头/成员/事件/规则/任务/家庭档案/感知范围） |

## 事件驱动（总线）

Miloco 的家庭事件（Activity）会被 SSR 桥接为总线事件
`miloco.activity.<type>`。要「当家里发生 X 时自动做 Y」，用 `bus_create_handler`
订阅 `miloco.activity.**`（或更具体的子主题），在处理器里调用上面的工具或
`push_notification` 主动触达。

## 未覆盖/受限能力（需 Miloco 端或 miloco-cli）

- **身份样本注册（录脸/录身形）**：需要上传图像/视频帧，SSR 暂未封装，请走
  Miloco 仪表盘或 `miloco-cli`（见 miloco-miot-identity-register skill）。
- **成员/任务/规则/家庭档案的写操作**：SSR 目前以**读取 + 设备控制 + 触发场景 +
  主动通知**为主；档案写入、任务创建/删除、规则增删请走 Miloco 端能力（官方 skill
  里的 `miloco-cli` 命令）或后续在 SSR 中补充对应工具。
