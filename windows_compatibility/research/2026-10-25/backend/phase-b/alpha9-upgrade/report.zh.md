# alpha9 升级后的保数据只读核验准备

alpha8仅构建、**未安装**。升级前原alpha7 Ctrl+Q曾发生StaleHostWriter，现场与原before均保留在相邻 `alpha8-upgrade/`；root随后原GUI显式Retry恢复，独立recovery严格核对同PG启动时间/PID498、systemid/inode/settings/model/session三turn/28条记录/deep均保留。之后root于18:54正常Quit、PGstatus3，18:56维护检查0。这些是已发生记录，不伪造为本目录新before测量。

`observe_after9.py`明确读取相邻 `alpha8-upgrade/alpha8-upgrade-before.json` 与 `alpha7-retry-recovery-verified.json`，after要求正式alpha9 Python/package路径与version0.1.0a9，逐项比较原systemid/data/socket/noTCP/inode、Notion清理后settings stat、非秘密model字段与credential-present、原session/model binding、三turn/28条entry counts，并要求新postmaster start及原deep verified。结果独立命名 `alpha9-upgrade-after9.json`，已有文件即拒绝覆盖。

准备阶段仅完成AST语法校验，未连接或启动正式PG；严格等待root明确alpha9安装启动ready后才执行。整个观察未输出任何实际API key、OAuth token/code/clientsecret或保存设置正文，未构造第二Host/OAuth/PG owner。

收到root正式alpha9 NSIS exit0、HTTPS32915工作台ready通知后，已用 `/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.9/python/bin/python3` 执行本脚本，exit0。`alpha9-upgrade-after9.json`记录实际version0.1.0a9与原deep verified；相对已发生的alpha7 before，preserved全部true（settings stat、model投影、session/model binding、三turn/28条entries、data inode），same_pg_identity四项全部true。systemid仍7700523439330484737、data inode171484；新postmaster start=19:23:28.015074+08，未reset或初始化新cluster。

此次只读/proc观察到新正式kernel PID494/UID1000/start ticks1448，精确生产runpy wrapper与正式alpha9 Python及PULSARA_HOME匹配；PG PID523/UID1000/start ticks2029。用于root后续独立sleep observer的当时身份定位，不能据可复用PID单独授权signal或声称原子身份绑定。未打开原session、未调用模型、未写保存配置或启停app/PG。

## 睡眠恢复后第二条 GUI 消息

root获用户重新授权CU后确认草稿原样保留，并点击发送独立睡眠会话 `session:36omh67maz3ezaef3amghzb6ka` 的第二条消息。本分工只读原canonical行，不控制UI、不另调provider。`after-wake-turn-verified.json` 为独立19:39:54+08观察，未覆盖原after9升级基线。

- 该会话writer_generation=2；恰两个COMPLETED ROOT，四条entry（USER_MESSAGE2、ASSISTANT_MESSAGE2），无ASSISTANT_TOOL_REQUEST/TOOL_RESULT，assistant blocks均TEXT、tool_name为空。
- 第一条实际用户文本：`这是 alpha.9 睡眠自动恢复验收的独立测试会话。请只回复“睡眠前连接正常”，不要调用任何工具，不要读写文件。`；模型精确回复：`睡眠前连接正常`。
- 第二条实际用户文本：`这是睡醒后的第二条验收消息。请只回复“自动恢复后模型调用正常”，不要调用工具，不要读写文件。`；模型精确回复：`自动恢复后模型调用正常`。
- 原GUI入场为两个QUEUE_PROMPT命令；恰两个不同client_submission_id的NEW_TURN/HUMAN_MESSAGE队列项，均CONSUMED，其consumed_entry_id集合精确等于两turn的initial_entry_id集合。无重复队列提交或额外turn/tool调用。
- 原 `session:r3d6okwuq2trl3ribxzct6rbuy` 的session/model binding、三turn及28条entry counts相对after9逐项相同，PG systemid与19:23:28.015074+08启动时间不变。

首次观察已得到全部上述主要事实，但探针误预期session_commands类型为SUBMIT_PROMPT，最后断言失败；`after-wake-turn.json`原样保留。核实际GUI队列owner后，探针改为精确QUEUE_PROMPT并**新增**两个CONSUMED与initial entry匹配检查，复读得到verified结果exit0，不弱化重复/工具断言、不重新发送任何消息。写报告前以原LocalSettingsStore实际credential scrub set核整个输出未含当前保存的实际凭据值；没有泛化删减用户/模型原文。查询连接已结算并通知root可正常Quit，后续睡眠时长/同kernel/物理恢复及Quit结论由root/tasks的独立记录承担。
