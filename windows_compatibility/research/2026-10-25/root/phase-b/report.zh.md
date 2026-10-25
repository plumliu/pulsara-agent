# Phase B正式接入与共享缺陷修正（进行中）

日期：2026-10-25。源码基线为`9e1150b7a4b16e212be36329182cbaa505c4df79`，包含`3ac7fc2b`。本报告记录用户授权进入B后的正式实现，不能用§22的G0探针结论代替新源码验收。

用户要求先修两个POSIX启动清理缺陷，然后按当前规范正式接入；实施中发现相关生产缺陷直接修复。继续复用原三位gpt-6.1-sol/high；已有Ubuntu/plumwsl、interop与Hiddify TUN保持。原生Ubuntu资格延期，尚无本轮macOS原生GUI重验。

## 已落地的边界

- POSIX原terminal owner：foreground timer创建/启动失败收回原预约；Popen后的stream、线程或state构造失败先关闭本次fd、终止并join真实进程组，再释放预约。原实现的新7项反例全部失败，候选相关62项通过。
- Windows：固定WSL目标、离线版本化资源、原HostOperations及有限PG helper、双锁/一次命令/真实物理结果、原Runtime与Application的TLS/OAuth控制链；用户级Linux目录服务和前端picker、本地File上传与同发行版UNC引用；Windows窗口控制、tray及NSIS维护检查。
- 跨域任务：独立PE使用Windows Job/HANDLE，Linux标准Unix socket透明转发；同scope有限monitor只补helper死亡后的真实HANDLE观察。terminal、stdio MCP、hooks及environment各自原owner持有scope，不导入output探针，不引入产品生命周期注册表。
- Runtime与PG额外共享修正：已发送start但尚未ready的失败不再当作空child；完整LF/fatal UTF8、failed/异常退出/缺stopped都不得冒充clean。初始化角色/库只由本次确认initdb成功授权；既有cluster缺库不自动重建。窗口/浏览器/启动cover退休等待实际WebContents销毁，取消不在旧Session退休后重新导航。

## 已完成验证及范围

以下计数属于各自源码快照和验证集合，存在重叠，不合计为覆盖率。

| 检查 | 当前结果 | 范围 |
| --- | --- | --- |
| 隔离ext4当前desktop源码编译、全部14个Node测试文件 | 105/105通过 | 含原POSIX和Windows适配测试；不是原生Ubuntu资格，也不是macOS实测 |
| Windows真实Runtime子进程 | 25/25通过 | 启动门、错误退出、完整帧、TLS交付、OAuth打开结果、关闭等待 |
| Windows主流程定向测试 | 10/10通过 | 首次选择、固定目标、维护检查、PG初始化许可、窗口替换时Quit、Session退休失败后重复Quit、Windows就绪前等待/取消；Electron对象为fixture |
| Windows真实StartupCover | exit0 | 原导航/居中/淡出/隐藏/最小化/替换，加并发show/retire真实销毁 |
| 前端聚焦7个测试文件、类型检查、修改文件lint、生产静态构建 | 325/325通过，静态检查及构建通过 | Windows入口及Linux picker；真实完整产品GUI另验 |
| PG/后端与interop | [PG/后端报告](../../backend/phase-b/report.zh.md)、[日志索引](../../backend/phase-b/log-index.zh.md)；interop以其分工报告为准 | 真实PG、目录/TLS/OAuth与真实Windows跨域owner验证；以各报告具体场景为准 |

根验证原始记录在`output/phase-b/root/`。WSL测试在`/home/plumwsl/workspaces/pulsara_agent/output/phase-b-desktop-*`私有快照，复用Linux依赖；没有把Windowsvenv/node_modules复制为Linux运行依赖，也未修改Linux主clone源码。生成静态资源由当前Windows源码构建，用于新wheel。

## 保留的失败与修正

