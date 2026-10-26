# Pulsara {{version}}

这是 Pulsara 桌面预览版。可以在本机配置模型，使用会话、工具、Skills、MCP 和插件完成任务，并在工作台查看文件和交付结果。

## 本次更新

- 精简系统提示词、内置工具参数说明和子代理使用指引。
- 扩展能力刷新统一使用 reload_capabilities，合并技能、MCP 与自动操作刷新入口。
- 内联图形发布标记统一为 pulsara-viz，HTML 内容根标记统一为 pulsara-viz-root；前后端、示例和测试同步更新。
- 调整数据分析技能的图表指引，将 HTML 宿主集成说明拆分为按需阅读的参考文档。
- 优化会话跟随、处理过程展示、记忆工具说明和相关桌面交互。

## 下载

{{downloads}}

GitHub 的 Source code 压缩包是源码，不是安装包。

## 安装

**macOS：**将 DMG 中的 Pulsara 拖入“应用程序”，推出 DMG，再从“应用程序”启动。此预览版没有 Apple Developer ID 分发签名，也未经过 Apple 公证；首次打开可能被拦截。确认下载来源后，先尝试打开应用，再到“系统设置 → 隐私与安全性 → 仍要打开”允许该应用。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

**Windows：**先安装 WSL2 Ubuntu 并完成 Linux 用户创建，再运行 EXE 图形安装器并选择安装目录。首次启动时确认要使用的 Ubuntu 用户。安装包尚未签名，Windows 可能提示未知发布者。

配置有效模型连接即可开始使用。应用自带运行所需的 Python、数据库、浏览器和搜索工具，不要求预先安装 uv 或 Docker；具体任务需要的软件可在对话中按需准备。

## 数据与更新

配置、会话数据库及默认工作目录保存在 `~/.pulsara`；显式设置 `PULSARA_HOME` 时使用指定目录。Windows 的这些数据位于所选 WSL 用户中。Windows 卸载器默认保留个人数据；选择“彻底卸载”会同时删除配置、会话、数据库和快速开始文件。更新前请完全退出 Pulsara，再运行新版安装器或替换 macOS 应用。开发构建和发行构建默认共用数据及 CLI，验收前请退出另一份应用。

**从 alpha.5 或更早版本升级：**本版删除了记忆来源关联字段，更新了数据库基线，不提供旧数据库的自动迁移。请先备份 Pulsara 数据；旧数据库不能直接沿用，保留历史需要另行迁移。请勿通过删除整个 `~/.pulsara` 来处理升级。

macOS 首次启动会注册 `~/.local/bin/pulsara`。已有其他来源的同名命令时不会自动覆盖，可在设置页查看和处理。

## 验证范围与已知限制

- macOS 使用本地增量构建；Windows 在本机 WSL2 Ubuntu 中构建 Linux 资源，再由 Windows 构建 Electron/NSIS 安装器。应用代码基于 v0.1.1-alpha.2，另有不改变运行行为的测试维护。
- 发布 macOS 15+ Apple Silicon DMG 和 Windows 11 x64 + WSL2 Ubuntu EXE；不提供本版原生 Ubuntu 安装包。
- Windows 相关桌面测试 83 项、WSL 桌面全量测试 121 项、Windows payload/构建平台检查 13 项通过；资源搬移后的 PostgreSQL、扩展、Python 与 Chromium 冒烟及版本一致性检查通过。未代用户执行图形安装与安装后验收。
- 当前仅提供手动更新，不包含 AppImage 或 macOS Intel 安装包，也不承诺跨版本数据库迁移。

问题反馈：[GitHub Issues](https://github.com/plumliu/pulsara-agent/issues)。请注明版本、系统、架构和复现步骤，不要附带 API Key 或 OAuth 令牌。
