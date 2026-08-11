# Round 2：同一 Host Terminal Observation / Control Plane 实施规格

_状态：ACTIVATED（2026-08-11，含两轮反向审阅修缮）；physical-completion wait、linearized launching admission、独立sanitizer reason与公共malformed-input语义均已通过新增故障门控。_

## 0. 基线、目标与结论

### 0.1 两个代码基线

本轮必须同时对照两个Git tree，二者用途不同：

| 基线 | Commit | 用途 |
| --- | --- | --- |
| hard-cut前产品真值 | `5b7ad9f7ffc8565bc572180b2bde0c81ab64473a` | 找回已存在并被测试过的Terminal三工具、实时输出、cursor、shell/env与cwd产品语义；不得照搬旧EventLog/recovery machinery |
| 当前减法Kernel | `739f4a209e61d89b3dba45d39106047205506983` | Round 1完成后的实际修改基线；所有新owner、safe-point与canonical acceptance必须落到当前conversation kernel |

起草时前置材料SHA-256如下：

```text
PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md
cb3e7b0a9f33e5e4c5b17850d47e1af580a3f23f094f868076351bb17a6a6e80

STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
8a30fb3db34bff7c152f3450ce5b18c7b403e3e657fb6f53d9e2e1d87b812b4a

STAGE_3_5_IMPLEMENTATION_SPEC.zh.md
c7a44c62857761f870532e2c6fec02de1a662d0d043854e2eff0df8c04427fbe

STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md
d58e1c585c0f718a516ab4b292061393c6d71f2e1fb2475c311ce11ac5ea82e5

POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
71db1cd88144d7d4601b372db59e9664f807f06a2d27b445aa2084804a05a083

ROUND_1_TOOL_OUTPUT_ARTIFACT_IMPLEMENTATION_SPEC.zh.md
bb91af4b95b5a02f966d24900aedc95ceaa446d41e46bf80b3790233ad054e58

round1_tool_output_artifact_activation.json
057dad550fb34263dc3d6f1b9548925c0584a0c43b63e521836963d7000523db
```

这些hash只证明本规格的review输入。Round 2实施开始前必须记录实际checkpoint HEAD与当时文档hash，不得把本段当作未来activation evidence。

### 0.2 为什么第二轮选择Terminal

Round 1已经恢复：

```text
完整sanitized tool output candidate
+ COMPLETE | HEAD_TAIL canonical preview
+ shared immutable artifact
+ scoped artifact_read
```

Terminal是该内容能力的第一个直接下游。当前Terminal仍能启动、yield、poll、wait、写stdin与kill，但缺少：

- 物理command尚未结束时的真实stdout/stderr增量；
- 精确的same-process retained-output cursor与typed GAP；
- `terminal_monitor` future notification与same-Host Agent wake；
- 用户shell/profile环境近似与default-deny env；
- foreground command的真实final cwd continuity。

因此本轮对应Gap Index中的PHC-01、PHC-03、PHC-04、PHC-05与PHC-06。它不是重建整个旧Terminal Runtime，而是在当前Kernel上补回一个封闭的、同一Host生命周期内的产品能力族。

### 0.3 最终拓扑

本轮目标拓扑冻结为：

```text
physical PIPE / PTY reader
        |
        v
single incremental sanitizer
        |
        v
Host-owned bounded TerminalOutputOwner
        |
        +--> ToolResult Start/Delta/End live projection
        |
        +--> terminal_process snapshot / since_cursor / GAP
        |
        +--> process-local TerminalMonitorCoordinator
                    |
                    +--> TerminalMonitor* LiveAgentEvent
                    |
                    +--> bounded mutable observation draft
                                  |
                                  v
                         Host provider safe point
                                  |
                                  v
                 canonical TERMINAL_OBSERVATION entry
                 + TerminalObservationAccepted occurrence
                                  |
                                  v
                       same-Host Agent continuation
```

物理与durability边界如下：

- process、output cursor、monitor registration、pending observation与autonomy counter全部process-local；
- Host close/crash/takeover后它们全部消失，不重绑、不replay、不补造；
- 只有在provider safe point被Host接受的monitor observation才成为canonical entry；
- canonical entry与对应committed occurrence同transaction写入；
- reopen只读取canonical entry，不恢复process、cursor、monitor或continuation future；
- Round 1 artifact继续保存一次Terminal tool result冻结时可证明的完整sanitized retained body；
- 本轮不增加durable monitor relation、terminal output journal、receipt、checkpoint、projection或job handler。

### 0.4 三个子切片

| 子切片 | 产品缺口 | 必须形成的独立闭环 |
| --- | --- | --- |
| R2-A Terminal output truth | PHC-03、PHC-04、PHC-06 | incremental sanitizer、cursor/GAP、真实ToolResult live streaming、Round 1 artifact衔接、foreground final cwd |
| R2-B Monitor与Agent wake | PHC-01 | `terminal_monitor register/list/cancel`、future observation、human-priority safe-point acceptance、same-Host continuation |
| R2-C Shell/env fidelity | PHC-05 | user shell detection、bounded login-shell snapshot、default-deny env、TTL/fallback、nearest `.venv/bin`、diagnostic |

R2-A是R2-B的硬前置。R2-C可独立验证，但Round 2整体只有三个切片都activated后才算完成。

## 1. 必须保持的上位架构约束

1. canonical relational row负责“现在是什么”；selective committed event负责“何时接受了什么”；live plane负责当前进程体验。
2. `AgentEvent`仍是typed extension protocol，不得重新承担Terminal execution recovery state machine。
3. tool-request assistant message必须完整提交，tool attempt必须接受，physical Terminal adapter才可达。
4. raw PIPE/PTY bytes不得进入canonical row、committed event、extension callback或artifact；所有下游只消费同一个incremental sanitizer产生的public text。
5. `ToolResultStart/Delta/End`仍是process-local LiveAgentEvent；Start不可变，Delta只更新一个bounded live assembler并可携带provisional Terminal text，End以authoritative replacement携带Round 1已经冻结的final canonical preview。
6. live streaming、monitor或observer失败不得改变physical process outcome、tool result acceptance或canonical commit。
7. Terminal output producer不得等待TUI、extension、monitor或provider consumer；overflow只允许GAP、coalesce、detach或丢弃live projection。
8. yielded process与monitor都绑定当前Host owner。Host close先停止monitor admission/delivery，再终止并join process；不承诺跨Host rebind。
9. monitor registration不是durable product fact；registration、policy、cursor、pending observation、rate counter与lease不得写PostgreSQL。
10. monitor notification只有在Host safe point接受后才是conversation fact。接受前crash可以丢失；接受后entry可跨Host保留，但旧process不可操作。
11. accepted Terminal observation不得伪装为human `USER_MESSAGE`、`USER_STEER`或ordinary prompt；它使用closed canonical entry kind与typed occurrence。
12. `TerminalObservationAccepted`只表示某一Terminal observation entry在sequence N被接受；event不复制output、不证明entry真实、不驱动reopen。
13. event subject继续引用exact canonical entry，因此不增加free-form subject或第14种subject slot。
14. append authority仍只有`HostWriterGuard | JobAttemptClaimGuard`；Terminal observation只能由HostWriterGuard追加。
15. 本轮允许core committed vocabulary从26增至27，因为新增类型有独立产品语义；Live vocabulary保持23、subject slot保持13、append guard保持2。Reassessment的Round 2补订已经同步active oracle；R2-0必须验证代码、schema与fixtures以该补订为唯一目标，不能继续沿用旧26类guard。
16. 26/23/13/2是Stage 2 activation记录，不是永久架构上限；历史activation evidence不得改写成“当时已有27类”。
17. ordinary hook只能接收授权后的typed/redacted projection；monitor callback、process owner、sanitizer、cursor或recorder不得进入event metadata。
18. 真正要求跨Host必达的未来Terminal automation必须另立ADR并升级为具名durable job；本轮不得为monitor恢复通用receipt graph。
19. Go TUI的高级Terminal viewer/editor不在本轮，但Protocol v3中已经公开的Live payload如被修改，Python/Go生成物与cross-language gate必须同步。
20. Memory、compaction、MCP、plan、hierarchical subagent、Standalone Inspector与Legacy Python REPL均不属于本轮。

## 2. 当前代码真值

### 2.1 当前保留的Terminal能力

当前[`src/pulsara_agent/terminal_process/manager.py`](src/pulsara_agent/terminal_process/manager.py)与[`models.py`](src/pulsara_agent/terminal_process/models.py)已经保留：

- `terminal`启动一个local PIPE或PTY command；
- `yield_time_ms`后返回Host-scoped `process_id`；
- `terminal_process`的`list/log/poll/wait/write/submit/close_stdin/kill`八个action；
- per-Host process ownership与Host close terminate/join；
- max live/finished process与finished TTL bound；
- current workspace containment；
- 最近`.venv/bin`的简化PATH prepend；
- Round 1 `ToolOutputArtifactCandidate`。

这些owner应被重塑而不是另起第二套Terminal registry。

### 2.2 当前output不是真正的stream truth

当前`_BoundedOutput`：

- 保存最近8 MiB raw bytes；
- snapshot时才一次性decode、ANSI strip与secret regex；
- 超限后整chunk从head删除；
- 没有stream identity、monotonic offset、revision或snapshot-subscribe线性化点；
- `log/poll/wait`只能反复返回tail，无法表达“自上次cursor之后的新输出”；
- sanitizer在chunk/retention boundary上的语义并不等价于one-shot完整输入。

raw bytes先retention、后sanitization的顺序必须反转。新owner只能retain sanitized text及其cursor metadata。

### 2.3 当前ToolResult Delta是完成后伪stream

当前[`conversation_kernel/runner.py`](src/pulsara_agent/conversation_kernel/runner.py)在physical attempt前发`ToolResultStart`，但随后：

```text
await tools.invoke()
prepare canonical preview
emit one ToolResultDelta(full preview)
emit ToolResultEnd
```

因此command运行期间没有Delta。实现者不得通过更频繁poll manager来伪装stream；delta必须由实际PIPE/PTY reader在command返回前产生。

### 2.4 当前TerminalMonitor Live名称不等于产品能力

[`conversation_kernel/tool_runtime.py`](src/pulsara_agent/conversation_kernel/tool_runtime.py)中的`_offer_terminal_live()`会在一次`terminal`或`terminal_process`返回后同步生成：

- `TerminalMonitorOpened`；
- `TerminalMonitorObservation`；
- 有时生成`TerminalProcessCompleted`与`TerminalMonitorClosed`。

但当前没有：

- `terminal_monitor` descriptor/executor；
- registration或monitor id owner；
- future output/completion观察；
- cursor去重；
- quiet/heartbeat/expiry；
- Host ingress或autonomous continuation。

这条同步伪producer必须删除。保留的四类Terminal Live event应由process owner或真实monitor coordinator产生。

