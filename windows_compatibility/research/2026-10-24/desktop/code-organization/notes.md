# 桌面代码组织补充建议

日期：2026-10-24。当前生产只读复核：`desktop/src/{main,runtime,postgres,preload,cli-install,browser-owner,startup-cover}.ts`、前端desktop-host/path/preview、资源builder、mise与desktop workflow。本轮没有实施、跑测试、跑G0、读实际凭据；所有新增文件只在本目录。以下为根规格§3/§11补充的组织建议，产品行为仍服从已冻结Windows合同，不新建unified platform runtime。

## 1. Common保留在哪里

| 当前owner | 继续共用的内容 | 可移到平台leaf的内容 | 明确不下放 |
| --- | --- | --- | --- |
| `desktop/src/main.ts` | app composition、single instance、origin信任、boot/reset/requestQuit顺序、确认UI、browser/cover绑定、用户级原生动作 | BrowserWindow options的少量平台字段、Windows session-end事件、tray支持/失败展示、CLI入口选择 | lifecycle状态不复制；PG-before-ready、child-close-before-PG-stop不能由每平台自定 |
| `desktop/src/runtime.ts` | JSONL start/ready/error/inspect/shutdown语义、请求map、stdout事件投影、stdin EOF、等待child close | 已解析python exe与Playwright路径注入；若Node child启动确有不同flags则局部spawn helper | 不把application业务、terminal执行、PG认证或会话状态塞入Node；不另建WindowsRuntime/PosixRuntime各一套 |
| `desktop/src/postgres.ts` | `BundledPostgres`唯一物理实例状态、PG_MAJOR/version校验、初始化/采用/stop/reset的共用顺序、startedHere/initialized | Unix socket/mode/uid参数；Windows TCP/auth/ACL/initdb pwfile/instance核实与reset-auth清理 | 不各自复制boot/quit，不新增DB manager或settings authority；调用者仍在Python退出后才stop |
| `desktop/src/preload.ts` | 显式有限IPC暴露、main-frame限制、Electron webUtils调用 | `process.platform`数据字段扩win32；无需另建Windows preload | 不暴露Node/任意IPC/任意FS；guest不能调用 |
| `browser-owner.ts`、`startup-cover.ts` | session/permission/navigation/native view所有权与retirement、启动cover lifetime | 仅经证实的坐标/OS事件接缝；Control/Meta现有分支可保留小分支 | 不为Windows另写browser owner，不改webview或开放权限 |
| `frontend/lib/desktop-host.ts` | renderer host accessor与有限UI helper | 平台快捷键显示和native-controls展示差异 | 无Node imports、无child/PG/ACL/auth逻辑，无active-session依赖的用户级服务 |

只为跨平台把main.ts机械拆成十个single-use包装没有必要。若实施期间main确需瘦身，可以把现有boot/reset/requestQuit一起移到一个`desktop/src/app-lifecycle.ts`，main仅composition；必须是一份状态，而不是main保留flag又让新controller再保存同名flag。不是本补充强制创建的独立框架。

## 2. 建议的具体落点和最小接口

### 2.1 Resource layout

新增`desktop/src/resource-paths.ts`，纯函数按已选择的darwin/linux/win32返回当前包内所需绝对路径。调用一次后把**完整路径值**传给需要者：

- `main.resolveHome`和`PythonRuntime`使用同一个`pythonExecutable`；不各自拼`python/bin/python3`或猜`.venv/Scripts`。
- `PythonRuntime`只收自己需要的`pythonExecutable`与`playwrightBrowsersRoot`（加既有home/onEvent），保持现有协议状态；PG只收自己需要的pg可执行路径，CLI收自己的packaged console入口。
- resolver只描述安装资源，不能解析PULSARA_HOME、读settings/auth、启动进程或决定DB endpoint。home继续由随包Python `require_pulsara_home`返回。
- 小闭合集合可包含`pythonExecutable`、`postgresBinDirectory`/少数pg exe、`playwrightBrowsersRoot`、`privatePwshExecutable`、`packagedCliExecutable`；每个consumer不必携带全局大DTO。
- Windows具体CPython布局以G0和recipe实际产物为准，不先把`bin`替换成`Scripts`声称成功。无fingerprint/manifest registry/cache authority；已有desktop-launch.json仍只归CLI→GUI启动，不扩成平台服务发现文件。