- 首次真实StartupCover在Windows字体布局下偏移0.015625px；改为有明确响应式宽度的居中容器，保留原严格几何断言，再跑通过。
- 初次全desktop Node测试的backend-session import触发Electron包的自动Linux二进制下载；精确停止这次测试的install子进程，保留初次93通过/1失败日志。改为只有生产默认调用才取Electron session；注入SDK的纯测试不触发下载，后续97项全过。
- 初次目录picker测试对showModal使用空mock导致dialog仍为hidden；将mock实现原生open可见性，保留原可访问性断言，3项通过。
- 新窗口退休串行化初版把cover加载也串行等待，使原“加载时Quit”回归挂起；改为只串行物理窗口变更，cover加载在队列外等待，原退出测试及新增替换中Quit门均通过。
- 新Runtime已提交启动检查揭示原early-exit fixture存在发送竞态；fixture先观察真实exit再释放PG，区分“确定未提交”与“向仍活child发送后结果未知”，不降低退出断言。
- 最终交叉审阅发现main在Session退休成功前清掉引用，第一次退休失败后再次Quit能绕过原失败继续停PG。新回归在修前失败；现在只在退休成功后清引用，重复Quit继续拒绝停库与exit0，原生错误窗口报告无法完成的清理。测试中的原启动状态文案与退出错误分别断言。
- Windows monitor保留已退出成员HANDLE，40个短任务使句柄从191增长到271；现只移除并关闭已确认signaled的HANDLE，修后同样实测稳定189。最后共享terminal输出回归又暴露revision观察先于原保留总量清理完成；阻塞callback确定性反例在旧output失败，修后129项共享回归及27项真实跨域集成通过；之后MCP Supervisor总关闭吞错与open未发布时丢库存两项也已修正，最终131项共享/28项真实跨域通过。详细记录见terminal分工报告。
- 真实interop和PG的最初失败、收敛过程由分工报告保留；外部精确清理不能计作产品成功。

## 仍在执行

首个正式NSIS候选已实际安装到当前用户目录`C:\Users\plumlocal\AppData\Local\Programs\pulsara`，图形首次setup确认已有Ubuntu/plumwsl并完成私有Linux资源部署。interop与Hiddify进程保持开启。但首次工作台启动失败，不能记作安装版闭环通过。

失败为`database_schema_action_required`，原verifier进一步定位`schema_catalog_drift`。Windows checkout将baseline SQL的2793处LF转成CRLF，原resource读取未经统一直接执行，使5个PL/pgSQL函数定义含CR；仅在内存诊断将观察文本CRLF还原LF就精确匹配原expected catalog。正式修正统一迁移SQL/JSON的资源身份及执行文本，不放宽observed verifier，也不手工改已装DB函数或ledger。

首次失败库精确身份为`7700516472209506840`，data=`/home/plumwsl/.pulsara/desktop/postgres`，socket=`/home/plumwsl/.pulsara/desktop/socket`，Unixsocket-only，`pulsara_v3.sessions`为0。实际点击Quit保留原失败并拒绝停PG；已安装exe维护检查返回1且明确PG仍活。原wait-owner确认kernel锁释放后，精确结束本次失败GUI主进程（PID13308、启动UTC08:16:11.3197386），再经原TS PG owner停止已验证实例；随后status3、已安装exe维护检查0、Windows主程序进程0。此为明确的开发候选外部清理，不能计作正常Quit成功。原故障、精确清理及维护日志在root output中保留。

修正版NSIS已实际通过原维护门完成同版本开发候选替换；在Finish取消自动运行后，经原TS reset guard核验同systemid并清除此空测试库，再持原install flock精确移除旧版本资源目录。第一次清资源尝试早于异步reset完成，被“PGDATA尚存”断言拒绝且未删除；等原reset明确结束后再次验证才删除。未修改摘要marker，Linux用户home/已选目标/其他工作保留。此操作是开发候选替换，不宣称产品自动升级或坏库自动修复。

修正版安装程序正常启动、原setup重新部署后，已实际进入完整HTTPS工作台（本次端口40407），显示本地服务已连接/数据正常；设置与模型页可用。新正式PG的systemid为`7700523439330484737`，原严格deep verifier通过，Unixsocket-only；检查时canonical sessions0。用户正在模型页亲自配置连接，尚未运行真实provider。安装版正常Quit/重开、图形workspace/附件/工具和其余矩阵继续执行，已通过的分项不代表B1–B5全部完成或Windows发布资格激活。

本轮开始时用户默认Linux与Windows Pulsara home均无保存模型连接。按用户选择，界面已打开“设置→模型”并通知其配置；配置完成后才运行真实provider验收，保存配置作为只读输入，不从环境变量编造凭据。不会输出API key或OAuth token。用户另明确pgcrypto来自以前的性能测试且生产不用；原迁移套件14通过/1缺该扩展测试前置的记录保留，不为凑绿扩充生产依赖或删除测试。

## 安装版真实模型与图形验收追加

