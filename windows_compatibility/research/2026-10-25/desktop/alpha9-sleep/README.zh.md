# alpha9 独立睡眠观察

最终已完成：所选闲置睡眠→自动恢复→真实新turn→正常Quit矩阵通过，当前app0/PG已正常停，maintenance0。详见report.zh.md与closure-results.json。以下准备与分阶段记录按发生顺序保留，不能把当时pending误读为当前状态。

已在root明确BEFORE后采得19:29:52+08正式beforeReady=true，原始before.json/run日志保留；**after未采、观察脚本未触发睡眠**。等待root安装alpha9并给新专用验收session、HTTPS端口、main/kernel PID；原3turn会话不是本次target。用户仅手动睡眠一次约60秒，root在唤醒后执行实际短文本新turn与正常Quit。本脚本不发送消息/工具，不调用provider，不启动/关闭app/kernel/PG，不改TUN/interop/电源，不读settings/token/authURL。

入口（root提供实际参数后执行；before/after文件已存在则拒绝覆盖）：

```powershell
& .venv/Scripts/python.exe -X utf8 output/phase-b/desktop/alpha9-sleep/observe.py before --port <端口> --session-id '<新sessionId>' --main-pid <WindowsPID> --kernel-pid <LinuxPID> --pg-pid <PGPID>
# root确认beforeReady后用户手动睡眠；等用户报告唤醒再执行。
& .venv/Scripts/python.exe -X utf8 output/phase-b/desktop/alpha9-sleep/observe.py after --port <同端口> --session-id '<同sessionId>' --main-pid <同WindowsPID> --kernel-pid <同LinuxPID> --pg-pid <同PGPID>
```

使用真实已装alpha9 Python，并核真实kernel executable同alpha9；精确PID+start/namespace、Windows HANDLE creation、原PG systemid/postmasterstart/Unixsocket/noTCP、同锁inode和busy、同control/listener/fullcert与Windows CA校验、TUN状态及System电源事件。公证书只在监听fd已核属于该kernel后读取；after沿用before证书/origin，不换pin、不重试健康读取。共用旧alpha7目录的两段只读native/HTTPS工具，**不调用旧before/after入口、不覆盖其证据**。

新增只读PG观察：目标session的writer_generation、writer_lease_owner_id、writer_lease_expires_at、数据库clock_timestamp与当时lease是否有效。允许旧Host已物理关闭后既有cold-open改变generation/owner，同时必须kernel不换、PG不换、原已完成turn仍在。相同generation和更高generation都按真实观察分别记录；不从用户说“睡了60秒”推定writer必失效，不把更高generation元数据本身冒充旧scope已join。

本脚本的比较不代替产品资格：after HTTP健康与lease有效仍不足以证明新turn/正常Quit成功。root须另保真实后续输入、回复、没有重发副作用及Quit→kerneljoin→PGstop/mainexit证据；物理close失败仍保原failure屏障。前后TCP端口可能改变，只作快照，不能宣称连接全程不断。单次TLS读取10秒期限是诊断I/O期限，未设睡眠或产品总寿命cap。若after失败保留原文件与stderr，不重启实例来制造同PID通过。

当前root给定的新专用目标已记target-prepared.json，尚未采样：session:36omh67maz3ezaef3amghzb6ka，HTTPS32915，Windows main18816，kernel494/start1448，PG523/start2029。等明确BEFORE后执行：

```powershell
& .venv/Scripts/python.exe -X utf8 output/phase-b/desktop/alpha9-sleep/observe.py before --port 32915 --session-id 'session:36omh67maz3ezaef3amghzb6ka' --main-pid 18816 --kernel-pid 494 --kernel-start 1448 --pg-pid 523 --pg-start 2029
```

唤醒后同一组参数改after；若root报告目标改变，先核明原因，不能拿新PID覆盖旧before来制造连续性。脚本仅新增精确starttime输入校验，没有提前读取系统/会话。

最新阶段：before确认新专用session仅1个COMPLETED turn，writer gen1/lease有效，原main/kernel/PG/HTTPS/fullcert/锁与tun0 Up已核；System bookmark1521。等待root协调用户手动睡眠及已唤醒通知，不自行continue。

AFTER阶段更新：用户手动睡眠后16项只读比较通过，同kernel/PG/fullcert，writer gen1→2/owner改变且lease有效；实际System506→507为108.4911011秒，TCP6→2全部换端口、TUNifIndex47→34但Up。完整产品新turn/Quit仍待root，见report.zh.md；不覆写before/after。

最终收口：root已实际发送保留草稿并获精确回复，PG独立确认2ROOT/4entry/2distinct consumed prompts/tool0，随后正常确认Quit→app0/kernel494不存在/PGstatus3，maintenance0。所选闲置睡眠闭环通过，见report.zh.md末节与closure-results.json；原19:40:50快照属于待退出确认页，未作failure。未重启产品，不扩active-task矩阵/B5。