### 2.5 当前safe-point可复用但缺少Terminal source

[`conversation_kernel/safe_point.py`](src/pulsara_agent/conversation_kernel/safe_point.py)已经线性化provider input freeze与subagent/job external result acceptance；[`conversation_kernel/runner.py`](src/pulsara_agent/conversation_kernel/runner.py)也在每次freeze前消费pending steer。

本轮应扩展这个单一safe-point owner，使其冻结并消费bounded process-local Terminal observation installation attempt。不得增加第二把“monitor provider lock”、durable input cursor或event replay reducer。

### 2.6 当前shell/env/cwd退化

当前：

- 使用`$SHELL`或`/bin/sh`直接执行`-c`；
- subprocess env继承父进程绝大多数变量，只按变量名suffix排除部分secret；
- 没有login/interactive shell snapshot、TTL、timeout、fallback与provenance；
- foreground completion后把`current_cwd`重新设为command启动cwd，而不是shell final cwd。

最近`.venv/bin`查找是有效保留能力，应并入新的env builder而不是删除。

### 2.7 Round 1的精确衔接

[`ROUND_1_TOOL_OUTPUT_ARTIFACT_IMPLEMENTATION_SPEC.zh.md`](ROUND_1_TOOL_OUTPUT_ARTIFACT_IMPLEMENTATION_SPEC.zh.md)已经冻结：

- artifact保存完整sanitized candidate，而不是preview；
- source coverage是`COMPLETE | RETAINED_SNAPSHOT`；
- retention gap与blob publication failure是两个正交维度；
- `artifact_read` offset相对stored artifact body；
- publication失败不把已知tool outcome降级成unknown。

Round 2不得重新发明Terminal artifact table或output blob journal。TerminalOutputOwner只负责process-local retained body；每次Terminal tool result冻结时，继续把该body交给Round 1 processor。

## 3. hard-cut前产品真值与禁止移植面

### 3.1 必读旧代码

实施与review至少读取以下旧tree路径：

```bash
PRE_HARD_CUT=5b7ad9f7ffc8565bc572180b2bde0c81ab64473a

git show "$PRE_HARD_CUT:src/pulsara_agent/ports/terminal.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/terminal/process.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/terminal/output.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/terminal/monitor.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/terminal/notification.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/terminal/tool_port.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/terminal/env.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/terminal/shell.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/terminal/session.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/tools/builtins/terminal_monitor.py"
```

旧tests的主要产品oracle：

```text
5b7ad9f7:tests/test_terminal_public_api_hard_cut.py
5b7ad9f7:tests/test_terminal_tool_ports.py
5b7ad9f7:tests/test_terminal_monitor_tm0.py
5b7ad9f7:tests/test_terminal_monitor_tm1_tm5.py
5b7ad9f7:tests/test_terminal_runtime.py
5b7ad9f7:tests/test_terminal_env.py
5b7ad9f7:tests/test_tools.py
```

归档设计材料：

