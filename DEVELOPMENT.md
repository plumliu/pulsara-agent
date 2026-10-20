# Build Pulsara / 从源码构建 Pulsara

For the desktop app, download an installer from [Releases](https://github.com/plumliu/pulsara-agent/releases/latest). User setup and updates are covered in [INSTALL.md](INSTALL.md).

本页面向贡献者。直接使用 Pulsara 请下载[桌面安装包](https://github.com/plumliu/pulsara-agent/releases/latest)，安装与更新见 [INSTALL.md](INSTALL.md)。修改代码前请阅读 [AGENTS.md](AGENTS.md)。

## English

Build on the target platform using Node.js 22.13+, npm, `uv` with managed Python 3.12, a C compiler, `make`, `pkg-config`, Bison, and Flex. macOS builds need Xcode command-line tools and `dylibbundler`; Ubuntu builds need `binutils`, `pax-utils` (`lddtree`), `patchelf`, and `libopenblas0`. These are build-machine requirements.

The resource builder builds PostgreSQL **17.9** and **pgvector 0.8.1** from pinned upstream source archives, downloads the pinned **pg_search 0.26.1** package for macOS ARM64 Sequoia or Ubuntu Noble PG17 x86_64/ARM64, verifies downloads, and bundles their native dependencies. It also packages a relocatable Python runtime and Playwright Chromium. Building macOS resources therefore requires macOS 15 or newer. To supply your own matching extension payload instead, pass both `--extension-lib` (native libraries) and `--extension-share` (control/SQL files); the same version and architecture checks apply.

From the repository root:

```bash
uv sync --frozen --managed-python --no-install-project
.venv/bin/python tools/prepare_ripgrep.py
uv sync --frozen --managed-python
npm --prefix frontend ci
npm --prefix frontend run build:desktop
npm --prefix desktop ci
.venv/bin/python desktop/scripts/prepare_resources.py
npm --prefix desktop run package:dir
```

The unpacked application is written under `desktop/release`. `npm --prefix desktop run package` produces the configured DMG or Linux deb artifacts. Release signing and notarization require separate release configuration; the local build is unsigned. Re-run the resource builder after Python changes, and rebuild the frontend before refreshing resources after UI changes. `npm --prefix desktop start` runs the Electron development shell against prepared bundled resources; it does not expose a second browser-based product entry.

Backend checks use the repository environment; desktop shell checks use Node's test runner:

```bash
.venv/bin/python -m pytest -m 'not postgres'
npm --prefix frontend test
npm --prefix desktop test
```

Database integration fixtures and Ubuntu package checks are described in [tests/linux/README.md](tests/linux/README.md). Actual desktop acceptance also checks native resources, shutdown, restart, and real model work as specified in the desktop plan.


## 中文

请在目标平台构建。构建机需要 Node.js 22.13+、npm、`uv` 管理的 Python 3.12、C 编译器、`make`、`pkg-config`、Bison 和 Flex。macOS 还需要 Xcode command-line tools 与 `dylibbundler`；Ubuntu 需要 `binutils`、`pax-utils`（提供 `lddtree`）、`patchelf` 和 `libopenblas0`。这些依赖用于构建机。

资源脚本从固定上游源码构建 PostgreSQL **17.9** 与 **pgvector 0.8.1**，下载对应 macOS ARM64 Sequoia 或 Ubuntu Noble PG17 x86_64/ARM64 的 **pg_search 0.26.1** 包，校验下载并收集动态库依赖，再打包可搬移 Python 运行时及 Playwright Chromium。因此 macOS 资源构建需要 macOS 15 或更新版本。如需使用自行准备的匹配扩展产物，可以同时传入 `--extension-lib`（动态库目录）和 `--extension-share`（control/SQL 目录）；同样执行版本和架构检查。

在仓库根目录运行：

```bash
uv sync --frozen --managed-python --no-install-project
.venv/bin/python tools/prepare_ripgrep.py
uv sync --frozen --managed-python
npm --prefix frontend ci
npm --prefix frontend run build:desktop
npm --prefix desktop ci
.venv/bin/python desktop/scripts/prepare_resources.py
npm --prefix desktop run package:dir
```

未封装的应用位于 `desktop/release`。`npm --prefix desktop run package` 生成配置中的 DMG 或 Linux deb 产物。发布签名与公证需要单独配置，本地构建不带发布签名。Python 变更后重新运行资源脚本；UI 变更后先重新构建前端，再更新资源。`npm --prefix desktop start` 使用已准备的随包资源运行 Electron 开发壳，不提供另一套浏览器产品入口。

后端检查使用仓库环境，桌面壳检查使用 Node 测试运行器：

```bash
.venv/bin/python -m pytest -m 'not postgres'
npm --prefix frontend test
npm --prefix desktop test
```

数据库集成夹具与 Ubuntu 包检查见 [tests/linux/README.md](tests/linux/README.md)。实际桌面验收还包括原生资源、退出、重开与真实模型任务，范围以桌面方案为准。
