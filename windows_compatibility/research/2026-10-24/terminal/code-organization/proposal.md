# 终端与共享进程适配：代码组织建议

本轮只读复核：`terminal_process/{manager,environment,models,output,monitor}.py`、`hooks/executor.py`、`conversation_kernel/mcp/sdk_facade.py`、`conversation_kernel/visualization_screenshot.py`、`desktop_runtime.py`。不改生产/根规格，不重跑测试或G0。

## 建议分界

保留一份业务owner与状态机，按真正不同的OS机制拆文件。不要复制`ProcessRegistryWindows`、`TerminalManagerWindows`、Windows output/monitor/tool-runtime；不要把全部业务搬进一个“跨平台subprocess framework”。

建议文件名如下；root可与其他专题统一窄平台包名称，但职责和依赖方向应固定。

| 文件 | 应拥有的内容 | 不应拥有 |
|---|---|---|
| `terminal_process/manager.py`（保留） | ProcessRegistry、decision/reservation/scope授权、`_ProcessState`、共同reader/watcher、physical_completion、join/rollback顺序、终态/capacity/monitor发布 | pty.openpty、Win32 imports、killpg、Job API、SDK对象判定、PowerShell字符串拼装 |
| `terminal_process/physical.py`（新增，小公开入口/类型） | terminal需要的byte-input/byte-output、root exit、managed scope、close-input与OS资源close的窄接口；确定平台factory | command/process IDs、权限、输出游标、保留队列、状态机、不同OS失败后的fallback |
| `terminal_process/_physical_posix.py`（新增） | 当前openpty/Popen/dup/fd/killpg机制；PIPE/PTY真实EOF、OS资源关闭；尽量机械搬移当前代码 | 第二套reader/watcher/Timer/physical_completion或终态 |
| `terminal_process/_physical_windows.py`（新增） | ConPTY/PIPE维护库接合、byte bridge/Enter、native资源与可能的依赖worker；复用共享Job owner | 模型工具解析、foregrounddecision、终端日志cache、event或job产品 |
| `terminal_process/shell.py`（新增） | shared shell调用计划的类型、command/probe准备入口；argv + 本次临时输入资源所有权转移 | env缓存、敏感变量策略、终端processID |
| `terminal_process/_shell_posix.py` / `_shell_windows.py`（新增） | POSIX shell候选/`-c`/probe与PS7 private path/短bootstrap/UTF8命令载荷/probe语法 | 在physicalspawn后偷偷替换command或偷偷fallbackshell |
| `terminal_process/environment.py`（保留） | TerminalEnvironmentOwner、singleflight/TTL/attempt关闭、终端defaultdeny/passthrough policy、snapshot adoption与diagnostics、venv overlay组织 | WindowsDLL加载、直接selectors/Popen/killpg、给MCP/hooks决定业务envpolicy |
| `platform_runtime/process.py`（新增共享窄入口，包名可统一） | exact process scope + owned native资源类型、sync/async消费者对应的creation/control接缝，typed创建失败 | terminal状态/monitor、MCP framing、hook deadline、截图结果、secret来源策略 |
| `platform_runtime/_process_posix.py` / `_process_windows.py`（新增） | POSIX group控制、WindowsJob创建前纳管/精确root handles/空集查询/终止/close；供terminal和非terminal复用 | 创建另一个全局process registry、配置搜索、通用调度/重试/恢复 |
| `platform_runtime/_windows_job.py`（仅实际复用需要时新增） | 如果Windowsprocess与ConPTY绑定都需要同一Job handle细节，可提炼精确Job原语；G0先查依赖API | 先造抽象基类/插件registry/ABI框架 |

文件数量以实际重复机制为依据，`_windows_job.py`只有真实依赖接合需要时提取；不因所有owner都“用进程”就强制其读写通道、同步/异步API和结果DTO一致。

## ProcessRegistry与reader/watcher/join