用户已在安装版保存DeepSeek连接；当前会话选择DeepSeek V4.1 Flash 1017、推理high。Windows图形Linux目录picker实际创建并选中`/home/plumwsl/workspaces/Windows 中文 验收 20261025`，由界面创建会话（短ID `r3d6okwu`），未用外部mkdir代替产品入口。2026-10-25 16:59通过实际composer提交限定当前目录的`smoke.txt`写入与读回任务；界面完成并显示绝对路径与“Windows WSL 中文验收通过”。模型报告31字节、LF及0600；这些具体文件与tool事实由另一路只读交叉核对，不能仅据模型自述验收。正式PG及用户settings/新session从此保留，不再沿用首个空库的开发reset流程。

Computer Use在用户明确恢复后可正常操作主窗口；原生文件选择模态可见，但set_value返回控件不在cached app state，重新激活并坐标操作仍返回not target window。因此安装版实际附件选择请求用户协助；此工具限制不记作产品导入失败，也不把旧G0的选择/拖放结果记为当前安装版已通过。

用户观察到Windows模型正文与macOS字形不同。当前`base.css`的正文栈为Iowan Old Style/Palatino Linotype/Palatino/Georgia/serif，没有统一正文webfont或在线字体下载；本机Windows装有Palatino Linotype，中文需系统fallback。记录为跨平台排版差异，尚未改字体设计。另Windows/Linux快捷键提示已由既有desktopHost平台值改为Ctrl，macOS保留⌘，定向5项及类型检查通过；此小修改尚未进入当前已装制品。

只读交叉核对已完成：完整会话ID为`session:r3d6okwuq2trl3ribxzct6rbuy`，5条实际tool记录含write_file/read_file SUCCESS；文件实际31字节、LF、0600、uid1000，与界面一致。记录位于`output/phase-b/desktop/provider-acceptance/gui-session-read-only.json`。用户随后完成正式安装版文件选择，副本`/home/plumwsl/.pulsara/imports/eb9a0e3ec1504ee78e7a6be77118e53d/Windows 中文附件.bin`与Windows原件285字节逐字节相同，文件0400、目录0500；`output/phase-b/root/installed-gui-file-import.json`保留实际路径与比较结果。此步未向provider发送附件，拖放单独验收。

当前mise/prepare_resources构建缓存合同7/7通过，使用独立ext4最小源码快照、原真实mise/uv与既有native动作fixture；不是新一轮native资源全量编译。首次5通过/2失败为测试启动环境缺uv PATH，原日志保留为`build-cache-first-missing-uv.*`，随后通过mise解析实际uv路径后原完整测试通过，未修改生产、测试或断言。最终日志为`output/phase-b/root/build-cache-current.*`。

用户实际拖放同一Windows测试文件后，第二份import为`a3c79954e08d441a8f0828d1c70efc8e`，同样285字节逐字节一致、0400/0500。随后才由GUI提交这份纯测试附件让真实provider只读检查并在当前workspace创建预览文件。HTML 3018字节、PNG 17701字节、PDF 592字节；实际PNG灯箱可见，用户完成Windows原生保存后`Windows 预览下载.png`与WSL原件逐字节相同。实际HTML预览运行于https，脚本求值的`typeof require`及`typeof window.pulsaraDesktop`均undefined；这是正确隔离，不是接口丢失。实际PDF viewer显示“PDF 已加载完毕”、1/1和“Windows WSL PDF OK”。记录见`installed-gui-previews.json`、`installed-gui-image-download.json`及PDF accessibility观察。模型首次给出的裸空格Markdown链接未解析，后续新用户turn只要求用尖括号重发路径后可点击；没有修改解析器或改写旧回复。

当前安装版窗口最大化、还原、最小化/恢复、Ctrl+K会话搜索均已操作观察。关闭窗口后list_windows不再显示Pulsara，但6个原应用进程ID完全不变；重新从已安装exe入口启动恢复原窗口ID30870200、原会话与预览，未重启kernel。此项证明后台驻留和单实例恢复，不等同于tray菜单点击、正常Quit/冷启动或完整DPI/IME矩阵。Windows快捷键提示修正后的静态前端已重新构建，当前安装制品仍是此前alpha.6内容。

