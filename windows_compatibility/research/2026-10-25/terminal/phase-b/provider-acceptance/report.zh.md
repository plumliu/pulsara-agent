# Phase B：真实 provider、ROOT 准入修复与 alpha7 复验

2026-10-25。本机 Windows Electron + WSL2 Ubuntu 选定矩阵。正式 alpha7 的 capacity、context 和 running process 跨 active compaction 跟进均通过；没有把先前 alpha6 或隔离源码回归称为 alpha7 全量重跑。

## 结果与实际调用

| 场景 | 实际安装包 | provider calls | prefix 检查 / 同 epoch 比较 | 关键事实 | 独立 PG |
| --- | --- | ---: | ---: | --- | --- |
| 原 MCP/hooks 能力变化 runner | alpha6 | 21 | 21 / 19 | MCP 原生管理、hook trust/enabled 变化与同 epoch 前缀；没有伪装成实际远端工具/hook 执行 | 已 drop |
| 原 capacity/MCP/compaction | alpha7 | 15 | 14 / 7 | 4 ACTIVE + 1 PENDING_START、MCP READY、COMPACTED、五任务真实 handoff、排队 child 实际 direct_echo、最终五 COMPLETED；最终 ROOT 成功继续 | 已 drop |
| 原 context/last_n/mailbox | alpha7 | 8 | 8 / 6 | 两 steer PENDING/PROMPT_QUEUED；worker EXPLICIT COMPLETED 含两个 ROOT markers 与 MIDFLIGHT marker；恰一 INTER_AGENT_MESSAGE | 已 drop |
| running process 跨 active compaction 跟进 | alpha7 | 4 | 3 / 1 | 唯一 terminal yield0 返回 running；真实 handoff 保留同 process ID；唯一 terminal_process.wait 自然取得 Linux/Windows PE 完成标记、exit0 | 已 drop |

alpha7 三项共 **27 次真实调用**，其中两次 `context_compaction_summary`；其余是原 agent loop。默认解析后的实际每次 reasoning 均为 `high`。R10 report 中 `reasoning_effort:null` 表示测试未覆盖显式 override，不代表真实请求未带 reasoning；实际值保存在每个 call 的 `reasoning`。

模型由原只读保存配置选择：`connection:oeeiipbn63ytbexaxw74nv5tgy` / `deepseek-flash` / `openai_chat_completions` / route `deepseek`。没有设置环境 API key，没有改保存配置，没有调用 Notion 或其他外部账户业务工具。每次通过原 scrub set 精确移除实际 credential 值，保留 provider 输入、tools、normalised responses、工具参数/结果、错误和终止细节。

每个 `command.json` 记录精确 argv；alpha7 先导入并断言 `/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/` 的正式包，再只加入私有 tests 根。没有 `src` overlay，没有导入日常 Linux clone 源码。`root-regression` 被显式限定为候选源码回归，禁止和安装版本参数一起调用。

## 原始失败与共享修复

旧 alpha6 capacity 三次分别保留：第一次确认真实挂起，第二次加入只读 task-await 栈，第三次加入原 repository 异常观测。实际停在最终 ROOT 的 `TurnAdmissionCoordinator._settle_root_intent`，不是 MCP close。第三次原异常为 `ConversationKernelConflict: ROOT provider input head changed`。第二次只读 SQL 确认旧11个 turn（含5个 seed）全部 COMPLETED、五子任务全部 COMPLETED，最终 ROOT command 尚未 canonical 提交。

原代码把此已回滚的确定性 precondition 冲突按 ACK-unknown 路径无限重发同一旧 prospective candidate。alpha7 正式复验提供了精确来源：candidate expected head **22**，实际 head **26**；新增23–26均为四个真实 `SUBAGENT_TASK TOOL_RESULT`。因此是候选准备到准入之间的真实子任务结算追加，不是凭推测归因于 compaction 或未完成 seed。

授权修复仅四个生产文件：

- `repository_errors.py`：引入 `PreparedRootInputHeadStale`，只表示尚未提交的 ROOT candidate 失去 transcript head。
- `_repository/conversation.py`：workspace mismatch 保持独立 conflict；只有同 workspace 的 latest sequence mismatch 使用 typed stale。
- `turn_admission.py`：原 FULL/CONFLICT/ABSENT 确认优先。FULL 继续既有 winner；只有 ABSENT + typed stale 退回原 runner。其他 ACK-unknown 仍走原确认/重发路径。
- `runner.py`：仅在 canonical ROOT/输入尚未安装时 close 旧 dry prospective，再以同一 immutable intent/command/frozen model resolution 取新 canonical candidate。原 Hook commit 在 loop 外。重准备前检查同一 `ActiveTurnCancellationIntent.cause` 与 Task 取消事实，防确认窗口消费过取消后重新准入。

