# Install Pulsara / 安装 Pulsara

从 [GitHub Releases](https://github.com/plumliu/pulsara-agent/releases) 选择预览版安装包。草稿尚未公开时，仅有仓库访问权限的协作者可以查看和下载安装附件。不要把 Source code 压缩包当作安装包。

Choose a preview installer from [GitHub Releases](https://github.com/plumliu/pulsara-agent/releases). Draft attachments are available to authorized repository collaborators until publication. The Source code archives are not installers.

| 系统 / System | 安装包 / Installer |
| --- | --- |
| macOS 15+，Apple Silicon | `Pulsara-0.1.0-alpha.1-macos-arm64.dmg` |
| Ubuntu 24.04，amd64 | `Pulsara-0.1.0-alpha.1-linux-amd64.deb` |
| Ubuntu 24.04，arm64 | `Pulsara-0.1.0-alpha.1-linux-arm64.deb` |

## macOS

1. 打开 DMG，将 Pulsara 拖入“应用程序”。
2. 推出 DMG，从“应用程序”打开 Pulsara。
3. 此预览版没有 Apple Developer ID 分发签名，也没有经过 Apple 公证。若被系统拦截，确认文件来自本项目 Release 后，进入“系统设置 → 隐私与安全性 → 仍要打开”，再确认打开。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

Open the DMG, drag Pulsara into Applications, eject the DMG, then launch the installed app. This preview has no Developer ID distribution signature or Apple notarization. If macOS blocks it, verify the download source, then use System Settings → Privacy & Security → Open Anyway. [Apple instructions](https://support.apple.com/en-us/102445)

## Ubuntu

用软件安装器打开对应架构的 deb，也可以在下载目录执行：

Open the matching deb in your software installer, or run this in the download directory:

```sh
sudo apt install ./Pulsara-0.1.0-alpha.1-linux-amd64.deb
```

ARM64 用户将文件名换成 `Pulsara-0.1.0-alpha.1-linux-arm64.deb`。安装后从应用菜单启动。Use the arm64 filename on ARM64, then launch Pulsara from the application menu.

## 开始使用 / Getting started

在设置中添加有效模型连接即可开始。应用自带 Python、PostgreSQL、浏览器和搜索工具，无需预装 uv、Docker 或外部数据库。具体任务依赖可以在对话中按需准备；安装失败时根据提示处理网络、权限或第三方账号问题。

Add a working model connection in Settings. The app bundles Python, PostgreSQL, Chromium and search tools; uv, Docker and an external database are not prerequisites. Task-specific software can be prepared as needed through the conversation.

## 数据与更新 / Data and updates

数据默认保存在 `~/.pulsara`；显式 `PULSARA_HOME` 指定另一个目录。开发构建和发行构建默认共用这些数据与 CLI，测试另一份构建前请完全退出当前应用。

手动更新时先选择“Pulsara → 退出 Pulsara”。macOS 覆盖“应用程序”中的旧应用，选择“替换”；Ubuntu 安装新版 deb。仅关闭窗口可能继续后台运行。普通覆盖安装、同版本重启或卸载应用不会主动删除配置、会话数据库和工作文件。本版未提供自动更新或跨版本数据库迁移承诺。

Data defaults to `~/.pulsara`, or an explicit `PULSARA_HOME`. Local development builds and release builds share that home and CLI by default. Fully quit the current app before testing another build. To update, quit Pulsara, then replace the macOS app or install the newer deb. Ordinary replacement, same-version restarts and uninstalling the app do not intentionally remove user data. This preview offers manual updates without a cross-version database migration guarantee.

## CLI

macOS 首次启动注册 `~/.local/bin/pulsara`，deb 管理 `/usr/bin/pulsara`。在“设置 → 通用 → 本地服务 → 命令行入口”检查状态。其他来源的同名命令不会自动覆盖，PATH 也不会自动修改。

macOS registers `~/.local/bin/pulsara` on launch; deb manages `/usr/bin/pulsara`. Inspect its status in Settings → General → Local services → Command-line entry. Other installations' commands are preserved, and shell PATH is not modified automatically.

```sh
pulsara --version
pulsara --help
pulsara app
```

## 校验下载 / Verify downloads

下载 `SHA256SUMS`，将对应文件的计算结果与其中一行比较。Download `SHA256SUMS` and compare the matching checksum:

```sh
# macOS
shasum -a 256 Pulsara-0.1.0-alpha.1-macos-arm64.dmg
# Ubuntu
sha256sum Pulsara-0.1.0-alpha.1-linux-amd64.deb
```

反馈问题请提供系统、架构、版本及复现步骤：[GitHub Issues](https://github.com/plumliu/pulsara-agent/issues)。不要提交 API Key 或 OAuth 令牌。Include the OS, architecture, version and reproduction steps, without credentials.