真实provider分工证据已归档：[terminal/输入关闭/工具链与Windows PE](../../backend/phase-b/provider-acceptance/report.zh.md)、[artifact、memory/history及异常Host恢复](../../desktop/provider-acceptance/report.zh.md)。后者只读复核正式GUI三个turn均COMPLETED，13条tool事实为12 SUCCESS及首次读取不存在AGENTS.md的1 FILE_NOT_FOUND，错误原样保留。各分工使用正式安装包、保存连接只读快照和独立UUID数据库，未改变用户已保存配置。安装版CLI实际返回0.1.0a6；应用正常运行期间维护检查实际返回1并报告锁busy，保留Electron并发profile的cache告警，不因该拒绝尝试更新或停PG。

## 长程验收新发现的共享准入缺陷

原subagent/capacity长程runner在完成compaction后，最终ROOT准入停滞。真实原异常为`ROOT provider input head changed`；原TurnAdmissionCoordinator对这一确定性过期仍按ACK unknown反复提交同一prospective候选。旧canonical turn及child均COMPLETED，不能归因于未退出的MCP服务或简单称seed遗留RUNNING；head变化的具体交错继续以分工窄观察为准。

正式修正仅给同workspace的head过期明确typed错误，保持原FULL/CONFLICT/ABSENT确认：FULL仍采用已提交结果，只有ABSENT后关闭旧prospective、以同一immutable intent/command及冻结模型选择重新准备输入。已装epoch及SYSTEM/tools不被重建，workspace/其他冲突不走此恢复路径；取消在旧确认中被消费后仍检查原cancellation intent/Task状态，拒绝再次准入。4生产文件修改已通过6项真实PG定向回归（原ACK矩阵、两类stale窗口、取消窗口）、28项相关测试和交叉只读审阅。当前已安装alpha.6尚不含此修正，原长程失败场景须新制品复验；版本变更按spec“不自行bump”等待用户明确确认，不覆盖运行中的同版本资源。

## 安装版正常退出与冷启动

全部独立provider DB任务先完成收尾后，实际以安装版Ctrl+Q走原退出流程；应用进程0、原pg_ctl status3，随后同一已装exe维护检查exit0。没有外部kill或手工pg_ctl stop。再从同一安装入口冷启动，启动窗口退休为新workbench Window，HTTPS端口40407→33877，完整界面显示本地数据正常、1组模型配置和原会话28条记录。

只读前后核验：PG system_identifier仍`7700523439330484737`、数据目录inode仍171484、Unixsocket-only；postmaster启动时间由16:43:52.967046变为17:32:00.940553，确认确实停后重启。原session、三个COMPLETED turn和workspace id逐值相同；保存settings的inode/size/mtime逐值未变，未输出或复制其凭据。证据为`installed-normal-quit.json`、`installed-maintenance-after-normal-quit.*`及`installed-restart-before/after.json`，观察脚本`observe_installed_restart.py`。这证明当前alpha.6正常Quit/冷重开保数据，不等同于尚待新候选的更新保数据或ROOT stale修复验收。

## alpha.7 本地升级验收（进行中）

用户明确批准`0.1.0-alpha.6`→`0.1.0-alpha.7`本机构建与安装，不打Git tag、不发布。升级前经只读快照记录正式PG、保存模型选择、原session/三个turn及settings文件属性；随后再次实际Ctrl+Q正常退出，应用进程0、PG status3、原已装exe维护检查exit0。记录为`alpha7-upgrade-normal-quit.json`及`alpha7-preupgrade-maintenance.*`；此处不调用首个空库的开发reset或删除旧runtime流程。构建和更新后的实际结果继续追加，不能用升级前检查代替保数据验收。

alpha.7实际NSIS构建、版本/lock/9项聚焦测试、wheel来源及取消护栏、静态Ctrl提示、许可与原资源搬移smoke均通过，见[构建报告](../../desktop/alpha7-build/report.zh.md)。同一NSIS经`/S`运行原维护门完成本机更新exit0；再启动经原setup部署新版本Linux资源，工作台HTTPS33451就绪。原session仍28条记录，Ctrl K/N提示已实际显示；没有模型调用或修改历史来填补更新。

随后再次正常Ctrl+Q，app进程0/PGstatus3；实际NSIS普通卸载exit0，Windows主exe确实消失，保存的WSL目标仍在。离线检查Linux资源、PGDATA inode、settings属性及四个工作区文件均保留。用同一alpha.7 NSIS重装exit0并启动，工作台HTTPS40363正常复用原资源及数据。升级后和重装后的独立只读核验均通过：systemid`7700523439330484737`/inode171484不变，settings inode/size/mtime/uid/mode、模型选择、三个COMPLETED ROOT及initial/final entry IDs、28条entry分类数量均不变，原deep verifier通过；两次postmaster实际新启动。详见[三阶段保数据报告](../../backend/phase-b/alpha7-upgrade/report.zh.md)。未reset数据库、删除旧runtime或改payload marker；这些结果是正常维护路径的实证。

