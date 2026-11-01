# 从源码构建 Pulsara

[English](DEVELOPMENT.md) · [安装说明](INSTALL.zh-CN.md) · [下载 0.1.3-alpha.1](https://github.com/plumliu/pulsara-agent/releases/tag/v0.1.3-alpha.1)

修改代码前请阅读 [AGENTS.md](AGENTS.md)。原生资源在目标系统构建；Windows 使用 WSL2 Ubuntu 中构建的 Linux 资源，以及 Windows 中构建的 Electron 界面。

MCP 网络所有权、可选资源模板发现和真实模型验收遵循 [MCP 本机网络与可选资源模板契约](archived_docs/PULSARA_MCP_HOST_NETWORK_AND_OPTIONAL_TEMPLATES_IMPLEMENTATION_SPEC.zh.md)。

## 构建工具

`mise.toml` 固定 Node.js 22.23.3、uv 0.13.4 和 protoc 34.0；`.python-version` 固定 Python 3.12.15，仓库 `.venv` 由 uv 管理。

- 原生构建：C 编译器、make、pkg-config、Bison 和 Flex。
- macOS 15+ Apple Silicon：Xcode command-line tools，以及 `brew bundle --file desktop/Brewfile` 中的依赖。
- Ubuntu：binutils、patchelf、xz-utils、libopenblas0 及下方系统包。不强制 Ubuntu 发行版本号，实际原生依赖仍须兼容。
- Windows：系统自带的 .NET Framework 编译器，用于构建 interop helper。

随包资源包括 PostgreSQL 17.9、pgvector 0.8.1、pg_search 0.26.1、Python 和 Playwright Chromium。pg_search 使用 macOS ARM64 Sequoia 与 Ubuntu Noble PG17 x86_64/ARM64 制品。

## macOS

在仓库根目录运行：

```sh
brew bundle --file desktop/Brewfile
mise trust
mise install
mise run --jobs 1 desktop:package
```

DMG 位于 `desktop/release`。只生成未封装应用时，先运行 `mise run --jobs 1 desktop:verify`，再运行 `mise exec -- npm --prefix desktop run package:dir`。递归 ad-hoc 签名与严格应用包校验交给 electron-builder（`mac.identity: "-"`）；不要设为 `null` 跳过签名，否则可能保留缺少资源封装的 Electron 链接器签名。预览版沿用现有非 hardened runtime 行为。Developer ID 签名与 Apple 公证需要单独配置。

上传 DMG 前，运行 `hdiutil verify`，只读挂载，并用 `ditto` 将应用复制出来，对复制后的应用执行 `codesign --verify --deep --strict --verbose=2`，同时确认存在 `Contents/_CodeSignature/CodeResources`。签名后不得再修改应用。Gatekeeper 信任是另一项检查，ad-hoc 签名不提供 Developer ID 身份或 Apple 公证。

隔离首次启动时，使用真实规范路径下的私有数据目录，路径长度须满足 Unix socket 限制。macOS 的 `/var`、`/tmp` 是 `/private/var`、`/private/tmp` 的别名，后端会拒绝带路径别名的运行目录。版本检查使用随包 Python/CLI 入口；`--pulsara-cli` 是 Linux 可执行包装器的参数，不是 macOS 图形应用参数。

## Windows + WSL2

准备同一提交的两份 checkout：Linux 仓库位于 WSL 的 ext4 文件系统，Windows 仓库位于 Windows。各自使用独立的 `.venv` 和 `node_modules`。

在 WSL2 Ubuntu 中装好原生构建依赖后运行：

```sh
mise trust
mise install
mise run --jobs 1 desktop:verify
mise run --jobs 1 desktop:wsl-payload
```

将 `desktop/build/wsl-payload/payload.tar` 和 `manifest.json` 复制到 Windows 仓库的 `desktop/resources/wsl-payload/`，再在 PowerShell 中运行：

```powershell
mise trust
mise install
mise run --jobs 1 desktop:windows-package
```

EXE 位于 `desktop/release/Pulsara-<版本>-windows-x64.exe`。构建会校验 payload 版本与摘要，并编译 Windows helper。本版不发布 Windows 安装包。安装和数据位置见 [INSTALL.zh-CN.md](INSTALL.zh-CN.md)。

## 原生 Ubuntu

0.1.3-alpha.1 不发布 Ubuntu deb，原生 x64/arm64 桌面验收仍延期。本地构建先安装 mise，再以普通用户运行：

```sh
sudo apt update
sudo apt install -y build-essential bison flex pkg-config binutils patchelf \
  libopenblas0 xz-utils ca-certificates curl git libyaml-0-2 libcrypt1
mise trust
mise install
mise run desktop:bootstrap
mise exec -- .venv/bin/python -m playwright install-deps chromium
mise run --jobs 1 desktop:package
sudo apt install ./desktop/release/Pulsara-0.1.3-alpha.1-linux-amd64.deb
```

ARM64 用户换用 arm64 文件名。容器构建使用同一任务图：

```sh
docker buildx build --platform linux/arm64 --target installer \
  --output type=local,dest=desktop/release/linux-arm64 \
  -f desktop/build-environment.Dockerfile .
```

amd64 使用 `linux/amd64` 和独立输出目录。Docker 只用于构建，不是运行依赖。

## 增量构建与检查

`src/pulsara_agent/desktop_app/static/` 由 Vite 生成，不受 Git 追踪，也不进入源码归档。`desktop:application` 依赖 `desktop:renderer`，桌面打包会先生成前端，再创建应用 wheel；Hatch 显式把生成目录加入 wheel，缺少前端时明确报错，避免生成没有界面的安装包。开发用 editable 安装可先于前端构建。单独构建 wheel 时，按原流程准备原生依赖，运行 `npm --prefix frontend ci` 和 `npm --prefix frontend run build:desktop`，再用 uv 构建 wheel。

两次构建之间保留 `desktop/build`、`desktop/resources` 和 mise 缓存。应用修改会重建前端与 wheel，未变的原生依赖继续复用；构建脚本、工具、SDK 或系统库变化时，刷新对应资源。标准 `desktop:verify`、`desktop:package` 任务包含资源冒烟检查。

单项重建使用 `mise run --force desktop:postgres` 或 `mise run --force desktop:python-dependencies`。自定义 PostgreSQL 扩展使用 `mise run --force desktop:postgres -- --extension-lib /path/to/lib --extension-share /path/to/extension`，外部输入变化后重新运行。不要修改 SDK 缓存或更改 payload 摘要来复用旧资源。

```sh
mise exec -- .venv/bin/python -m pytest -m 'not postgres'
mise exec -- npm --prefix frontend test
mise exec -- npm --prefix desktop test
mise exec -- npm --prefix desktop run test:startup
mise exec -- .venv/bin/python -m pytest -q desktop/scripts/test_build_cache.py desktop/scripts/test_build_platform.py
```

数据库夹具和原生 Linux 检查见 [tests/linux/README.md](tests/linux/README.md)。Windows 的 Python 命令改用 `.venv/Scripts/python.exe`。资源准备好后，运行 `mise exec -- npm --prefix desktop start` 启动开发界面。

Windows 宿主的桌面测试使用 `mise exec -- npm --prefix desktop run test:windows`，与 Windows CI 共用入口，并检查实际 shell 字体打包。上面的桌面全量测试包含 POSIX 脚本、权限及符号链接行为，应在 macOS 或 WSL/Linux 中完整运行。

Electron 壳只打包启动页需要的 Inter、Noto Sans SC 和 Gelasio。工作台字体（含 MiSans 全字重）随 Python 包的前端静态资源分发，Windows 将其放在 WSL payload 中。启动页必须独立于 WSL；工作台专用字体不再复制进 Electron archive。

## 发布

0.1.3-alpha.1 仅提供本地构建的 macOS ARM64 DMG。Python/CLI 版本为 `0.1.3a1`，桌面端与 npm 元数据使用 `0.1.3-alpha.1`，设置页显示 `0.1.3`；Python 包、运行时常量、npm 包及所有锁文件必须一致。新的数据库基线不兼容早期发行版数据，见[安装说明](INSTALL.zh-CN.md)。

GitHub Actions 仅手动触发。Native Desktop Packages 工作流与 `release_artifacts.py assemble/draft` 要求四个平台安装包，因此本次仅 macOS 发布使用本地 `desktop:package` 和 GitHub CLI：提交版本及文档、推送对应标签，使用 `--verify-tag` 创建草稿，仅上传 `Pulsara-0.1.3-alpha.1-macos-arm64.dmg`，核对远端文件名与大小后，以 `--prerelease=false --latest` 发布为普通 Release 并标记 Latest。已发布的标签与资产保持不变。本版不附带 Windows、Ubuntu、blockmap 或更新源资产。

版本一致性检查：`mise exec -- uv run --no-sync python desktop/scripts/release_artifacts.py check --tag v0.1.3-alpha.1`。
