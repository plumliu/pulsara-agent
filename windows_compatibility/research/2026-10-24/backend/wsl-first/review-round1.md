# WSL-only 387 行候选全文审阅

2026-10-24；backend；gpt-6.1-sol / high。

**结论：可冻结，无剩余阻断。** 审阅对象为根 `PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md` 当前 387 行 WSL-only 候选。本结论是条件实施规格批准，不是 Phase A/G0-W/WSL部署/Windows产品通过声明；root汇总其他专题反馈后仍须复核最终语义候选。

已通读全文，并交叉核对 terminal、desktop、backend 的 `wsl-first/proposal.md`。对提案中已被root明确取代的路径，以根候选为准：workspace唯一Linux-backed用户级picker；UNC只作文件/目录reference输入及OS handoff；Windows全局CLI首版不做；TS BundledPostgres唯一lifecycle owner。没有把这些已关闭的取舍误报成提案不一致阻断。本轮未改root/production/配置/数据库，未运行测试、G0或provider。

## 已闭合的关键合同

1. **范围和A真实收窄。** 原生Windows PG/ConPTY/Job/Win fd/Proactor、宽typed binding/ProcessScope及Unix IPC抽象已从全文前置移除；A只抽Node实际host/locator/PG物理操作，所有改签名caller同片注入，现Linux机制留原owner。§1/3/4/11/12/13没有旧G0或原native预算继续生效的矛盾。
2. **专用环境与home。** 固定Pulsara/Ubuntu24.04/nonroot，日常显式distro/user/cwd/exec，interop、WindowsPATH和automount分别控制；不改用户发行版或全局WSL配置。WindowsElectron data和Linux业务home/PG/workspace/资源四种位置分清；Linuxsavedsettings唯一，没有Windows凭据副本。部署权限不变成模型root入口。
3. **workspace闭包。** §6唯一Linux-backed用户级服务覆盖新建/分页/选择，无session可用；NEW resolver本来会resolve symlink这一事实未被掩盖。所有create-session入场前重验，canonical/availability/key仍原owner，recent不按同名自动重绑。实际filesystem/mount检查与nofollow各自作用明确，拒DrvFs/挂载替代不只看`/mnt/c`拼写。目录创建冲突、取消、会话失败及结果不明没有伪造rollback或重试。
4. **File来源与路径。** §7完整处理`pathForFile`非空的现有直接reference路径，Windows路径无法假装Linux；标准file input取得真实File后复用已有raw upload，现`chooseFiles`返回字符串不足以供byte upload的缺口有明确动作。Windows目录不隐式递归导入；同发行版UNC转换最后由Linux重验，字符串映射不产生authority。
5. **preview/export。** ext4原fd-held读、token/connection/revoke/read-at及取消join保持；HTML/markdown逐级nofollow、一次decode、CSP/iframe/null-Origin限制没有因桥而放宽。普通BrowserOwner继续封控制port与file协议；外部open/reveal有限类型与OS handoff边界保持。模型Linuxexport和Windows保存副本分别结算，覆盖/取消/磁盘满/partial/unknown由实际owner报告，不引任意Windows路径写IPC或同步关系。
6. **PG唯一owner和物理lock。** TS保initialized/startedHere与顺序，Linuxfinite helper只返当前操作事实，Pythonprepare_database独占role/db/schema。Node不以UNC stat、PID或旧path证明Linux权限/实例。helper复用现kernel flock，NB busy失败；先持锁ready再接唯一operation帧，Node在实际发送前重查close/cancel，因此不会预载一个尚未获锁的旧operation在父死后晚执行。操作后失联持锁直至真实child/readback结算；CLOEXEC不传常驻PG。旧wait-owner只是屏障、后续helper须再取锁，未虚构持久授权。
7. **握手的限制被诚实封闭。** ready-before-operation必要：仅NB锁不能排除旧helper延迟调度。此握手足以作为计划内正常pipe/取消交接设计；它不证明helper遭支持范围内异常终止后PG子命令一定消失。§3.3明确若该异常释放锁而pg_ctl仍存活则G0失败，不能凭瞬时status3或让PG永久继承锁fd蒙混过门。没有为了未证实故障预建registry/receipt/恢复工作流。
8. **正常关闭与失败不混。** §9明确stopped/锁释放/carrier.close不等于成功，当前stop在child清空后直接return的漏洞由本次attempt的process-local失败/unknown事实关闭；只有真实Linuxexit0、无收尾failed及完整pipe才可继续PG stop/reset。lostreply/startinflight/WSL外部终止皆保unknown/数据，重开先锁后真实PGfacts，不replay/清库/全局shutdown。
9. **网络身份。** §10 TLS方案是复用aiohttp SSLContext/Electron certificate API的窄G0候选；每次launch新Session+实际WebContents退役、exact scheme/host/port与完整cert同时检查，首个导航和后续fetch/SSE/preview/download全覆盖，没有nonce后回明文、忽略证书或全局proxy fallback。其可行性仍由真实G0决定，条件合同已封闭。
10. **安装/验证/成本。** 原发行版/数据保留、名字冲突不接管、更新停机、卸载不默认unregister与NSIS不强杀都在同一owner边界。A验证依据实际触及范围，不再强迫未改terminal全矩阵；B installed/WSL/provider/长程门仍完整。50–95分包正确合计，63–129含不确定性口径不与旧native/旧A及三专题估计重复相加。

## 两项非阻断精确化建议

### OAuth复用证据及额外故障场景

已只读检查当前虚拟环境实际SDK：`.venv/lib/python3.12/site-packages/mcp/client/auth/oauth2.py:390–431`生成128字符PKCE verifier、S256 challenge、随机state，callback后compare_digest state并校验RFC9207 `iss`；`:441–454`带verifier换token。生产 `conversation_kernel/mcp/oauth.py:149` 的子类只改resource context，不覆写grant；`:473–492`先校验state、拒已完成callback，`:515–533`先启动Linuxlistener再open_browser。无需为WSL复制OAuth协议或另造callback bridge。

建议§10.3与G0-W-N显式写PKCE/state/iss继续归SDK/原OAuth owner，新增Windows端口碰撞、callback rebind/取消、重复callback与错误state/iss/PKCE实际负测。现HTTP callback截获可能令授权失败/超时；不能据此误标authorized，也不宣称state本身隐藏code。此为现有门的具体化，现§10.3已经要求真实转发/state/取消，因此不是阻断。

### “短命证书”的时间语义

建议把§10.2“短命”明确为本次进程/网络物理生命周期：不持久复用，不因任意小时/天数TTL给worker/任务增加隐藏寿命cap；长程跨sleep/重连/新连接仍需有效的本次pin与完整TLS合同。现§2.2已经禁止新任务寿命cap，故本建议是避免实现误读，不是新增产品要求或阻断。

## 冻结边界

同意当前387行实施语义。后续若root只追加上述精确化或冻结元数据，我会复核具体delta；若G0证明TLS/WSLrelay/helper锁无法满足所选合同，必须修订机制与规格后重审，不能把本批准当作执行证据或fallback许可。
