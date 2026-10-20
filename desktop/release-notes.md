# Pulsara {{version}}

这是 Pulsara 首个桌面预览版。可以在本机配置模型，使用会话、工具、Skills、MCP 和插件完成任务，并在工作台查看文件和交付结果。

## 下载

{{downloads}}

GitHub 的 Source code 压缩包是源码，不是安装包。

## 安装

**macOS：**将 DMG 中的 Pulsara 拖入“应用程序”，推出 DMG，再从“应用程序”启动。此预览版没有 Apple Developer ID 分发签名，也未经过 Apple 公证；首次打开可能被拦截。确认下载来源后，先尝试打开应用，再到“系统设置 → 隐私与安全性 → 仍要打开”允许该应用。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

**Ubuntu：**使用软件安装器打开对应架构的 deb，或运行 `sudo apt install ./下载的文件名.deb`，安装后从应用菜单启动。

配置有效模型连接即可开始使用。应用自带运行所需的 Python、数据库、浏览器和搜索工具，不要求预先安装 uv 或 Docker；具体任务需要的软件可在对话中按需准备。

## 数据与更新

配置、会话数据库及默认工作目录保存在 `~/.pulsara`；显式设置 `PULSARA_HOME` 时使用指定目录。卸载或替换应用不会主动删除这些用户数据。更新前请完全退出 Pulsara，再替换应用或安装新版 deb。开发构建和发行构建默认共用数据及 CLI，验收前请退出另一份应用。

macOS 首次启动会注册 `~/.local/bin/pulsara`；Ubuntu 由 deb 管理 `/usr/bin/pulsara`。已有其他来源的同名命令时不会自动覆盖，可在设置页查看和处理。

## 验证范围与已知限制

- CI 覆盖 macOS ARM64、Ubuntu 24.04 amd64 和 arm64 的原生打包及资源搬移；Ubuntu 另有完整后端测试、wheel 隔离安装和 deb 安装/卸载检查，前端回归、类型和 lint 单独检查。
- 下载后的 macOS Gatekeeper 首次打开及 Ubuntu 图形桌面闭环仍待人工验收。草稿中的 CI 通过不代表这些步骤已经完成。
- 首版仅提供手动更新，不包含 AppImage、Windows 或 macOS Intel 安装包，不宣称兼容所有 Linux 发行版，也不承诺跨版本数据库迁移。

问题反馈：[GitHub Issues](https://github.com/plumliu/pulsara-agent/issues)。请注明版本、系统、架构和复现步骤，不要附带 API Key 或 OAuth 令牌。
