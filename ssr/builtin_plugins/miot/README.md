# MIoT Device MCP Server (.dxt / .mcpb)

这是一个基于 Model Context Protocol (MCP) 实现的**第三方小米智能家居控制服务扩展包**。支持将您绑定的米家智能家电（如灯具、开关、插座、加湿器等）一键以 Tool / Prompt 形式集成到 Claude Desktop 等 AI 客户端中。

> **⚠️ 免责声明 (Disclaimer)**  
> 本项目属于第三方个人开发者开源适配的桥接工具，**并非小米官方发布的产品**。本扩展中使用的米家控制 SDK（miot_kit）所有权和版权归小米公司（Xiaomi Corporation）所有。请勿将此包用于任何商业用途，在使用过程中请遵守小米服务条款。

---

## 🚀 快速开始与安装

### 1. 准备环境 (Prerequisite)
本扩展基于极速 Python 环境管理工具 `uv` 进行运行时依赖自动解析。在使用本扩展前，请确保您的系统中已安装 `uv` 命令行工具：

*   **Windows 系统 (PowerShell)**:
    ```powershell
    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    ```
*   **macOS / Linux 系统 (Terminal)**:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

### 2. 导入扩展包
1. 在您的 Claude Desktop（或其他支持 MCPB 的 AI 客户端）中，选择 **Import Extension**。
2. 选中并导入打包生成的 `miot-device-mcp.mcpb` (或 `miot-device-mcp.dxt`) 扩展包。

### 3. 本地米家账号授权
在后台服务首次连上云端前，需要在本地生成登录凭证（Token）。
1. 在终端中，运行包内的登录辅助脚本（请确认您已处于该解压目录中）：
   ```bash
   python login_to_miot.py
   ```
2. 脚本会自动在浏览器中打开小米 OAuth 授权登录页面，请使用您的米家账号登录。
3. 登录成功后，页面会跳转到一个无法访问的 `127.0.0.1` 页面。**直接复制浏览器地址栏的完整 URL**，将其粘贴回终端命令行中并按下回车。
4. 提示登录成功后，凭证会自动保存在您本地的缓存目录中。
5. **完全退出并重新启动** 您的 Claude Desktop 客户端，AI 助手将自动加载所有的米家控制工具！

---

## 🛠️ 支持的 AI 工具 (Available Tools)

| 工具名称 | 功能描述 |
| :--- | :--- |
| `get_area_info` | 查询您家里的家庭和房间列表 |
| `get_device_classes` | 获取当前支持的智能设备类别（如 `light`, `outlet`） |
| `get_devices` | 获取指定房间或类别下的设备列表（包括在线状态、`did` 标识符） |
| `get_device_spec` | 获取指定小米智能家电的标准化属性定义（SIID / PIID 规范） |
| `send_get_rpc` | 查询某个家电的具体传感器读数或当前状态属性值 |
| `send_ctrl_rpc` | 控制智能家电（开关、调节档位、亮度、执行特定 Action 等） |

---

## 📄 开源许可
本扩展逻辑及文档遵循 MIT 许可证进行分发。使用的米家接口代码所有权归小米公司所有。