close 退还旧 dry surface/hook/wire/Empty reservation；已安装 epoch 的 planning abort 不重建 root。仍由原 continuity owner 执行 same-epoch SYSTEM/tools byte-identical 和 messages append-only。没有新增 epoch authority、durable receipt、registry 或总重试/寿命 cap。

新增确定性回归覆盖初次 stale、ACK unknown 后重发遇 stale、confirm 物理线程门中取消；加原 lost-ACK/NONE→FULL/conflicting winner 三项，共6项真实独立 PG 通过。测试断言同一 intent 对象/command、重准备前旧 resources 已退、既有 SYSTEM/tools/messages 前缀、取消后 accept 仅一次且无 canonical ROOT/无 provider open。另原轻量 TODO28/28、Ruff及 diff 检查通过。Windows本地venv缺psycopg，不冒称Windows pytest通过；这些测试在私有 WSL 源码快照执行，alpha7 provider 资格另由上表正式安装版取得。

两位 peer 只读检查过四文件、dry close/abort planning 和取消护栏，无新增具体阻断；不把只读审阅当作他们重跑了本矩阵。

## 两种探针假设失败

原 context runner 只等 process-local `_active_turn_id`，早于 canonical ROOT/RUNNING。alpha6首轮8calls实际两 steer REJECTED，所有 provider input 均无那两个 ROOT marker，worker 如实未列出；该旧记录没有 public_code，不能倒填 `STEER_TARGET_STALE`。输出限定的 async gate 改为原 canonical accept 返回、原 run_accepted_turn 之前；原两条 steer 各执行一次后开放，保留 PENDING/全部 markers/唯一消息原断言。修门后 alpha6 又遇旧 head 无限重发；alpha7 同门严格通过。

process 补例的两次失败也原样保留：

1. 首次用 `command:` + hex UUID，原 canonical ID 校验拒绝，0 provider calls；探针改为原 `random_id("command")`。
2. idle force compact 本身成功、原进程仍 running，但探针假定“下一新 ROOT 一定携 COMPACTION_RUNTIME_HANDOFF”，在读取 `body=None` 时失败；3 calls。该事实不能单独证明 idle 产品缺陷。本次没有修改 idle 代码、注入手造 handoff 或删掉实际 handoff 断言。

最终补例选择原 active compaction 合同：输出限定包装原 tool.invoke 的返回点，在 terminal 已真实返回 running 后等原 `request_manual` 发布，再放行原工具结算/安全点。继任 provider input 的实际 handoff 必须包含同 process ID，现场原 manager 仍返回 running，才触碰本次 fixture release 文件。模型从真实 handoff 用同 ID 调原 terminal_process.wait。没有向模型补发替代 turn、修改产品状态或复制 owner。

同一个进程在 release 后继续执行 Linux 完成输出和真实 `/mnt/c/Windows/System32/cmd.exe`，均出现在该次 wait 的自然输出中。cmd.exe 的 UNC cwd 提示按实际本地编码解码保留，其中文有替换字符；ASCII 完成标记及 exit0清楚。它不证明 cmd 支持 UNC cwd，也没有改用户代码页。原 core.shutdown 返回后的实际 state 为 PHYSICALLY_JOINED，reader/watcher/scope worker均已退出、helper0、scope reason=settled/cancelled=false、scope目录已退休；不是在 wrapper 外额外 join 后冒称原 owner 正确。

## 精确清理与边界

正式 PG 目标为 Unix socket `/home/plumwsl/.pulsara/desktop/socket`、port5432、system identifier `7700523439330484737`。只为本次测试建/迁移/drop 独立 UUID DB，不监听 TCP，不 start/stop/reset 正式 cluster，不改用户库/设置、WSL默认项、interop或TUN。

挂起的旧 probe 只按 exact argv/uid/starttime 持 pidfd 发 SIGINT，让原 finally 关闭 owners 并 drop 自有 DB；没有泛杀 PID，也不把外部干预当产品通过。失败 scratch 先保留，原物理 owner 结束后才按各 result 的确切路径审计、删除；不删仍有 `desktop/i-*` 的 home。

最终只读审计：本次累计 **15 个确切测试 DB 全不存在**、**26 个测试目录/短 home/私有源码/scratch 路径全不存在**；删除前匹配本次路径的活进程为空。此前真实 scope/长寿命SDK等跨域故障矩阵仍引用Phase B28case报告，不声称全部在alpha7重跑。已保留最小 raw、命令、source 与清理审计；没有 code/doc SHA。

本报告覆盖当前保存的 DeepSeek 文本模型、上述有限真实场景。它不授予其他模型/全部subagent模式、所有平台或未执行组合的资格；实际 MCP stdio/HTTP+hook 执行及其 Windows PE 物理回收由 backend 的独立 provider 验收另证，能力变化21call不替代它。