本机interop仍enabled，Hiddify `sing-tun Tunnel`/`tun0`为Up，记录`alpha7-network-preserved.json`。NSIS的PE ProductVersion只呈现`0.1.0.0`，不据此误认alpha编号；实际随包manifest为`0.1.0-alpha.7`，正式guest package为`0.1.0a7`，二者与构建owner一致。

## alpha.7 安装版长程复验

三组均明确断言正式安装路径`/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7`，没有source overlay；保存连接仍只读加载，使用各自UUID DB和私有home。完整记录见[安装版长程复验报告](../../terminal/phase-b/provider-acceptance/report.zh.md)。原capacity完整通过：15次真实provider调用（含summary）、4ACTIVE+1PENDING、MCP READY、COMPACTED交接、queued child真实echo及最终五task COMPLETED。此次实际再次触发ROOT候选head22→26，新增四条均为SUBAGENT_TASK TOOL_RESULT；安装包按typed stale关闭旧prospective并以同一intent重新准备后成功前进，原失败不是仅靠不再触发竞态而消失。context完整通过：8次真实调用、两次PENDING/PROMPT_QUEUED、worker显式完成含三个marker、唯一inter-agent message及原prefix检查。

跨压缩进程补项沿原active ROOT manual-compaction合同完成：4次真实调用（含summary）；terminal yield0实际返回running，COMPACTED successor的真实handoff包含同一仍活process ID，模型随后用原terminal_process wait获得Linux_DONE和实际cmd.exe的PE_DONE、exit0。没有重启任务或外部kill；原core.shutdown后PHYSICALLY_JOINED，reader/watcher/scope worker退出、helper0、scope目录退休，独立DB/home/workspace清理完成。首次自造非canonical commandID及随后误将idle压缩路径当作active handoff的两次探针失败原样保留，未修改产品或放宽active断言。各场景完整prompt、响应、工具结果及prefix观察由terminal分工归档；此处不把此前alpha.6其他provider项目改称alpha.7全量重跑。

## 尚待用户参与的正式界面验收

正式alpha.7由用户级能力页创建禁用的唯一`notion-oauth-alpha7-smoke`连接，沿原登录动作发起Notion OAuth；Linux callback端口17839从Windows只读HEAD得到404，UI为“等待浏览器授权”，已请求用户亲自完成。未向现有会话启用/重载该MCP、未调用Notion业务工具；授权、连接目录测试及仅本机临时授权/配置清理须据后续实测记录，不能沿用G0授权通过结论。普通睡眠/唤醒与IME/tray/DPI等界面矩阵仍待补，Windows支持尚未激活。

用户随后完成授权，正式UI显示“已授权”；只读LocalSettingsStore核指定grant存在且未过期、原模型选择和会话/PG身份未变。实际编辑器“测试连接”返回“连接正常：65个工具、8项资源、0个资源模板、2个提示词”，保存于`alpha7-notion-test-success.txt`。原test仅discover目录，不调用tools/call或resources/read；不能把这次测试缩写成只发送list_tools。首次测试未捕获短暂提示，第二次只读测试明确捕获上述UI结果，未启用服务或调用业务操作。

本次临时配置/本机grant尚待清理。非UI清理探针因没有当前ready管道的可信公共证书副本而未执行，没有绕过TLS校验；改用原GUI时，Computer Use规则要求在界面删除本地数据前动作时确认，已请求用户确认唯一测试owner。此处不将准备好的清理脚本记为已执行，也不将本机清理等同Notion远端revoke。

用户确认后，原GUI实际“注销本地授权”显示未登录，再“移除服务→确认移除”，界面MCP0。独立只读检查确认唯一测试config/grant均不存在、专属managed credential0、Linux callback17839为ECONNREFUSED；原模型投影/凭据存在性、session/三turn/28条entry和PG identity保持。见[Notion正式UI与清理报告](../../backend/phase-b/alpha7-upgrade/notion-ui-report.zh.md)。没有捕获HTTP cleanup_attention原响应或直接观察原manager内存pending，不将这些写成观测事实；API清理探针始终未执行。

