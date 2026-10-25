# 正式安装版alpha9：同kernel睡眠后会话writer恢复观察

最终结论：所选“闲置睡眠→自动恢复→真实新turn→正常Quit”矩阵通过；不扩至active-task全矩阵或B5。下文前两节保留19:35时点的只读观察，最后一节是root/PG后续实际收尾。

root已安装alpha9并确认保数据；本次使用独立session:36omh67maz3ezaef3amghzb6ka，未改原3turn会话。睡前真实GUI已完成第一条短文本回声“睡眠前连接正常”，第二条仅在draft未发送。root明确BEFORE后采样；用户手动睡眠并报告唤醒后，root明确AFTER才采样。观察脚本不操作UI、发消息、点击Retry、调用工具/provider、启停app/PG/WSL或改变网络/电源/interop。

## 19:35只读观察阶段（当时实际后续turn与Quit待root）

before为19:29:52.751507+08，after为19:35:14.097181+08，16项比较全部通过。两次仍同Windows main18816/HANDLEcreation134374009866424052、kernel494/start1448、PG523/start2029、PIDnamespace4026532221/bootid；PG systemid7700523439330484737、postmasterstart19:23:28.015074+08、PGDATAinode171484与strict Unixsocket/noTCP事实不变。kernel锁145305与PG锁171582仍busy，原control/listener/full公证书不变。Windows after使用before完整PEM为CA并比完整DER，原HTTPS32915 /healthz一次200/ready，没有换pin/origin或重试业务请求。

目标session/工作区未换，after仍只有原COMPLETED turn:ayj2dbwvk6jtfchjr7nl3dorwu/latestentry2，没有发送已保留草稿。writer generation1→2，owner由host:vluuyqd74zjwtk5ysfxro6zkfu换成host:piazsjena2nerxdwumqfkkxtzm；after数据库19:35:09.296938+08观察lease有效至19:35:32.011864+08。这与用户未点Retry/未发送、观察仅只读时的产品自动cold恢复一致；generation改变不是第二kernel。该元数据不单独替代旧物理close的实证，真实有工作Core/terminal/controller组合已由终端agent独立验证；本次正式GUI为idle/无MCP。

before记录lease有效至19:30:13.037238+08，确被after数据库时间跨过；但before至实际睡眠间仍可发生正常续约，因此该旧snapshot expiry不能冒称真正入睡前最后一次lease expiry。实际代际变化/当前有效性按两次真实行值报告，没有将IOunknown称作writer已被别的owner抢占。

## 实际系统事件与连接变动

Microsoft-Windows-Kernel-Power 506 record1523为UTC11:30:35.4317565，507 record1527为UTC11:32:23.9228576，事件间隔**108.4911011秒**。用户目标约60秒不当作实际值；快照间隔321.345674秒也不是睡眠时长。原始事件7位小数保留，没有修改系统clock。

同Windows网络owner33048/精确creation不变，但Established TCP由6条变2条，旧6个localport全部消失；不能说原连接全程不断。TUN仍tun0/sing-tun Tunnel/Up，ifIndex47→34如实保留，不能把它写作设备index完全不变。未读取或改变VPN设置。

本阶段只读检查没有失败，before/after与stdout/stderr/run日志不覆盖。pidfd/HANDLE/锁观察fd、SQL连接、TLS读取与WindowsHTTP观察均在各次有限命令退出时关闭，不持跨睡眠daemon。19:35只读观察时正式app/PG仍运行。**当时还不能仅凭健康检查、lease有效或界面normal宣称通过；后续root实际发送原draft、新turn与正常Quit的补齐证据见下一节。**

## 后续已补齐：实际新turn与正常Quit，所选闲置睡眠闭环通过

root真实CU核验睡醒后draft原样保留，随后只发送这一条原草稿，实际模型精确回复“自动恢复后模型调用正常”。GUI证据为root-evidence/alpha9-after-sleep-draft-ui.txt与alpha9-after-sleep-second-turn-ui.txt；不是观察脚本代替用户发送或用fake模型推导。

PG agent于UTC11:39:54独立只读验明同systemid/postmasterstart，专用session为generation2、2个COMPLETED ROOT、4条canonical entry（2USER_MESSAGE/2ASSISTANT_MESSAGE）、2个QUEUE_PROMPT命令、2个不同client_submission_id，各CONSUMED至对应ROOT精确initial_entry；无tool entry/重复输入，第二条assistant block原文即该回复。原session:r3d6…仍原3个完成turn/28entry。完整行与校验保留pg-evidence/after-wake-turn-verified.json，未重置数据库或读取rawsettings/token。

root随后Ctrl+Q进入原正常退出确认。19:40:50的alpha9-post-sleep-normal-quit.json仍app4/PG running，是等待用户选择的确认阶段，**不是产品failure**，原文件与alpha9-quit-confirmation-ui.txt一并保留。root点击确认页140“退出Pulsara”后，19:42:12.3329672+08的独立核验确认app0、原kernel494的/proc已不存在、pg_ctl status3/no server running；未使用外部kill。19:42:26.0925862+08原maintenance检查exit0且app0，stdout/stderr原文件保留。观察分支没有为这些检查再次启动app或PG。

因此，本次正式alpha9所选矩阵“闲置睡眠→无用户Retry的自动会话cold恢复→真实新模型turn→正常Quit/维护门”已通过。此前末段pending是19:35阶段记录，现由上述root/PG实际证据补齐；before/after原始快照没有覆盖。该结论不扩成active-task/全部MCP与terminal在睡眠中的穷举矩阵，也不宣布B5激活。alpha7真实lease/退出失败及其限定继续作为历史保留。
