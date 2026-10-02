# Ubuntu Desktop 安装与验证交接

日期：2026-10-03。交给运行在 Ubuntu 虚拟机内的 Codex。

## 任务与已知环境

用户已经在 Windows 宿主机的 VMware Workstation 中安装 Ubuntu Desktop 24.04 LTS（x86_64），安装了 uv、Node.js，并克隆了 Pulsara。先确认实际版本和仓库位置，再完成剩余依赖安装、自动化测试与真实桌面验证。虚拟机此前分配了 2 个 CPU 核、6 GB 内存、45 GB 虚拟磁盘，使用 NAT 网络。

这是 Linux 客户机验证，不是 Windows 原生移植。此前 Ubuntu 后端与安装包已在容器中验证；这次重点补齐真实 GNOME 桌面、文件选择和默认应用打开行为。不要把容器测试通过写成桌面已经通过。

先读仓库根目录 `AGENTS.md`。Python 命令优先使用仓库根目录的 uv `.venv`。保留已有工作区修改，不要为了安装使用 `git reset --hard` 或 `git clean`。不要改模型前缀、数据库合同、权限或测试断言来绕过失败。

## 1. 拉取适配代码并核对环境

在用户克隆的仓库根目录执行：

```bash
git status --short
git branch --show-current
git pull --ff-only origin main
git log -1 --oneline
uname -m
cat /etc/os-release
uv --version
node --version
npm --version
df -h .
printf 'session=%s DISPLAY=%s WAYLAND_DISPLAY=%s\n' "$XDG_SESSION_TYPE" "$DISPLAY" "$WAYLAND_DISPLAY"
```

如果当前分支或本地修改使快进拉取失败，先核对并保留用户修改，不要强制覆盖。拉取后应存在：

- `src/pulsara_agent/web_app/native_desktop.py`
- `tests/test_native_desktop.py`
- `tests/linux/installed_package_smoke.py`
- `.github/workflows/ubuntu-tests.yml`

缺少这些文件说明还没有拿到 Ubuntu 适配，不能通过跳过对应测试继续宣布完成。

Node.js 需要 **22.13.0 或更新版本**，以 `frontend/package.json` 为准。Python 使用 3.12。仓库最好放在 Ubuntu 自己的文件系统中；`/mnt/hgfs` 适合传文件，不适合作为本轮虚拟环境、数据库或主要测试目录。

## 2. 安装系统依赖和 Python 环境

Ubuntu 的 APT 管系统组件；uv 管 Python 和锁定的 Python 依赖；npm 管前端依赖。Zenity 管原生目录选择，xdg-utils 管默认应用和文件管理器打开。不要另外实现文件选择器。

```bash
sudo apt update
sudo apt install -y ca-certificates curl git binutils zenity xdg-utils
uv python install 3.12
uv run --no-project --python 3.12 python tools/prepare_ripgrep.py
uv sync --locked --dev --python 3.12
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/pulsara --version
```

注意顺序：必须先准备随包携带的 ripgrep，再安装项目。准备脚本下载固定版本及许可文件，校验固定的上游摘要；Linux 构建通过 binutils 的 `readelf` 检查 ELF 架构和静态链接。不需要再安装系统 `rg`，也不要从 Mac 复制 `.venv` 或私有 rg 二进制。

Playwright 命令同时准备 Chromium 和其 Ubuntu 系统依赖，可能要求 sudo 密码。以普通用户运行 uv、Python、npm 和 Pulsara；只有系统安装与 Docker 管理需要 sudo。

如果下载失败，记录具体 URL、HTTP/DNS 错误和代理模式。用户正在使用 Hiddify；浏览器能联网不代表 APT、Docker 守护进程和 uv 都能联网。先分别定位，不要关闭证书验证、删除锁文件或随意替换依赖版本。

## 3. 安装、测试并构建本地前端

仍在仓库根目录执行：

```bash
npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend test
npm --prefix frontend run build:local
```

`build:local` 写入 `src/pulsara_agent/web_app/static`，完整应用使用这里的资源。它会产生静态文件的 Git diff，这是现有构建路径。不要使用 `npm run build` 的网站部署产物替代本地应用资源。首次运行 npm/构建时观察磁盘和内存；失败时保留原始错误。

## 4. PostgreSQL 16 与 pgvector

优先复用根目录 `docker-compose.yml` 的 `postgres` 服务，避免单独配置宿主机 PostgreSQL 和手工编译 pgvector。不需要启动 `oxigraph` 来完成这轮安装与桌面验证。

若 Ubuntu 内还没有 Docker Engine/Compose，可用 Ubuntu 24.04 软件源安装：

```bash
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo docker compose version
```