- [`archived_docs/PULSARA_TERMINAL_PUBLIC_TOOL_API_SPLIT_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_TERMINAL_PUBLIC_TOOL_API_SPLIT_HARD_CUT_IMPLEMENTATION.zh.md)；
- [`archived_docs/PULSARA_TERMINAL_PROCESS_MONITOR_AND_AGENT_WAKE_DESIGN.zh.md`](archived_docs/PULSARA_TERMINAL_PROCESS_MONITOR_AND_AGENT_WAKE_DESIGN.zh.md)；
- [`archived_docs/TERMINAL_SHELL_ENV_V1_IMPLEMENTATION_PLAN.zh.md`](archived_docs/TERMINAL_SHELL_ENV_V1_IMPLEMENTATION_PLAN.zh.md)。

### 3.2 必须找回的产品语义

- 三工具精确分工：start、immediate process operation、future monitor；
- physical reader产生真实output delta；
- ANSI/UTF-8/secret sanitizer跨chunk一致；
- retained cursor、quiet、heartbeat、completion与expiry；
- cancel monitor不kill process；
- human input与stop优先于autonomous wake；
- foreground final cwd continuity；
- bounded login-shell env snapshot与default-deny inheritance；
- complete retained output与bounded display分离。

### 3.3 禁止找回的旧machinery

下列旧类型/owner即使与产品代码同文件，也不得移植：

- `TerminalOutputJournalSegment`的durable page/spool authority；
- restart cursor recovery与UI replay buffer；
- monitor registration/observation/receipt/termination durable event chain；
- notification reservation account、process head、candidate、delivery disposition与ACK；
- reducer、projection、checkpoint、reconciliation、repair与latch；
- RuntimeSession reopen时的monitor adoption；
- completion reservation、origin run event reference与EventLog proof；
- generic autonomous delivery receipt graph；
- yielded OS process跨Host adoption。

旧代码只用于提炼产品语义与测试输入，不是新package拓扑模板。

## 4. Round 2范围与非目标

### 4.1 必须完成

1. current Host内的single TerminalOutputOwner与incremental sanitizer。
2. 真实`terminal`与`terminal_process.wait` output streaming。
3. `terminal_process log/poll/wait`的optional `since_cursor`与typed GAP。
4. Round 1 artifact从sanitized retained body冻结。
5. foreground final cwd capture与workspace validation。
6. `terminal_monitor register/list/cancel` public descriptor、strict schema、executor closure。
7. output/quiet、heartbeat、completion、expiry future observations。
8. monitor draft的bounded coalescing、immutable installation、human priority、safe-point acceptance与same-Host continuation。
9. canonical `TERMINAL_OBSERVATION` entry与`TerminalObservationAccepted` occurrence。
10. process-local shell detection、login-shell env snapshot、TTL/cache/timeout/fallback、default-deny allowlist与nearest-venv overlay。
11. current Python Host/runner/Terminal happy path回归、fresh clean-v0 migration与real-provider dogfood。
12. Gap Index中PHC-01/03/04/05/06按实际activation证据更新。

### 4.2 明确不做

- Go专用Terminal artifact viewer、download UI或process dashboard；
- cross-Host process/monitor rebind；
- Host crash后恢复OS process、cursor、pending observation或autonomous turn；
- durable Terminal output、monitor、notification或delivery relation；
- new durable job handler；
- binary/HTML/screenshot Terminal artifact；
- unlimited output retention；
- command sandbox/container化；Terminal仍是当前trusted host shell；
- shell history、tmux/screen owner或remote terminal；
- plan、MCP、compaction与subagent graph修复；
- Standalone Canonical Inspector或Legacy Python REPL。

## 5. R2-A：Terminal output truth

### 5.1 Single incremental sanitizer

每个physical process只能有一个sanitizer owner：

```text
raw PIPE / PTY chunks
   -> incremental UTF-8 decoder
   -> CR/LF normalization
   -> ANSI/OSC stripping
   -> closed secret assignment / bearer redaction
   -> sanitized text chunks
```

sanitizer使用process-local immutable `TerminalSanitizerPolicyV1`。本轮固定：

```text
maximum undecided UTF-8 carry = 4096 bytes
maximum unterminated ANSI/OSC carry = 4096 bytes
oversized sensitive token policy = redact_entire_token
unterminated escape overflow policy = suppress_sequence_and_emit_bounded_marker
malformed UTF-8 policy = replace
```

冻结要求：

- UTF-8 code point、ANSI escape、OSC sequence、CRLF与secret pattern可跨任意raw chunk boundary；
- one-shot oracle必须调用同一个streaming automaton的`feed(all_bytes) + finalize()`，不得另写regex实现；同一raw byte sequence无论以何种chunk切分，最终sanitized bytes必须相同；
- malformed UTF-8使用稳定replacement语义；
- closed sensitive grammar只包含本轮明确实现并具测试向量的assignment-key与Bearer token；不保留没有sealed issuer/grammar的泛化`known-token`承诺；
- 一个仍可能成为secret或escape的prefix在quiet/explicit observation boundary不得输出；它留在不超过4096 bytes的undecided carry中；
- undecided sensitive token超过carry时整token变为固定redaction marker并丢弃直到token boundary，绝不把已缓存prefix或后续suffix原样释放；
- unterminated ANSI/OSC超过carry、quiet boundary或EOF时都不得泄漏其payload；overflow产生fixed bounded diagnostic marker，EOF只finalize已判定安全的text；
- raw chunk在sanitizer消费后即可丢弃，不进入retained ring；
- live delta、cursor read、monitor、artifact与final result只读取sanitized stream；
- sanitizer state有明确close/finalize，process EOF后不遗留半个escape或decoder buffer；
- partial line在quiet bound后可观察，但不得因此泄漏可识别secret；
- sanitizer异常只使该process output进入typed unavailable/gap，不能杀死process或改写exit code。

每个process state还必须拥有exact一个completion watcher（可为bounded thread或registry task）：

- 它等待physical process terminal，随后join reader并finalize sanitizer；
- 它只推进一次process terminal transition，并由process owner产生一次`TerminalProcessCompleted`；
- monitor completion不依赖模型再次调用`poll/wait/log`；
- max live process为8，因此watcher数量同样有界；
- Host release必须join reader、completion watcher与process group，不能留下daemon thread继续回调旧owner。

process state使用closed physical retirement状态：

```text
RUNNING
  -> TERMINALIZING       # 已观察到exit/kill，reader/sanitizer/watcher仍可能退出中
  -> PHYSICALLY_JOINED   # child/process group已reap，reader与watcher已退出，sanitizer已finalize
  -> PRUNABLE            # observation lease count = 0，且无in-flight read
```

- finished TTL或capacity prune只能移除`PRUNABLE` state；`_join_physical()` timeout/失败必须保留state并使close/retirement显式失败，不能先删registry再留下physical owner；
- dormant与active monitor registration都持有revocable process observation lease。lease pin identity、lifecycle与subscription，不pin无限output bytes；
- completion/expiry/cancel先撤销subscription，in-flight installation结算后release lease；Host close先停止admission并撤销lease，再推进join；
- `PHYSICALLY_JOINED`必须证明subprocess/process group、PIPE/PTY reader与completion watcher全部结束，不能只观察`poll() != None`。

实现可参考旧`runtime/terminal/output.py`的streaming sanitizer，但不得复制其durable journal/spool部分。

### 5.2 Process-local identity与cursor

内部closed DTO至少包含：

```text
TerminalOutputCursor
  owner_epoch
  process_id
  stream_id
  sanitized_utf8_offset

TerminalOutputSnapshot
  process_id
  stream_id
  output_revision
  retained_from_offset
  through_offset
  text
  process_status
  exit_code
  source_coverage

TerminalOutputReadDisposition
  CURRENT_SNAPSHOT
  EXACT_DELTA
  GAP
  INVALID_CURSOR
  UNAVAILABLE
```

语义冻结：

- `stream_id`在process创建时产生，直到process被prune保持不变；
- sanitized UTF-8 offset从0开始，只在完整code-point boundary推进；
- `output_revision`在sanitized text或terminal lifecycle变化时单调推进；
- snapshot在一个process output lock cut中读取status、retained range、text与through cursor；
- cursor是process-local lookup token，不是artifact offset、Protocol committed cursor或durable capability；
- wrong process、wrong owner epoch、wrong stream、client-ahead与malformed cursor返回`INVALID_CURSOR`，不得伪装成retention GAP；
- requested cursor早于`retained_from_offset`返回`GAP`，同时给出当前retained snapshot与新的through cursor；
- exact delta只包含`since_cursor < offset <= through_cursor`的sanitized text；
- Host close后旧cursor稳定不可用，不从artifact或event replay恢复。

public tool schema使用opaque `since_cursor: string | null`与`output_cursor: string`，不要求模型拼装内部字段。token必须绑定owner epoch、process与stream，但不需要成为bearer secret。

### 5.3 Retention与内存预算

本轮将单process sanitized retained hard bound与Round 1 blob hard bound对齐为16 MiB，并增加Host aggregate hard bound：

```text
TERMINAL_RETAINED_OUTPUT_HARD_BYTES = 16 MiB
TERMINAL_HOST_RETAINED_HARD_BYTES = 128 MiB
maximum live processes per Host = 8
maximum finished process records per Host = 32
retained authority representation = UTF-8 bytes
```

要求：

- 不超过16 MiB且从stream offset 0连续保留时，artifact candidate为`COMPLETE`；
- 超过后从head淘汰并变为`RETAINED_SNAPSHOT + TERMINAL_RETENTION_GAP`；
- 不能只按chunk数量淘汰而意外超过byte bound；
- 大量tiny chunk必须mechanically coalesce，segment metadata保持bounded；
- 16 MiB是单process上限而不是Host内存证明；live与finished process的retained bytes合计始终不超过128 MiB；
- aggregate pressure先按确定性oldest-first淘汰finished process的retained bytes，再推进live ring的head；每次淘汰都推进`retained_from_offset`并形成显式GAP，不能静默丢内容；
- monitor observation lease只pin process identity/lifecycle与subscription owner，不允许绕过aggregate byte budget；被pin process的output仍可按上述规则产生GAP；
- Python `str`只允许作为bounded decode/display临时值，不能与retained UTF-8 bytes形成第二份长期16 MiB authority；
- response `max_output_chars`只限制本次展示，不改变retained authority或coverage；
- 16 MiB是process-local memory与artifact publication的共同上界，不建立disk spool；
- 进程输出超过上界并不杀process，也不改tool result state。

### 5.4 `terminal_process` cursor contract

`log`、`poll`与`wait`增加optional `since_cursor`：

```text
since_cursor absent
  -> bounded CURRENT_SNAPSHOT

since_cursor exact and retained
  -> bounded EXACT_DELTA

since_cursor valid but evicted
  -> GAP + current retained snapshot

since_cursor malformed / wrong Host / wrong process / client-ahead
  -> typed application error
```

所有response至少返回：

```text
output
output_disposition
output_cursor
retained_from_cursor
gap_before_output
truncated_by_response_bound
source_coverage
```

`poll`仍是即时lifecycle check；`log`仍是即时output read；`wait`仍只在本次tool call内等待最多30秒。增加cursor不得把三者合并成一个泛化subscribe工具。

`write/submit/close_stdin/kill`可返回操作后的current snapshot与cursor，但不接受`since_cursor`，避免把process mutation与read continuation混成开放union。

### 5.5 Round 1 artifact衔接

每次`terminal`或`terminal_process`形成ToolResult时：

1. 在同一output lock cut冻结sanitized retained body、coverage与current through cursor；
2. structured ToolResult payload携带bounded output与cursor metadata；
3. `ToolOutputArtifactCandidate.text`使用冻结的retained body，不使用JSON envelope或bounded preview；
4. Round 1 processor决定COMPLETE/HEAD_TAIL与artifact availability；
5. publication failure仍接受已知process/tool outcome；
6. `artifact_read` offset相对该次冻结的stored body，不等于Terminal live cursor。

如果retention gap已经发生，artifact marker必须继续明确“仅为retained snapshot”；不得声称可读取原始offset 0之前的bytes。

## 6. R2-A：真实ToolResult live streaming

### 6.1 Producer边界

runner继续在attempt acceptance后、physical invoke前发不可变`ToolResultStart`。随后为本次tool call建立process-local `ToolResultLiveSink`：

```text
runner ToolResultStart
  -> DirectKernelToolPort.invoke(..., live_sink)
       -> TerminalOutputOwner emits sanitized deltas
       -> live_sink nonblocking handoff
       -> provisional ToolResultDelta before physical call returns
  -> physical result freezes
  -> sink drain/close
  -> Round 1 artifact preparation
  -> final canonical preview freezes
  -> ToolResultEnd(authoritative canonical preview replacement)
  -> canonical ToolResult acceptance
  -> COMMITTED settlement
```

`terminal`与`terminal_process.wait`必须接入真实sink。`poll/log/write/submit/close_stdin/kill`是即时操作，不要求制造长stream，但其final ToolResult仍有正常Start/End。

### 6.2 Thread与backpressure

PIPE/PTY reader不得直接调用extension callback或async observer。handoff必须：

- thread-safe；
- nonblocking；
- 有event与byte hard bound；
- 至多安排一个待执行event-loop drain，不为每个raw chunk无限`call_soon_threadsafe`；
- 可mechanically coalesce相邻text delta；
- overflow时使对应live observation出现GAP/aborted settlement，physical reader继续；
- sink close有bounded drain，不能让slow observer阻塞tool completion；
- observer、hook或TUI异常被LiveAgentEventBus隔离。

不得为了“确保每个delta必达”写PostgreSQL、文件spool、receipt或retry job。

### 6.3 Single live assembler

Terminal ToolResult live view使用一个bounded assembler：

- Delta只包含新增sanitized provisional terminal text；
- assembler维护bounded head/tail provisional public view与gap flag；
- physical completion后必须先drain/close sink、冻结Round 1 artifact disposition与canonical preview；
- `ToolResultEnd.final_text`是authoritative replacement，exact等于Round 1 final canonical preview，而不是要求等于此前provisional assembler view；客户端用同一block identity以End替换draft，不新增第二个assembler或settlement type；
- End不是canonical acceptance proof，也不要求复制完整16 MiB retained output；
- canonical result提交后由existing COMMITTED settlement替换live draft；
- live overflow/crash时partial view丢失，canonical ToolResult如果已经接受仍保持真实；
- 非Terminal tool维持现有bounded one-shot Delta/End行为。

本轮不得新增`RawTerminalDelta`、durable segment、draft adoption ACK或第二套ToolResult event grammar。

### 6.4 Actual Terminal Live producers

完成本切片后：

| Live event | 唯一production producer | 语义 |
| --- | --- | --- |
| `ToolResultStart/Delta/End` | runner + current tool-call live sink | 当前tool result live lifecycle |
| `TerminalProcessCompleted` | physical process owner | exact process在当前Host进入terminal status |
| `TerminalMonitorOpened` | monitor coordinator | real registration已安装 |
| `TerminalMonitorObservation` | monitor coordinator | real future observation已冻结 |
| `TerminalMonitorClosed` | monitor coordinator | real registration因cancel/completion/expiry/Host close结束 |

`tool_runtime._offer_terminal_live()`式同步伪monitor producer必须为0。

## 7. R2-A：foreground cwd continuity

### 7.1 规则

```text
foreground command completed before yield
  + final cwd captured
  + final cwd inside workspace
    -> terminal session.current_cwd advances

yielded/background command
    -> never advances session cwd, even if later wait observes completion
```

其他冻结要求：

- command exit code必须保持原值；cwd probe failure不能改写process outcome；
- final cwd落在workspace外时，不推进session cwd，并返回bounded diagnostic；
- current cwd已被外部删除时，下一command确定性回退到最近仍存在的workspace ancestor，最终至少回到workspace root；
- 每个有可能在foreground wait内完成的`terminal`调用都可在spawn前创建per-process cwd probe；是否采用只由yield linearization decision决定；
- foreground completion winner读取并验证probe后才推进cwd；yield winner永久把该probe标为`adoption_disallowed`，后续`wait`即使读到内容也只能清理、不能采用；
- yielded probe若command尚可能写入，必须由completion watcher或Host close在physical join后清理，不能提前unlink后允许shell重建孤儿文件；
- cwd probe path/content不得进入committed event；
- PTY与PIPE语义一致。

旧`_wrap_command()`/`read_captured_cwd()`可作为实现参考，但新代码不得恢复completion event owner。

## 8. R2-B：`terminal_monitor` public contract

### 8.1 三工具职责保持封闭

```text
terminal
  start + bounded foreground wait + exact process_id

terminal_process
  one immediate operation

terminal_monitor
  register/list/cancel future observation
```

descriptor必须明确：

- completed `terminal` result没有live process，不能monitor；
- `terminal_process.wait`不会安排future wake；
- `terminal_monitor.cancel`不kill process；
- long-running command应register monitor，而不是无限poll；
- observation output是bounded preview，更多内容通过`terminal_process.log`及其artifact读取。

### 8.2 Strict input union

public schema冻结为：

```text
TerminalMonitorInput =
  Register {
    action: "register"
    process_id: non-empty
    conditions: {
      output: null | {
        min_new_output_chars: 1..65536 = 200
        quiet_period_ms: 0..10000 = 500
      }
      heartbeat_interval_seconds: null | 5..1800
    }
    delivery: {
      max_output_chars: 512..32000 = 4000
      minimum_progress_observation_interval_seconds: 5..1800 = 5
    }
    lifetime: {
      maximum_duration_seconds: 1..36000 = 36000
    }
  }
  | List { action: "list" }
  | Cancel {
      action: "cancel"
      monitor_id: non-empty
    }
```

output与heartbeat都为null表示completion-only monitor；completion始终隐含启用。

### 8.3 Tool outcomes

closed result至少包含：

```text
REGISTERED
  monitor_id
  process_id
  baseline_cursor
  expires_at
  policy summary

INVENTORY
  up to 8 current Host-owned monitors
  monitor_id / process_id / state / observation_ordinal / pending

CANCELLED
  monitor_id
  process_id
  cancellation outcome

REJECTED
  not_found | already_terminal | capacity | duplicate | owner_closed
```

JSON shape、closed action或字段值不合法属于所有built-in共用的descriptor
validation边界，稳定返回`INVALID_ARGUMENTS`；它不进入`terminal_monitor`执行器，
也不另造monitor专用`REJECTED malformed`语义。

`register`必须通过atomic process snapshot-and-subscribe边界：要么观察到process已经terminal并返回`already_terminal`，要么registration安装后该process的后续output/completion一定能唤醒coordinator；不能在两步之间漏completion。

`terminal_monitor`只允许ROOT main-run调用；SUBAGENT_TASK scope稳定返回`ROOT_SCOPE_REQUIRED`。这是因为future observation只会进入ROOT conversation，不能让child在结束后遗留一个归属不明的autonomous wake owner。subagent仍可在自己的活跃turn内使用`terminal`与`terminal_process`即时操作。

### 8.4 Capacity与lifetime

初始hard bounds沿用已验证产品量级，但全部process-local：

```text
maximum active monitors per Host = 8
maximum progress observations per monitor = 119
maximum progress observations per 600 seconds = 60
maximum same-Host autonomous continuations per monitor = 12
maximum monitor lifetime = 10 hours
maximum mutable drafts per monitor = 1
maximum in-flight installation attempts per monitor = 1
maximum successor drafts while in-flight per monitor = 1
```

达到任一bound：

- 不阻塞process；
- 不建立durable backlog；
- coordinator以typed reason关闭或降为live-only observation；
- process继续运行；
- 不自动创建durable job。

本轮冻结为：autonomous continuation budget耗尽后关闭monitor并产生`DELIVERY_BUDGET_EXHAUSTED` close reason。

### 8.5 Registration activation与origin ToolResult

`register`必须先执行atomic snapshot-and-subscribe，才能避免process在ToolResult提交前完成而永久漏通知；但它在origin `ToolResultAccepted`成功前只能是process-local dormant registration：

```text
terminal_monitor.register physical invoke
  -> prepare dormant registration + exact baseline
  -> return REGISTERED ToolResult candidate
  -> canonical ToolResultAccepted commit / exact confirmation
  -> activate registration exactly once
  -> emit TerminalMonitorOpened
```

冻结要求：

- dormant期间可coalesce output/completion事实，但不得autonomous accept、不得产生`TerminalMonitorOpened`；
- `KernelToolResult`只允许携带closed process-local settlement union：`NoProcessLocalSettlement | PreparedTerminalMonitorRegistrationSettlement`；不得携带任意callback或mutable coordinator；
- runner在ToolResult commit或ACK-unknown exact confirmation后调用activate；
- deterministic acceptance failure、turn cancellation或Host close调用discard；
- activate/discard都幂等并绑定origin attempt/result entry identity；
- 该settlement token不序列化、不进event metadata、不形成receipt/checkpoint/repair；
- `list`与`cancel`没有dormant activation side branch；cancel effect在attempt后立即发生，result缺失时沿普通tool attempt/result规则解释。

production capability seam必须显式传递closed invocation context，不能让Terminal tool从ambient global猜scope：

```text
KernelToolInvocationContext
  session_id / workspace_id
  turn_id / assistant_entry_id / tool_call_id / attempt_id
  conversation_scope_kind = ROOT | SUBAGENT_TASK
  scope_subagent_task_id nullable
  Host owner epoch

ProcessLocalEffectSettlementDisposition
  COMMITTED
  DISCARDED
```

- `KernelToolPort.invoke(..., invocation_context)`将runner已知的exact scope交给tool port；`terminal_monitor.register`只在`ROOT`创建dormant registration，child在physical installation之前稳定拒绝；
- `KernelToolPort.settle_process_local_effect(token, disposition)`是独立typed method；token只含sealed identity/fingerprint，不能携带callback、coordinator、future或任意metadata；
- runner仅在ToolResult commit或exact confirmation winner后发送`COMMITTED`；deterministic acceptance failure、cancel或Host close发送`DISCARDED`；重复settlement幂等；
- settlement lookup与mutable coordinator只存在于DirectKernelToolPort内部的Host-owned map，不能进入event metadata、serializer或canonical DTO。

## 9. R2-B：MonitorCoordinator语义

### 9.1 Process-local registration

每个registration至少持有：

```text
monitor_id
owner_epoch / Host owner
process_id / stream_id
registration baseline cursor
last accepted delivery cursor
policy
opened_at / expires_at
observation ordinal
mutable observation draft or none
immutable installation attempt or none
bounded successor draft or none
rate/autonomy counters
revocable registration lease
revocable process observation lease
```

这些字段不得进入PostgreSQL或event serializer。`monitor_id`只在当前Host有效。

registration lease绑定session、writer generation、Host owner、origin tool attempt与当时effective permission policy fingerprint；每次autonomous acceptance前重新验证lease尚未撤销、Host未closing且writer generation仍匹配。controller detach本身不取消monitor，同一Host仍可按已授予的bounded autonomy继续；后续tool dispatch继续经过当前policy，若需要human confirmation而无controller则沿现有规则deny。Host close或writer takeover立即撤销全部lease。

### 9.2 Observation kinds

closed observation kinds：

- `PROGRESS`：新sanitized output达到threshold，并满足quiet与minimum interval；
- `HEARTBEAT`：heartbeat deadline到达，process仍running；
- `COMPLETION`：process进入success/error/timeout/killed等terminal status；
- `EXPIRY`：monitor lifetime到达而process仍running。

每个observation包含：

```text
observation_id
monitor_id / process_id
observation_ordinal
kind
process_status / optional exit_code
output disposition
delivery_coverage = COMPLETE | HEAD_TAIL
available_source_utf8_bytes
included_source_utf8_bytes / omitted_by_delivery_bound_utf8_bytes
bounded sanitized output
retained_from_cursor / through_cursor
gap flag
observed_at
```

public payload不得包含raw bytes、env values、private URL、callback identity或OS pid。

`delivery_coverage`只描述本次可读取sanitized source range是否因delivery/canonical byte bound被裁剪，与retention语义正交：

- `gap flag`表示在available range之前已有bytes因retention不可得；
- `COMPLETE`要求`omitted_by_delivery_bound_utf8_bytes = 0`且output包含全部available source；
- `HEAD_TAIL`要求`omitted_by_delivery_bound_utf8_bytes > 0`，output使用固定UTF-8-safe head/omission/tail marker；
- `included_source_utf8_bytes + omitted_by_delivery_bound_utf8_bytes = available_source_utf8_bytes`；marker自身不计入included source bytes；
- 因此`GAP + HEAD_TAIL`是合法组合：前者说明更早内容已经丢失，后者说明当前仍可读取的范围又因delivery bound被裁剪。

### 9.3 Cursor与coalescing

- registration baseline cursor之前的output默认不触发progress；register result已返回baseline snapshot位置；
- observation只读取上一个accepted delivery cursor之后的output；
- heartbeat如携带new output，也推进同一个delivery cursor，避免后续重复；
- 同一monitor最多一个`MutableObservationDraft`；它只持当前process observation与cursor range，不持target turn、canonical entry/revision id或ACK状态；
- 新progress可与draft中的progress/heartbeat coalesce到更高through cursor；completion可覆盖尚未freeze的progress/heartbeat draft；
- Host scheduler先在其safe-point/scheduling lock内冻结`PreparedInstallationTarget`，再调用`TerminalMonitorCoordinator.freeze(target)`；TerminalMonitorCoordinator只在自己的短锁内裁决cancel/draft freeze并构造immutable `TerminalObservationInstallationAttempt`，随后立即释放锁。PostgreSQL I/O期间该attempt的content、identity、fingerprint与target都不可修改；
- in-flight attempt存在时，后续事实最多形成一个bounded successor draft；completion可覆盖该successor中的progress/heartbeat，但不能改写in-flight attempt；
- expiry不覆盖已经观察到的completion；
- cursor gap必须显式进入draft与installation attempt，不能把retained tail冒充exact delta；
- canonical acceptance成功或exact confirmation成功后才推进`last accepted delivery cursor`；cursor推进到attempt冻结的完整through cursor，即使delivery coverage为`HEAD_TAIL`也不重复投递被有意省略的source bytes；canonical envelope必须明确暴露该coverage与omitted count；
- `UNKNOWN`必须先对同一immutable attempt做exact confirmation；只有得到`NONE`或确定性target conflict后，coordinator才可根据当前Host state冻结新的installation attempt，不能在unknown期间改target重投；
- live observer收到event不等于Agent已接受，不推进canonical delivery cursor。

closed process-local DTO分层：

```text
MutableObservationDraft
  monitor_id / process_id / draft_revision
  kind / process status / optional exit code
  retained_from_cursor / through_cursor / gap
  delivery coverage / available-included-omitted UTF-8 byte counts
  bounded sanitized output / observed_at

PreparedInstallationTarget
  ExistingTurnInstallation | NewTurnInstallation
  stable canonical ids

TerminalObservationInstallationAttempt
  session_id / workspace_id / writer_generation
  observation_id / observation_ordinal
  immutable draft snapshot + content digest
  exact PreparedInstallationTarget
  occurred_at / actor
  candidate fingerprint
```

二者都只在当前Host进程存在；“attempt”表示一次immutable canonical installation candidate，不是tool/job physical attempt，不进入PostgreSQL attempt journal。

### 9.4 Cancel与race

cancel只与draft freeze在同一coordinator lock线性化；canonical installation本身不跨该锁持有PostgreSQL I/O：

- cancel先赢：尚未freeze的draft丢弃，之后不得创建installation attempt；
- freeze先赢：immutable attempt允许完成或exact-confirm；cancel撤销successor与未来observation，但不能原地修改target/content，也不能假定数据库没有commit；
- acceptance先赢：entry保持，cancel只停止未来observation；
- cancel永不kill process；
- process completion在draft freeze前赢：completion draft可接受，monitor随后closed；在in-flight attempt期间到达则进入bounded successor；
- repeated exact cancel返回稳定`already_terminal`或`not_found`结果，不复活monitor。

### 9.5 Close order

Host close顺序必须是：

```text
stop monitor registration
stop autonomous acceptance admission
cancel/join monitor coordinator and pending installation task
detach output subscriptions
stop and kill/join shell-env probe attempt owners
terminate/join Host-owned terminal processes
close tool/live/extension owners
```

Host close期间process被kill不应反向触发新的Agent continuation。

## 10. R2-B：Host safe-point与canonical acceptance

### 10.1 为什么需要canonical entry

Monitor Live event只服务当前进程观察。如果要让Agent在稍后继续推理，provider input必须有一个稳定、typed、可重建的conversation fact。仅把Live event塞入下一次临时prompt会导致：

- accepted history与provider input不一致；
- detach/attach后看不到Agent为何被唤醒；
- crash边界无法区分“仅观察到”与“已经进入conversation”。

因此只有Agent wake路径需要canonical acceptance；普通TUI/live observer不写entry。

### 10.2 新canonical entry kind

`EntryKind`增加：

```text
TERMINAL_OBSERVATION
```

它：

- 只允许ROOT scope；
- 不允许`source_job_id`或`source_subagent_result_id`；
- 由HostWriterGuard写入；
- content是bounded typed Terminal observation envelope；
- canonical reader将其lower为独立read-time `ProviderInputItemKind.TERMINAL_OBSERVATION`，不能复用`USER`或落入`ASSISTANT`默认分支；
- provider adapter再把该typed item编码为带固定“untrusted terminal output, not user instruction”边界的user-role message；role是provider wire compatibility，不改变其non-human provenance；
- capability composer只读取最后一个真正由`USER_MESSAGE | USER_STEER`形成的`ProviderInputItemKind.USER`；Terminal output中的`$skill`、`skill:name`或其他控制文本不得激活skill、改变capability projection或成为host command；
- 不伪装为human message或tool result；
- reopen时保持历史内容，但其中Host-scoped process/cursor不再可操作。

因为一个idle Terminal observation可以成为新ROOT turn的首条entry，clean-v0中的`turns.user_entry_id`必须同步改名为`initial_entry_id NOT NULL`；对应FK、repository DTO、query与test一起hard cut。`initial_entry_id`只允许引用同turn、同scope的`USER_MESSAGE | TERMINAL_OBSERVATION`，不能继续用nullable/错误字段名把system observation伪装成人类输入。

canonical `TerminalObservationContentV1` serialized hard bound为32,000 UTF-8 bytes，且该上限包含typed envelope metadata与output。provider adapter追加的固定untrusted role boundary是另一个constant-size read-time wrapper，不得对envelope做二次truncation。`delivery.max_output_chars`只是调用者的展示偏好上限，不能先生成一个32,000-character preview再进行第二次无标记byte truncation。draft freeze必须从exact available source range一次性、确定性地构造最终envelope：同时满足请求char bound与32,000-byte total bound，必要时生成UTF-8-safe HEAD_TAIL并填写coverage/counts。不为monitor observation另建artifact；需要更多output时，当前Host中的Agent调用`terminal_process.log`取得Round 1 artifact。

### 10.3 Typed observation envelope

canonical bytes使用closed schema，不接受free-form metadata map：

```text
TerminalObservationContentV1
  schema_version = "terminal_observation.v1"
  observation_id
  monitor_id
  process_id
  observation_ordinal
  observation_kind
  process_status
  exit_code nullable
  output_disposition
  gap_before_output
  delivery_coverage = COMPLETE | HEAD_TAIL
  available_source_utf8_bytes
  included_source_utf8_bytes
  omitted_by_delivery_bound_utf8_bytes
  output
  host_scoped = true
```

coverage/count fields属于canonical observation content，因此detach/attach后的模型与TUI仍能知道历史preview是否省略了可读取内容；它们不进入`TerminalObservationAccepted` event payload。`COMPLETE`不得出现omission marker，`HEAD_TAIL`不得声称“full output”。

`output_cursor`不进入canonical envelope：它只对当前process-local owner有效。model-facing renderer可提示“若process仍在当前Host运行，可使用exact process_id调用terminal_process”，但不得把历史cursor宣传成可resume capability。

`ProviderInputItemKind.TERMINAL_OBSERVATION`是canonical reader构造的ephemeral compiler DTO，不是第24类LiveAgentEvent、durable event或新relation；它只保留provenance直到provider wire encoding与capability composition分流完成。

### 10.4 新committed occurrence

新增：

```text
TerminalObservationAccepted
  subject = exact transcript entry FK
  guard = HostWriterGuard only
  projection = ImmutableEntryProjection
  sensitivity = S1 typed/redacted
```

event payload只包含closed小字段，例如：

```text
entry_kind = TERMINAL_OBSERVATION
observation_kind = PROGRESS | HEARTBEAT | COMPLETION | EXPIRY
```

不得复制output、monitor policy、cursor、callback、lease或process owner。

### 10.5 Acceptance transaction

Host scheduler先构造process-local target，coordinator再冻结installation attempt。target为closed union：

```text
PreparedInstallationTarget =
    ExistingTurnInstallation(
      exact running ROOT turn_id,
      stable entry_id
    )
  | NewTurnInstallation(
      stable turn_id,
      stable context_binding_revision_id,
      stable initial entry_id
    )

TerminalObservationInstallationAttempt
  session_id / workspace_id / writer_generation
  observation_id / candidate fingerprint
  exact PreparedInstallationTarget
  canonical inline content + digest
  occurred_at / actor
```

唯一调用形态与lock order冻结为：

```text
acquire Host scheduler / ProviderSafePoint admission lock
  -> select active-turn or idle-turn mode and mint PreparedInstallationTarget
  -> acquire TerminalMonitorCoordinator lock
       -> revalidate registration lease, cancel state and draft revision
       -> freeze(target) or return no-draft/cancelled
     release TerminalMonitorCoordinator lock
  -> install/confirm exact immutable attempt while Host safe-point lock remains held
release Host safe-point lock
```

全仓lock order只能是`Host scheduler/safe-point lock -> TerminalMonitorCoordinator lock`；TerminalMonitorCoordinator、cancel与process callbacks不得反向读取Host scheduler state、获取Host lock或携带callback。Host lock可跨bounded canonical install/confirmation以阻止provider freeze或第二个turn scheduler，但TerminalMonitorCoordinator lock不得跨PostgreSQL I/O，因此output、cancel与physical retirement不会被数据库等待阻塞。

`ExistingTurnInstallation`由同一Host writer transaction完成：

```text
lock/revalidate session writer
lock/revalidate exact target turn
require_provider_safe_turn in this transaction
allocate entry sequence
insert TERMINAL_OBSERVATION entry
append TerminalObservationAccepted
advance entry/event high-water
commit
```

`NewTurnInstallation`由同一Host writer transaction完成：

```text
lock/revalidate session writer and prove no active ROOT turn
allocate entry sequence
insert ROOT RUNNING turn with stable initial_entry_id and revision-0 id
insert FULL_HISTORY revision 0 with source_through_sequence = entry_sequence - 1
insert TERMINAL_OBSERVATION initial entry
append TerminalObservationAccepted
advance entry/event high-water
commit
```

这不是client command，因此不新增`session_commands` command kind/row。event不得用于证明entry成功。ACK unknown时使用stable IDs、content digest、target union、event type与subject做stateless exact confirmation；确认`FULL`后才能启动runner，`UNKNOWN`时不得换target，`NONE`时只能重写同一immutable attempt。只有确定性target conflict解决了旧attempt的状态后，才可从尚存事实冻结一个new attempt。

### 10.6 Active turn与idle turn

```text
active RUNNING ROOT turn
  + no prepared provider handle/model operation
  + canonical require_provider_safe_turn succeeds
  -> accept observation into exact active turn
  -> next provider cut includes it

active provider call or outstanding tool request/result gap
  -> keep one bounded process-local draft
  -> never insert behind already-frozen cut

active turn completes before installation attempt resolves
  -> do not back-insert
  -> resolve any in-flight ExistingTurnInstallation first
  -> when Host idle, freeze a NewTurnInstallation from the remaining draft/fact

Host idle
  -> after any already-canonical human prompt queue head
  -> create new ROOT turn with TERMINAL_OBSERVATION as its initial entry
  -> start runner
```

这保证：

```text
provider freezes H=100
monitor observes at process-local time T
assistant commits at sequence 101 with cut=100
terminal observation later commits at 102 in new turn
```

不会把模型未见过的observation倒插到assistant之前。

“没有active provider handle”单独不构成safe point：assistant tool-request commit后handle已经关闭，但physical tools可能尚未返回。active-turn acceptance必须同时通过repository的`require_provider_safe_turn()`，证明该turn所有accepted tool call都有result。

### 10.7 Human priority与ordering

- already-canonical user prompt queue head优先于idle monitor continuation；
- active turn中的durable user steer先于pending Terminal observation draft进入下一cut；
- stop/cancel Host action优先，不能被monitor wake反向重启同一turn；
- 同一monitor observation按ordinal FIFO；coalescing只合并尚未接受的replaceable progress/heartbeat；
- 不承诺human prompt与尚未canonical的process-local observation之间的跨domain全局时间顺序；
- 一旦两者成为canonical entry，entry sequence就是唯一历史顺序。

### 10.8 Safe-point owner

扩展现有`ProviderSafePointCoordinator`或其单一pre-freeze consumer，不新建第二套锁或第二个turn scheduler：

- input freeze与Terminal observation acceptance互斥；
- monitor coordinator只更新process-local draft并向Host scheduler发bounded wake signal；它不得直接写repository、创建turn或启动runner；
- Host scheduler是`PreparedInstallationTarget`唯一producer，也是`TerminalMonitorCoordinator.freeze(target)`唯一caller；TerminalMonitorCoordinator从不自行选择/更换turn，也不读取human queue或provider handle；
- active runner每轮按`durable user steer -> bounded Terminal observation installation -> provider input freeze`的顺序消费；installation位于现有steer consumer之后、freeze之前；
- Host idle时也只由现有Host prompt/turn scheduler处理human queue head后再执行`NewTurnInstallation`并启动runner；
- draft入队本身不阻塞provider；
- 每个safe point最多接受bounded数量，避免monitor耗尽一次turn budget；
- subagent task turn不得消费ROOT Terminal observation；
- Host takeover时旧writer guard使canonical acceptance fail closed，旧draft/installation attempt随owner消失。

## 11. R2-C：Shell/profile/env fidelity

### 11.1 Shell detection

process-local `TerminalShellConfig`至少包含path、name与argv policy。检测顺序：

1. executable `$SHELL`；
2. `/bin/zsh`；
3. `/bin/bash`；
4. `/bin/sh`。

实际command使用检测到的shell执行non-login `-c`。login/interactive模式只用于bounded env probe，不能让每条command重新加载profile或把profile noise写入command output。

### 11.2 Default-deny environment

subprocess env从default-deny allowlist建立，不再“继承全部、按suffix排除”。默认允许的类别：

- identity/locale/temp：`HOME USER LOGNAME SHELL TMPDIR TEMP TMP LANG LC_ALL LC_CTYPE TERM COLORTERM`；
- inert desktop/path metadata：`XDG_SESSION_TYPE XDG_CURRENT_DESKTOP XDG_DATA_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_STATE_HOME`；
- toolchain roots：`NVM_DIR VOLTA_HOME PNPM_HOME BUN_INSTALL CARGO_HOME RUSTUP_HOME PYENV_ROOT RBENV_ROOT ASDF_DIR MISE_DATA_DIR MISE_CONFIG_DIR MISE_CACHE_DIR HOMEBREW_PREFIX HOMEBREW_CELLAR HOMEBREW_REPOSITORY GOPATH GOROOT`；
- `PATH`。

以下默认拒绝：

- provider/API credentials；
- socket/network endpoint、display与session-bus等active capability environment；
- `PYTHONPATH`、`NODE_OPTIONS`、`LD_PRELOAD`、`DYLD_*`等loader/hook注入；
- 未在closed allowlist或explicit user allowlist中的任意变量。

显式`inherit_allowlist`与`passthrough_names`都只接受exact environment name；前者仍执行secret-shaped value scan，后者是可绕过value scan的高权限escape hatch。配置名可审计，但值不得写log/event/diagnostic。

如果继续使用旧环境变量入口，本轮closed config surface为：

```text
PULSARA_TERMINAL_SHELL_SNAPSHOT
PULSARA_TERMINAL_SHELL_SNAPSHOT_TTL_SECONDS
PULSARA_TERMINAL_SHELL_SNAPSHOT_TIMEOUT_SECONDS
PULSARA_TERMINAL_ENV_INHERIT_ALLOWLIST
PULSARA_TERMINAL_ENV_PASSTHROUGH_NAMES
PULSARA_TERMINAL_EXTRA_PATH_PREPENDS
PULSARA_TERMINAL_VENV_OVERLAY
```

README与`.env.example`只记录名称、默认值和风险，不提供secret passthrough示例。

### 11.3 Login-shell snapshot

env probe contract：

```text
default enabled
TTL = 300 seconds
timeout = 5 seconds
maximum stdout = 1,000,000 bytes
stdin = DEVNULL
stderr = DEVNULL
detached process group
NUL-delimited sentinel + env -0
```

probe output经过同一name allowlist与secret-value defense。timeout、oversize、non-zero、malformed或spawn failure只形成process-local fallback diagnostic，command继续使用sanitized parent env + sane PATH。

cache key至少绑定shell path、HOME、workspace、relevant startup-file stat signature与allowlist configuration。Host close后cache消失。

每个cache key由一个process-local `TerminalEnvProbeAttemptOwner` single-flight：

- probe stdout以streaming reader执行1,000,000-byte hard cap，不先无界`communicate()`后再检查；
- spawn使用detached process group；timeout、oversize、parse failure、Host close与caller cancellation都向整个group发送terminate/kill并reap child，同时join stdout reader；只调用`proc.kill()`不满足contract；
- cache只安装exit 0、内容合法且process group/reader已经physical join的成功snapshot；失败结果不冒充cached environment，也不留下negative durable state；
- concurrent相同key调用共享同一bounded future；owner不持env cache lock等待process I/O；
- Host close必须先停止新probe admission，再kill/join所有probe attempt，之后才能释放Terminal env owner。

### 11.4 PATH composition

PATH顺序：

1. effective cwd向workspace root搜索到的最近`.venv/bin`；
2. configured existing extra prepends；
3. sanitized login-shell snapshot PATH；
4. sanitized parent PATH；
5. sane platform fallback；
6. stable de-duplication。

不得搜索workspace外的`.venv`。

### 11.5 Diagnostics与sensitivity

Terminal result可展示：

- shell path/name；
- shell snapshot used；
- closed error code而非raw stderr；
- removed variable count与secret-shaped value count；
- nearest venv path；
- PATH entry count。

不得展示：

- 完整env map；
- 任意env value；
- startup file content；
- secret variable name inventory；
- probe stdout/stderr。

这些diagnostic默认属于operational/product metadata，不进入selective committed event。

## 12. Schema、event、transaction与Protocol预算

### 12.1 Physical schema

Round 2保持24张product relations。clean-v0 baseline只做：

- `transcript_entries.entry_kind`增加`TERMINAL_OBSERVATION`；
- `turns.user_entry_id`改为`initial_entry_id text NOT NULL`并收紧允许的initial entry kind；clean-reset baseline没有保留nullable过渡态；
- 现有deferrable FK继续保证same session；同一个deferred invariant trigger还必须验证`initial_entry_id`指向exact same turn、same conversation scope及允许kind：ROOT允许`USER_MESSAGE | TERMINAL_OBSERVATION`，SUBAGENT_TASK只允许其exact task scope的`USER_MESSAGE`；
- `NOT NULL`在turn insert时要求先给出stable future entry id；不增加`initial_entry_kind`复制列或repository-only约定，deferrable FK/constraint trigger仍允许turn与initial entry在同transaction任意合法insert order下统一验证；
- associated CHECK/reader/query/protocol mapping同步；
- `agent_events.event_type`closed CHECK增加`TerminalObservationAccepted`；
- type→subject slot/guard数据库约束增加exact mapping。

不得新增：

```text
terminal_processes
terminal_output_segments
terminal_monitors
terminal_monitor_observations
terminal_notification_receipts
terminal_delivery_attempts
terminal_cursors
```

### 12.2 Vocabulary oracle

Round 2目标oracle：

```text
CommittedAgentEvent = 27
LiveAgentEvent = 23
typed subject slots = 13
append guards = 2
product relations = 24
durable job handlers = 4
```

Live payload字段可为真实cursor/gap语义收窄或扩展，但event type数量不变。所有import-time oracle、SQL fixtures、Protocol enum与architecture tests必须一致。

### 12.3 Transaction budget

| 行为 | PostgreSQL transaction | committed event |
| --- | ---: | ---: |
| raw output delta / cursor advance | 0 | 0 |
| monitor register/list/cancel | 仅其普通tool result acceptance沿现有路径 | 仅existing `ToolResultAccepted` |
| progress/heartbeat live observation但未Agent wake | 0 | 0 |
| accepted Agent wake observation | 1 Host writer transaction | 1 `TerminalObservationAccepted` |
| shell/env snapshot | 0 | 0 |
| foreground cwd update | 0 | 0 |
| Host close monitor/process drain | 0 | 0 |

Terminal observation正文不复制进event payload。普通monitor delivery不追加`TerminalProcessCompleted` committed occurrence。

### 12.4 Protocol v3

现有23类Live vocabulary已经包含Terminal四类，因此本轮不增加Live enum。必须让payload表达真实事实，至少能区分：

- exact process/monitor/observation identity；
- observation kind与ordinal；
- current status/exit code；
- bounded preview；
- cursor range或typed gap；
- delivery coverage与available/included/omitted source byte counts；
- close reason。

若proto字段改变：

- Python schema、generated Python、generated Go、gateway mapping与fingerprint同一diff更新；
- Protocol major仍为v3；未发布workspace允许minor hard cut；
- Go高级Terminal UI仍是non-goal，但decoder不能丢字段或崩溃。

## 13. Failure与crash矩阵

| 故障 | 目标行为 | 禁止行为 |
| --- | --- | --- |
| sanitizer chunk failure | process继续；output变typed unavailable；artifact candidate固定为`RETAINED_SNAPSHOT + TERMINAL_SANITIZER_UNAVAILABLE`且不得声明`COMPLETE`；result保留真实exit | 冒充`TERMINAL_RETENTION_GAP`、kill process、把exit改unknown、raw fallback泄漏、归档不完整source为完整artifact |
| sanitizer undecided carry/unterminated escape overflow | 整个敏感token或escape payload抑制并产生fixed bounded marker；任意chunking与one-shot同结果 | quiet flush泄漏prefix、超限后原样释放suffix |
| live sink overflow | GAP/abort该live projection；physical tool继续；canonical result照常 | 阻塞reader/provider、写durable delta |
| slow TUI/hook | observer detach/GAP；run不受影响 | backpressure到subprocess |
| retention eviction | cursor read返回GAP；artifact标retained snapshot；仍有效的exact cursor artifact只覆盖本次selected source range | tail冒充exact delta、artifact重放cursor之前的retained bytes、恢复disk spool |
| Host aggregate retained pressure | oldest finished bytes先淘汰，再推进live ring head并显式GAP；总量始终≤128 MiB | 只算8个live process、finished registry各留16 MiB、长期复制`str` |
| prune physical join timeout | state停在`TERMINALIZING`或`PHYSICALLY_JOINED`之前，close/prune显式失败并可重试join | 删除registry state后遗留child/reader/watcher |
| malformed/wrong cursor | typed invalid cursor | 当作retention GAP或跨Host查artifact |
| process completes during register | already-terminal或安装后收到completion，二者必有其一 | registration成功但永久漏completion |
| shell leader退出但同组descendant仍存活 | 保持`TERMINALIZING/running`、占用live capacity；foreground wait与`terminal_process.wait`都等待watcher发布的group/reader/sanitizer physical-completion边界 | 以leader `wait()`冒充physical completion、立即解除live订阅、提前释放capacity |
| concurrent process launch at capacity edge | registry锁内以`live + launching`统一admission；reservation与published process原子交接，spawn failure与owner close均确定性释放 | 两个caller同时通过检查后各自`Popen`、用durable lease修补process-local竞态 |
| monitor capacity exhausted | register typed reject；process继续 | 创建durable backlog/job |
| monitor普通拒绝 | capacity、duplicate、missing、already-terminal、owner-close均返回closed `REJECTED` reason | exception冒泡成generic `SYSTEM_ERROR` |
| pending progress重复 | bounded coalesce到latest cursor | 无界queue或重复相同tail |
| monitor锁外读取期间draft被冻结或cursor推进 | 以evaluation generation/base identity重验；变化则丢弃candidate并从新base重读 | successor重复in-flight已覆盖区间、清空后到output通知 |
| delivery preview超过char/byte bound | 单次UTF-8-safe HEAD_TAIL；canonical envelope记录coverage与omitted source bytes，cursor仍推进through | 二次静默截断、把HEAD_TAIL标COMPLETE、用retention GAP代替delivery coverage |
| cancel与acceptance竞争 | coordinator短锁只决定cancel或draft freeze；freeze winner的immutable attempt结算/confirm后entry不撤回 | 持锁跨DB I/O、kill process、删除entry |
| Terminal output含`$skill`/`skill:name` | provider以untrusted user-role observation看到内容；capability composer仍只读取最后一个human USER item，skill activation为0 | 把Terminal item归为USER、从stdout改变capability projection |
| Host target freeze与monitor cancel竞争 | lock order固定Host safe-point→TerminalMonitorCoordinator；freeze返回exact attempt或none，monitor coordinator lock在DB I/O前释放 | monitor coordinator自行选turn、反向取Host lock、持其锁等待PostgreSQL |
| provider call active或tool request尚缺result | draft留在process-local pending；必须同时通过no-handle与canonical safe-turn predicate才可安装 | 只检查handle关闭就把observation插到tool request之后 |
| active turn先完成 | observation进入显式新turn | 改写旧assistant cut |
| canonical ACK unknown | exact确认同一immutable installation attempt；unknown期间target/content不变 | 换target/new IDs重投、receipt/repair graph |
| idle new-turn transaction中途失败 | turn/revision-0/initial entry/event全无；ACK unknown按stable IDs exact-confirm，FULL后才启动runner | 只有entry/event、半个turn、用`session_commands`伪装internal wake |
| writer takeover | old guard fail closed；draft/installation attempt随旧Host消失 | 新Host恢复monitor/cursor |
| Host close | stop monitor delivery，join coordinator，再kill/join process | close中触发autonomous turn |
| env snapshot timeout/oversize/Host close | probe按`SPAWNING → RUNNING → JOINED`封闭；close禁止新`Popen`并等待attempt done；kill整个probe process group并join child/reader；sanitized fallback env + diagnostic；失败不入cache | `Popen`返回到owner登记之间逃逸、只kill parent、阻塞command、继承全部secret env |
| PTY close_stdin | 向terminal driver发送EOT并在真实EOF后允许process完成；失败返回typed error | 只关闭duplicated master却报告成功 |
| SUBAGENT_TASK调用monitor任一action | `register/list/cancel`统一在dispatch前返回`ROOT_SCOPE_REQUIRED` | child枚举或取消ROOT monitor |
| subagent启动的Terminal process完成 | process-local origin携带exact child turn/scope/task，completion按原scope投影 | 固定ROOT或伪turn attribution |
| cwd probe missing/corrupt | 保持previous cwd，command outcome不变 | 用猜测cwd推进session |
| artifact publication failure | known tool outcome仍接受，UNAVAILABLE marker | side-effect unknown、tool retry |

## 14. 实施切片

### R2-0：Inventory与negative guards

- 记录checkpoint HEAD、前置文档hash与current test baseline；
- 验证Reassessment的Round 2补订已经把active oracle冻结为27 Committed / 23 Live / 50 formal，并保留Stage 2 26/49为历史基线；编码前不得仍有“禁止`TerminalObservationAccepted`”的active guard；
- 枚举Terminal producer/consumer、线程/task、tool descriptor、Live payload与Host close owner；
- 冻结27/23/13/2/24/4目标oracle；
- 建立禁止durable Terminal relation/replay owner的architecture guards；
- 证明当前同步伪monitor producer与post-return one-shot Delta位置。

### R2-A1：Incremental sanitizer与TerminalOutputOwner

- raw-before-sanitize rolling buffer替换为sanitize-before-retain；
- 增加stream identity、offset、revision、snapshot/read-since/GAP；
- 冻结4096-byte sanitizer carry与chunking-equivalence oracle；retained authority改为UTF-8 bytes，执行16 MiB per-process / 128 MiB per-Host aggregate bound并保持segment metadata bounded；
- 引入`RUNNING -> TERMINALIZING -> PHYSICALLY_JOINED -> PRUNABLE`与observation lease prune gate；
- Round 1 artifact candidate改从exact sanitized snapshot冻结。

### R2-A2：真实ToolResult streaming

- runner/KernelToolPort增加bounded live sink边界；
- `terminal`与`terminal_process.wait`接physical reader delta；
- 删除完成后伪造整段Delta与同步伪monitor路径；provisional Delta在artifact preparation后由authoritative canonical-preview End替换；
- actual process owner产生一次`TerminalProcessCompleted`；
- 验证overflow/failure isolation。

### R2-A3：Cursor public API与cwd

- `terminal_process log/poll/wait`增加`since_cursor`；
- descriptor/schema/executor fingerprint闭合；
- 所有可能foreground完成的call在spawn前准备cwd probe，以yield linearization决定adopt或永久discard，并在physical join后cleanup；
- yielded/background cwd不推进。

### R2-B1：`terminal_monitor` tool与coordinator

- strict register/list/cancel schema；
- descriptor、permission/action policy与production executor closure；
- closed `KernelToolInvocationContext`与identity-only process-local settlement port，ROOT-only在installation前执行；
- atomic snapshot-and-subscribe；
- output/quiet、heartbeat、completion、expiry；
- mutable draft / immutable installation attempt / bounded successor、observation lease、cancel与Host close。

### R2-B2：Safe-point acceptance与Agent wake

- process-local mutable draft与immutable `ExistingTurnInstallation | NewTurnInstallation`；
- `TERMINAL_OBSERVATION` entry/compiler lowering；
- 独立`ProviderInputItemKind.TERMINAL_OBSERVATION`、untrusted user-role adapter mapping与human-only capability composition；
- `TerminalObservationAccepted` event与same-transaction append；
- delivery coverage/counts与single-pass bounded preview；active-turn同时校验no provider handle与canonical safe-turn；idle transaction原子创建turn/revision-0/initial entry/event；Host唯一冻结target并按Host→coordinator lock order调用freeze，monitor只发wake；
- ACK unknown exact confirmation；
- committed count从26更新为27。

### R2-C：Shell/env fidelity

- shell detection与non-login command execution；
- bounded、process-group-owned、per-key single-flight login/interactive env probe；
- inert default allowlist、active environment默认拒绝、通用exact allowlist/value scan与explicit passthrough；
- TTL/cache/fallback、nearest venv、safe diagnostics；
- PIPE/PTY一致性。

### R2-F：Activation与证据

- fresh clean-v0 install、repeat verify与catalog fingerprint；
- targeted、full pytest、PostgreSQL、Protocol/Go gates；
- real-provider Terminal stream→yield→monitor→autonomous continuation dogfood；
- machine evidence新增独立Round 2 artifact，不改写Stage 2/Stage 3–5/Round 1历史evidence；
- 验证Reassessment的active 27/50 oracle、Round 2 schema/fixtures与Gap Index状态一致；Stage 2历史规格与activation evidence保持原样；
- Gap Index仅按真实通过的切片更新状态。

每个子切片应净删除旧伪producer或重复owner；不得以“先恢复旧Runtime、之后再减”为过渡策略。

## 15. 主要修改面

预计production修改面：

- [`src/pulsara_agent/terminal_process/models.py`](src/pulsara_agent/terminal_process/models.py)
- [`src/pulsara_agent/terminal_process/manager.py`](src/pulsara_agent/terminal_process/manager.py)
- [`src/pulsara_agent/ports/terminal.py`](src/pulsara_agent/ports/terminal.py)
- [`src/pulsara_agent/ports/live_agent_event.py`](src/pulsara_agent/ports/live_agent_event.py)
- [`src/pulsara_agent/conversation_kernel/tool_runtime.py`](src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [`src/pulsara_agent/conversation_kernel/runner.py`](src/pulsara_agent/conversation_kernel/runner.py)
- [`src/pulsara_agent/conversation_kernel/host.py`](src/pulsara_agent/conversation_kernel/host.py)
- [`src/pulsara_agent/conversation_kernel/safe_point.py`](src/pulsara_agent/conversation_kernel/safe_point.py)
- [`src/pulsara_agent/conversation_kernel/repository.py`](src/pulsara_agent/conversation_kernel/repository.py)
- [`src/pulsara_agent/conversation_kernel/reader.py`](src/pulsara_agent/conversation_kernel/reader.py)
- [`src/pulsara_agent/conversation_kernel/direct_model.py`](src/pulsara_agent/conversation_kernel/direct_model.py)
- [`src/pulsara_agent/conversation_kernel/contracts.py`](src/pulsara_agent/conversation_kernel/contracts.py)
- [`src/pulsara_agent/conversation_kernel/vocabulary.py`](src/pulsara_agent/conversation_kernel/vocabulary.py)
- [`src/pulsara_agent/conversation_kernel/live.py`](src/pulsara_agent/conversation_kernel/live.py)
- [`src/pulsara_agent/capability/builtin_catalog.py`](src/pulsara_agent/capability/builtin_catalog.py)
- [`src/pulsara_agent/capability/resolver.py`](src/pulsara_agent/capability/resolver.py)
- [`src/pulsara_agent/capability/tool_action.py`](src/pulsara_agent/capability/tool_action.py)
- [`src/pulsara_agent/capability/result_contracts.py`](src/pulsara_agent/capability/result_contracts.py)
- [`src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql`](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)
- [`src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto`](src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto)
- [`src/pulsara_agent/terminal_protocol/v3_gateway.py`](src/pulsara_agent/terminal_protocol/v3_gateway.py)
- [`README.md`](README.md)
- [`.env.example`](.env.example)

允许新增少量中性leaf，例如：

```text
terminal_process/output.py
terminal_process/monitor.py
terminal_process/environment.py
conversation_kernel/terminal_observation.py
```

它们必须分别只有一个owner，不得形成`runtime/terminal`复刻package或依赖EventLog。

## 16. 必须有的tests

### 16.1 Sanitizer与output owner

- UTF-8 code point跨chunk；
- ANSI/OSC跨chunk；
- CR/LF normalization；
- secret assignment/bearer/token跨chunk；
- arbitrary chunking与one-shot结果一致；
- 4096-byte边界、oversized sensitive token、unterminated ANSI/OSC与partial-line quiet boundary不泄漏prefix/suffix；
- 100,000 tiny segments后byte与metadata仍bounded；
- 16 MiB per-process以内COMPLETE，超出后RETAINED_SNAPSHOT；8 live + 32 finished组合仍受128 MiB Host aggregate bound；
- aggregate淘汰finished-first、live ring GAP与长期retained `str`副本为0；
- process/reader/watcher任一join失败时state不可prune；lease释放前不可prune，Host close最终全部physical joined；
- raw secret不进入retained body、live delta或artifact。

### 16.2 Cursor

- snapshot返回exact through cursor；
- since cursor只返回新增text；
- UTF-8 boundary cursor；
- retention GAP给出new retained range；
- wrong process/owner/stream、client-ahead与malformed返回invalid；
- Host close后cursor不可用；
- response char bound不改变authority cursor；
- concurrent append与snapshot无丢失/重复。

### 16.3 Real streaming

- command sleep前输出sentinel，physical completion前收到Delta；
- PIPE与PTY均成立；
- normal stream Start一次、Delta顺序、End一次；
- provisional Delta可与canonical preview不同；End用同一block identity authoritative replacement并exact等于Round 1 canonical preview；
- canonical ToolResult仍在physical return与artifact preparation后提交；
- live sink overflow/observer exception不延迟process或commit；
- non-Terminal tool行为不回归；
- synthetic `_offer_terminal_live` producer为0。

### 16.4 Artifact与cwd

- foreground完整output进入Round 1 artifact；
- yielded initial result只冻结yield时retained body；
- later log/wait冻结新的retained body；
- retention gap两个axis保持正确；
- `cd src`后下一foreground command从`src`开始；
- yielded process完成后不推进cwd；
- spawn后才决定yield时，probe只在foreground winner采用；yield winner后即使晚写probe也只cleanup；
- workspace外final cwd不推进；
- deleted cwd回退nearest workspace ancestor；
- cwd temp file全部close path清理。

### 16.5 Monitor tool

- descriptor/schema/executor exact closure；
- register/list/cancel；
- cancel不kill；
- already-terminal registration race；
- dormant registration在origin ToolResult commit前捕获但不投递；
- ROOT invocation context可注册，SUBAGENT_TASK在physical installation前稳定拒绝；settlement token不含callback/coordinator；
- ToolResult commit/confirm后exact一次activate，失败/cancel后discard；
- output threshold + quiet；
- heartbeat；
- completion；
- expiry；
- cursor不重复旧tail；
- retention GAP；
- pending progress coalesce；
- completion覆盖pending progress；
- delivery COMPLETE/HEAD_TAIL、char preference bound、32,000-byte envelope bound及`GAP + HEAD_TAIL`正交组合；accepted HEAD_TAIL后cursor推进且下一observation不重复omitted source；
- freeze后draft不能改写in-flight attempt；DB I/O期间只允许一个successor draft；
- ACK unknown先exact-confirm原target，不能因turn变化换target重投；
- max 8 monitors、rate/lifetime/autonomy bounds；
- slow live observer不阻塞monitor/process。

### 16.6 Safe-point与continuation

- active provider handle期间draft不写entry；handle已关闭但tool request尚缺result时也不写entry；
- canonical reader把Terminal entry映射为`TERMINAL_OBSERVATION` item，provider adapter映射为带固定边界的user role；包含`$skill`、`skill:name`和相似控制字符串时capability projection/skill activation保持不变；
- next safe point接受后下一input cut包含observation；
- assistant cut=100、observation later=102时不倒插；
- active turn先结束后创建显式new ROOT turn；
- new ROOT安装在一个transaction内创建stable turn、revision-0、initial entry与event，任一kill point无半状态且FULL confirmation前runner为0；
- pending human prompt优先；
- user steer优先于monitor observation draft；
- stop/close后不自动重启；
- subagent turn不消费ROOT observation；
- canonical entry + event同transaction；
- event subject exact entry FK；
- deferred invariant拒绝initial entry跨turn、跨scope或错误kind；不增加`initial_entry_kind`列；
- ACK unknown exact confirm同一entry/event；
- Host scheduler是target唯一producer，Host→TerminalMonitorCoordinator lock order通过cancel/freeze/DB delay竞态测试且monitor coordinator lock不跨I/O；
- crash before acceptance丢draft/attempt；acceptance后reopen只读entry、不恢复process；
- takeover旧guard不能accept。

### 16.7 Shell/env

- `$SHELL`与fallback detection；
- command不是login shell，probe是bounded login/interactive；
- profile PATH可见；
- provider/API key、loader/hook vars默认消失；
- inert/toolchain allowlist保留；active capability environment全部默认消失，只有通用exact allowlist/passthrough可显式加入；
- explicit passthrough exact生效；
- secret-shaped value scan；
- snapshot timeout/oversize/nonzero/cancel/Host close均kill process group并join reader/child；成功且joined才cache；same cache key single-flight；
- TTL与startup-file signature invalidation；
- nearest `.venv/bin`按effective cwd；
- diagnostic不含env value；
- PIPE/PTY child env一致。

### 16.8 Database、Protocol与historical product oracle

- fresh clean-v0与repeat verifier通过；
- product relation仍exact 24；
- committed/live/subject/guard exact 27/23/13/2；
- type→entry subject→Host guard数据库约束；
- `turns.initial_entry_id`为NOT NULL；null、cross-turn、cross-scope与错误kind均由数据库拒绝，合法deferred insert order通过；
- Protocol generator Python/Go exact；
- old product tests中的有效语义已由新Kernel tests承接；
- old receipt/replay/checkpoint assertions没有复活。

## 17. Static architecture guards

必须有自动化guard证明：

1. production不存在`runtime/session.py`、EventLog或old `runtime/terminal` import。
2. schema没有Terminal process/output/monitor/notification/receipt relation。
3. `terminal_monitor`存在且descriptor、strict schema、action policy、executor binding闭合。
4. `DIRECT_KERNEL_TOOL_NAMES`与production bindings包含exact三Terminal tools。
5. `TerminalMonitor*` Live events只有真实coordinator producer。
6. `TerminalProcessCompleted`只有physical process owner producer。
7. physical reader不import repository、event serializer、extension host或Protocol gateway。
8. monitor coordinator不importmigration、job executor、event replay或canonical query tailer。
9. raw bytes不进入Live payload、artifact candidate或committed event。
10. committed `TerminalObservationAccepted` subject exact entry且guard only HostWriter。
11. event payload没有output、cursor、policy、callback、lease或pid。
12. monitor state没有pickle/JSON checkpoint、SQL repository或filesystem spool。
13. process-local cursor没有出现在migration/schema或canonical observation content。
14. Host close顺序先monitor、后process，且全部join有hard deadline。
15. live sink/monitor callback异常不能到达runner failure/canonical rollback。
16. product relation/job/guard数量保持24/4/2。
17. process-local monitor settlement是closed union，不能承载callback、coordinator或任意metadata。
18. retained authority只保存bounded UTF-8 bytes；per-process 16 MiB与per-Host 128 MiB hard bound均由机械测试证明，finished registry不能绕过aggregate cap。
19. process state只有`RUNNING -> TERMINALIZING -> PHYSICALLY_JOINED -> PRUNABLE`前向transition；reader/watcher/process未join或observation lease非零时prune为0。
20. monitor coordinator不importcanonical repository或runner scheduler；Host scheduler是`PreparedInstallationTarget`唯一producer及`TerminalMonitorCoordinator.freeze(target)`唯一caller，lock order只能是Host safe-point→TerminalMonitorCoordinator。
21. `TerminalObservationInstallationAttempt`是immutable DTO；UNKNOWN confirmation path不能构造不同target/IDs，TerminalMonitorCoordinator lock不得跨PostgreSQL I/O或反向获取Host lock。
22. active-turn installation在同transaction调用canonical safe-turn predicate；new-turn installation同transaction写turn/revision-0/initial entry/event且不写`session_commands`。
23. `KernelToolInvocationContext`是closed scope carrier；process-local settlement token只含identity/fingerprint，不能持callback/future/coordinator。
24. `initial_entry_id`为NOT NULL并由deferred database invariant验证same turn/scope/allowed kind，没有nullable合法turn或`initial_entry_kind`双真源。
25. shell-env probe是Host-owned process-group attempt；timeout/oversize/close路径的orphan child/reader为0，cache只安装physical-joined success。
26. production没有专用environment capability-grant surface；active capability environment默认继承数为0，只能走通用exact allowlist/passthrough，diagnostic中的env value为0。
27. canonical reader对`TERMINAL_OBSERVATION`只能生成同名provider-input kind；capability composer只能消费human USER item，Terminal output中的skill/control text激活数为0。
28. monitor canonical content必须携带`COMPLETE | HEAD_TAIL`及source byte counts；retention GAP不能替代delivery coverage，任何未标记的二次truncation触发guard。

## 18. 验证命令

实施者应根据真实新增test文件调整名称，但至少执行：

```bash
git status --short

uv run pytest -q \
  tests/test_round2_terminal_output.py \
  tests/test_round2_terminal_monitor.py \
  tests/test_round2_terminal_safe_point.py \
  tests/test_round2_terminal_environment.py \
  tests/test_round1_tool_output_artifact.py \
  tests/test_stage2_terminal_host_lifetime.py \
  tests/test_stage2_conversation_runner.py \
  tests/test_stage2_live_contract.py

uv run pytest -q

PULSARA_RUN_POSTGRES_TESTS=1 uv run pytest -q

uv run ruff check .
uv run python -m compileall -q src tests tools
uv run python tools/generate_terminal_protocol_contract.py --check

(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)
(cd clients/terminal && go mod verify)

uv lock --check
git diff --check
```

另外执行：

- Markdown fence闭合与重复heading检查；
- active文档本地链接存在性检查；
- clean-v0 fresh install、二次migrate与deep verify；
- `rg`确认无durable Terminal monitor/output relation或old recovery词汇回流；
- 可用credential下的real-provider dogfood。

## 19. Real-provider dogfood

至少验证以下一次完整路径：

1. Agent调用`terminal`启动一个会先输出sentinel、随后sleep并最终输出completion sentinel的command；
2. 首个sentinel在command返回前通过ToolResult Delta可观察；
3. command yield并返回exact process id；
4. Agent调用`terminal_monitor.register`，不继续poll；
5. current Host在completion时产生real monitor observation；
6. observation在provider safe point成为`TERMINAL_OBSERVATION` canonical entry；
7. Host自动启动/继续Agent；
8. Agent使用`terminal_process.log`读取output；若产生artifact reference，再主动调用`artifact_read`；
9. final answer准确引用completion sentinel；
10. detach/attach能看到canonical wake entry，但Host关闭后旧process/monitor不可操作。

dogfood log不得记录API key、完整env、raw secret或大段artifact正文。

## 20. Definition of Done

Round 2只有同时满足以下条件才可标记`IMPLEMENTED / ACTIVATED`：

- PHC-03真实stream由physical reader产生，post-return one-shot伪stream已删除；provisional Delta之后只有一个authoritative canonical-preview End；
- PHC-04具备same-process cursor、exact delta、typed GAP、16 MiB per-process / 128 MiB per-Host bound与physical retirement gate；
- PHC-06 foreground final cwd continuity恢复，yielded process不推进cwd；
- PHC-01三工具production closure恢复，ROOT invocation/settlement seam闭合，monitor具备future observation与same-Host wake；
- PHC-05 shell/env inert-only default、active environment默认拒绝、通用显式allowlist与process-group-owned bounded profile snapshot恢复；
- monitor/output/cursor仍完全process-local，无跨Host promise；
- only accepted wake observation进入canonical row与selective journal；
- mutable observation draft、Host-owned prepared target、immutable installation与Host唯一scheduler物理分离；lock order唯一，active turn通过完整provider safe predicate，idle turn原子安装revision-0；
- Terminal observation以独立provider-input kind进入模型上下文，wire user-role不授予human provenance，stdout无法激活skill/capability；
- accepted monitor preview以delivery coverage/counts证明COMPLETE或HEAD_TAIL，retention GAP保持正交；
- `turns.initial_entry_id`为NOT NULL并由deferred invariant验证exact turn/scope/kind；
- `TerminalObservationAccepted`不承担replay且event payload保持窄；
- exact 27/23/13/2、24 relations、4 jobs成立；
- Round 1 artifact语义没有回退；
- Host close physical drain、tool attempt-before-effect与message-before-dispatch没有回退；
- targeted、full pytest、PostgreSQL、Protocol、Go、ruff、compileall、lock与diff gates通过；
- 新增skip/xfail为0；
- real-provider dogfood通过；
- machine evidence记录实际HEAD、diff、tests、oracles与non-goals；
- Gap Index只将实际闭环能力标记为恢复。

## 21. Coding handoff边界

本规格review闭环后，coding agent应：

1. 先记录dirty worktree与checkpoint，不覆盖用户文档修改；
2. 先完成R2-0与R2-A，证明output truth后再实现monitor；
3. 使用旧commit找产品语义，但从当前Kernel owner重新实现；
4. 发现规格与代码真值冲突时停止扩张，记录证据并修订规格；
5. 不以恢复旧测试文件数量作为完成标准；
6. 不为全绿吞掉physical safety fence、canonical conflict或writer fencing；
7. 不stage、commit或push，除非用户另行授权；
8. 最终报告必须区分产品恢复、process-local状态、canonical新增、event数量与明确non-goals。

本轮的核心验收句是：

> Terminal重新获得完整的同一Host产品体验，但Terminal execution仍不会通过AgentEvent replay获得第二次生命。