睡眠前只读快照已完成，原Windows主进程、正式alpha.7 kernel/PG、busy锁与原会话均确认；Windows以本机已核kernel监听的公共证书校验完整DER，HTTPS40363 `/healthz`为200。该探针是前后快照，不是持续抓包或GUI连接全程不中断证明。已请求用户手动睡眠后唤醒，睡后结论待观察。

用户手动睡眠并回复已唤醒后，after观察exit0、10项比较全通过。实际System Kernel-Power 506→507事件为本地18:21:37.0856655→18:22:57.0034213，间隔79.9177558秒；前后快照相隔239.920636秒，不能当睡眠时长。原Windows main8324、Linux kernel468/PG498及各自启动身份、namespace/boot、同systemid/PGstart、两把busy锁inode、diagnostics socket、原3turn/28entry会话及listener/完整cert均不变，Windows健康检查仍200。原network owner未重启，但TCP4→6且旧localport均消失，结论仅为重连后恢复可用。GUI再次打开原会话显示已连接/已完成，Hiddify tun0 Up、interop enabled。没有重启或终止app/WSL/PG。完整证据见[正式安装版睡眠报告](../../desktop/installed-sleep/report.zh.md)。

已请用户以真实中文输入法输入不发送的测试草稿、通过系统托盘恢复窗口并操作鼠标贴靠/还原；另确认是否有不同缩放第二屏或AltGr布局。Computer Use文字注入不作为IME候选框证据，尚未执行的硬件/输入布局不记通过。

用户随后逐项回复“都没问题”，并确认没有不同缩放第二屏或AltGr布局。原窗口重新观察显示同HTTPS40363、原会话已完成、composer中仍有未发送的“中文输入验收”，证据`alpha7-ime-tray-snap-ui.txt`。真实IME/托盘点击/贴靠操作的证据来源是用户现场执行反馈，root只读核对最终草稿与会话；不冒称由自动化逐步观测，更不把当前单屏结果扩为混合DPI或AltGr资格。草稿保留，未发送模型或擅自删除。

## 本轮交付边界

本机alpha.7已安装并保持运行，用户模型、原会话和工作区保留；本次临时Notion连接/本机grant及测试DB/进程均收尾。真实长程分工审计的15个测试DB与26个临时路径已不存在，正式PG继续运行。interop/TUN保持，未打tag或发布。当前候选已经完成上述本机正式路径实证，仍不是Windows支持激活声明。

仍缺的环境/资格分开列明：混合DPI及AltGr布局资格尚未验收；WSL未安装/需系统重启、真实整盘耗尽等未在日常共享主机施加，现有注入/研究证据按原边界保留；受影响macOS原生及其应用包GUI新回归未执行，原生Ubuntu按用户要求延期；完整跨平台发布制品/签名与§13激活矩阵未执行。不能用删除规范条目、旧Mac/G0记录或本机WSL结果补齐这些事实。

用户随后补充笔记本有可用内屏，当前使用HDMI外接屏，键盘也有右Alt键。此前“都没有”的回答不继续作为硬件缺失结论；正在只读检查活动显示器/有效缩放和输入布局，再安排双屏实测。右Alt是物理按键，是否提供AltGr字符取决于布局，二者不等同。

## 双屏与快捷键追加（进行中）

用户亲自启用扩展后，原生只读API确认外屏DISPLAY6为1920×1080/96DPI/100%，右侧内屏DISPLAY1为2560×1600/144DPI/150%；原Pulsara PID8324/window thread36108从外屏96DPI实际移动到内屏144DPI，窗口完整位于内屏。root通过Computer Use核同HTTPS40363、未发送中文草稿保留，PDF显示Windows WSL PDF OK/已加载完毕，HTML显示正确隔离的两个undefined，标题栏最大化/还原按钮实际命中且布局可用。原生API元数据与root UI观察分别保存，移回外屏正在等待用户操作。

与跨屏问题独立的共享键盘缺陷由原React组件回归复现：CtrlAlt K/N被误作快捷键、组合期CtrlK/N吞键、搜索弹窗CtrlAltK误关闭。仅全局handler与搜索关闭handler修正，5增/3减；原失败8项中3过5败保留，修后新增13+原搜索9=22，相关5文件254项通过，tsc/lint及交叉审阅通过。mapped AltGraph glyph原实现也通过；当前中文HKL0804、物理右Alt或合成DOM测试均不冒称真实AltGr布局资格。详见[快捷键审阅与可审diff](../../terminal/phase-b/shortcut-review/report.zh.md)。当前alpha.7静态资源未含该修复，已按规范请求本地alpha.8版本授权，未自行bump或同版本覆盖已装资源。