平台分支简单时这一文件内明确switch就足够；只有出现独立native依赖/复杂构建布局时才拆`resource-paths-posix.ts`/`resource-paths-windows.ts`。unsupported platform不能fallback到posix。

### 2.2 PostgreSQL leaf

建议`desktop/src/postgres-posix.ts`与`desktop/src/postgres-windows.ts`只在现有`postgres.ts`的物理接缝下使用，选择点在composition/工厂一次确定，不在循环内尝试某平台失败后退另一个。

小接口应围绕**实际操作**，例如准备受限端点/认证材料、提供initdb与server的具体配置、核实正在运行的精确实例、形成该已验证实例DSN、在确认data reset完成后清除其auth。具体命名由实现决定，不能抽出万能`Platform.run/read/write/get/set`。

`BundledPostgres`共用顺序仍显式可读：准备已验证home/data-dir → 版本检查 → 平台首建材料/initdb → status/adopt或start → 物理实例证明 → settings；reset为确认→停确切owner→删除精确data-dir→删除绑定auth。Windows leaf拥有TCP/SCRAM材料和SID ACL，POSIX leaf保留私有socket/uid/mode。类型必须区分Unix endpoint与Windows认证endpoint，不能有`socket? password? port?`一袋可选字段让非法组合潜入共用逻辑。

Windows安全文件原语在Python实现时，Node不能自行用`fs.readFile+chmod`假装等价。G0需选定最小跨语言调用接缝（例如复用随包Python的一次限定操作），只传已需的auth/endpoint结果，协议和权限仍归原PG owner；不为读一个auth文件引入常驻platform daemon、通用FS RPC或新的持久凭据权威。

### 2.3 Renderer-safe contract和native path

可以新增`desktop/src/desktop-contract.ts`作为**仅类型、renderer-safe**的宿主契约，沿用现有`browser-contract.ts`已经跨desktop/frontend type-import的做法：收齐PulsaraDesktop、CliStatus和已有startup动作签名，frontend/lib/desktop-host.ts只import type+host accessor，preload只实现显式映射。不要另建Node-only共享库并让Vite打进renderer。

`main.ts:trusted/handle`继续验证sender、mainFrame、origin和startup许可后才调用任何adapter。拆出handler文件时也只能经同一trusted wrapper注册，不能平台分支另绕过验证。CLI修复/DB reset/文件动作的参数校验各留自己owner，不搞universal invoke。

路径字符串的两种来源保持明确：dialog/drop是native path；markdown链接是URL/address。Node `pathToFileURL/fileURLToPath`或Python标准库适配放**宿主/backend**。frontend`file-preview.ts`保留HTTP/anchor/markdown相对资源分类；`file-reference.ts`保留普通文本reference和显示解析，不承担真实path授权。按backend contract硬切清楚的native-path/URL入口，避免renderer为同步调用便利再复制Windows file URL parser。preview read_token/root/source binding仍是后端owner；转换成功不等于授权成功。

### 2.4 CLI与window差异

- `desktop/src/cli-install.ts`保留共同状态结果与“不覆盖冲突、不改PATH、注册best-effort不阻塞DB”的产品规则；将POSIX脚本/X_OK/symlink leaf移到`cli-install-posix.ts`，Windows packaged native console entry/PATHEXT/入口检验放`cli-install-windows.ts`。禁止Windows假读shell marker或mode位。
- 优先复用uv/Python maintained console launcher；仅证明不足时在`desktop/native/windows-cli/`放一个窄原生转发器，不拥有命令解析/业务/网络。对应C/C++/Rust选择以G0实际依赖决定，不新加通用runtime包。
- Electron window options/tray/session-end差异足够小，main里集中OS分支即可；若因原生事件复杂需拆，单个`desktop/src/window-platform.ts`返回options并绑定OS事件，callback调用现有`requestQuit`，不自己关Python/PG。

## 3. 构建/安装/CI的位置

