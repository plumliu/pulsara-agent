# 独立审阅：active-sleep 与 spec §11–13

本次只读审阅，不启动服务或资源构建。结论：root 的 alpha10 **所选真实活动 PE 睡眠→自动关闭旧scope/冷恢复→新消息→正常Quit**结论成立；未发现要求撤回该分项的实质矛盾。不能把所选通过扩成所有工作类型/硬件/发布资格；root报告已有这些限定。

## active-sleep 原证据核对

- before/after与16比较字段一致，kernel472/start9469、PG499/start10106、原main34108、endpoint37313/完整cert及锁身份保持；writer1→2且新lease有效。
- Windows observer源码只OpenProcess/持同HANDLE/Wait/GetExitCode，不发signal；ready/exited的PID14916、image、creation134374126291452207一致，原HANDLE实际signaled、exit1。该exit1是被收尾测试PE的结果，不能写成工具自然成功；root没有这样写。
- 三组实际506→507间隔为1.076、7.430、132.121秒，末次UTC14:42:43.0974559→14:44:55.2181384；没有用预定时长冒充实际。
- stdout marker的准确依据是`gui-draft-before.txt`的296“区域 命令输出”/298单独文本。最初yield0 TOOL_RESULT确实output为空；我初次只查prepared/task-running而提出的“未捕获marker”疑点已撤回，改准确引用即可。
- session-after-message两ROOT COMPLETE、两独立client的QUEUE_PROMPT/CONSUMED initial_entry对应；tool_calls完整值与睡前相同且仍仅一次terminal。UI草稿/新回复有实际CU观察。正常退出后app0、原kernel不存在、PGstatus3、maintenance0；确认前中间快照未被当产品failure。
- 本run未持全部Linux task pidfd，不能独立宣称每个旧PID/FD都被逐一审计。root明确限制这一点，并把双域owner物理join证据交给独立真实生产scope套件，合理。无需因此追加睡眠中每种owner各一套测试。

## §11–13 逐项判定

