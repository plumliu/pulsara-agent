# Windows开发准备/G0-W清单：桌面交叉审阅

2026-10-24。模型沿用gpt-6.1-sol/high。结论：**当前401行候选无阻断，同意作为Windows接手操作说明交付**；这不是Windows命令实跑、G0-W通过或Phase B授权。未修改根文档/生产/git，未运行构建、Windows/WSL、数据库或provider。

已通读 `PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md`，交叉阅读terminal `windows-handoff/recommendations.md`、backend `windows-handoff/proposal.md`，复核当前main、package/lock、mise任务图、Electron44.9.0实际SDK与三个纯逻辑测试文件。

## 通过的边界

- Windows原生Node/Electron宿主与Linux ext4独立clone清楚；同Git提交、各自产物/依赖、普通用户/root维护分离，路径中Windows用户目录有引号，不依赖Git Bash改写Linux argv。没有引导共享node_modules/.venv或在/mnt/c构建Linux资源。
- `mise install node@22.23.3`与`mise exec node@22.23.3 -- ...`只选择宿主Node；npm.cmd避免PowerShell同名脚本问题，不需全局放宽ExecutionPolicy。Linux mise与当前CI pin另装，Windows无需提前安装产品Python/PG/pwsh/MSVC。
- 实际Electron44.9.0没有postinstall，index.js首次require会执行同包install.js；文档显式安装锁定binary再`--version`符合实际SDK。没有npx latest或跨架构变量伪装。
- Windows现可运行的是tsc与browser-policy/oauth-return/ui-preferences三个文件共5个case；当前46项全集含真实POSIX fixture，留Linux执行且不改skip/断言。实际Windows结果明确待记录。
- `main.ts`目前在顶层选posixHostOperations并同步resolve resources/python/bin/python3，随后选POSIX PG；现构建配方只支持Mac/Ubuntu、packaging无正式Windows目标。文档明确不能npm start/package，不把装好Node/WSL误报产品已接通。
- 专用Pulsara导入同名冲突先停、只操作本次发行版、非root默认用户、禁interop/automount真实PE负测、不以ENOENT代替负测均与spec一致；未授权unregister/global shutdown/默认distro更改。
- 第6节资源图会先完成installed项目再pytest；Playwright OS库以明确root维护调用已建立的venv，普通构建继续nonroot。Linux资源/WSL结果没有冒充Phase A原生Ubuntu双架构/GUI资格。
- pipe示例使用Windows原生Node绝对wsl.exe、固定distro/user/cwd、shell:false与pipe3，输入含中文分chunk及大量stderr；仅给最小byte/EOF/exit结果，不冒充JSONL、join或整门C。后来helper/取消/EOF未知结果仍要求真实probe。
- F要求真实Windows Electron与有限Linux目录服务的图形可行性，Explorer只算子项；UNC转换、Windows File按字节导入、preview/download没有偷换成file://。N明确HTTP只算基本可达，TLS连接身份、Session退休、OAuth-only动作仍待独立probe，未用明文challenge/忽略证书替代。
- 探针/原始Mac output不会随Git clone交付已说明；证据含实际命令/exit/事实，不造hash；A待验收、G0未执行、B未进入三个状态闭合。

## 建议的三处小补文（非阻断）

1. §3.3的`node --version`后补如下观察，使文本要求的win32/x64/实际exe来源可直接记录：

   ```cmd
   mise exec node@22.23.3 -- node -p "JSON.stringify({platform:process.platform,arch:process.arch,version:process.version,execPath:process.execPath})"
   ```

   Node22.23.3官方附npm10.9.9，可同时在版本说明写出预期npm值；Electron内部Node与开发Node不是同一版本事实，不要求两者相等。

2. §7.1重定向命令后立即`echo %ERRORLEVEL%`，让新手能观察实际exit0。CMD重定向只保存文本，不把退出码保存进日志；必须在另一条命令覆盖它之前记录。
3. 工具表中2026.10.15是CI/Linux安装pin；Windows的winget示例并未加`--version`，建议写明“Linux按CI版本；Windows记录实际mise且满足仓库最低版本”，或提供确定版本安装方式。不应暗示winget无版本参数仍安装同pin。

官方依据：[mise exec](https://mise.jdx.dev/cli/exec)、[mise Windows安装](https://mise.jdx.dev/installing-mise.html#windows)、[mise Windows/WSL支持边界](https://mise.jdx.dev/faq.html)、[Electron安装机制](https://www.electronjs.org/docs/latest/tutorial/installation)、[Node22.23.3发布](https://nodejs.org/en/blog/release/v22.23.3)。本审阅没有以这些平台文档替代目标机器证据。
