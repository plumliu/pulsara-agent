# alpha8 升级前后只读数据资格

2026-10-25，用户授权本机alpha8更新；root负责正常Quit/维护准入/NSIS/启动，本分工仅只读保存配置与已验证本地PG，不控制UI、启停或reset数据库，不调用provider。

`observe_upgrade.py`复用原alpha7升级观察逻辑并置于独立目录，before要求正式alpha7 Python/package，after要求正式alpha8；文件存在即拒绝覆盖。本次基线必须在Notion临时连接清理后重新读取，不使用旧alpha6 settings stat。`prepare_observer.py`完成机械复制及AST校验；首次试用Windows内联Python复制命令因引号SyntaxError未执行，改为此小文件后成功，未改变产品源码或数据。

收到root通知采基线后，实际运行：

```text
wsl.exe -d Ubuntu -u plumwsl --cd /home/plumwsl --exec /usr/bin/env PULSARA_HOME=/home/plumwsl/.pulsara /home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/python/bin/python3 /mnt/c/python_workspace/pulsara-agent/output/windows-g0/pg_admission_r3/alpha8-upgrade/observe_upgrade.py before
```

exit0，正式version0.1.0a7，原deep verifier status=verified。systemid7700523439330484737/data inode171484/socket仍原目标、无TCP；postmaster start=17:57:33.694648+08。settings当前inode145427/size440/mtime_ns1792923525820928476/uid1000/mode0600，记录非秘密唯一deepseek-flash/chat_completions/catalog_standard连接字段与credential-present。原session、三个COMPLETED ROOT的initial/final IDs/model binding及28条entry counts完整保存。未导出API key/settings正文/OAuth秘密，也未创建配置hash。

已通知root基线完成、可正常Quit。after待root确认正式alpha8安装启动后才执行，逐项比较该before；此时不宣称升级已通过。旧alpha7升级/卸载重装/Notion观察文件保持原样。

## 随后实际 Quit 失败，安装暂停

root原Ctrl+Q后，原alpha7 cover报告 `StaleHostWriter: host writer generation is stale`、退出尚未完成、数据库尚未停止；root暂停安装。没有把此失败覆盖成正常Quit。root独立证据为 `output/phase-b/root/alpha8-preupgrade-quit.json` 和 `alpha8-preupgrade-stale-host-ui.txt`。

本分工18:48:43+08实际只读观测 `quit-failure-observation.json`：原PG PID498、systemid/data/socket/inode及postmaster start均不变；唯一正式session writer_generation=4、owner=`host:7qax3apinsxdmcnce6tnx5hlbq`，lease_expires_at=18:21:57.736407+08、updated_at=18:21:27.744315+08，已过期约27分钟。entry sequence仍28、event sequence70。当时pg_stat_activity除本次admin只读观察外无其他client backend。before文件实际创建/写入10:47:22Z，即18:47:22+08，明显晚于该lease过期时点；该时间比较不能单独证明整个历史从无其他owner，但不支持本次before调用刚使lease过期的猜测。

`audit_installed_verifier.py`直接从**正式安装alpha7包**保存 `installed-verifier-call-sites.txt`，该源码审计自身不连PG。before的 `PostgresRuntimeConnectionFactory.verify_deep → PostgresSchemaVerifier._verify` 只读取identity、ledger、vector/search capability、catalog与权限；`_verify_runtime_grants`中的INSERT/UPDATE/DELETE只是传给`has_table_privilege`的权限名，不是数据写SQL。此调用链不构造KernelHostCore、不acquire/renew host writer，不触发writer generation接管。普通连接设置search_path/statement_timeout不修改canonical host行。

实际 `_require_writer` 将generation/owner不等或lease过期都报同一个 `host writer generation is stale`，因此该错误文本本身不证明其他owner抢占。当前证据支持过期lease进入close路径这一窄方向，完整根因和修复由terminal分工继续审Host续约/SessionEnd/aclose；本分工保持只读，未改Host生产文件、未写SQL、未reset或stop正式PG。after仍未运行，等待故障结算与root新指令。

## 原 GUI 显式 Retry 恢复，独立 recovery 阶段

root已沿原GUI Retry回到正式alpha7工作台HTTPS42805，停在总览、未打开原session或调用模型。静态路径是 `boot → runtime.start → waitForPreviousOwner → BundledPostgres.start`，旧kernel实际退出/锁释放后，PG status0且精确data/socket匹配即reuse；initialized=false，因此新start帧不重新初始化。它不把旧failed close改成clean，也不以kernel锁空闲泛证明所有外部scope均已join。

`observe_upgrade.py recovery`单独保存 `alpha7-retry-recovery-verified.json`，exit0：原PID498/systemid/data/socket/noTCP/inode及postmaster start完全相同，new_postmaster_start=false；settings stat、模型投影、session/三turn/28条entry counts相对本轮before全部preserved=true，原deep verified。新kernel PID53665、UID1000/start ticks340742、正式alpha7 Python、精确生产runpy/SIGHUP wrapper与PULSARA_HOME匹配，排除wait-owner进程。该/proc读取是此次观察，不声称原子PID身份绑定或可观察全权限进程。

初次recovery探针只识别裸`-m pulsara_agent.desktop_runtime`，漏掉正式host-wsl使用的固定runpy wrapper；其PG/数据检查全部通过但kernel_processes为空、最后断言失败，原 `alpha7-retry-recovery.json`保留。按真实生产argv修正探针后才得到独立verified结果，没有弱化唯一kernel断言或重开PG。after文件仍不存在：alpha8尚未安装，root计划待Host修复后改用alpha9，不把这次Retry写成alpha8升级通过。
