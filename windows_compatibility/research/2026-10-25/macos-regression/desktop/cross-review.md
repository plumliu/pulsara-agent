# Mac原生共享修复交叉审阅

2026-10-25，gpt-6.1-sol/high。**结论：terminal EPERM两leaf及backend post-admission head-only修复，无剩余源码/已执行定向证据阻断，可进入最终fresh wheel/provider/应用包复验。最终激活仍须这些新候选实证通过，不能沿用改前包或旧provider成功。** 本审阅只读，未改他人文件；root刷新wheel期间未运行Python、构建或重复前端suite。

## 1. EPERM与物理进程组结算

通读 `hooks/executor.py` 的 `_process_group_live`、`_signal_process_group`、原 `_abort_process_group/_wait_process_scope`，以及 `conversation_kernel/mcp/sdk_facade.py` stderr失败和 `_terminate_stdio_group`；交叉核 `terminal_process/manager.py::_process_group_exists`、`terminal_process/environment.py`的原PermissionError合同。

- EPERM仍表示原进程组可能存在。读取存在性时返回live，TERM/KILL拒绝只保留原join流程；真正结束仍要求原process.wait和后续ESRCH。没有把leader退出或权限拒绝当“组空”，没有提前归还slot/关闭worker或静默丢弃原输出。
- MCP stderr reader失败仍调用原offer_failure，信号PermissionError不会替换这个原错误；transport close继续原物理回收/重复取消owner。原2秒TERM→KILL是既有有限动作边界，未增加总任务/worker寿命cap。
- Windows跨域scope没有被这两个POSIX谓词替代；未新建process registry、快照authority或恢复路径。无法signal的真实group若始终不消失仍会留在join，此为保留未知物理事实，不是假报成功。
- 新 `tests/test_posix_process_group_settlement.py`三项使用真实新session子进程、刻意尚未wait/reap的Z状态、真实killpg及真实HookCommandExecutor。Darwin特定EPERM→手工wait/reap→ESRCH是直接正/负对照；MCP close在reap前必须pending；natural fork-tail要求准确stdout、root exit0后仍等待tail、全worker完成、permit恢复。测试中ps/5秒hang detector不进入生产。

已核原始 `terminal/zombie-before.log`旧源码2 failed/1 passed；`zombie-after-focused.log`68 passed；`real-scope-after.json`包括root先退、TERM忽略tail、两次cancel仍同close task join、准确自然输出、Hook取消、group_absent/worker/permit结算；`hook-focused-after.json`30次自然tail样本与terminal最终报告一致。未将早先results或未修wheel首轮provider当作最终实证。

## 2. 已accept ROOT的head-only退休

通读 `ProviderDispatchCoordinator.activate_prospective_root_input`、`_root_dispatch_read_has_only_later_head`、runner `run_accepted_turn`的prospective分支，以及identity/snapshot/compile/replay/完整dispatch DTO的constructor验证和continuity.abort_planning。

- 原prospective已被accept，但还没有安装provider输入。新分支先独立验证实际model binding等于候选绑定，head必须严格前进且prepared为ROOT。
- 归一化只在本地未安装比较值中把through-sequence及其已有派生fingerprint归回prepared。每个dataclasses.replace都触发现有constructor验证；完整DTO相等覆盖其他实际字段，而不是选几项相等或新建fingerprint authority。actual scope/identity、payload、closures、late outcomes、context/permission、workflow/freshness/outcome或replay变化均不能借head前进通过。
- 返回None前由同一局部owner退役handle、dry borrow、hook reservation及planning。清理项调用前撤本地ownership，finally继续尝试后续项，外层except不会重试已执行项；测试覆盖四种单独清理失败，保持同一个原异常对象、各项各调用一次。没有将清理失败改报正常恢复。
- runner在调用前已清pending prospective槽，None只continue现有accepted turn循环，model_call_count尚未增加；不会回ROOT accept、生成新command/turn或重发消息。下一步由原ordinary prepare重新读取/测量，未安装旧wire不复用；installed epoch/SYSTEM/tools/messages由现continuity owner继续保持，没有新增rebase边界。
- 新真实PG反例在原ROOT accept返回后立即调用原accept_subagent_turn，确定性推进全局head。断言目标ROOT只accept一次、同turn、两次prospective分别一次正常/一次退休、safe-point active handle清空、资源均退休，并严格保持原SYSTEM/tools/messages前缀。它没有用sleep碰概率窗口或修改repository准入authority。
- 新15项覆盖head-only正例、相等head+内容变化、倒退head、binding/identity/payload/closure/late-outcome/permission/context/replay漂移，以及四资源单独退役失败。测试DTO用原constructor重新冻结，不把无效对象伪装成实际可发生差异。

已核backend的post-admission原失败、23项candidate通过和15项负例原log；最终11模块 `pytest-final.log`为 **244 passed、21条现有aiohttp shutdown_timeout deprecation、0skip**，`pytest-final-exit.json`为exit0。旧真实capacity成功与两次真实context `CANONICAL_PREFIX_CONFLICT`如实分开；本次审阅不读取/复述凭据，也不把源码测试冒充新wheel context成功。

## 3. fixture与规范

- `test_round5_long_horizon_execution_envelope.py`仅给object.__new__裸Core fixture补 `_history_maintenance=None`，与真实host.py:7889现constructor相同；不改shutdown、GC结算断言或产品错误处理。原卡住日志/诊断由backend保留。
- spec §24新增两条准确限定ROOT退休与EPERM物理join边界；均未授权新schema、事件、receipt、registry、重试cap或prefix重建。历史Windows/WSL通过记录仍不代替本次Mac资格。
- 我方前端83文件1372通过、types/lint、startup visibility fixture三次通过仍有效；这些没有被后端审阅当作真实硬件IME、最终包装或OS sleep/wake证据。root已明确实际CmdN+原Mac原生目录dialog图形创建选择的改前a57包事实；后端修复后的final包须另外保数据重开验证。

## 4. 最终收口仍要确认

1. root统一fresh wheel确实含三处生产变更，bundled-Python实际模块来源/版本成立。
2. backend真实context/mailbox重跑通过，并复核SYSTEM/tools字节与messages suffix；terminal真实provider路径使用同最终wheel复跑，失败/清理事实完整保留。
3. root最终startup smoke、browser owner、新包GUI保私有home/数据重开/Quit和原用户CLI恢复证据通过。
4. 未执行OS sleep/wake、无法完成的真实硬件IME/系统权限或未跑其他平台回归仍保持对应待验收项。

以上是新候选行为资格的明确剩余前提，非本审阅发现的源码阻断。满足后可对本次Mac实际覆盖范围作最终批准；不把本机成功扩大成Ubuntu/Windows新版本或对外发布资格。

### 最终候选补充复核

root最终startup、package provenance/smoke及backend/terminal同freshwheel provider和确定性PG补证现已逐份复核，上述第1/2项关闭，原源码结论不变。具体批准见 `final-review.md`：测试/源码及已完成Mac证据可冻结，root新签名包DesktopFolder系统授权与随后保数据ready/Quit仍待GUI收口，不提前填写通过。没有新构建或重复测试。