用户随后明确批准alpha.8及本轮后续必要本地alpha候选递增，并要求修改实施规范；spec §8.3与§24、README已同步，不再逐次询问本地版本权限。alpha.8构建完成，见[构建报告](../../desktop/alpha8-build/report.zh.md)。用户暂不方便鼠标操作，随后明确稍后协助移回外屏；该项暂留待补，不阻塞其他独立检查。

## alpha.8 更新前正常退出失败（待修复）

2026-10-25 18:47，先采集独立的alpha.7当前只读保数据基线，原模型、三个COMPLETED ROOT及28条entry、PG身份与deep verifier通过；settings使用Notion清理后的当前属性，不沿用旧alpha.6基线。root核对未发送草稿“中文输入验收”仍在后执行Ctrl+Q。正常退出没有完成，原应用显示“内核退出时报告收尾失败 (StaleHostWriter: host writer generation is stale)；数据库尚未停止”。保留`alpha8-preupgrade-quit.json`与`alpha8-preupgrade-stale-host-ui.txt`原失败，未运行安装器、强杀进程、停库或重置数据。

PG分工只读现场观察：原session writer generation为4，lease在18:21:57.736407到期，最后updated_at为18:21:27.744315，落在本次18:21:37至18:22:57睡眠区间；报错名称本身不能区分generation变化与lease到期。当前证据指向睡眠后的会话写入资格与关闭路径，根因及修复继续由原三位agent并行调查。先前睡眠快照证明的同实例/锁/网络及UI恢复不被改写，但未验证有效writer或恢复后的新turn/正常Quit，完整睡眠恢复验收因此重新开放。

后续只读确认原kernel468已退出、kernel锁空闲、控制socket已退休，正式GUI无活动任务、临时MCP已清理。root使用原失败页“重试”，经原锁准入启动新kernel53665/HTTPS42805，原PG498、systemid/启动时间/inode、settings、模型与三轮/28条记录全部保持，deep检查通过。随后在未打开会话的空闲总览正常Ctrl+Q，18:54:26实证app进程0/PGstatus3，18:56:10原维护检查exit0。没有外部kill、手工停库或SQL修改lease；这证明明确重试后的正常退出，并不把旧失效会话的失败改称成功。见[退出失败与重试现场](../../desktop/quit-failure-alpha7/report.zh.md)及[独立保数据观察](../../backend/phase-b/alpha8-upgrade/report.zh.md)。

用户观察新窗口已回到外屏，并关闭了内屏显示；该窗口由重试重建，不作为同一原窗口150%→100%的拖回实证。用户随后要求优先自动恢复，失败时提供友好重试界面；新边界已写入spec §9.3，原terminal分工修共享Host失效收尾、desktop分工复用现有controller/前端恢复链、backend分工交叉审阅。当前alpha.8仍未安装，后续修复通过后按已授权本地候选递增构建；无tag或对外发布。

## writer失效收尾与自动恢复修正（源码验证阶段）

正式installed alpha.7在独立私有PG中复现过期lease的原SessionEnd guarded read失败，完整trace与最初错误TerminalManager API探针均保留。Host仅增加一个process-local续约异常槽，派生unavailable与typed lost；非正常取消的续约失败封旧准入，续约task结束后委托原Core.close_session，避免close取消/等待renew造成自等。SessionEnd仅对binding读取的StaleHostWriter跳过无权限的hook发起；fence、dispatch、普通DB异常和实际scope/MCP/I/O close错误仍保留。

controller复用原_ResumeInFlight预约等待同一个Core close，成功后在同任务进入原cold resume，两个并发请求取得同一新handle；旧关闭失败保持_Quarantined和原cause，应用正在关闭则禁止再冷开。bridge观察writer不可用，触发现有前端450ms重连路径；没有新增重连循环、持久恢复记录、lease续活或用户消息/工具重放。前端恢复提示及技术详情沿原runtimeStatus；永久close失败不提供无效的普通重连，API retryable=false，说明需日志和完整重开。公共详情复用既有真实凭据scrub，不能取得安全边界时保留固定typed说明与私有cause。startup cover仅调整呈现，退出/重试/维护权限不变；不承诺部分reset失败的数据仍完整。