- `_ProcessState.process: Popen`、`stdin`、`master_fd`替换为一个有确切OS资源所有权的physical handle；root-exit code与scope liveness接口分开。共享状态继续有唯一physical_state/termination_intent/final_status等值，不复制在后端。
- 一份terminal reader通过physical byte source读取，继续调用原`TerminalOutputOwner.append_raw`；一份watcher按root exit、输出reader结束、scope empty和资源回收边界冻结现有终态，finalize output，再发布原physical_completion与completion subscriber。顺序及失败语义按现行合同/G0证明；不把SDKexitcallback直接当completion。
- terminal reader/watcher与Timer仍由ProcessRegistry现有安装/rollback边界启动和join，当前`reader_started`等部分安装状态仍由同一owner管理。依赖内部自己的I/O worker则由physical backend显式close/join；不能一边说“SDK自己拥有”，一边在Registry漏掉等待它结束的条件。
- `_join_physical`继续是terminal层的组合结算：根退出、terminal reader、terminal watcher、可选Timer、输入退休、managed scope为空、依赖/native资源已关闭。共享OSscope的join/close不应自己置terminal physical_completion、改capacity或通知monitor。
- 未返回完整physical handle前的部分spawn由backend拥有cleanup；返回后由同一Registryrollback接管，不能出现OS进程已启动、`_ProcessState`未安装却无owner的缝隙。无法join的状态必须保留launch/资源占用及原清理责任，不能raise后当未占槽。

## 共享到非terminal的范围

实际重复点：hooks `_execute_command`用asyncio shell+creationflags，MCP `_BoundedStdioTransport.start`用asyncio exec+start_new_session，截图worker用asyncio exec+killpg；terminal probe也有自己的Popen/group终止。这些共享“用户代码之前已纳管的exact scope及其终止/空集/handleclose”机制，不共享业务的admission/result/state。

- hooks保留commandWindows选择、source trust、环境overlay、stdinJSON、output bounds、deadline/cancel与HookCommandExecution。
- MCP保留config/path/env/secret resolver、frame bounds、reader/writer/stderr任务、SDK会话和transportclose语义。
- screenshot保留render deadline、communicate输入/PNG结果、error和worker set；只替换OS创建/终止/回收。
- envprobe保留singleflight/TTL、输出上限与仅成功且已join才安装snapshot。
- sync terminal与async消费者可有很小的同步/异步桥，但底层WindowsJob不能各复制一份。不要让async code直接阻塞eventloop执行同步wait/ReadFile；也不要为复用接口把所有sync调用机械搬进无owner的`to_thread`。
- 最重要：hooks与MCP当前credential_guard覆盖最后不可逆spawn，并shield/retrieve取消时仍在执行的spawn。共享factory必须在同一个guard中完成真实processcreation及publication；不能返回一个lazy计划，稍后guard外才spawn，也不能在guard外启动异步helper。这是接缝合同的一部分，而非平台包接管credential policy。

## 平台选择与imports

只在窄factory/composition root按真实`sys.platform`选择，不在tool_runtime/manager各个action重复ifwin32。公共types与POSIX可导入模块不顶层importpty/fcntl/Win32binding；在选中的模块加载平台依赖（TYPE_CHECKING用于仅注释类型），Windowsimport失败如实报告缺资源，不能catch后降为POSIX。测试通过constructor注入明确fake/factory，不修改全局os.name、不加平台registry或runtime capability热切换。

## 实施顺序建议

1. G0先证明所选维护库/扩展，按已冻结合同确定最小physical/shell资源能力；代码组织不能用来跳过G0。
2. 抽窄types与factory，把POSIX机制机械迁出；Registry/result/output/monitor/permission保持一份，让现有macOS/Linux实际合同测试证明没有改变。删除旧manager内部机制，避免并行旧/新路径。
3. 加WindowsJob/PIPE与ConPTY backend，Registry只消费窄接口；保留创建失败/取消/关闭race的原owner。接上PS7shell plan及临时载荷资源转移，移除environment.py中的直接OSspawn/selector/group控制。
4. 将经过验证的共享scope/Windowslauncher接入hooks、MCP、screenshot与probe；各消费者保留自己的env/credential/cancel/result任务与join。一步接好一个owner并删除其被取代OS分支，不留默认不受管路径。
5. 再收口import closure、真实Windows整机/打包与三平台回归、provider/长程激活；前端不因backend拆文件新增平台业务分叉。

本建议是根spec新增代码组织章节的输入，不是已实现结构或完整接口签名承诺；G0内选择具体binding参数可调整私有文件细节，但不可移动上述业务/物理所有权边界。
