# Windows shared recheck：backend结果

2026-10-25。本轮针对0ea98e63后的最小共享修复重验，结果不借用旧alpha9/Mac资格，不表示完整Windows激活或剩余B矩阵全部完成。当前正式候选alpha10；desktop负责版本/资源/NSIS，root负责GUI/Quit，backend没有修改生产代码、依赖或保存配置。

## R1：当前源码与真实私有PG

`r1-first/pytest.log`和JUnit：**27 passed、0 skipped、0 failures/errors**，2.89秒，原三目标与完整断言保留。包含head-only正例、binding/payload/permission/context/replay/closure/late-outcome等严格负例、四处退休失败保持原异常/各清理一次、原prefix测试，以及实际PG的post-accept child推进窗口：单accept、同ROOT、原SYSTEM/tools与messages suffix。使用当前WSL `.venv`/ext4 clone，Git0ea98e63加授权alpha10版本构建差异；另只读核到venv实际包和provider_dispatch均来自该clone的src，而非已装旧包。

独立cluster `/home/plumwsl/.wsr-ax19kear`：systemid7700606352142954921、PG17.9、精确data/socket、无TCP、0700/ext4、原admin/runtime角色。fixture仅创建/drop此cluster中的UUID DB，CI=true；私有cwd避免原fixture读取仓库.env。收尾复核身份、剩余测试DB0，原pg_ctl正常stop/status3后私有home已删除。没有使用正式PG或读取provider凭据。R1不是PG finite-helper死亡矩阵。

## 升级保数据 before/after

root正常启动旧installedalpha9后采本轮新`upgrade-before.json`，随后root正常Quit/PGstatus3/maintenance0。alpha10经NSIS安装并正式GUIready后，使用其installed Python **-I**采`upgrade-after.json`，exit0：

- version0.1.0a10、site-packages路径正确；三共享修复文件完整bytes等当前ext4源码，不建per-file SHA登记。
- systemid7700523439330484737、data/socket/noTCP/data inode保留，postmaster start更新。
- settings存在/stat、完整非秘密模型连接/凭据存在性、原2sessions/5turns/32entries及assistant blocks、workspace元数据与四文件实际bytes全部逐项相同；deep verified。

before/after采完才通知root创建新的GUI见证会话，避免其合法新增数据污染升级比较。没有reset、手工迁移、写SQL、dump settings/key或启停用户PG。

## R5：新安装包的真实provider context

`run_r5.py`外层仅用现项目环境编排本次新私有PG；真正provider子进程固定新installedalpha10 Python `-I`，先核包路径/三共享源码完整bytes，再只追加tests/helpers。复用原Windows Round10 scenario、原recording adapter、原canonical accept gate和原全部断言，不调用旧CLI的saved TCP建库路径。saved经原LocalSettingsStore/require_pulsara_home只读读取，再以内存override注入私有UUID DB；实际重新核连接`connection:oeeiipbn63ytbexaxw74nv5tgy`/deepseek-flash，不使用key环境fallback。

首轮 `r5-first/result.json` exit0、context completed/passed，**8次真实provider调用**。两个steer均PENDING/PROMPT_QUEUED，worker COMPLETED/EXPLICIT，唯一mailbox entry=1；原ROOT和MIDFLIGHT marker断言通过。完整实际provider输入/响应/工具结果保存在`context/result.json`。独立`review_results.py`按两epoch对8份raw wire记录再次核6次已有前缀：SYSTEM在完整messages前缀内、tools完整序列化值精确相同，旧messages按suffix追加。child第2次输入有两个ROOT marker，第6次输入另含MIDFLIGHT marker。

`r5-provenance.json`记录真实configure_interop：正式Windows安装目录的helper（不是开发helper）、Ubuntu/plumwsl、UID1000、新installed Python；所有Runner实际短私有home `/home/plumwsl/.pa-isd0ie9q`、Linux/ext4、配置已安装。wrapper只观察原constructor/configuration，不创建第二运行owner或改provider状态机。

保留实际一次 **pre-accept** PreparedRootInputHeadStale，expected head12→actual14，原确认/reprepare恢复成功。随机context没有独立记录新post-accept退休分支是否命中；不以此冒充该分支证据，R1确定性PG反例承担这个证明。

私有provider cluster systemid7700609098189023198、`/home/plumwsl/.wsr-provider-5w4_48eh`、PG17.9/noTCP，UUID DB已drop且余0；原core关闭/child exit0，正常PGstop/status3后精确删除root；原adapter已移除scratch与`.pa-*`。独立只读扫描可访问范围无本次exact路径进程，425/427的同UID环境不可读，未冒称全权限进程为空。原正式DB/settings未由harness修改；root此时独立GUI新增见证不算harness副作用。

## 新GUI见证与诚实限定

root创建并保留 `/home/plumwsl/workspaces/Windows 共享修复 alpha10 验收`、精确`session:uctdxsxom55pycdl5l3dvg5owu`。只读`gui-completed.json`确认1个COMPLETED ROOT、唯一QUEUE_PROMPT/CONSUMED精确匹配initial entry；一次terminal在该工作目录输出`WINDOWS_ALPHA10_GUI_OK\nEXIT_CODE=0\n`、exit_code0。原两个session/turn/entry全文投影仍相同。

模型**额外先调用一次find_files查AGENTS.md**，返回空列表；terminal命令除printf外带echo退出码。首次新observer误把“简单terminal往返”解释成“总tool必须1”，末尾断言失败，原事实文件保留。`review_gui.py`离线严格核实际两个tool、唯一期望terminal/queue/输出/退出码及旧投影，得到`gui-verified.json`；没有弱化原context断言、重发模型消息或改生产。这是成功的简单终端往返，**不是**literal printf-only/恰一个总tool的资格。root后续草稿/隐藏/CtrlK/Quit/冷重开另见其实际记录，当前不在此提前宣布。

## 交叉审查与后续

已只读核R2的71/0skip、R3的10case/12真实scope物理join/relay0/helperdiag0/workerdead/目录清理、desktop R0/R4与Windows task最小cmd路径修复，无新增阻断；不是本人重跑或全平台声明。见`cross-review.zh.md`，其final teardown cancelled=true限定保留。正式PG helper深故障已有证据/仍缺最小计划见`pg-fault-coverage.zh.md`；本轮没有开这第二批实验。

`review.json`的24份当时结果精确credential scan=0；最终归档前另做全本目录实际保存credential扫描。原始调用正文/失败/额外工具如实保留，不泛化redact。所有本分工运行已结算，正式GUI新会话/工作区保留作后续见证，不擅自删除。R5的GUI退出/重开尚由root进行，不能把backend部分通过写成Windows全部B或支持激活。