| 规范位置 | 本轮判定与剩余范围 |
| --- | --- |
| 11.1 A0/A1/A2 | 原owner提取/真实before来源/consumer闭包有既有记录；本轮没有新增platform接口或空owner。alpha10重新核安装来源，不沿旧editable代证。 |
| 11.1 A3、13.1受影响POSIX | 所选Mac arm64真实回归与WSL真实源码/资源结果各自成立。native Ubuntu及未具备硬件资格仍按用户延期，不能称WSL等于native Ubuntu；不为补表新增用户未提供的机器或改共享环境。 |
| 11.2 B0 | 既有G0 D/C/P/F/N有限机制结论与正式接入不同层次；本轮未导入探针为产品依赖，不需重做整个G0。 |
| 11.2 B1、13.2接入/部署 | 真实已有目标/非root/ext4/private payload、正常安装/更新/卸载保数据、明确固定用户、目标/root/摘要失败与取消已有正式证据。offline支持由固定自足payload、实际G0无网络namespace材料验证与生产只使用stdin本地资源的接缝分层支撑，不称本轮切断用户网络。真实WSL未装/需重启已用户延期。默认配置变动输入阴性+实际固定-d/-u调用证明不静默换已保存目标，不要求修改用户默认配置制造资格。 |
| 11.2 B2、13.2控制/PG | 原3pipe/完整帧/EOF与unknown、精确实例/双锁/stop/reset合同已有生产记录，本批PG5故障另由backend归档。实际同endpoint pin、自然cert跨界/新握手、同端口错cert拒绝、seal/retire与关闭失败缓存有production owner证据。不能把carrier0单独当双域join。 |
| 11.2 B3、13.2图形工作区/文件网络 | 原产品picker图形新建/选中/中文路径、实际File/drop、HTML/image/PDF/下载，真实路径/替换/权限与mount negative分层已具备。明确漏索引的“Explorer新建后刷新”现已按原表补：真实Explorer创建，提前打开的正式picker点刷新后看到并选中，见本目录`explorer-*`；不另加目录全故障GUI组合。 |
| 11.2 B4、13.2安装GUI | 实际NSIS、冷暖启动/工作/正常Quit重开、卸载保data、已装OAuth/窗口/tray/中文输入与快捷键有既有证据；原生AltGr/混合DPI往返等按实际硬件范围限定，双屏已用户延期。不能把合成中文输入当新硬件IME证据，也不要求所有键盘布局组合。 |
| 12成本/范围 | 不是新测试门：相同验证只计一次；不扩原生Win32内核、任意distro、多目标或C盘workspace。不因本轮讨论把每个故障与每个owner作笛卡尔积。 |
| 13.1具体测试/cache/recipe | Mac/WSL/Windows各原结果已有归档；本轮mise一行改动补原cache7项通过。7项不测试cmd语法，实际Windows原mise入口成功单独证明。未新增skip/削弱旧断言。 |
| 13.2 Linux工具/interop | 真实PIPE/PTY/Windows PE/取消/后代/隔离/8槽/MCP/hooks/guard已有正式套件及本轮R2/R3回归，root真实活动PE睡眠为额外端到端样本；没有“每一种工具都必须再睡眠一次”的条款。 |
| 13.2回归/发布 | 当前本地Windows候选版本/资源/许可/NSIS来源已核；全平台发布集合、其他架构/签名分发不能由本机候选推定完成。保持手动workflow、不tag/publish，不把尚未授权的公开发布变成当前必须执行动作。 |
| 13.3 provider/长程 | 已有用户唯一保存连接、隔离DB/home下terminal/artifact/memory/history/MCP/hooks/subagent/compaction真实闭环，含正常/异常Host后检索；alpha10受影响context新包与R1确定性窗口另验。按原合同复用，不要求每次仅版本变化就把所有模型场景重跑，更不添加总调用/寿命限制。 |
| 11.2 B5及13.3最后条件 | 由root总表按已执行/明确延期/未授权分发区分，不能把本审阅或本机所选通过当作所有环境/原生平台/发布资格完成。未发现本批引入未解P0/P1；延期项不得偷标passed。 |

## 不应新增的两种门

**无需真实填满日常磁盘。** 11.2 B1写的是“磁盘满不误删或改环境”，13.2部署列“磁盘满/失败重开”，没有要求把共享ext4或物理盘灌满。正式prepare在真实shell的payload open分配点取得注入errno28、原dash exit2传播、trap清stage、无发布、旧marker/settings/PGDATA元数据保持，足以验证所测ENOSPC错误保数据合同。继续如实写“有限分配点注入”，不写“真实物理整盘满”或“解压全部写点已覆盖”。不能把未采用的更危险硬件故障方式自动升级成新验收阻断。

**无需同一main run让每种网络流全部同时在途。** 10.2要求旧Session封准入、原请求/下载对象结算、真实WebContents销毁后网络清理，生产renderer/IPC/connection按各原owner装配。现原main的showStartup封pin→retire(await destroyWindow)→runtime.stop，真实产品Quit/窗口更换、实际BrowserOwner/StartupCover destroyed、production BackendSession/readiness/download、renderer SSE和LocalImport部分流关闭可组合审阅；规范未要求同一run同时持每一种stream才可承认这些证据。

我先前写“本fixture不证明正式main全部在途闭包”是来源范围限定，**不等价于另增B5硬门**。若主表确无任何main退休中renderer在途请求的实际观察，最小补强仅需一条真实renderer import已出现stage时原Quit，核无发布/stage清/原退出完成（依据7.2导入取消及10.2原owner结算），不需要并行SSE/download/pending，也不需要新生产机制。当前无具体产品缺陷迫使新增这个组合；不建议为凑“全闭包”另开完整矩阵。