| 落点 | 保留/新增边界 |
| --- | --- |
| `desktop/scripts/prepare_resources.py` | 保留CLI入口、组件任务排序、版本/许可证/应用wheel与验证编排，不把build recipe塞进runtime resource resolver。 |
| `desktop/scripts/resource_build/common.py` | 若现有单文件为引入Windows变长，可提取原download/checksum/source-tree/copy/版本校验等共用build操作；仅供构建，不import生产home/settings。 |
| `desktop/scripts/resource_build/{macos,linux,windows}.py` | 按真实机制分开Mach-O/ELF/PE、loader依赖/签名、PG构建产物；platform selector只导入选中native leaf，无import时缺工具副作用。Windows无G0支持前不留伪成功stub。不是要求把所有函数都拆三份。 |
| `desktop/scripts/smoke_resources.py` | 一个CLI和同一真实行为oracle；用平台fixture准备专用PG endpoint/运行路径，各平台执行搬移后Python/PG/extensions/browser/rg/PS/CLI资格。不创建Windows-only校验绕过共用版本规则。 |
| `desktop/scripts/windows-installer.nsh` | static tracked source，electron-builder `nsis.include`引用；实现customCheckAppRunning不强杀门。不要放进当前disposable `desktop/build/`后让关键安装策略被忽略/丢失。 |
| `desktop/assets/pulsara.ico`、`prepare_icons.py` | 和现有png/icns同一素材来源；Windows运行/installer图标与AUMID资格验证。 |
| `desktop/package.json` | Windows target/NSIS/include/extraResources/console launcher/签名声明；先用builder supported config，不无理由新增自制sign pipeline。 |
| `desktop/scripts/release_artifacts.py` | 保留一个全平台产物集合/版本核实/draft owner；新增exe名称和标签，别复制成release_windows.py。 |
| `mise.toml` | 一个任务DAG，按平台声明exe/dll/解释器outputs；Python用uv run或repo环境选出的绝对exe；缓存按平台/架构/recipe输入隔离，无新fingerprint registry。 |
| `.github/workflows/desktop-package.yml` | Windows native job跟随同一资源/版本/上传/draft规则；真实installed NSIS smoke位于Windows用户环境。Server runner只能承担它能证明的检查，不能冒充Win11 GUI支持。 |

common不能import平台leaf再在import时执行外部命令；缺native依赖必须在选中该目标时明确失败。TS CJS与frontend Vite的现有边界保留，不为统一组织强改整个monorepo模块系统。

## 4. 实施依赖顺序建议（补§11.1）

1. **先给owner合同和选平台位置**：G0按已有规格验证底层语义；写明文件/进程/端点小接口和完整typed值，native binding版本及缺口。不要先搬整个目录搭空abstract framework。
2. **先建立足以让所有平台import的边界**：Windows不导入pty/fcntl/Unix-only flags；已有POSIX调用移动到POSIX leaf，消费者仍走同一API。所需移动与该Windows实现一个hard-cut交付，不保留no-op adapter获取绿测。
3. **先下层安全与资源路径，再PG/IPC**：G0选择的file/ACL/lock/native process primitives可用后，接资源locator、PG auth/instance、kernel lock/diagnostics、Protocol-v3与Node JSONL。不要让frontend先假造路径成功或让PG先用trust TCP等后补认证。
4. **接同一boot/quit/reset，再接用户流程**：确保启动失败、不产生第二writer、Python close后才PG stop；再完善native path输入→backend安全owner→preview token→宿主open/reveal、tray/IME/controls/OAuth。W6纯display可提前做，但功能完成依赖W3/W4。
5. **资源build/安装闭环**：locked dependencies与recipe→资源版本/PE/DLL验证→搬移smoke→desktop构建→NSIS（已替换默认强杀策略）→已安装CLI/GUI/更新卸载保数据。CLI launcher依赖确认的bundle layout，不能先注册到猜测路径；installer不能先允许强杀再留todo。
6. **CI/发布资格最后闭环**：native backend/资源→真实安装测试→整个产品长程/real-provider→三平台有效回归→精确release集合/draft。Windows worker不是现有Ubuntu service-container的重命名；继续手动触发/明确发布，不增加发布权限和自动更新。

W0–W7原有总预算已经包含这些组织工作；本补充是细化落点和先后，不另加一份重构工程。真实新增native helper/上游patch超出G0已选小接缝时按原规格重新估算，不用“平台抽象”名义扩大scope。

## 5. 根补充二审关注点

- common是否仍只有一个boot/quit/PG状态/前端协议真相，OS leaf是否仅物理/配置差异。
- 是否明确资源locator与build recipe、用户home resolver各自拥有不同事实，未以一份platform context携带整个应用。
- TS renderer-safe contract是否没有Node运行依赖，IPC拆分后是否仍经trusted来源门。
- 新静态安装宏/原生helper是否放tracked source而非ignored disposable build目录。
- 原有G0、UTF8、SCRAM、native-control/tray、NSIS不强杀、file token/owner、prefix与durability合同是否被组织改动削弱。
