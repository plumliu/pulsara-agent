# Windows开发准备/G0-W新清单全文审阅

2026-10-24；terminal / gpt-6.1-sol high。审阅根 `PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md` 全文，以及backend/desktop `windows-handoff/proposal.md`、冻结规格G0/执行域/生命周期与实际Linux构建脚本。未改根/源码/tests，未运行Windows/WSL实验，不进入B。

**结论：同意交付；无剩余阻断。Windows命令/示例必须保持待目标机实测，当前没有G0通过或产品已支持Windows的声明。**

## 实际发现与闭合

初版7.1把process.env复制为普通JS对象后只delete env.WSLENV，会漏掉Windows环境键的大小写变体。我给root的精确修文已采纳：遍历Object.keys，对key.toUpperCase()==='WSLENV'逐项删除。已读最新源码段确认该变更。root亦采纳Windows Node platform/arch/version/execPath JSON观测，可区分win32/x64与误进Linux终端。

该修文不创建新环境框架/安全registry，仅使实验自身的“不转发宿主WSLENV清单”真实成立。没有要求给生产新增Windows适配。

## 全文核对结果

- Windows cmd、Pulsara root维护、pulsara nonroot bash明确标注，WSL引擎管理员动作与日常普通用户分开。工具/Node/uv/protoc/Python/PG pins和现repo来源一致；PowerShell7/系统PG/Rust/pgrx/Windows Python不作为当前无关前提。
- 两个独立clone/同Git提交、各自.venv/node_modules/dist/resources/build/缓存、Linux ext4根与WindowsNTFS路径有清楚说明；不复制跨OS产物，不以C盘直接workspace替代目标。Windowsclone只安装Node/Electron/tsc与明确纯测试小集合，禁当前npm start/package/mise POSIX图，避免误以为PhaseB已接通。
- rootfs使用官方Ubuntu24.04 amd64入口，浮动current不被当作支持版本；下载文件/实际摘要/签名/包版本须记录并固定本次材料。未知Pulsara同名/残留不接管、不unregister重试。一次root provision仅本次发行版，不给pulsara免密sudo，不改默认distro/全局.wslconfig。
- wsl.conf default user/interop/PATH/automount四个边界与重开生效明确；terminate仅标注为初次无工作配置维护或显式fault，而非正常cleanup。真正PE正负对照区别于不存在的/mnt/c路径，不能用ENOENT或清环境变量伪造interop禁用。
- Linux依赖来自当前desktop workflow/build_environment；bootstrap、OS库root维护、非root资源graph、测试顺序正确。不照历史PG16/Docker/web_app文档。Playwright install-deps明确由root维护入口调用本checkout已准备的venv Python；其余构建不root。mise verify包含真实自有PG/Chromium资源smoke，清单没有说它只做无副作用静态检查。
- 7.1用Windows Node直接spawn系统wsl.exe，shell:false/固定distro-user/明确Linuxcwd/原始pipe3；Python先读完stdin再写大stderr/回显bytes/退出7，两端同时drain，close后核完整bytes/2MiB stderr与退出。输入规模只是探针，不引入产品cap；spawn/stdin错误或assert失败均exitCode1。UTF8第一字节处分段有意义，Windows行为仍应真实跑后确认，不能以我静态审阅代替。
- 该示例清楚只证明最早byte/EOF/exit子项，没有冒称JSONL/kernel close/PGID/后代join/取消/完整G0-C。后续原desktop_runtime、独立PG/home、partial帧/unknown、pipe/carrier故障、sleep/reopen、真实terminal/MCP/hooks均作为待补probe/矩阵，不偷偷修改生产PythonRuntime求绿。
- PG/文件/网络各门有可复用实际入口与尚未提供的有限helper/picker/TLS/OAuth探针标记；旧status3、carrier close、stopped/锁释放、HTTP可达均不被当作整门通过。测试PG只用自有home/data/socket，不连用户DB，不改保存settings，不为测试开TCP trust。
- GUI/Linux目录/Windows File字节导入、Linux export与Windows save、普通browser/preview边界及TLS新Session/每连接身份没有被初始HTTPserver简化掉。OAuth涉及真实账号由用户现场完成，否则待验证；清单不自动调用provider。
- 证据位置明确output被忽略、不随Git传输；不为了交接add -f巨大资源/secret。退出/清理按确切owner，不自动全局shutdown/unregister。Ubuntu原生/GUI待验收、G0待Windows实测、B未进入和未来申请条件始终一致。

## 交付限制

这是新手可跟随的准备/最小probe说明，不是完整G0 runner或Windows产品。无本机Windows验证；root可检查示例JS语法和本机适用测试，但必须保留Windows执行结果待实测。后续G0发现WSL包/rootfs/Node quoting/pipe/代理等具体失败，按真实错误停在相应子项，不fallback或放宽原断言。
