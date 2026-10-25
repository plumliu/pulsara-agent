# 安装 Pulsara

[English](INSTALL.md) · [从源码构建](DEVELOPMENT.zh-CN.md)

## 下载 0.1.1-alpha.1

| 系统 | 安装包 |
| --- | --- |
| macOS 15+，Apple Silicon | [下载 DMG](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.1-alpha.1/Pulsara-0.1.1-alpha.1-macos-arm64.dmg) |
| Windows 11 x64 + WSL2 Ubuntu | [下载 EXE](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.1-alpha.1/Pulsara-0.1.1-alpha.1-windows-x64.exe) |

本版不提供原生 Ubuntu 安装包。amd64、arm64 的本地构建见[开发指南](DEVELOPMENT.zh-CN.md)；原生 Ubuntu 桌面验收仍延期。

## macOS

1. 打开 DMG，将 Pulsara 拖入“应用程序”。
2. 推出 DMG，从“应用程序”打开 Pulsara。
3. 若被系统拦截，确认下载来源后，进入“系统设置 → 隐私与安全性 → 仍要打开”。此预览版没有 Apple Developer ID 分发签名，也未经过 Apple 公证。[Apple 说明](https://support.apple.com/zh-cn/102445)

## Windows + WSL2

1. 尚未安装时，先按 [Microsoft 说明](https://learn.microsoft.com/zh-cn/windows/wsl/install)安装 WSL2 和 Ubuntu。按提示重启，首次打开 Ubuntu 并完成 Linux 用户创建。
2. 运行 EXE 安装包，再从开始菜单打开 Pulsara。安装包尚未签名，Windows 可能提示未知发布者。
3. 首次启动时，选择已有的 WSL2 Ubuntu 并连接。Pulsara 使用默认非 root 用户，安装随包 Linux 应用资源，保留 interop、自动挂载和发行版中的其他工作。

工程目录从 Linux 用户家目录下选择。“浏览…”也可以打开定位到该家目录的 Windows 目录窗口；Windows 磁盘和 Linux 家目录之外的路径不能作为工程目录。Windows 附件可通过选择文件或拖放导入输入框。

## 配置模型

在设置中添加模型连接，再创建快速开始会话或打开工程目录。应用自带 Python、PostgreSQL、Chromium 和搜索工具，无需另装 uv、Docker 或数据库。

## 数据与更新

| 数据 | 位置 |
| --- | --- |
| 配置、会话与数据库 | `~/.pulsara`，或配置的 `PULSARA_HOME` |
| 快速开始目录 | `<Pulsara 家目录>/workspaces/quick-*` |
| Windows 应用的 Linux 资源 | 所选 WSL 用户的 `~/.local/share/pulsara/runtime/<版本>` |

Windows 版的上述数据路径均属于 Linux 用户。Windows 应用数据目录保存宿主设置和日志；在 Windows 中设置 `PULSARA_HOME` 不会切换 Linux 数据目录。

更新前请完全退出 Pulsara；只关闭窗口仍会在后台运行。macOS 替换“应用程序”中的旧应用，Windows 运行新版安装包。卸载 Windows 应用会保留 Ubuntu、Linux 资源与用户数据；任务或数据库维护尚未结束时，安装器会拒绝继续。

**从 0.1.0-alpha.5 或更早版本升级：**alpha.6 更改了数据库基线，不提供这些旧版本的自动迁移。请先备份数据，保留旧会话需要另行迁移，不要删除整个 `~/.pulsara`。

开发版与安装版默认共用数据目录，打开另一份前先退出当前应用。当前采用手动更新。

## 命令行

macOS 首次启动注册 `~/.local/bin/pulsara`，原生 Ubuntu deb 管理 `/usr/bin/pulsara`。在“设置 → 通用 → 本地服务”查看入口；已有同名命令不会自动覆盖，PATH 也不会自动修改。

Windows 版请在 WSL 终端使用设置页显示的命令路径；桌面窗口从 Windows 启动。

```sh
pulsara --version
pulsara --help
```

反馈问题时，请提供系统、版本与复现步骤：[GitHub Issues](https://github.com/plumliu/pulsara-agent/issues)。不要附带 API Key 或 OAuth 令牌。
