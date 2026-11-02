# 安装 Pulsara

[English](INSTALL.md) · [从源码构建](DEVELOPMENT.zh-CN.md)

## 下载 0.1.3-alpha.1

| 系统 | 安装包 |
| --- | --- |
| macOS 15+，Apple Silicon | [下载 DMG](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.3-alpha.1/Pulsara-0.1.3-alpha.1-macos-arm64.dmg) |

本版仅提供 macOS Apple Silicon DMG；0.1.3-alpha.1 不发布 Windows 或 Ubuntu 安装包。这些平台的源码构建方式保留在[开发指南](DEVELOPMENT.zh-CN.md)中。

## macOS

1. 打开 DMG，将 Pulsara 拖入“应用程序”。
2. 推出 DMG，从“应用程序”打开 Pulsara。
3. 若被系统拦截，确认下载来源后，进入“系统设置 → 隐私与安全性 → 仍要打开”。此预览版使用覆盖应用与随包资源的 ad-hoc 签名，没有 Apple Developer ID 分发签名，也未经过 Apple 公证；ad-hoc 签名有效不代表 Gatekeeper 会自动信任。[Apple 说明](https://support.apple.com/zh-cn/102445)

若系统提示应用已损坏，请勿全局关闭 Gatekeeper。请重新下载当前 DMG；本版修正了早期安装包不完整的应用签名。若仍失败，请在反馈中附上 `codesign --verify --deep --strict --verbose=2 /Applications/Pulsara.app` 的输出。

桌面、文稿等目录由 macOS 单独授权，可在“系统设置 → 隐私与安全性 → 文件与文件夹”中按需要开启。[Apple 文件授权说明](https://support.apple.com/guide/mac-help/mchld5a35146/mac)。重新签名后，既有“完全磁盘访问”授权可能与新程序不匹配；若系统日志报告签名要求不匹配，需由用户移除旧条目、重新添加当前 Pulsara。“完全磁盘访问”允许访问整盘，请按实际需要选择授权范围。

## Windows + WSL2

本版没有 Windows 安装包，以下步骤适用于另行构建的 Windows 包。

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

更新前请完全退出 Pulsara；只关闭窗口仍会在后台运行。macOS 替换“应用程序”中的旧应用，Windows 运行新版安装包。任务或数据库维护尚未结束时，安装器会拒绝继续。

Windows 卸载器只有两个选项，两者都会删除应用和 WSL 运行资源：

- **保留个人数据**（默认）：保留配置、会话和文件。
- **彻底卸载**：同时删除配置、会话、数据库和快速开始文件。

Ubuntu 和 Pulsara 数据目录之外的工程文件会保留。WSL 清理失败时，卸载会停止，可处理后重试。

**从 0.1.1 或更早版本升级：**0.1.3 更改了数据库基线、代理会话结构与身份格式，早期发行版数据库不能直接沿用，也不提供自动迁移。首次打开新版前，请完全退出旧版并备份整个 Pulsara 数据目录。保留旧应用和旧数据以查看历史；将历史迁入新版需要另行迁移。请为新版使用新的数据目录，不要删除已有 `~/.pulsara`。例如，安装后运行 `PULSARA_HOME="$HOME/.pulsara-0.1.3" /Applications/Pulsara.app/Contents/MacOS/pulsara`，再到设置中配置模型连接。

开发版与安装版默认共用数据目录，打开另一份前先退出当前应用。当前采用手动更新。

## 命令行

macOS 首次启动注册 `~/.local/bin/pulsara`，原生 Ubuntu deb 管理 `/usr/bin/pulsara`。在“设置 → 通用 → 本地服务”查看入口；已有同名命令不会自动覆盖，PATH 也不会自动修改。

Windows 版请在 WSL 终端使用设置页显示的命令路径；桌面窗口从 Windows 启动。

```sh
pulsara --version
pulsara --help
```

反馈问题时，请提供系统、版本与复现步骤：[GitHub Issues](https://github.com/plumliu/pulsara-agent/issues)。不要附带 API Key 或 OAuth 令牌。
