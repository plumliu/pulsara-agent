# Pulsara {{version}}

本次先发布 **macOS 15+、Apple Silicon DMG**。Windows + WSL 版本将在 Windows 端测试后另行发布；本次不提供 Ubuntu 或 macOS Intel 安装包。

## 本次更新

- **代理协作与 Code Mode：**通过代码 Cell 组合工具调用、安排任务和查看代理运行情况；改进会话控制、上下文整理和工具结果展示。
- **搜索与扩展：**加入网页搜索和抓取，改进 MCP 连接、能力发现、插件与 Hook 支持。
- **多媒体与工作台：**支持本地音视频审看，改进视频批注、文件预览、设置和会话交互。
- **不兼容数据重置：**启动时若本地数据与当前版本不兼容，可以在确认删除后重置并重新开始。此操作会清除会话、设置、API Key、授权及 Pulsara 数据目录中的文件，不迁移旧数据。

## 下载

{{downloads}}

GitHub 的 Source code 压缩包是源码，不是安装包。

## 安装与升级

打开 DMG，将 Pulsara 拖入“应用程序”，推出 DMG，再从“应用程序”启动。应用自带 Python、PostgreSQL、Chromium 和搜索工具；在设置中配置模型连接即可开始使用。

更新前请完全退出旧版。旧数据不会自动迁移；如果启动页提示数据不兼容，可先备份需要保留的文件，勾选确认框后选择“重置并重新开始”。快速开始文件也位于 Pulsara 数据目录内，会一并删除。开发版与发行版默认共用数据目录，不要同时运行。

此预览版采用 ad-hoc 签名，**没有 Apple Developer ID 分发签名，也未经过 Apple 公证**。首次打开若被拦截，确认下载来源后进入“系统设置 → 隐私与安全性 → 仍要打开”。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

详细说明见 [安装指南](https://github.com/plumliu/pulsara-agent/blob/v0.2.0-alpha.1/INSTALL.zh-CN.md)和[开发指南](https://github.com/plumliu/pulsara-agent/blob/v0.2.0-alpha.1/DEVELOPMENT.zh-CN.md)。

自动化检查不代替完整图形桌面验收；Windows + WSL 版本尚待 Windows 端验收。

问题反馈：[GitHub Issues](https://github.com/plumliu/pulsara-agent/issues)。请注明版本、系统和复现步骤，不要附带 API Key 或 OAuth 令牌。