Ubuntu 软件包说明见 [docker-compose-v2](https://packages.ubuntu.com/eu/noble/docker-compose-v2)。如果已安装 Docker 官方软件源版本，则复用现有 Engine 和 Compose，不混装另一套；官方安装说明见 [Docker Ubuntu 文档](https://docs.docker.com/engine/install/ubuntu/)。

检查端口及现有 Compose 实例后启动：

```bash
ss -ltn '( sport = :5432 )'
sudo docker compose ps
sudo docker compose up -d postgres
sudo docker compose exec -T postgres pg_isready -U pulsara_admin -d pulsara
sudo docker compose exec -T postgres psql -U pulsara_admin -d pulsara -c "SELECT version();"
sudo docker compose exec -T postgres psql -U pulsara_admin -d pulsara -c "SELECT name, default_version FROM pg_available_extensions WHERE name = 'vector';"
sudo docker compose exec -T postgres psql -U pulsara_admin -d pulsara -c "SELECT rolname FROM pg_roles WHERE rolname = 'pulsara_runtime';"
```

若 5432 已被其他数据库占用，先明确其归属，不要杀掉未知服务。若复用了旧容器且缺少 runtime role，可运行仓库已有的幂等初始化脚本：

```bash
sudo docker compose exec -T postgres bash -s < docker/postgres-init/001-runtime-role.sh
```

在未设置密码覆盖变量的新 Compose 实例中，开发连接为：

```text
Runtime DSN: postgresql://pulsara_runtime:pulsara_runtime@127.0.0.1:5432/pulsara
Admin DSN:   postgresql://pulsara_admin:pulsara_admin@127.0.0.1:5432/pulsara
```

这里是仓库的本地开发凭据，不是用户的模型或 MCP 密钥。如果用户已有密码覆盖或保存配置，读取实际配置，不要强行替换。Compose 数据保存在仓库 `.pulsara/postgres`，`down` 不会删除这个绑定目录。重置前必须验证确切的本地可丢弃目标，不得重置未知或远端数据库。

## 5. 自动化验证

全量 Python 测试使用 fixture 模型，不需要生产模型 API key。PostgreSQL 测试会创建、迁移并删除独立的 `pulsara_test_*` 数据库，管理员连接需要创建数据库的权限。

以下环境变量只用于自动化测试，不是 Pulsara 生产配置入口。如果第 4 节使用了其他实际连接，应相应替换：

```bash
export PULSARA_POSTGRES_ADMIN_DSN='postgresql://pulsara_admin:pulsara_admin@127.0.0.1:5432/pulsara'
export PULSARA_POSTGRES_DSN='postgresql://pulsara_runtime:pulsara_runtime@127.0.0.1:5432/pulsara'
export CI=true
.venv/bin/ruff check
.venv/bin/python -m pytest -q tests/test_native_desktop.py tests/test_native_directory_picker.py tests/test_session_file_preview.py tests/test_private_ripgrep_packaging.py
.venv/bin/python -m pytest -q -n 2
```

`-n 2` 与该虚拟机的两个核匹配，只控制物理测试并发。设置 `CI=true`，使关键数据库 fixture 不会因为数据库缺失而悄悄跳过。结束后检查总结中的 skipped；真实模型的显式 opt-in 项可以未运行，但不得将因缺失依赖跳过的测试视为通过。完成自动化测试后：

```bash
unset CI PULSARA_POSTGRES_ADMIN_DSN PULSARA_POSTGRES_DSN
```

再验证实际 wheel，不能只验证 editable 安装。以下命令从仓库根目录开始，不需要生产数据库或模型凭据：

```bash
pulsara_repo="$PWD"
wheel_smoke_root="$(mktemp -d)"
uv build --offline --no-build-isolation --sdist --wheel --out-dir "$wheel_smoke_root/dist"
uv export --locked --no-dev --no-emit-project --no-hashes --output-file "$wheel_smoke_root/constraints.txt" > /dev/null
uv venv --python 3.12 "$wheel_smoke_root/env"
uv pip install --python "$wheel_smoke_root/env/bin/python" --constraint "$wheel_smoke_root/constraints.txt" "$wheel_smoke_root"/dist/*.whl
(
  cd "$wheel_smoke_root"
  env -u PYTHONPATH "$wheel_smoke_root/env/bin/python" "$pulsara_repo/tests/linux/installed_package_smoke.py"
)
```

成功时输出 launcher、assets、search、terminal rg、Skill/Plugin publication、HTTP startup/shutdown 全部通过。这个脚本确认从隔离环境的 site-packages 加载实际 wheel；不要设置源码 `PYTHONPATH` 让它误通过。Hatch 构建本身离线，但隔离环境安装依赖仍可能需要网络。

## 6. 启动完整应用并保存本机配置

从 Ubuntu 图形桌面里的普通用户终端启动，不能使用 sudo、无显示的 SSH 或容器替代桌面：

```bash
.venv/bin/pulsara app
```

默认选择空闲端口，终端打印实际 URL 并打开默认浏览器。使用这个实际地址，不要假设仍是 Mac 上的端口。保持该进程运行，Ctrl+C 正常退出。

在“设置 → 本地服务”的 PostgreSQL 区域保存第 4 节的实际 Runtime/Admin DSN，执行“初始化 / 升级”和“检查连接”。应用和 `pulsara db` 都读取有效 Pulsara home 下的 `local-settings.yaml`，不读取测试 DSN 环境变量作为生产配置。

如需终端复核，保存后在另一个仓库终端执行：

```bash
.venv/bin/pulsara db verify --deep
```

默认 home 是 `~/.pulsara`；显式 `PULSARA_HOME` 则以 resolver 实际结果为准。已有保存配置是只读诊断输入，不要手改或覆盖来绕过失败。Linux 新机器如果还没有模型配置，请用户在“设置 → 模型”中配置一次；不要索取密钥到聊天、复制 Mac 的原始设置文件、使用虚构密钥或添加 `.env` 回退。无模型配置时仍可完成环境、自动化测试和原生目录选择，真实会话要等有效配置。

## 7. 真实 Ubuntu 桌面验证清单

按项记录输入、可见结果和失败原因。GUI 自动化若不可用，让用户执行对应点击并提供结果，不得把 mock 或 HTTP 200 当作窗口实际打开。

1. **启动与停止**：默认浏览器打开真实页面；本地服务正常连接；Ctrl+C 后应用退出，重复启动正常。
2. **选目录**：新建会话 → 指定目录，应打开 Zenity 原生目录选择器。选择 Ubuntu 内实际目录，确认显示的工作目录正确；另测取消、Escape、连续再次打开，不能误报创建成功或遗留卡住的窗口。
3. **路径**：选一个含空格和中文的目录再测。例如用户目录下 `pulsara-gui-check/中文 空格`。测试目录放在 Ubuntu 本地文件系统。
4. **能力目录打开**：能力页面的目录打开入口能够在文件管理器中打开用户 `.agents` 和有效 Pulsara home；设置页显示的 Pulsara 用户目录与 resolver 一致。
5. **文件预览**：真实会话读取测试文件后打开文件预览。分别验证“系统打开”和“在文件管理器中显示”；Linux 的后者只保证打开所在目录，不承诺选中该文件。实际默认应用要打开正确文件，中文和空格路径不能截断。
6. **错误与恢复**：缺失桌面组件或不可用路径应显示可理解的错误。若验证缺失 Zenity，可给单个测试进程使用不包含 Zenity 的 PATH；不要为了这个测试卸载用户组件或改全局环境。
7. **真实模型与工具**：复用保存模型配置，先完成一轮短文本响应，再读取/搜索上述测试目录，运行一个简单 terminal 命令，委派一个任务并等待结果。检查任务详情和继承上下文显示，确认会话后续仍可继续。
8. **桌面会话类型**：记录 `XDG_SESSION_TYPE`。先完成当前会话类型的全部验证；若登录界面提供 Ubuntu on Xorg，可注销切换后复测目录选择和文件打开。无法切换时明确另一类型未验证，不自行修改系统显示管理器。

Pulsara 的原生窗口打开在运行后端的 Ubuntu 上，不会打开 Windows 宿主机的窗口。VMware 的共享剪贴板、共享文件夹、Hiddify 和输入法问题需与 Pulsara 产品问题区分，记录清楚复现位置。

## 8. 交付结果

在忽略的 `output/ubuntu-desktop-validation/` 中保存必要日志、截图与简短报告；不要将浏览器缓存、数据库目录或大批验证产物提交到 Git。

报告包含：实际 Git commit/dirty 状态、Ubuntu/架构、Python/Node 版本、Wayland/X11、各安装步骤、Python/前端/实际 wheel 的结果、真实 GUI 清单结果、真实模型是否配置且运行，以及仍未验证的项。只排除实际密钥值，不整段删除错误信息或模型输入输出。

发现代码问题时，先定位当前实现再做小范围修复，并补相应验证。最终检查 `git diff`；不要降低断言、加 skip/xfail 或改数据来获得绿色。用户当前授权已覆盖安装依赖与验证，不需要对每条可逆的安装或测试动作重复询问；后续提交/推送遵循用户在 Linux 会话中的明确指示。
