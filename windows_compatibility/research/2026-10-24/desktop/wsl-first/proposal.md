# Windows Electron + WSL2 全内核：桌面、部署与文件闭环提案

日期：2026-10-24。本轮只读源码和官方资料；未改生产/根规格/保存配置，未运行G0、构建、provider或数据库操作。根规格仍需据本轮明确路线选择重写后交叉审阅；此前原生Windows版本的冻结不批准本次新路线。

## 1. 产品决策与A的收窄

建议首版固定 **Windows 11 25H2 x64普通用户 + Windows Electron + 专用`Pulsara` Ubuntu24.04 WSL2 + Linux非root用户`pulsara`**。Linux执行域拥有Python、PG/Unix socket、terminal/PTY、MCP/hooks/Skills/plugin、Linux Playwright和实际工作区。Windows拥有Electron、系统浏览器/对话框、用户选择的Windows导入/导出、壳UI偏好与安装体验。

正常执行在专用发行版设置`interop.enabled=false`、`appendWindowsPath=false`；不让Linux任务隐式启动Windows exe。两开关作用不同。不能承诺这些开关组成安全沙箱，当前Windows用户可以自行以root进入其发行版。已有用户distro不默认采用、不改配置、不要求用户全局`.wslconfig`/默认distro变更。WSL安装启用可需要Windows提权/重启；普通运行不提权。首版只支持专用发行版，用户自有distro是将来单独规格化入口，不做运行时fallback。[Microsoft配置说明](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)、[WSL安装](https://learn.microsoft.com/en-us/windows/wsl/install)

**Phase A应收窄**：现有Mac/Linux只抽现有桌面构造需要的小host adapter、纯Node资源locator、PG物理exec/files port，并接齐实际caller/聚焦和安装回归。保留`main.ts`唯一boot/reset/requestQuit，`runtime.ts`唯一child/JSONL/ready/close，`BundledPostgres`唯一生命周期状态。为ConPTY/Win HANDLE而规划的全库ProcessScope/typed binding/权限抽象、原生pg_search/PS7/Win32 helper全部移出本路线的必选工作，不再把它们列为A前置。已有POSIX安全机制保持原样。

不建立unified platform runtime、通用文件服务/RPC、第二配置副本、endpoint注册表、执行恢复或新持久事件。新host adapter返回现有owner需要的命令/目录/实际结果；不判断工具成功或接管kernel。

## 2. 生产源码事实与建议落点

| 现有代码 | 已核实事实 | 本路线最小变化 |
| --- | --- | --- |
| `desktop/src/main.ts:14–37` | 模块加载即用本机Python解析home；同home保存Electron数据、构造PG和runtime | 初始化改为异步host composition；Windows壳home先用Electron标准userData，Linux有效home由Linux resolver返回；不在Windows用Node fs解释Linuxhome |
| `main.ts:52–89,128–148` | runtime imports与PG准备并行；start frame前wait旧owner；quit先runtime退出再PG stop | 保留这个唯一状态机；各实际Linux操作经明确host port，不把Windowschild.close当Linux已停止证明 |
| `desktop/src/runtime.ts:19–46,78–99,119–126` | Python路径在wait/start重复；写本机kernel.log；start frame注入DSN；stop等close | 注入命令/日志写入port；WSL固定`wsl.exe -d Pulsara -u pulsara --exec <absolute bundled python> ...`，无shell包装；Linux日志不走Node `path.join(linuxHome,...)` |
| `desktop/src/postgres.ts:10–95` | state仅initialized/startedHere；exec pg_ctl/initdb与目录/权限/实例/reset检查混在Node | 保留TS唯一owner；提取有限Linux exec/files操作，不搬role/schema owner，不把Linuxsocket转为WindowsTCP |
| `src/pulsara_agent/desktop_runtime.py:31–72,261–328` | Linuxkernel flock、0700socket、严格DSN；EOF→原application.aclose→最后释放lock | 保留Linux机制/DSN验证；只适配可信启动材料/可选TLS的composition，不能改成Windowsnamedpipe或Protocol-v3 TCP |
| `main.ts:217–235`、`preload.ts:34–37` | picker返回本机path，openPath直接Node stat/shell，drop直接得原生path | Windows必须跨域选择/转换；不把`/home/...`送Node stat，不把`C:\...`直插Linuxprompt |
| `frontend/lib/runtime-adapter.ts:2230–2245` | workspace由host.chooseDirectory返回后提交原API | 保留原Linuxworkspace owner与canonical判定；host返回Linux路径，session/workspace DB不存UNC |
| `frontend/lib/prompt-draft.ts:181–207` | `pathForFile`非空就直插原路径，否则已有浏览器File上传 | Windows本机File/drop走明确“导入副本”流程，复用现上传，不能只把drive替换`/mnt/c` |
| `desktop_app/file_import.py:56–155`、`http_server.py:2642+` | 已有流式octet-stream上传、私有imports、exclusive发布、失败清理，不造附件身份行 | 复用现HTTP导入owner/取消/权限，不新建任意Linux文件RPC |
| `file_preview_http.py:154–229` | action重新校验后给Linuxpath；内容/HTML资源走read-token HTTP，HTML仅iframe+CSP | 预览继续现URL/token；open/reveal跨域单独适配，不用fileURL替代受限预览 |
| `desktop/src/browser-policy.ts:6–23` | 普通Browser只接HTTP(S)，拒绝整个控制port及file协议 | **不放宽这个规则**；HTML/file preview仍专用view，不因WSL把guest加白控制port |
| `desktop_launcher.py:11–35`、`cli-install.ts` | Python沿现manifest启动本机GUI；当前CLI为POSIX shell | Windowsconsole入口`app`直接交同一GUI单实例，其余命令经固定WSL目标；Linux中禁interop时不能假称可用Windowsexe manifest |

建议实际文件：`desktop/src/host-adapter.ts`仅定义main/runtime/PG当前所需窄契约；`host-posix.ts`保现实现；`host-wsl.ts`固定distro/user的Windows命令适配。`resource-paths.ts`纯定位Windows安装资源和Linuxpayload声明的布局，区分两种路径类型，不建manifest authority。`postgres.ts`是唯一PG状态；Linux侧可新增专用`desktop_wsl_files.py`有限操作模块，短命、不持PG生命周期、不读写用户settings。无需为拼目录创建更多薄文件。

## 3. PG、启动、关闭：与backend已达成的唯一方案

已与backend专题直接核对，采用 **TS BundledPostgres唯一生命周期owner + 有限Linux操作helper**。不采用“Linux bootstrap再拥有一套PG start/stop”的初步方案，也不同时保留两种路线。现Node fs不能操作Linux对象是操作接缝缺口，不构成搬迁owner理由。

有限操作可为`prepare_layout`、`inspect_layout`、`exec_pg(initdb|status|start|stop, typed inputs)`、`delete_verified_pgdata`。Linux helper拥有本次执行/文件binding，返回结构化stdout/stderr/Linux退出码和确切事实；不决定何时start/stop，不重试失败，不执行任意argv/fsRPC。请求经私有UTF-8 stdin，协议stdout与普通stderr分开，不拼用户shell文本。实际创建/检查/删除在Linux执行并再验证uid/no-follow/version/socket/status；返回旧path不是后续删除授权。

正常链：Windows单实例→确认目标distro/user/payload→启动LinuxPython导入→原wait-owner成功→PG布局/版本/status/精确实例检查或首次initdb→PG ready→原start frame+Unix DSN→Linuxrole/database/migration→application ready→验证Windows连接来源→工作台。

正常退出链：停止接纳→原Linuxapplication取消/结算terminal/MCP/hooks/child→Linuxkernel释放lock与控制管道结束→TS确认真实Linuxruntime退出→PG `stop -w -m fast`并取得Linux结果/停止事实→Windows退出。不使用`taskkill wsl.exe`、`wsl --terminate`或`--shutdown`当正常退出。Windowschildclose、wsl.exe返回、Linuxroot退出、PG停止是不同证据。

start/status/stop响应丢失或截断必须unknown，不自动重放start/reset、不清库、不按PID猜进程。**仅一次status=3不能排除旧start helper仍inflight、稍后才启动。** 因而有限PGhelper复用同一个Linuxkernel flock，覆盖其完整命令物理执行和readback；命令未settle不能因控制管道失联释放锁，不能丢下尚live的exec子进程。它在kernel未运行阶段取锁，拿不到则busy，不排队一个未来自动start；ready期间诊断继续原Unix socket。原main wait-owner因此也等待旧PG物理操作，后续helper仍要自己取锁，等待结束本身不是写授权。下一次启动只在该物理串行事实成立后依据真实Linuxfacts/status验证已存在的专用实例。G0注入丢reply、旧start延迟、helper外壳退出和重开，证明没有晚到操作穿过锁。Node意外退出时PG可能继续；这是已有PG独立生命周期与崩溃恢复，不是执行恢复。失联不得立即删除Linux数据或把所有发行版关掉。

## 4. 数据home、壳home与scope

- Linux业务home默认`/home/pulsara/.pulsara`，仍由`require_pulsara_home`/LocalSettingsStore唯一拥有，settings/secret/DB/imports/socket均在ext4；不将home放`/mnt/c`或UNC。可接受的显式`PULSARA_HOME`必须是Linux绝对且经Linuxresolver验证；Windows路径值报错，不继承后悄悄翻译。
- Windows Electron缓存、UI偏好、窗口/tray等放Electron标准userData，使用现UiPreferences owner；它们不存模型routes/credentials/PG配置，不回写Linuxsettings。现`app.setPath('userData', path.join(home,...))`在Windows必须拆开。
- 固定专用distro/user不增加可编辑distro配置。home/processscope仍为Linux现语义；同home锁仍Linux kernel flock，不增加租约/世代/receipt。Windows单实例只约束GUI，不能代替Linuxowner。
- 日志来源区分：host日志在WindowsuserData；kernel/PG原日志在Linuxhome。查看Linux日志通过有限只读操作或明确UNC展示，不在Windows fs权限位上假证0700。敏感settings不可因诊断便利复制到Windows日志/偏好。

## 5. 工作区图形流程：选一个owner，不留未定产品路径

**建议首版保留Electron原生Windows dialog作为唯一picker**，默认定位固定distro的`\\wsl.localhost\Pulsara\home\pulsara\workspaces`，沿用openDirectory/createDirectory；Linux后端仍唯一workspace admission。图形流程为“新项目→浏览/新建文件夹→选择→Linuxcanonical/ext4校验→创建会话”；recent workspace与path completion继续Linux路径。quick workspace也由Linux现owner创建，不拿Windows临时目录代替。

host只接受精确专用distro UNC（可支持WSL官方别名`\\wsl$`的等价识别），按组件转换Linux绝对路径；不能字符串替换盘符、casefold整路径或接受别的distro/share/device namespace。Linux再验证实际canonical root、availability和允许filesystem，UNC仅显示/OS打开，不写进session/workspace canonical数据。dialog导航到了C盘或其他distro，返回具体拒绝和“请把工程放到Pulsara Linux工作区”的指导，不能静默接为`/mnt/c`workspace。

reveal：Linux后端现action重新确认目标→host把该Linux绝对路径映射到固定distro UNC→Windows Explorer打开/定位。不能调用Linux `explorer.exe`绕过interop禁用。系统open只保原允许的文档/图片等类型；不存在、不可表示的Windows叶名、UNC服务不可用、发行版停止均给具体错误；不因此改文件名或启动任意脚本。UTF8中文/emoji/空格、叶名`# % & ' ( )`、深路径与大小写敏感按真实边界验证。

这是**待G0-W-F证明的唯一默认方案**。必须在普通用户已安装Windows桌面实测UNC初始定位、浏览/新建、取消、错误distro、失联/重启与合法Linux名但Windows API不能表示的拒绝。如果原生dialog无法满足明确支持范围，G0不通过；必须修订为一个Linux-backed picker（现HTTP owner提供有界分页浏览/目录创建，renderer提供图形选择，Linuxowner校验），删除原生UNC picker路径。不能同时保留两套自动fallback或把Linuxpicker空壳称为已交付。根规格冻结时应明确支持的叶名/目标范围，而非假称全部Linux名均经WinAPI可用。[Microsoft文件系统建议与Explorer访问](https://learn.microsoft.com/en-us/windows/wsl/filesystems)

## 6. Windows文件导入/导出与Linux预览

### 6.1 导入

原生Linux文件/目录选择结果变成Linux引用；Windows来源的File/drop则必须标识“导入副本”，复用`LocalImport`现有octet-stream上传、取消、exclusive发布和read_token后续机制。完成后只把返回的Linuxpath插入draft。不能保留当前`pathForFile`非空就直接插入Windowspath的分支，也不能声称引用会追踪Windows原件的后续修改。

建议把“添加Linux文件/目录引用”和“从Windows导入文件”明确成两种动作：前者保留host原生UNC picker，返回Linuxpath；后者用renderer标准`<input type=file multiple>`打开系统文件窗取得浏览器File，直接复用现draft上传。现`chooseFiles():Promise<string[]>`经IPC不能直接返回File，不能只把Windowspath标成upload便假称已有字节；也无需为此新增任意path读IPC或selection-tokenregistry。现已有File对象的drop/clipboard可直接走原draft upload owner。若以后合并为单一文件按钮，必须完整规定受控stream和授权来源再hard cut接口。Windows目录不是File单流，**首版目录drop明确拒绝跨域导入**并指导在Linuxworkspace创建/复制工程，不能给假零字节文件、只传path或隐式递归复制。若要首版目录导入，须单独明确已有容量/名称/结构/取消/失败清理、逐项结果与安全发布合同，估算并入文件工作包后再冻结，不在实现时临时补总数cap。

跨域文件复制是用户明确请求的行为；Linuximports仍原authority，不新增“同步关系”、双写、跨重启job或恢复receipt。应用上传8MiB请求边界等现有合同不能因为平台迁移被悄悄放宽或缩小；超出时保留真实错误/明确后续产品决定。

### 6.2 保存/导出

Linux文件通过既有preview content/download与token校验流到Windows保存；用Electron DownloadItem/save dialog能力处理Windows目标、Unicode、覆盖确认、取消和失败，只接受trusted workbench发起的当前已授权download。保持普通Browser guest的will-download阻止策略。不能给renderer任意Windows路径写IPC，不能把Linuxfile URL改为UNC后绕过read-token。

Linux文件的普通preview/image/PDF/HTML继续现HTTP内容/资源路由，Windows Chromium不需要访问Linux磁盘。相对HTML资源由Linux descriptor-bound resource owner处理，CSP、iframe-only、null-Origin的现限制保留；网络桥采用HTTPS时资源前缀跟实际origin，不夹带fileURL。

**普通BrowserOwner继续阻止控制port、file协议和跨来源宿主能力。** 它不是本地HTML预览owner，不白名单整个preview URL空间。现HTML资源冻结与专用preview已经可复用；Linux Playwright负责原Linux截图/HTML任务。Windows Chromium和Linux Chromium分别验证，不因同为Chromium就互相代替。

## 7. G0-W-N：Windows→WSL网络与每次启动的身份

官方说明Windows可通过localhost访问WSL中的服务；这不是“当前占用该端口的一定是本次内核”的证明。当前`LocalHttpServer`在`http_server.py:357–377`固定HTTP，`_security`只做Host/Origin/Fetch-Site，不能据端口或一次成功GET取得身份。不能发送bootstrap secret给尚未验证的listener，不能只做一次nonce签名后恢复明文多连接。[Microsoft WSL网络](https://learn.microsoft.com/en-us/windows/wsl/networking)

建议G0优先验证一个窄标准TLS路径：Linux本次进程持有短命server key/cert，经原可信stdio传完整public cert及实际endpoint；Windows trusted workbench session在任何导航/fetch前验证确切cert，并独立限制精确origin，Linux私钥只由本次Linuxowner持有；如stdlib加载需要私有临时文件或memfd，按现Linux安全文件机制创建、使用后清理，不成为持久secret配置。aiohttp `TCPSite`只增加已验证TLS参数与实际origin scheme；main当前HTTP-only ready检验同步修改。不是另建通用proxy/RPC，也不是全局忽略证书错误。

实际已核查锁定Electron声明：`setCertificateVerifyProc`取`certificate.data`完整PEM，callback0接受/-2拒绝，**结果被network service缓存**；request只有hostname无port。因此每次backend launch须独立workbench网络session/context，cert验证与exact-origin request admission各有作用；不得热替pin后沿用旧cache，不允许未匹配本次cert但系统信任的其他127.0.0.1证书通过。retire先封新请求/关连接，再废弃该session；旧view/iframe/SSE/WS/download/host upload不得拿旧地址在新listener上继续。session并非新的持久凭据registry。当前main只创建一次BrowserWindow，WebContents.session在构造时固定；重开内核需要新Session时，也须经同一main owner替换workbench WebContents/Window并正确退役旧BrowserOwner/StartupCover/IPC，不能声称热替pin等价。G0应验证这一接入，或证明官方API可以真正清掉相关验证缓存的更窄机制；`clearAuthCache`不能未经证据当证书缓存清除保证。全证书精确比较即可，不新增文档/源码hash。若用TLS SPKI/cert digest，只能是该真实认证边界的pin。[Electron session API](https://www.electronjs.org/docs/latest/api/session#sessetcertificateverifyprocproc)

TLS只证明server来源；若跨WSL边界需要独立HTTP client authorization，须在这个已验证通道内，用本次stdio提供的process-local引导材料建立最小Secure/HttpOnly会话，复用现Host/Origin/read-token与显式revoke；不要把Protocol-v3 launch capability送renderer或当新的HTTP通用token。具体机制应由G0选定维护库/现会话扩展点，不能文档许愿“做challenge即可”。若现机制可给每条后续连接同等保证则优先采用更窄方案。

**硬退出条件**：Windows本机端口已被别的服务占用、其他WSLdistro同端口、NAT/mirrored、VPN/防火墙、WSL重启/rebind、假证书/旧证书、错误distro stdout、ready前取消、iframe/null-Origin、fetch/SSE/WS/upload/download和旧session缓存竞态均实测。误连必须在任何bootstrap/session secret或业务请求发出前拒绝；连接中断后撤销UI/宿主能力，不偷偷换IP/0.0.0.0/portproxy。如果做不到则G0未通过，修订网络机制/规格后再实施。HTTPS证书缓存/会话隔离及Linux Playwright访问自身origin是必须证明的集成缺口，尚无实测通过声明。

## 8. rootfs、初始化、更新与卸载

### 8.1 固定镜像与分发

采用Canonical官方Ubuntu24.04 amd64 WSL rootfs，recipe锁具体point/build、URL、官方摘要/签名、许可证；不能`latest`下载或沿浮动current目录。当前官方已提供tar-based `.wsl`格式及定制指南，但文档示例会随当前LTS改变，不能把它当固定24.04构建锁。Linux环境中准备/验证rootfs，保留uid/mode/symlink；不在Windows zip解压后声称rootfs等价。[Canonical定制指南](https://ubuntu.com/wsl/docs/latest/howto/custom-ubuntu-distro/)、[24.04官方镜像目录](https://releases.ubuntu.com/24.04/)

建议安装包携带已验证rootfs和固定Linuxpayload，首启离线import；WSL engine本身缺失时给明确前提/微软离线安装入口和可能重启，不悄悄在线拉最新。NSIS大体积/磁盘解压空间、WSL存储VHD空间作为真实资源失败报告。再分发Ubuntu与修改后的品牌/许可义务要按Canonical官方政策在release前核实；不能把定制rootfs简单仍标官方Ubuntu发行物。[Canonical政策](https://canonical.com/legal/intellectual-property-policy)

### 8.2 初始化

`wsl --import Pulsara <per-user retained data location> <verified rootfs> --version 2`；导入默认root，所以首次有限provision创建pulsara用户、home权限、固定应用payload与专用wsl.conf，随后所有正常命令显式`-d Pulsara -u pulsara --exec ...`。不对用户默认发行版运行命令，不加入sudo组当正常前提。[Microsoft自定义发行版导入](https://learn.microsoft.com/en-us/windows/wsl/use-custom-distro)

若名为Pulsara的发行版已存在，检查WSL registration/版本、Linux发行版/uid/既有package/layout与兼容版本，**不按名称自动认领未知distro、不覆盖或unregister**；未知冲突明确停下。应用正常重装复用已保留的本产品发行版时也做实际检查，不能信任新自造manifest上的owned=true。标准WSL registration与现安装/package metadata是各自事实来源；如首次保留/重装身份不能充分证明，应让用户明确选择保留/接管前提，而不是加第二真相或删库。

### 8.3 更新与保数据

默认手动更新，只在GUI/kernel/PG正常关闭且专用owner无占用后更新Windows资源及Linux应用payload。Linuxpayload固定版本对应Windows壳版本；完整准备后再切唯一已安装路径，不能新旧protocol协商/fallback。失败不自动启动错版本或替换settings/PGdata。必要的不可变payload内容验证沿现资源boundary，不另存receipt/recoverygraph。

rootfs不是每次更新可替换的临时cache：VHD包含Linuxhome/DB/workspace。禁止“新import+unregister旧distro”作为普通更新。Ubuntu安全更新与WSL engine更新要有明确人工维护/受支持版本验证路径；不默认后台dist-upgrade/跨LTS，也不能永远冻结未维护基镜像而声称支持。离线payload更新、磁盘不足/中断、版本不匹配都应真实失败并保留用户数据。

卸载默认仅删Windows壳/应用资源，**保留专用distro、Linuxhome/DB/workspace和Windows保留数据目录**，显示重新安装/数据位置说明。永久删数据为单独显式确认动作，先正常关闭精确本产品owner，说明`wsl --unregister Pulsara`会删除该distro全部数据，不能放进普通NSIS卸载或仅确认“卸载应用”就执行。[Microsoft unregister语义](https://learn.microsoft.com/en-us/windows/wsl/basic-commands#unregister-or-uninstall-a-linux-distribution)

NSIS继续用现`customCheckAppRunning`扩展点替代默认Stop-Process/kill，不另抄整套installer。已知main退出但Linuxowner/PG/Chromium仍有占用，或失联unknown时失败/等待用户正常退出，不强杀wsl/全局shutdown后覆盖。只验证Windows安装目录可改并不等于Linuxpayload可改，两个资源边界都要查。

## 9. Windows CLI、前端与宿主动作

Windows标题栏原生控制、tray失败有入口、IME/AltGr/Ctrl快捷键、DPI、多屏、system open失败与OAuth仍须Windows实机闭合，WSL不会免费解决这些事项。前端host.platform可为win32，执行域另以明确只读事实说明Linux/WSL，不伪装Linux Electron，不让平台文案改变当前epoch的SYSTEM/tools。

Windowsconsole launcher保Unicode argv/stdio/退出码/Ctrl-C；`app`交Windows已有GUI单实例，其余已授权管理命令交固定LinuxCLI，不默认把Windowscwd解释为Linuxworkspace。CLI要选Linuxcwd时用明确Linux参数/固定distro UNC转换并经Linux验证；不能把`C:\project`偷偷切`/mnt/c/project`。不自动改PATH，不覆盖外部同名命令。

Linux中`pulsara app`在interop禁用时不能靠现desktop-launch.json指向Windowsexe。建议首版明确该动作需从Windows入口启动/聚焦，不为了方便开启interop或新增常驻反向RPC；WindowsCLI本身提供完整app入口。若用户要求LinuxCLI也原样聚焦GUI，这是独立产品缺口，须在根规格明确有限可信宿主动作而非隐藏fallback。其余LinuxCLI诊断继续访问Linux AF_UNIX diagnostics，WindowsCLI通过WSL执行同一client，无需Windowsnamedpipe或新诊断服务。

OAuth系统浏览器仍由Windows宿主打开HTTP(S)授权页，回调实际落Linux原loopback owner；G0-W-N必须验证Windows浏览器经localhost正确回到该owner及state校验，自定义`pulsara://oauth/return`只focus，不接受code/grant，不把URI交shell。

## 10. 实施顺序与验收

| 阶段 | 退出证据 |
| --- | --- |
| A0 | 新路线根spec冻结；真实资源/命令/caller清单与改前Mac/Linux基线；scope仅真实host接缝 |
| A1 | Node纯locator+host/PG物理port实际接齐；POSIX机制原样；单一main/runtime/PG owner；旧路径删掉 |
| A2 | Mac arm64、Ubuntu native x64/arm64现资源/CLI/wheel搬移，受影响Python/Node/frontend与已安装GUI；原pipeline/manualrelease不改；不用Mac绿代Linux |
| G0-W-D部署 | 原生Win11普通用户：无WSL/政策不允许/需重启/离线安装；导入非root/interop禁用；名字冲突、保留重装；payload版本与PG扩展relocation/中文路径 |
| G0-W-C控制 | 真实WindowsNode→wsl→LinuxPython UTF8无BOM；拆chunk/emoji/EOF/brokenpipe/ready前失败；Linuxwaitowner；start/stopunknown、不重放、不提前停PG |
| G0-W-N网络 | 第7节逐项每次启动来源/全连接lifetime硬证据；不以localhost可达或一次GET替代 |
| G0-W-F图形文件 | 唯一picker创建/浏览/选择Linux工作区；精确UNC与Linuxcanonical双向；Windows file/drop导入与download保存；preview/HTML隔离、未知路径拒绝 |
| B接入 | 只在上述闸门通过后实现WSL适配与installer/front-end；没有ConPTY/原生pg_search/NTFS安全全库移植前置 |
| 最终验收 | 已安装NSIS在新/既有Windows用户、离线、无/已有WSL、冷暖启动、LinuxPG/memory/BM25/vector、workspace/附件/preview/HTML、Windows save/reveal、OAuth、CLI、tray、慢退、host崩溃/WSL重启、重装/更新/卸载保数据；真实provider/长程Linux任务与WindowsGUI闭环；Mac/Linux受影响回归 |

现`smoke_resources.py`只证明搬移后的LinuxPython/PG/rg/Playwright，不证明WindowsGUI/网络/installer；现`test:startup`/`test:browser`为native Electron层，需在真实Win图形session运行。现Ubuntu原生CI证明Linux代码，WSL目标另有发行版/网络/控制边界，不能彼此替代。

真实provider沿AGENTS：Linux有效home的LocalSettingsStore只读加载、隔离已验证disposable DB，不复制原secret到Windows环境/报告；Mac与Linux/WSL实际配置可用性先preflight。此前artifact runner baseline phase会删tool的限制仍成立，前后均用candidate工具面；每个checkout独立环境记录实际import/资源来源。

## 11. 成本与仍待G0关闭的风险

此前native 88–154人日总数不应原样换标题。WSL路线避免WindowsPG扩展/ConPTY/Win安全文件全库移植，但增加专用distro供应链、维护和跨域GUI文件/网络身份。旧“已有WSL35–65 + 专用distro15–30”仅为原先替代路线级粗估，本轮选择专用distro、网络认证与明确图形流程后须重新拆账。

本专题建议核算范围（重叠部分由root单一分配，不能与backend/terminal估计再相加）：

| 工作包 | 人日建议区间与假设 |
| --- | --- |
| A桌面host/locator/PG port+现平台针对回归 | 5–9；不含全库typed binding/scope，现POSIX机制不重写 |
| 专用rootfs/离线初始化/版本/手动更新/保数据卸载 | 10–17；采用官方镜像、固定单distro/架构，无自有WSLkernel或用户distro兼容 |
| WSL启动控制/PG机制helper/真实EOF与unknown | 6–11；与backend主要重叠，root只归一处 |
| 网络身份/端口/会话退役与原API集成 | 6–12；复用stdlib/aiohttp/Electron，不包含另建proxy；G0若揭示缺口另修估算 |
| picker/UNC/action/file导入/download/preview闭环 | 6–11；首版跨Windows目录不递归import，现Linuxpreview/upload大部分复用 |
| WindowsUI/CLI/NSIS/签名/资源CI | 8–14；Windowsconsole入口与壳系统行为，签名等待时间不算工程人日 |
| installedGUI/三平台影响回归/provider/故障证据 | 7–12；共用矩阵只算一次，现native CI可用，不含长时间等待硬件/企业审批 |

上述是一组用于root去重拆包的**整体相关工作草算48–86人日**，不是桌面专题额外加价，也不是承诺固定排期。G0部署/网络若需另一机制，成本和规格一起修订；可再对真正未证实的集成风险留25–35%余量，但不要同时叠旧native总数与旧WSL备选总数。A/B劳动分别记录，A已验证且不受B改动影响的证据可复用；B触及处仍重跑，不为省数字省掉实机资格。

主要未决风险：①普通用户WSL前提/企业策略与离线安装；②TLS exact pin及Chromium缓存/iframe/生命周期能否达到每次launch来源要求；③UNC picker能否满足明确支持的图形范围；④WSL客户端失联与Linux命令真实unknown；⑤rootfs安全维护/再分发许可与大体积分发；⑥Linux CLI app与跨Windows目录导入的明确首版范围。它们都有具体G0或产品决定出口，不能留到实现时默默选fallback。

## 12. root汇总后的路线收敛（待全文审阅）

root已明确选择并将在根候选写入以下唯一产品路径，取代本提案第5节“原生UNC作为主workspace picker”的建议：

- **唯一Linux-backed user-level workspace picker**负责新建/浏览/选择工作区，不依赖active session；Linux原owner验证canonical/ext4/availability。不能用现session-scoped path completion假冒用户级picker。
- Electron原生UNC picker仅为Linux文件/目录引用选择；Windows导入另用renderer标准file input取得真实File；Explorer负责reveal及用户手动管理，不能成为workspace authority。
- 本次不做Windows全局console CLI；设置明确只提供WSL内CLI，`pulsara app`指导使用Windows图标，不开interop、不新建反向RPC。这会从第9节建议范围减去Windowsconsole launcher，相关成本由root去重重算。
- 每次Linuxbackend launch使用新的trusted workbench Session；由同一main owner替换Window/WebContents，完整retire旧BrowserOwner/StartupCover/IPC。此为第7节TLS候选接缝的必要集成范围，不是hot-swap pin。
- PG有限helper与所有旧PG命令继承同一Linuxkernel flock直到物理结算/readback，close/失联不能留下锁外late start；根候选会细化该链。

此处仅记录root方案收敛，不提前批准尚未全文审阅的根候选；仍未执行测试/G0/构建/provider。

有限PG调用还需ready-before-operation，不能仅NB锁：helper先取得现kernel.lock并发送ready，Node确认该调用仍有效才写唯一operation；ready前EOF/取消没有操作帧则不执行。帧发出时锁已经持有，之后失联仍持有到真正命令结算/readback。这样已死host不能留下一个未调度、稍后才取得锁并执行argv预载操作的helper。它只是单次现有start-frame模式，不新增持久ack/registry。

锁在helper意外退出后的继承仍需核查：不能未经证明把kernel.lock FD传给pg_ctl，导致其常驻postgres子进程永久继承并阻塞runtime。G0须实测“PG命令未settle时锁仍有效、PGready后runtime可取得锁、carrier/有限helper/实际命令不同退出点与late调度”；具体窄机制由backend/terminal/root收敛，不能以文本声称持锁代替实际FD/进程事实。
