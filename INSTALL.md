# Install Pulsara / 安装 Pulsara

macOS 用户可从 [GitHub Releases](https://github.com/plumliu/pulsara-agent/releases/latest) 下载 alpha.6。Ubuntu 用户优先[从源码构建](DEVELOPMENT.md)，下方保留 alpha.2 deb 下载。不要把 Source code 压缩包当作安装包。

macOS users can download alpha.6 from [GitHub Releases](https://github.com/plumliu/pulsara-agent/releases/latest). Ubuntu users should [build from source](DEVELOPMENT.md); older alpha.2 deb downloads remain available below. The Source code archives are not installers.

| 系统 / System | 安装包 / Installer |
| --- | --- |
| macOS 15+，Apple Silicon | [Pulsara-0.1.0-alpha.6-macos-arm64.dmg](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.0-alpha.6/Pulsara-0.1.0-alpha.6-macos-arm64.dmg) |
| Ubuntu 24.04，amd64 | [Pulsara-0.1.0-alpha.2-linux-amd64.deb](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.0-alpha.2/Pulsara-0.1.0-alpha.2-linux-amd64.deb) |
| Ubuntu 24.04，arm64 | [Pulsara-0.1.0-alpha.2-linux-arm64.deb](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.0-alpha.2/Pulsara-0.1.0-alpha.2-linux-arm64.deb) |

## macOS

1. 打开 DMG，将 Pulsara 拖入“应用程序”。
2. 推出 DMG，从“应用程序”打开 Pulsara。
3. 此预览版没有 Apple Developer ID 分发签名，也没有经过 Apple 公证。若被系统拦截，确认文件来自本项目 Release 后，进入“系统设置 → 隐私与安全性 → 仍要打开”，再确认打开。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

Open the DMG, drag Pulsara into Applications, eject the DMG, then launch the installed app. This preview has no Developer ID distribution signature or Apple notarization. If macOS blocks it, verify the download source, then use System Settings → Privacy & Security → Open Anyway. [Apple instructions](https://support.apple.com/en-us/102445)

## Ubuntu

优先按[源码构建指南](DEVELOPMENT.md)在 Ubuntu 本机或容器内构建，再安装生成的 deb；两种方式均支持 amd64 和 arm64，不强制Ubuntu发行版本号。构建所需的工具与步骤见该指南，实际依赖兼容性须验证。alpha.6 未发布 Ubuntu deb，alpha.2 包不包含最新修复。

Build the current source on Ubuntu, either natively or in a container, then install the resulting deb. Ubuntu release numbers are not enforced; actual dependency compatibility must be verified. Both amd64 and arm64 are supported by the [build guide](DEVELOPMENT.md), which lists the required tools and commands. No Ubuntu deb was published for alpha.6; alpha.2 packages lack the latest fixes.

如果仍需安装旧版 alpha.2，用软件安装器打开对应架构的 deb，或在下载目录执行：

To install the older alpha.2 package, open it in your software installer or run this in the download directory:

```sh
sudo apt install ./Pulsara-0.1.0-alpha.2-linux-amd64.deb
```

ARM64 用户将文件名换成 `Pulsara-0.1.0-alpha.2-linux-arm64.deb`。安装后从应用菜单启动。Use the arm64 filename on ARM64, then launch Pulsara from the application menu.

## 开始使用 / Getting started

在设置中添加有效模型连接即可开始。应用自带 Python、PostgreSQL、浏览器和搜索工具，无需预装 uv、Docker 或外部数据库。具体任务依赖可以在对话中按需准备；安装失败时根据提示处理网络、权限或第三方账号问题。

Add a working model connection in Settings. The app bundles Python, PostgreSQL, Chromium and search tools; uv, Docker and an external database are not prerequisites. Task-specific software can be prepared as needed through the conversation.

## 数据与更新 / Data and updates

**alpha.6 数据库变更：**本版删除记忆来源关联字段，旧版数据库不支持自动迁移。升级前先备份 Pulsara 数据；保留旧会话需要另行迁移，不能直接沿用旧库。不要删除整个 `~/.pulsara`。

**alpha.6 database change:** Memory origin fields were removed. Older databases are not migrated automatically. Back up your Pulsara data before upgrading; retaining previous conversations requires a separate migration. Do not delete the entire `~/.pulsara` directory.

数据默认保存在 `~/.pulsara`；显式 `PULSARA_HOME` 指定另一个目录。开发构建和发行构建默认共用这些数据与 CLI，测试另一份构建前请完全退出当前应用。

手动更新时先选择“Pulsara → 退出 Pulsara”。macOS 覆盖“应用程序”中的旧应用，选择“替换”；Ubuntu 更新源码、重新构建后安装生成的 deb。仅关闭窗口可能继续后台运行。普通覆盖安装、同版本重启或卸载应用不会主动删除配置、会话数据库和工作文件。本版未提供自动更新或跨版本数据库迁移承诺。

Data defaults to `~/.pulsara`, or an explicit `PULSARA_HOME`. Local development builds and release builds share that home and CLI by default. Fully quit the current app before testing another build. To update, quit Pulsara, then replace the macOS app, or update the source, rebuild and install the resulting deb on Ubuntu. Ordinary replacement, same-version restarts and uninstalling the app do not intentionally remove user data. This preview offers manual updates without a cross-version database migration guarantee.

## Windows + WSL2 开发候选 / Development candidate

Windows 接入正在进行 Phase B 验收，尚未激活发布资格。当前候选为 Windows 11 x64 的 per-user NSIS 安装包；实际验收范围与待办见 [Windows 兼容目录](windows_compatibility/README.md)。使用已有 WSL2 Ubuntu 和默认非 root 用户，Ubuntu 发行版本号不设门槛。首次启动需明确确认发行版和用户，再离线安装随包 Linux 资源；应用保留 interop、自动挂载及发行版内其他工作。

Windows 界面安装在当前用户的 Programs 目录。资源位于所选 Linux 用户的 `~/.local/share/pulsara/runtime/<应用版本>`，配置和数据库默认位于该用户的 `~/.pulsara`。Windows 的应用数据目录只保存宿主设置及日志；Windows `PULSARA_HOME` 不代替 Linux 有效 home。工程通过应用内 Linux 目录选择器选择，Windows 本地附件通过原上传入口复制到 Linux；相同发行版的 UNC 文件可引用。首次工作台可用后，在设置页配置模型连接。

更新或卸载前使用应用的“退出”动作；关闭窗口会保留后台应用。安装器遇到仍活的任务、数据库或无法确认的清理结果会拒绝继续，不强杀 WSL 或 PostgreSQL。卸载 Windows 界面保留 Linux 配置、数据库、资源和工作文件。相同版本的资源内容不一致会拒绝复用；开发者替换候选须明确结算并核对自己的测试环境，不能修改摘要标记绕过检查。

Windows support is undergoing Phase B acceptance and is not release-qualified. The per-user installer connects to an existing WSL2 Ubuntu and its selected non-root user, preserving interop and automount. Confirm that target on first launch. Linux owns application resources, settings, PostgreSQL and workspaces; Windows owns the desktop shell. Configure model connections in the workbench settings. Quit normally before maintenance; the installer refuses busy or uncertain cleanup and does not terminate WSL or PostgreSQL. Uninstalling the Windows shell preserves Linux data and resources. See the [current acceptance status](windows_compatibility/README.md) before treating a development build as a supported release.

## CLI

macOS 首次启动注册 `~/.local/bin/pulsara`，deb 管理 `/usr/bin/pulsara`。在“设置 → 通用 → 本地服务 → 命令行入口”检查状态。其他来源的同名命令不会自动覆盖，PATH 也不会自动修改。

macOS registers `~/.local/bin/pulsara` on launch; deb manages `/usr/bin/pulsara`. Inspect its status in Settings → General → Local services → Command-line entry. Other installations' commands are preserved, and shell PATH is not modified automatically.

```sh
pulsara --version
pulsara --help
pulsara app
```

反馈问题请提供系统、架构、版本及复现步骤：[GitHub Issues](https://github.com/plumliu/pulsara-agent/issues)。不要提交 API Key 或 OAuth 令牌。Include the OS, architecture, version and reproduction steps, without credentials.