候选私有源码与真实PG验证：过期idle正常close；真实cmd.exe terminal在失效后reader/watcher/scope worker均退出、helper0/socket退休；普通续约I/O错误也封准入并关闭；随后原cold resume使generation1→2，旧guard不复活。真实controller并发恢复同handle，最后controller/Core关闭与私有PG/home清理成功。Host新增9项含4项真实PG负例通过，相关11文件315通过/1项原PG条件skip；renderer全29项、前端App+Workbench全363项、tsc/lint/Ruff、真实Electron StartupCover通过。初次前端361/363及测试fixture初始化失败均保留，未改旧行为断言或新增skip取得通过。原三位agent交叉审阅后冻结生产源码，开始alpha.9构建，尚未将本段当作已安装制品或真实睡眠复验。

## alpha.9 正式安装、睡眠恢复与正常退出（本机分项通过）

alpha.9完成正式构建及制品检查，见[构建报告](../../desktop/alpha9-build/report.zh.md)。本地NSIS正常更新在19:22:17返回0，沿原setup部署`/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.9`，包版本`0.1.0a9`；alpha.8仅构建未安装。独立只读保数据核验确认正式PG systemid/inode、保存模型与settings属性、原会话三个COMPLETED ROOT及28条entry保持，未reset或改写配置，见[alpha.9保数据报告](../../backend/phase-b/alpha9-upgrade/report.zh.md)。本轮未打tag或对外发布。

真实GUI核验Ctrl+Alt+K/N不再误开弹窗，搜索内Ctrl+Alt+K不误关闭，普通Ctrl+K/N仍可用，未发送草稿保持。这证明本次快捷键修复在已装制品有效，不代替真实AltGr布局或IME候选组合的硬件资格。

通过GUI创建独立会话`session:36omh67maz3ezaef3amghzb6ka`，使用用户已保存的DeepSeek模型；睡前只要求回复“睡眠前连接正常”，实际回复一致且无工具调用。随后留下未发送草稿：“这是睡醒后的第二条验收消息。请只回复‘自动恢复后模型调用正常’，不要调用工具，不要读写文件。”原始UI记录保留实际标点与完整内容，不将本段转述作为逐字证据。

用户手动睡眠并唤醒，System Kernel-Power 506→507事件间隔108.4911011秒。原Windows main18816、kernel494、PG523及各自启动身份保持，原HTTPS端口32915、完整证书与健康检查通过，writer generation自动由1变为2。未点击重试、未重启应用或数据库；同一窗口草稿原样保留，恢复足够快时无需停留在重连页面。Hiddify tun0仍Up，ifIndex由47变为34，不能写成网卡所有身份均不变；interop保持开启。前后16项比较及事件证据见[alpha.9睡眠报告](../../desktop/alpha9-sleep/report.zh.md)。

用户确认误按Esc后，root恢复Computer Use，核对草稿并实际发送一次；19:38模型准确回复“自动恢复后模型调用正常”，界面完成且输入框清空。独立数据库核对两轮均COMPLETED、四条正文entry、两条不同client ID的QUEUE_PROMPT及两条精确对应initial entry的CONSUMED队列记录，工具entry为0；原用户会话仍为三轮/28条entry。首个观察脚本误用SUBMIT_PROMPT分类的失败保留，后续按实际GUI的QUEUE_PROMPT owner严格核对，没有放宽重复消费断言。

随后以Ctrl+Q打开原退出确认框，并点击“退出Pulsara”。19:40:50的第一次只读快照处于确认尚未点击阶段，原JSON保留为中间状态，不算产品关闭失败。19:42:12再次观察应用进程0、原kernel494不存在、正式pg_ctl status3（no server running）；19:42:26已装exe维护检查exit0，应用仍0。未外部kill、手工停PG或终止共享WSL。证据为本报告`evidence/alpha9-after-sleep-*.txt`、`alpha9-quit-confirmation-ui.txt`、`alpha9-post-sleep-normal-quit*.json`及`alpha9-post-sleep-maintenance.*`。

当前应用保持关闭，测试会话与用户原数据保留。至此本机alpha.9的“闲置睡眠→自动恢复→真实新消息→正常Quit”分项通过；没有把独立活动进程回收实验冒充本次睡眠时正在执行任务的证明。活动任务睡眠全矩阵、同一原窗口150%→100%返回、真实AltGr布局、其他故障环境及受影响macOS原生回归仍按各自证据边界记录，原生Ubuntu继续延期，Windows支持/B5尚未激活。
