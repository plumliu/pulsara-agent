# Windows WSL2 G0 最终验收记录

2026-10-25；本轮受测生产基线b741dd92，复用三位gpt-6.1-sol/high。仓库已fetch origin/main，main不领先当前基线，含3ac7fc2b。根AGENTS及当前Windows规格优先，早期原生Windows/专用发行版研究只作历史。

范围为Windows11 25H2 x64普通用户、WSL2.7.11.0、Linux kernel6.18.33.2、已有Ubuntu26.04.1/plumwsl UID1000/ext4、Windows Electron44.9.0及固定资源。Ubuntu数字版本不是准入条件。interop、Windows PATH、自动挂载、Hiddify TUN保持；实际网络nat，tun0/sing-tun Tunnel在睡眠后仍Up。结论不自动覆盖其他WSL/Windows/网络组合。

**最终结论：本机所选矩阵的G0-W-D/C/P/F/N机制可行性验收通过。** 两条终审发现的具体缺项——dirty PG真实恢复与OAuth callback陌生服务rebind——均已实际补验，原失败保留。研究候选没有接入生产；Phase B未实施，Windows支持未激活。

## 门表

| 门 | 当前结论 | 实际证据与限度 |
| --- | --- | --- |
| G0-W-D | 本机有限机制可行性通过 | 默认发现/用户已明确确认/显式目标重开；实际目标缺失/未知用户/root拒绝；完整私有payload字节传输、非root无网络namespace安装、缺制品/坏摘要/写点ENOSPC/部分stage失败保数据及发布后执行。默认变化仅输入阴性，没改系统默认；未假造WSL未装/需重启。 |
| G0-W-C | 本机有限机制可行性通过 | 真实terminal跨域绑定/启动取消/物理join/槽位；MCP/hooks/environment真实owner；原runtime异常退出反例与候选；实际bundled kernel原EOF/aclose/mainQuit；SIGHUP策略对照；独立distro终止及真实睡眠；原样PGDATA在新attempt由PG自己恢复并kernel ready。 |
| G0-W-P | 本机有限机制可行性通过 | 两锁/TS唯一owner、29个剩余物理case、实际ext4 clean-v0/BM25/snippet/vector、搬移/reset/非零readback；真实kernel先退再PGstop、睡眠前后相同PG与SQL；dirty恢复后同system_identifier/PGDATA inode/canonical记录及原HTTP读取。 |
| G0-W-F | 本机有限机制可行性通过 | Explorer、无session图形目录、路径/挂载/替换/unknown、用户真实选择与拖放、逐字节权限、原HTML/image/PDF与下载；追加大小写/Unicode独立identity和真实另存为取消/拒绝覆盖/确认覆盖。 |
| G0-W-N | 本机有限机制可行性通过 | 完整cert+精确origin、新Session、TLS旧服务/rebind负控、原Host/Origin/隔离、实际流退休、自然有效期、Hiddify TUN下睡眠恢复、真实Notion OAuth+SDK握手/list_tools。另实测取消后陌生服务重绑OAuth旧端口，假成功页不改变原owner授权状态。 |

本表验收隔离研究接缝与真实既有owner的可行性，不把fixture当完整产品。HTTP session依赖有预置SimpleNamespace；main callback由AST取自生产，部分dialog/app.exit为观测stub，补充使用了真实可见BrowserWindow.close。正式setup/持久选择、Windows主壳整体装配、NSIS、tray/IME/DPI/快捷键、更新卸载及真实provider长程仍在Phase B/激活门。原生Ubuntu x64/arm64及GUI按用户要求延期，不作本轮G0前置，不记通过。

## 本轮新增索引

