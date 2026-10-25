# Windows + WSL2 开发与构建准备

实现与发布边界以[当前合同](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md)为准。用户安装版不需要 Node、Python 或编译工具；以下要求仅用于从源码构建。

## 环境

- 使用已有 WSL2 Ubuntu 和默认非 root 用户；不要求专用发行版名、用户名或 Ubuntu 发行版本号。
- 保留 interop、Windows PATH 注入、自动挂载、代理/VPN 与现有发行版工作，不改全局 `.wslconfig`。
- Windows 与 WSL 使用两份相同提交的仓库；WSL 仓库位于原生 Linux 文件系统中，不共用 `.venv` 或 `node_modules`。
- 工具版本由 `mise.toml`、`.python-version`、npm 锁文件和资源构建脚本拥有。
- Windows 构建使用 Git、mise、Node、项目 Python 环境，以及 Windows .NET Framework 4.x 的 `csc.exe`。Windows 不构建 PostgreSQL 或 Linux 扩展。
- Python 优先用项目 uv 环境；若当前 Windows 的 uv 无法运行，可使用已存在的项目 `.venv/Scripts/python.exe`。不关闭系统安全设置绕过阻止。

## 构建

1. 将 Windows 和 WSL 两份仓库同步到同一提交，保全各自尚未提交的修改。
2. 在 WSL 仓库运行 `mise run --jobs 1 desktop:wsl-payload`。它沿既有资源 owner 构建应用、前端、随包 Python、浏览器、PostgreSQL 和扩展。
3. 将生成的 `desktop/build/wsl-payload/payload.tar` 与 `manifest.json` 复制到 Windows 仓库的 `desktop/resources/wsl-payload/`。
4. 在 Windows 仓库准备项目 Python 环境和 desktop npm 依赖，运行 `mise run desktop:windows-package`。已有依赖环境也可运行 `npm --prefix desktop run package -- --win --x64 --publish never`；beforePack 会校验 payload 并编译 helper。
5. 普通候选构建使用 `--publish never`。更新前正常退出应用；不要改 payload 摘要标记来复用同版本不同资源。

## 验证与数据

源码及集成验证范围见[定向重验清单](PULSARA_WINDOWS_SHARED_FIX_MINIMAL_REVALIDATION.zh.md)。只运行当前任务授权的检查；构建、合入 main、版本更新与发布资格分别处理。

真实模型配置由 Linux 有效 Pulsara home 的原设置 owner 读取，不能将原始设置或凭据写入测试输出。只在核实的本地可丢弃环境中进行故障注入，不删除用户日用发行版。原生文件弹窗、OAuth、睡眠或硬件动作需要用户参与时，按具体场景交接。

临时日志和实验输出放在 Git 忽略的本地目录。生产代码和回归测试不得依赖实验档案或个人临时路径。
