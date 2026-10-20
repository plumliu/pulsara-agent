# Build Pulsara / 从源码构建 Pulsara

macOS users can download the alpha.3 DMG from [Releases](https://github.com/plumliu/pulsara-agent/releases/latest). Ubuntu users should build the current source using this guide; the available alpha.2 deb packages do not include the latest fixes. User setup and updates are covered in [INSTALL.md](INSTALL.md).

macOS 用户可下载 alpha.3 [桌面安装包](https://github.com/plumliu/pulsara-agent/releases/latest)。Ubuntu 用户建议按本页从当前源码构建，现有 alpha.2 deb 不含最新修复。本页也适用于贡献者；安装与更新见 [INSTALL.md](INSTALL.md)，修改代码前请阅读 [AGENTS.md](AGENTS.md)。

## English

GitHub Actions run manually for now; pushes (including tags) and pull requests do not trigger them. Start a workflow from the Actions page when needed. The current alpha.3 release was built and published locally with only a macOS DMG. The retained Native Desktop Packages draft workflow and assembly script expect one DMG and two Ubuntu deb installers.

Build on the target platform. `mise.toml` pins Node.js 22.23.3 and uv 0.13.4, retains protoc 34.0, and `.python-version` pins uv-managed Python 3.12.15. The repository `.venv` remains uv-owned. Native builds need a C compiler, `make`, `pkg-config`, Bison, and Flex. macOS builds need Xcode command-line tools and `dylibbundler`; Ubuntu builds need `binutils`, `patchelf`, `xz-utils`, and `libopenblas0`. These are build-machine requirements.

The resource builder builds PostgreSQL **17.9** and **pgvector 0.8.1** from pinned upstream source archives, downloads the pinned **pg_search 0.26.1** package for macOS ARM64 Sequoia or Ubuntu Noble PG17 x86_64/ARM64, verifies downloads, and bundles their native dependencies. It also packages a relocatable Python runtime and Playwright Chromium. Building macOS resources therefore requires macOS 15 or newer. For custom extension payloads, run `mise run --force desktop:postgres -- --extension-lib /path/to/lib --extension-share /path/to/extension`; the same version and architecture checks apply. Re-run that forced task whenever those external files change.

From the repository root:

```bash
# macOS only, once: brew bundle --file desktop/Brewfile
mise trust
mise install
mise run --jobs 1 desktop:verify
mise exec -- npm --prefix desktop run package:dir
```

The unpacked application is written under `desktop/release`. `mise run --jobs 1 desktop:package` builds, validates and produces the configured DMG or Linux deb artifacts; it never publishes them. Linux packaging uses electron-builder's ICNS conversion to supply standard `hicolor` PNG sizes; a lone 1024px PNG lands in a directory Ubuntu's theme does not index. Release signing and notarization require separate release configuration; the local build is unsigned. The mise graph refreshes the renderer and application wheel on every build and reuses dependency resources only when their declared inputs and outputs remain valid. `npm --prefix desktop start` runs the Electron development shell against prepared bundled resources; it does not expose a second browser-based product entry.


### Resource reuse

mise owns task ordering and content-based freshness; uv owns locked Python dependency installation, Playwright owns browser revisions, and electron-builder owns installers. Pulsara only declares their inputs/outputs and records exact native tool/SDK versions in disposable `desktop/build/environment.json`. It does not maintain a separate fingerprint registry or artifact recovery system.

- Application code, prompts, Skills and UI changes always produce a fresh application wheel; they reuse PostgreSQL, Python dependencies and Chromium.
- Runtime requirements are exported by uv without project/dev metadata. Project version bumps and dev-only lock changes therefore keep the native dependency cache warm. A changed runtime export rebuilds Python and browser resources.
- Native recipes, configured tool versions, compiler/SDK or Ubuntu system packages changing invalidate the affected resource tasks. Missing declared outputs and failed tasks cannot count as successful cache hits.
- `mise run --force desktop:postgres` or `mise run --force desktop:python-dependencies` explicitly rebuilds that resource. The old manual `--skip-*` switches are removed. Cache hits still pass architecture/version checks, and packaging always runs the relocation smoke in a disposable database.

uv installs with copy mode and browser binaries are copied from Playwright's own cache before relocation. Native rewriting must never modify SDK cache files. Keep `desktop/build`, `desktop/resources` and mise's local cache between builds; clearing them deliberately makes the next build cold.

### Ubuntu 24.04 native build

Install `mise` first, then run the following as your normal desktop user. Use `sudo` only for system dependencies and the final package installation. The same steps work on amd64 and arm64; ARM64 users should change the final filename to `Pulsara-0.1.0-alpha.3-linux-arm64.deb`. The build includes the latest source and does not publish anything.

```bash
sudo apt update
sudo apt install -y build-essential bison flex pkg-config binutils patchelf \
  libopenblas0 xz-utils ca-certificates curl git libyaml-0-2 libcrypt1
git clone https://github.com/plumliu/pulsara-agent.git
cd pulsara-agent
mise trust
mise install
mise run desktop:bootstrap
mise exec -- .venv/bin/python -m playwright install-deps chromium
mise run --jobs 1 desktop:package
sudo apt install ./desktop/release/Pulsara-0.1.0-alpha.3-linux-amd64.deb
```

### Ubuntu container builds

The Dockerfile pins Ubuntu and mise images by digest, installs all native packaging tools (including xz), and uses the same pinned tools and task graph. Playwright installs its own OS prerequisites from the locked SDK version. Builds run as an unprivileged user so the PostgreSQL smoke can exercise `initdb`. BuildKit cache mounts separate resources and task state by architecture. Docker is only a build-machine option, not an application dependency.

```bash
docker buildx build --platform linux/arm64 --target installer \
  --output type=local,dest=desktop/release/linux-arm64 \
  -f desktop/build-environment.Dockerfile .
# amd64: use linux/amd64 and a different output directory
```

To prepare only the fixed environment, use `--target environment --load -t pulsara-build-env:arm64` instead of the installer/output options. Do not prune BuildKit caches after every build. If Docker injects an unavailable proxy, explicitly pass empty proxy build arguments for that invocation; no global Docker settings need changing.

Build-cache regressions run through the real mise task engine and real uv wheel builds:

```bash
mise exec -- .venv/bin/python -m pytest -q desktop/scripts/test_build_cache.py
```

Backend checks use the repository environment; desktop shell checks use Node's test runner:

```bash
.venv/bin/python -m pytest -m 'not postgres'
npm --prefix frontend test
npm --prefix desktop test
npm --prefix desktop run test:startup
```

The startup regression runs real Electron windows with the production preload and IPC. Hiding, minimizing, replacing or destroying a fading startup cover must settle startup immediately; a background animation must not keep the workbench inert. Electron owns native view destruction, and cover cleanup also handles an already destroyed WebContents.

Database integration fixtures and Ubuntu package checks are described in [tests/linux/README.md](tests/linux/README.md). Actual desktop acceptance also checks native resources, shutdown, restart, and real model work as specified in the desktop plan.


## 中文

GitHub Actions 暂时仅手动运行，推送（包括标签）和 Pull Request 均不自动触发。需要时在 Actions 页面手动启动。当前 alpha.3 在本地构建并发布，仅上传 macOS DMG；保留的 Native Desktop Packages 草稿工作流和汇总脚本仍要求一个 DMG 与两个 Ubuntu deb。

请在目标平台构建。`mise.toml` 固定 Node.js 22.23.3、uv 0.13.4，并保留 protoc 34.0；`.python-version` 固定由 uv 管理的 Python 3.12.15。仓库 `.venv` 仍由 uv 管理。原生构建还需要 C 编译器、`make`、`pkg-config`、Bison 和 Flex。macOS 还需要 Xcode command-line tools 与 `dylibbundler`；Ubuntu 需要 `binutils`、`patchelf`、`xz-utils` 和 `libopenblas0`。这些依赖用于构建机。

资源脚本从固定上游源码构建 PostgreSQL **17.9** 与 **pgvector 0.8.1**，下载对应 macOS ARM64 Sequoia 或 Ubuntu Noble PG17 x86_64/ARM64 的 **pg_search 0.26.1** 包，校验下载并收集动态库依赖，再打包可搬移 Python 运行时及 Playwright Chromium。因此 macOS 资源构建需要 macOS 15 或更新版本。自定义扩展使用 `mise run --force desktop:postgres -- --extension-lib /path/to/lib --extension-share /path/to/extension`，仍执行版本和架构检查；外部文件变化时重新执行该强制构建命令。

在仓库根目录运行：

```bash
# macOS only, once: brew bundle --file desktop/Brewfile
mise trust
mise install
mise run --jobs 1 desktop:verify
mise exec -- npm --prefix desktop run package:dir
```

未封装的应用位于 `desktop/release`。`mise run --jobs 1 desktop:package` 完成构建、验证并生成配置中的 DMG 或 Linux deb，不执行发布。发布签名与公证需要单独配置，本地构建不带发布签名。mise 每次刷新前端和应用 wheel；只有声明的依赖输入未变且输出仍存在时，才复用对应资源。`npm --prefix desktop start` 使用已准备的随包资源运行 Electron 开发壳，不提供另一套浏览器产品入口。


### 资源复用

mise 管理任务依赖和内容变化判断，uv 管理锁定的 Python 依赖，Playwright 管理浏览器版本，electron-builder 管理安装包。Pulsara 只声明输入输出，并在可删除的 `desktop/build/environment.json` 中记录原生工具和 SDK 的完整版本值，不另建指纹注册表或产物恢复机制。

- 每次都重新生成应用 wheel，包含最新代码、提示词、Skills 和前端；这些修改不会重编 PostgreSQL 或重新准备 Python、Chromium 依赖。
- uv 导出不含项目与开发依赖元数据的运行依赖。只修改应用版本或开发依赖时，原生依赖缓存保持有效；运行依赖导出结果变化时，刷新 Python 和浏览器资源。
- 原生构建脚本、配置的工具版本、编译器/SDK 或 Ubuntu 系统包变化时，对应资源失效重建。缺少声明的输出、任务构建失败，都不能算缓存命中。
- 可用 `mise run --force desktop:postgres` 或 `mise run --force desktop:python-dependencies` 强制重建某项资源，旧的手动 `--skip-*` 参数已移除。缓存命中仍需校验架构和版本；打包前始终在临时数据库中执行搬移检查。

uv 使用复制模式安装，浏览器也先从 Playwright 缓存复制到产物目录，再做原生库搬移；不能修改 SDK 缓存里的二进制文件。两次构建之间保留 `desktop/build`、`desktop/resources` 和 mise 本地缓存。主动清理它们后，下次将重新构建。

### Ubuntu 24.04 本机构建

先安装 `mise`，再以普通桌面用户执行以下命令，仅安装系统依赖和最终 deb 时使用 `sudo`。amd64 和 arm64 步骤相同；ARM64 用户把最后的文件名改为 `Pulsara-0.1.0-alpha.3-linux-arm64.deb`。生成的安装包包含最新源码，不会执行发布。

```bash
sudo apt update
sudo apt install -y build-essential bison flex pkg-config binutils patchelf \
  libopenblas0 xz-utils ca-certificates curl git libyaml-0-2 libcrypt1
git clone https://github.com/plumliu/pulsara-agent.git
cd pulsara-agent
mise trust
mise install
mise run desktop:bootstrap
mise exec -- .venv/bin/python -m playwright install-deps chromium
mise run --jobs 1 desktop:package
sudo apt install ./desktop/release/Pulsara-0.1.0-alpha.3-linux-amd64.deb
```

### Ubuntu 容器构建

Dockerfile 用 digest 固定 Ubuntu 和 mise 镜像，预装包含 xz 在内的完整打包工具，使用同一份版本配置和任务图。Playwright 按锁定的 SDK 版本安装自身的系统依赖；构建使用普通用户，使 PostgreSQL 的 `initdb` 冒烟验证能正常运行。BuildKit 按架构隔离资源与任务缓存。Docker 只用于构建，不是使用 Pulsara 的前提。

```bash
docker buildx build --platform linux/arm64 --target installer \
  --output type=local,dest=desktop/release/linux-arm64 \
  -f desktop/build-environment.Dockerfile .
# amd64 使用 linux/amd64，并换一个输出目录
```

只准备固定环境时，将 installer/output 选项换成 `--target environment --load -t pulsara-build-env:arm64`。不要在每次构建后清理 BuildKit 缓存。如果 Docker 注入了不可用的代理，可仅对此次构建显式传入空的代理 build arguments，不需要改全局 Docker 配置。

缓存回归使用真实 mise 任务引擎与真实 uv wheel 构建：

```bash
mise exec -- .venv/bin/python -m pytest -q desktop/scripts/test_build_cache.py
```

后端检查使用仓库环境，桌面壳检查使用 Node 测试运行器：

```bash
.venv/bin/python -m pytest -m 'not postgres'
npm --prefix frontend test
npm --prefix desktop test
npm --prefix desktop run test:startup
```

启动回归使用真实 Electron 窗口、生产 preload 与 IPC。淡出期间隐藏、最小化、替换或销毁遮罩都必须及时结算启动，不能因后台动画使工作台持续不可交互。原生视图销毁由 Electron 管理，遮罩清理同时处理 WebContents 已销毁的情况。

数据库集成夹具与 Ubuntu 包检查见 [tests/linux/README.md](tests/linux/README.md)。实际桌面验收还包括原生资源、退出、重开与真实模型任务，范围以桌面方案为准。
