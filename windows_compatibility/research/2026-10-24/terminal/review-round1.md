# 根规格交叉审阅 Round 1（terminal agent）

审阅对象：`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`，2026-10-24 首份348行草稿。已通读全文，包括非terminal的安全文件、数据库、MCP/hooks、打包、前端、成本、WSL与激活条件。只写本工作区，不改根规格。

结论：**当前有1项阻断（root已经识别并正在补），另有1项需要在冻结前明确的terminal合同措辞。** 修复下述R1、R2后，我接受“native设计冻结，G0尚未执行，未激活”的状态划分；G0尚无Windows证据本身不是设计文档冻结的阻断。

## R1 / P1：internal Protocol v3仍缺Windows transport决策

位置：第3节结构表、第7.3节（当前约196–198行）、第11节W4和第13节验收。

现有 `src/pulsara_agent/terminal_protocol/v3_gateway.py:193` 使用 `asyncio.start_unix_server`，`desktop_app/protocol_client.py:105` 使用 `open_unix_connection`；`DesktopApplication`启动时无条件创建该server。它是browser bridge到kernel的私有protobuf/auth/attachment控制链，不是CLI diagnostics，也不是Electron↔Python JSONL。只替换diagnostics named pipe和宿主管道，应用仍无法Windows ready。CPython的Windows asyncio不支持这些Unix接口。

具体修法：为这一条独立连接冻结Windows私有transport（推荐current-user ACL named pipe，复用维护库/CPython支持，不另造通用RPC）；保持现有protobuf帧、8MiB单frame边界、launch_id/capability只在Python边界、attachment/controller关闭的精确owner与回收，不让browser获得launch secret。若最终选private loopback TCP，需明确地址、auth与endpoint注入边界，不能悄悄复用公开HTTP server或硬编码端口。加入G0-R与W4，以及重连/取消/非授权client/Host关闭的原机验收。root已告知正在补此项。

## R2 / P1措辞待封闭：PowerShell exit合同须承认其为适配器政策

位置：第5.3节约118行。

草稿要求“最后原生命令失败保留其非零码”，同时要求正确处理最终cmdlet、pipeline、return、函数与过期LASTEXITCODE。这可以作为Pulsara主动选择的新Windows合同，但**不是**PowerShell原生 `pwsh -Command` 默认行为：官方写明最后external code非0/1会映射为1，保留原码需要显式exit；`-File`的默认行为又不同。因此不能把某个通用短bootstrap当作已经由PowerShell自动保证所有这些选择。

具体修法二选一并写清：A）采用标准PS7 `-Command`观察语义：最终成功0，最终失败1，显式exit N保留N；exact native code由用户显式exit获取。B）保留草稿的numeric-preserving政策，但明确它是Pulsara adapter的额外行为，G0必须用PowerShell维护的parser/runtime扩展证明能选择“最后实际结果”，不靠`$LASTEXITCODE`非空/非0推断，更不能自行复制PowerShell控制流解释器。B增加的依赖/API缺口、成本应被W2与G0-T覆盖；G0失败仍按现有停止规则，不改成混合短/长command语义。

两种都可以作为设计决策；我建议A，符合“返回真实shell退出码”的现有terminal owner边界，长期维护也小。根规格当前已有足够G0失败规则，但需要把标准shell行为与额外adapter政策的区别明确，避免实现者误认为只需加`exit $LASTEXITCODE`就完成。官方依据：[about_pwsh的Command退出码](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh?view=powershell-7.6)。

## R3 / P2：WSL备选缺Windows interoperability进程边界

位置：第12节“单一执行域/生命周期”。

WSL默认可以调用Windows `.exe`，且追加Windows PATH。Linux backend的killpg不能单凭LinuxPGID证明其interop启动的Windows进程物理退出。“Python/terminal/MCP均在Linux”并不自动得到“全部副作用都在Linux”。备选不是当前执行路线，因此不阻断native冻结，但会影响35–65人日备选与它的单域承诺。

具体修法：增加明确决策要求：专用distro可通过`[interop] enabled=false`禁Windows启动，并`appendWindowsPath=false`禁PATH混域（两者是不同开关）；采用用户既有distro时不能静默改其配置，须声明Linux-only受管范围并修订跨域调用产品合同，或额外提供受管Windows执行owner并重估。禁止把PATH里找不到exe当作执行权限封闭。依据：[WSL interop配置](https://learn.microsoft.com/windows/wsl/wsl-config)。

## R4 / P2：Job emptiness的具体判据补入5.2

位置：5.2第4–5条。

现在分开root exit/range empty/drain/join正确。建议明确 `QueryInformationJobObject(JobObjectBasicAccountingInformation).ActiveProcesses==0` 是当前空集查询；IOCP只作提示，消息不保证送达，普通Job handle wait也不是通用全部退出证明。原生handle释放与ConPTYhost/helper是否计入Job放G0。它不需要新durability或新的调度器。依据：[JobObjects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[ActiveProcesses](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)。

## R5 / P2：避免错误设置-NonInteractive

位置：5.3/5.4。

建议明确不在所有PIPE/TTY调用上固定加`-NonInteractive`。它会让Read-Host/确认提示成为statement-terminating error，不能从deepseek普通pwsh工具照搬。Pulsara的PIPE与PTY都允许长期输入，交互应由tty与确切stdin决定，不由常见CI启动flag意外删除。可在bootstrap G0场景中加Read-Host/Console.ReadLine及Pythonstdin，不必扩大为完整PowerShellHost UI仿真。官方[about_pwsh NonInteractive](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh?view=powershell-7.6)。

## R6 / P3：现状表“冒号PATH”措辞

`environment.py:191,524`已用`os.pathsep.join/split`，并非代码硬写冒号。真正POSIX专有的是默认PATH条目、shell候选、venv/bin、env keys和snapshot。建议现状表改为“POSIX默认PATH与venv布局；分隔符本身已使用os.pathsep”，避免重复改已可移植部分。

## 其余全文判断

- 选择native、受限首批OS/NTFS/x64矩阵、私有PS7.6 LTS、无silentfallback、G0失败停止这套决策清楚，未暗中授权WSL或另一产品。
- EOF没有假装ConPTY关闭是输入halfclose，也没有在未知时自行给unsupported outcome，当前阻断条件正确。
- 文件安全要求覆盖操作身份、reparse/祖先替换、atomicpublish、settings replacement、借用/删除锁，未以flag=0或onlyread退化来偷过；由backend专家核对API可行性。
- native PG/Search被列为独立G0，TCP新secret有独立产品理由，未新增execution durability；密码作为actualsecret处理与可检查DSN地址并不矛盾。
- hooks/MCP不把SDK使用当成放弃已有credential/frame/cancel保护，.cmd没有被当裸exe；跨域所有权方向正确。
- 私有临时command载荷、Job、锁与helper都只承载物理操作，没有新事件/receipt/hashauthority，符合AGENTS。
- 成本合计88–154、25–35%后110–208数学正确；W1+W2加W0分摊比我的terminal22–43工程日更保守，没有低估。WSL报价明确不与native相加。12–22周是依赖/人员假设，未作为承诺。
- 日常正常关闭不新增总超时，OS强制中断不假装settlement success；用户级页面不绑active session，权限/前缀/三平台回归保留。
- 当前文档明确没做实现/Windows运行/发布；这与冻结G0可检验规格兼容。修复R1/R2并补root所说参考实现结论后可进入最终再审。