- [PG剩余失联](../../backend/pg-boundaries-round5/report.zh.md)：29物理case，有限JSONL+EOF准入及typed结果；保留partial delete UNKNOWN，不造恢复日志或重试器。
- [MCP](../../terminal/mcp-round5/report.zh.md)、[hooks/environment](../../terminal/hooks-environment-round5/report.zh.md)：真实跨域scope集成；MCP call cancel与session close保持原边界；55/21项原测试。
- [kernel控制](../../terminal/kernel-control-round6/report.zh.md)及[补充distro/SIGHUP/窗口/睡眠](../../terminal/kernel-control-round6/distro-signal-gui.zh.md)：主22次/18完成观测/4原探针失败；补8次/7窄合格/1原窗口fixture失败，失败不改写。
- [接入/离线payload](../../desktop/discovery-payload-round6/report.zh.md)：完整资源真实无网络安装、有限失败与新位置执行；1.6GB制品和副本已清，不入Git。
- [图形文件/真实HTTP owners](../g0-w-round5/report.zh.md)：中文目录、选择/拖放、HTML/image/可见PDF、实际保存及原失败；[本轮附加审核](review.zh.md)与directory-names.json、save-dialog-verification.json补路径identity和原生保存窗口。
- [网络与睡眠](../../desktop/network-round7/report.zh.md)：自然cert跨validTo、在途SSE/上传/下载/pending retirement、真实powerMonitor suspend/resume。旧连接一次断开并只读重连，未重启guest或重放工具。
- [OAuth原owner与真实Notion](../../backend/oauth-round6/report.zh.md)及[callback rebind补证](../../backend/oauth-round6/rebind/report.zh.md)：原SDK/management、Windows系统浏览器及8项mock、真实授权、独立rebind单项；精确秘密处理、本地清理与远端授权保留分别记录。初次N终审曾遗漏rebind这个明确must-have，root复核后撤回整门结论，补验后才恢复，不隐去这次审阅修正。
- [dirty PG恢复](../../terminal/distro-recovery-round7/report.zh.md)：唯一新可丢弃发行版，canonical POST201→仅该distro终止→不改PGDATA/pidfile/socket→新attempt原wait-owner/TS start/initialize=False→PG真实redo/ready、同原记录HTTP200→正常Quit；不是手动删pidfile或重建数据库。
- [D/F/N最终交叉审阅](../../desktop/network-round7/final-gates-review.zh.md)、[P/OAuth终审](../../backend/oauth-round6/final-gates-review.zh.md)；C原[具体缺项审阅](../../terminal/kernel-control-round6/c-gate-final-review.zh.md)与上条实际补验分别保留。三位复用agent分别完成实际owner、PG/OAuth、环境/网络审阅，root复核must-have后作本机G0结论。

## 睡眠与真实授权事实

用户现场睡眠自报1–2分钟，独立powerMonitor事件间隔88.493秒。真实kernel1062/start42308、PG1100/start42462、host HANDLE creation与namespace4026532222前后相同；双锁仍busy且inode相同、postmaster启动时间/数据库名SQL不变，随后原main正常Quit、PGstop及精确清理。网络另一个HTTPguest保持678/start28429及原cert；SSE opens1→2/errors0→1如实记录断线重连，恢复后的新fetch成功。

真实Notion由用户亲自授权。原Linux owner先装listener，Windows系统浏览器由受限Electron callback打开，原SDK完成token200、协商MCP2026-07-28及list_tools65（无next_cursor）。没有tools/call、Notion页面读写、resources/prompts枚举或真实模型provider调用。code/token/clientsecret不进host通道、报告或Git；本地grant/home/listener已清理。Notion服务端授权与DCR注册未主动revoke，不能把本地删除写成远端撤销。

## 生产实施必须保留的结论

当前生产仍有已经实测的缺口：Linux单域join不足以收Windows后代；PG catch不能混淆not-submitted/unknown/已结算非零；Runtime child清空后不能跳过本次exit检查；宿主强退后真实SIGHUP需由Linux启动接缝处理，让原EOF/aclose拥有关闭；目录已写后失联必须unknown，接纳要重核原fd身份；TLS退休须结算原请求/下载/WebContents而不只清变量或连接缓存；PDF只允许锁定内置扩展本地资源，后端保持精确origin/full cert。所有修正仍为output候选及规格，未实施Phase B。

## 清理、证据和后续边界

各轮独立进程、PG、Linuxhome、资源副本及临时故障发行版均按精确身份/范围清理；未向日常Ubuntu执行terminate/shutdown/unregister，默认注册不变。R7最后复用清理脚本要求共享Ubuntu仍Running的断言实际失败，fresh list为Stopped；原因没有独立证据，不归因为空闲自动停，也不为求绿启动它。原失败保留，凭注册/default/明确管理命令范围核清理边界。之后为授权的Linux仓库同步正常显式启动Ubuntu，不能倒填该观察。实验Electron profile缓存有工具自动审查拒删，具体原因为blocked by policy；停止尝试，留在ignored output且不归档。root另存为闲置profile同样仅为未提交缓存。本次不会为了清空output绕过审批。

不提交资源、VHD、PGDATA、raw settings或秘密，不生成代码/证据hash。归档332个选定文件（约8.3MB），89个Python只做行为不变格式整理并核AST/Ruff；其他内容在归档复制时逐字节核对，JSON可解析，39个JS/CJS语法通过。OAuth实际秘密先在Linux按值scrub，再通过host结构/authquery扫描。Git正常文本行尾转换与协议原字节证据分别处理，协议需要处已有base64/逐字节事实。

暂存后git diff --check指出原样保留的native stderr、UI树、stdout末空行与三份已执行CJS快照空白行的格式警告；没有把它记成全绿，也不为消警改写原日志或已执行快照。没有生产src/desktop/frontend/tests改动。最终本地commit和Linux ff同步由收尾结果给出，不推送、不发布Windows支持，不把原生Ubuntu延期或未实施B隐去。
