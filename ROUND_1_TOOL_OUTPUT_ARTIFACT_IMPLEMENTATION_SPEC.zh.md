# Round 1：完整 Tool Output Artifact 与 `artifact_read` 实施规格

_状态：IMPLEMENTED / ACTIVATED（2026-08-11）；实现、clean-v0 identity、回归与real-provider dogfood证据见 [`round1_tool_output_artifact_activation.json`](benchmarks/suites/core/v1/round1_tool_output_artifact_activation.json)。_

## 0. 基线、目的与结论

### 0.1 两个代码基线

本轮必须同时对照两个Git tree，二者用途不同：

| 基线 | Commit | 用途 |
| --- | --- | --- |
| hard-cut前产品真值 | `5b7ad9f7ffc8565bc572180b2bde0c81ab64473a` | 找回已经存在并被测试过的tool artifact产品语义；不得照搬旧EventLog/recovery machinery |
| 当前减法Kernel | `12636e34085fae107d64f8c6247c2567e28a25d8` | 本轮实际修改基线；所有新owner、事务与读取路径必须落到当前canonical Kernel |

基线文档SHA-256如下：

```text
PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md
7f4168989f734b3cc11a59f06833a642c0edb4d06adf3dd9a1d9deeef76d2bae

STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
8a30fb3db34bff7c152f3450ce5b18c7b403e3e657fb6f53d9e2e1d87b812b4a

STAGE_3_5_IMPLEMENTATION_SPEC.zh.md
c7a44c62857761f870532e2c6fec02de1a662d0d043854e2eff0df8c04427fbe

STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md
d58e1c585c0f718a516ab4b292061393c6d71f2e1fb2475c311ce11ac5ea82e5

POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
d4ecc9bef1a3cee9b81efe00214b60c7bbb029553133e9d9ad931f8cfff25cd3
```

`POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md`在起草本规格时是用户现有的未跟踪文件；实施者不得覆盖或丢弃它。

### 0.2 为什么这是第一轮

Stage 3–5完成了真实的架构更新：conversation、tool、job与coordination current truth已经由canonical relational rows承担；selective committed event只记录accepted occurrence；raw provider与semantic block增量留在process-local live plane；execution reopen不再依赖EventLog replay。

但hard-cut也切掉了一批与旧durability machinery共址、实际属于产品层的能力。第一轮选择PHC-02：

```text
完整 tool output artifact
+ bounded artifact_read
+ capability catalog / executor最小闭合guard
```

选择它的理由是：当前工具结果在进入canonical content之前会被不可逆截断；被省略内容既不是canonical truth，也没有任何可读owner。后续terminal retained output、terminal monitor、长上下文、MCP与plan都可能依赖同一个“完整内容与bounded observation分离”的基础边界。

### 0.3 最终结论

本轮冻结以下架构：

```text
physical tool execution
        |
        v
process-local complete output candidate
        |
        +--> shared content-addressed blob publication
        |
        +--> COMPLETE | HEAD_TAIL model-facing preview
        |
        v
single Host-writer acceptance transaction
  transcript preview entry
  + tool_results row with nullable artifact edge
  + existing ToolResultAccepted occurrence
```

其中：

- transcript entry拥有provider与history实际看到的bounded preview；
- `tool_results`直接拥有该accepted result到完整blob的nullable canonical edge、artifact disposition与source coverage；
- shared blob拥有immutable content bytes；
- `ToolResultAccepted`继续只表达“这一tool result在sequence N被接受”，不复制正文，也不证明row已经存在；
- `artifact_read`通过canonical relation鉴权后读取bounded slice；
- reopen仍读取canonical rows，不replay artifact event；
- 本轮不增加CommittedAgentEvent或LiveAgentEvent类型。

这不是恢复旧三层EventLog。它是在当前canonical Kernel上补回一个被误删的产品内容能力。

## 1. 必须保持的上位架构约束

本轮所有实现必须同时满足：

1. canonical relational row负责“现在是什么”；selective journal负责“何时接受了什么”；live plane负责当前进程体验。
2. tool-request assistant message完整提交前，physical tool adapter不可达。
3. tool attempt在physical effect前接受；artifact publication不是effect admission owner。
4. complete tool result、artifact canonical relation与`ToolResultAccepted`由同一Host writer在同一PostgreSQL transaction接受。
5. blob可在acceptance transaction之前幂等发布；孤儿blob由现有bounded GC处理，不能为此新增receipt、hold、projection或repair owner。
6. committed event payload不复制完整tool output、preview正文或blob body。
7. artifact不参与execution recovery；crash前未接受的artifact不得被event replay补成历史result。
8. `artifact_id`是受scope校验的lookup handle，不是bearer capability，不等于blob id，也不暴露storage identity/private URL。
9. ordinary hook/plugin默认不得仅凭artifact id读取正文；需要正文的扩展必须有独立、可撤销的content-read capability。
10. `artifact_read`是普通read-only model tool，不是pre-commit policy，也不是durable background job。
11. 26个Committed events、23个Live events、13个subject slots是Stage 2 activation oracle，不是长期枚举上限；本轮恰好无需增加event或subject。
12. 两类append guard保持不变：`HostWriterGuard | JobAttemptClaimGuard`。普通tool、hook或artifact reader不得自行append committed event。

## 2. 当前代码真值

### 2.1 已经存在且可复用的基础

当前Kernel已经具有：

- [`src/pulsara_agent/conversation_kernel/blob.py`](src/pulsara_agent/conversation_kernel/blob.py)：workspace-scoped、content-addressed immutable blob，16 MiB hard bound，exact read与bounded byte range read；
- [`src/pulsara_agent/conversation_kernel/repository.py`](src/pulsara_agent/conversation_kernel/repository.py)：`accept_tool_result()`在一个Host writer transaction中写transcript entry、`tool_results`与`ToolResultAccepted`；
- [`src/pulsara_agent/conversation_kernel/reader.py`](src/pulsara_agent/conversation_kernel/reader.py)：从canonical transcript entry重建provider input；
- [`src/pulsara_agent/conversation_kernel/runner.py`](src/pulsara_agent/conversation_kernel/runner.py)：tool-request commit、attempt acceptance、physical invoke、ToolResult live lifecycle与canonical result acceptance的顺序owner；
- [`src/pulsara_agent/conversation_kernel/tool_runtime.py`](src/pulsara_agent/conversation_kernel/tool_runtime.py)：当前真实production tool surface；
- [`src/pulsara_agent/tools/builtins/artifact.py`](src/pulsara_agent/tools/builtins/artifact.py)：仍保留`artifact_read`的`info | text`产品形状；
- [`src/pulsara_agent/capability/builtin_catalog.py`](src/pulsara_agent/capability/builtin_catalog.py)：仍保留`artifact_read` descriptor；
- [`src/pulsara_agent/ports/artifact.py`](src/pulsara_agent/ports/artifact.py)与[`src/pulsara_agent/message/blocks.py`](src/pulsara_agent/message/blocks.py)：仍保留adaptive preview和artifact ref的大部分旧DTO，但当前production Kernel没有使用它们。

这些残留类型只是参考材料。实施者必须判断并收窄其最终owner，不能因为文件仍存在就认为能力已经恢复。

### 2.2 当前真实缺口

当前happy path是：

```text
ToolExecutionResult.output
  -> _truncate_tool_result_utf8(maximum=4 MiB)
  -> KernelToolResult.content
  -> canonical transcript entry
```

这产生以下事实：

- 4 MiB之外的内容在blob publication之前永久丢失；
- 截断只有prefix与byte omission marker，没有tail；
- current canonical blob保存的也只是截断结果；
- `DirectKernelToolPort.tool_specs`不会暴露`artifact_read`；
- `ArtifactReadTool`没有production read port；
- clean schema保持24张product relations，但当前`tool_results`尚无artifact handle/blob edge/disposition/source coverage；
- resume后只能看到截断preview；
- descriptor inventory与真实executor不闭合。

### 2.3 Terminal的额外事实

当前terminal不是普通“完整字符串producer”：

- [`src/pulsara_agent/terminal_process/manager.py`](src/pulsara_agent/terminal_process/manager.py)使用8 MiB rolling buffer；超限后淘汰最早chunk；
- public snapshot再按`max_output_chars`取tail；
- `ToolExecutionResult.output`是包含status、exit code、cwd、process id与public output的JSON，不是完整process output；
- 因此在generic executor末端归档`result.output`，只能归档已经裁剪的JSON，不能恢复完整terminal observation。

本轮必须为tool-specific完整输出提供process-local candidate边界，但不顺手实现PHC-03实时streaming、PHC-04完整cursor journal或PHC-01 monitor。

## 3. hard-cut前产品真值

### 3.1 必读旧代码

实施与review时至少读取：

```bash
PRE_HARD_CUT=5b7ad9f7ffc8565bc572180b2bde0c81ab64473a

git show "$PRE_HARD_CUT:src/pulsara_agent/ports/artifact.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/ports/tool_execution.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/tool_artifacts.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/tool_executor.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/tool_composition.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/tools/builtins/artifact.py"
git show "$PRE_HARD_CUT:tests/test_tools.py"
git show "$PRE_HARD_CUT:tests/test_tool_artifact_processing_policy.py"
git show "$PRE_HARD_CUT:tests/test_artifact_store_contract.py"
```

归档设计材料：

- [`archived_docs/TOOL_RESULT_ARTIFACT_PR1_IMPLEMENTATION_PLAN.zh.md`](archived_docs/TOOL_RESULT_ARTIFACT_PR1_IMPLEMENTATION_PLAN.zh.md)；
- [`archived_docs/TOOL_RESULT_ADAPTIVE_PREVIEW_IMPLEMENTATION.zh.md`](archived_docs/TOOL_RESULT_ADAPTIVE_PREVIEW_IMPLEMENTATION.zh.md)。

### 3.2 应找回的产品语义

旧代码确认的有效产品语义包括：

- generic长tool output与terminal完整observation共享一种artifact协议；
- 完整artifact authority与model-facing preview分离；
- 中等输出可完整展示，避免无意义的额外read；
- 大输出展示head与tail，中间有明确omission marker；
- marker明确告诉Agent完整artifact id及如何调用`artifact_read`；
- `artifact_read`支持info与offset/limit text slice；
- artifact read跨session表现为not found；
- read result不递归产生新artifact；
- terminal preview保留status、exit code、cwd与process id等小型结构字段；
- UTF-8 bytes用于retention阈值，字符数用于preview与read offset。

### 3.3 禁止找回的旧machinery

不得移植或换名恢复：

- universal EventLog artifact carriers；
- artifact receipt、hold、finalization、projection-ready或delivery ACK graph；
- RuntimeSession reducer/checkpoint/reconciliation；
- artifact event replay；
- tool execution result由event slice重新组装为canonical truth；
- consumer成功作为artifact/result acceptance gate；
- `.pulsara/terminal-output`私有持久目录；
- second durable artifact index与shared blob并行充当内容真源。

旧代码用于恢复产品语义，不用于恢复旧authority topology。

## 4. 本轮范围与非目标

### 4.1 本轮必须完成

1. 删除当前tool result在artifact处理前的lossy 4 MiB prefix truncation。
2. 增加process-local完整输出candidate。
3. 对超过retention阈值的文本tool result发布shared blob。
4. 冻结`COMPLETE | HEAD_TAIL`两种model-facing preview策略。
5. 将artifact handle、blob FK、availability与source coverage直接并入canonical `tool_results`；不增加新表。
6. 在现有`accept_tool_result()` transaction中原子接受entry、tool result artifact edge与existing occurrence。
7. 将`artifact_read`绑定到production tool surface。
8. 支持session-scoped `info | text(offset_chars, max_chars)`读取。
9. detach/attach后仍可从canonical preview调用`artifact_read`读取同一内容。
10. generic tools与当前terminal/terminal_process至少在它们实际拥有完整sanitized observation时共享同一协议。
11. 建立descriptor-to-executor闭合guard，避免再次出现“catalog有名字、production不可达”。
12. 为tool result acceptance增加process-local immutable candidate与stateless exact confirmation，覆盖ACK-unknown且不重跑physical tool。

### 4.2 明确不做

- Go TUI artifact viewer、download UI或copy UX；
- Standalone Canonical Inspector或Legacy Python REPL；
- terminal实时stdout/stderr streaming；
- terminal cursor、retained delta、跨调用日志continuation或monitor；
- terminal跨Host重绑；
- object storage/S3；
- binary screenshot、HTML bundle或多artifact tool result；
- 独立`tool_result_artifacts`表或第二套artifact authority；
- artifact显式删除、retention policy UI或memory ingestion；
- exact provider-input audit；
- 新的CommittedAgentEvent、LiveAgentEvent、subject slot或append guard；
- 通用plugin可自定义artifact retention policy；
- 超过physical hard bound的无界输出承诺；
- 为artifact publication失败增加durable retry job。

Memory子系统仍按用户决定单独重设计，不进入本轮。

## 5. 两种展示策略

### 5.1 三个正交维度

Review确认不能用一个`COMPLETE | HEAD_TAIL`枚举同时表达“源是否完整”“artifact是否可读”和“preview如何展示”。本轮冻结三个正交closed union：

```text
ToolOutputSourceCoverage =
    COMPLETE
  | RETAINED_SNAPSHOT

ToolOutputArtifactDisposition =
    NOT_REQUIRED
  | AVAILABLE
  | INCOMPLETE
  | UNAVAILABLE

ToolResultDisplayKind =
    COMPLETE
  | HEAD_TAIL
```

语义分别为：

- `source_coverage`回答candidate body是否覆盖该次tool observation的完整输出。`RETAINED_SNAPSHOT`表示owner只能证明当前retained body，不能证明原始prefix或全生命周期输出；
- `artifact_disposition`回答完整/retained正文能否由独立artifact继续读取。`NOT_REQUIRED`用于小型、已完整进入transcript的普通结果；`AVAILABLE`有完整source blob；`INCOMPLETE`有可读blob但source coverage只是retained snapshot；`UNAVAILABLE`表示tool outcome已知且preview可提交，但需要的独立artifact留存失败、超限或无法确认；
- `display_kind`只回答canonical transcript preview如何从当时的candidate body产生，不对source coverage或artifact availability作隐含承诺。

因此`DisplayKind.COMPLETE`的精确定义是：

- model-visible body包含当时candidate body的全部文本；
- 它不自动等于`SourceCoverage.COMPLETE`；
- terminal retained suffix可以是`RETAINED_SNAPSHOT + INCOMPLETE + COMPLETE`，但必须同时显示固定coverage warning；
- 若结果因retention阈值归档，正文后仍可带短artifact footer。

`DisplayKind.HEAD_TAIL`的精确定义是：

- model-visible body为candidate body的UTF-8-safe head + omission marker + tail；
- marker是preview的一部分，整个preview受同一字符预算约束；
- 只有`AVAILABLE | INCOMPLETE`且存在artifact handle时，marker才可给出`artifact_read`调用；
- `HEAD_TAIL + UNAVAILABLE`必须明确说明省略内容不可读取；任何`UNAVAILABLE`组合都不得生成虚假的`artifact_id`；
- 任何head prefix都不得标记为complete JSON或完整输出。

历史`head_tail`与`head_tail_huge`在产品语义上都是`HEAD_TAIL`。本轮不把`head_tail_huge`继续冻结为第三种schema/event状态；未来若根据负载缩小超大输出的preview预算，只能作为数值策略变化，不能改变两态协议。

### 5.2 Retention与display的组合

默认值冻结为：

```text
artifact_archive_threshold_bytes = 8_000
complete_display_body_chars      = 32_000
head_tail_preview_chars          = 8_000
head_ratio                       = 0.65
tail_ratio                       = 0.35
artifact_read_default_chars      = 20_000
artifact_read_hard_chars         = 32_000
canonical_tool_result_preview_hard_bytes = 65_536
artifact_content_hard_bytes      = 16 MiB  # 复用当前canonical blob hard bound
```

对应行为：

| 完整输出 | Artifact | Display |
| --- | --- | --- |
| UTF-8 bytes `<= 8,000`且source完整 | `NOT_REQUIRED` | 若最终envelope `<= 65,536` bytes则`COMPLETE`，否则确定性降为`HEAD_TAIL` |
| bytes `> 8,000`、chars `<= 32,000`且publication成功 | `AVAILABLE` | 若最终envelope `<= 65,536` bytes则`COMPLETE`，否则确定性降为`HEAD_TAIL`；附短read reference |
| chars `> 32,000`且完整内容在16 MiB内、publication成功 | `AVAILABLE` | `HEAD_TAIL`，默认总预算8,000 chars |
| Terminal只持有retained body、blob publication成功 | `INCOMPLETE` | 对retained body使用`COMPLETE | HEAD_TAIL`，并显示coverage warning |
| 完整内容超过16 MiB或publication最终不可用 | `UNAVAILABLE` | 对已知candidate使用`COMPLETE | HEAD_TAIL`；保留tool outcome，明确没有可继续读取的独立artifact |

阈值单位不得混用：

- archive与blob hard bound按UTF-8 bytes；
- preview、head/tail、`offset_chars`与`max_chars`按Python Unicode code points；
- source coverage完整且计数可证时，marker可报告candidate chars与UTF-8 bytes；`RETAINED_SNAPSHOT`不得伪造原始总数或原始start offset；
- 切分不得产生非法UTF-8或拆坏surrogate policy；process-local tool output contract应拒绝非可编码Unicode，而不是悄悄把raw truth替换成`errors=replace`结果。

`complete_display_body_chars`是产品display的字符预算，不是canonical写入的物理许可。最终tool-result preview必须把正文、envelope、marker、warning与artifact footer一起按UTF-8编码后验证不超过65,536 bytes。超限时必须使用UTF-8-safe的head/tail再次确定性收紧，直至可以直接建立`InlineContent`。这个降级只改变`display_kind`，不改变tool outcome、`source_coverage`或`artifact_disposition`；final preview不得再经过会转向blob的通用content publisher。

### 5.3 Marker最小语义

`AVAILABLE`完整source的推荐文本形状如下；具体标点可调整，但字段语义不可丢失：

```text
[OUTPUT TRUNCATED / PREVIEW: omitted 120542 chars from the middle.
Full retained output: artifact_id=artifact:tool-result:...
Use artifact_read({"artifact_id":"...","offset_chars":5324,"max_chars":20000})
to inspect content after the visible head.]
```

`suggested_offset_chars`必须等于visible head结束位置，不能默认建议从0重复读取已经看过的prefix。

`INCOMPLETE`必须额外包含等价固定警示：

```text
[SOURCE COVERAGE: retained snapshot only; earlier output is unavailable.
artifact_read offsets are relative to the retained artifact body, not the original process stream.]
```

`HEAD_TAIL + UNAVAILABLE`必须使用不含artifact handle的固定警示：

```text
[OUTPUT RETENTION UNAVAILABLE: the tool outcome is known and accepted, but omitted
output could not be retained. Do not retry the tool automatically.]
```

`COMPLETE + UNAVAILABLE`已在canonical preview中展示当时candidate的全部正文，不得声称有内容被omitted；它使用独立警示：

```text
[ARTIFACT UNAVAILABLE: the complete candidate output is shown inline, but no
separate readable artifact was retained. Do not retry the tool automatically.]
```

Marker/footer是model-visible文本，不是持久化wire grammar。写入时由typed dimensions渲染，读取时不得反向解析marker来恢复disposition、coverage、display kind或计数。

所有`artifact_read.offset_chars`都相对实际stored blob body。对`RETAINED_SNAPSHOT`，offset 0是retained body的开头，不是原始process output的开头。

### 5.4 Structured tool output

本轮不要求把任意JSON拆成新的durable structured schema。规则是：

- generic工具的完整model-visible字符串是artifact source；
- terminal类工具保留小型status/exit code/cwd/process id envelope，只对其中output字段应用adaptive preview；
- 若head-tail使原JSON不再完整，最终model-visible值必须是明确的preview envelope，不能伪称仍是原tool JSON；
- artifact正文保存tool owner交出的sanitized candidate body，而不是preview envelope；其是否覆盖完整source由`source_coverage`独立表达。

## 6. Process-local输出candidate

### 6.1 最小typed boundary

在[`src/pulsara_agent/ports/tool_execution.py`](src/pulsara_agent/ports/tool_execution.py)恢复一个收窄后的process-local DTO，概念形状为：

```text
ToolOutputArtifactCandidate
  role = OUTPUT
  text
  source_coverage = COMPLETE | RETAINED_SNAPSHOT
  source_coverage_reason?
  original_utf8_bytes?
  source_format_hint? = TEXT | JSON  # process-local only
```

`ToolExecutionResult`最多携带一个primary output candidate。本轮不恢复多artifact数组、screenshot、page HTML或任意metadata map。

所有Round 1 primary output blob统一使用：

```text
media_type = text/plain
codec      = utf-8
```

JSON只是process-local source-format hint，用于构造正确的preview envelope，不进入blob physical identity。这样相同workspace、相同bytes的primary output始终以相同media type/codec发布，避免当前`workspace + digest` blob id与media-type exact-confirm冲突。本轮不修改全局blob identity算法。

约束：

- candidate只存在于当前physical invocation调用栈；
- `source_coverage=COMPLETE`当且仅当`source_coverage_reason IS NULL`；
- `source_coverage=RETAINED_SNAPSHOT`当且仅当`source_coverage_reason`为closed reason，Round 1唯一允许值为`TERMINAL_RETENTION_GAP`；
- candidate不进入event metadata、serializer、schema registry或LiveAgentEvent payload；
- callback、recorder、writer owner或port实例不得被放入candidate；
- generic tool没有candidate时，processor以`ToolExecutionResult.output`作为完整source；
- `artifact_read`显式标记为source read，结果不得生成新artifact；
- candidate text必须已经通过该工具的产品级sanitization/redaction；raw terminal bytes、secret carrier与private URL不得因“完整保存”而绕过敏感边界。

### 6.2 Terminal producer契约

Terminal必须提供两份不同视图：

```text
public TerminalResult
  bounded status envelope + output preview

private ToolOutputArtifactCandidate
  当前调用实际观察到的sanitized combined output
```

其中：

- foreground process在terminal owner完整持有其输出、且未越过retention hard bound时，candidate必须是完整combined output；
- `terminal_process log/poll/wait/write/submit/close_stdin/kill`的candidate表示“该次tool observation的完整sanitized snapshot”，不谎称是仍运行process的最终全生命周期输出；
- 当前8 MiB rolling buffer若已经发生early-output eviction，candidate必须`source_coverage=RETAINED_SNAPSHOT`并给closed `source_coverage_reason=TERMINAL_RETENTION_GAP`；
- 本轮不得把8 MiB buffer暗中改造成旧durable terminal spool/cursor journal；PHC-04后续单独设计；
- public preview必须从candidate构造，不能继续从已经tail-clipped的`result.output`二次归档；
- status、exit code、cwd、process id、timed_out与yielded状态仍完整保留在preview envelope中。

`RETAINED_SNAPSHOT`是source coverage边界，不是第三种display strategy。它必须对Agent可见，且不得出现暗示完整原始process output的字样或坐标。

## 7. Canonical schema

### 7.1 Authority保持合并

上位架构已经冻结：

```text
tool_result_artifacts并入global blobs + transcript/tool-result FK
```

因此Round 1不新增第25张表。唯一物理边界为：

- `transcript_entries`：唯一model-facing preview bytes authority；
- `tool_results`：result outcome及其nullable primary artifact edge、artifact disposition、source coverage、display kind与两类独立reason；
- `blobs`：唯一body、digest、size、media type与codec authority；
- `agent_events`：现有`ToolResultAccepted` occurrence，不复制artifact字段。

这避免恢复已被hard-cut删除的独立artifact index/authority。

### 7.2 `tool_results`最小扩展

实施SQL可以调整列顺序，但必须直接在`pulsara_v3.tool_results`表达：

```text
workspace_id                    text not null
output_artifact_disposition     NOT_REQUIRED | AVAILABLE | INCOMPLETE | UNAVAILABLE not null
output_artifact_id              text nullable unique
output_artifact_blob_id         text nullable
output_source_coverage          COMPLETE | RETAINED_SNAPSHOT not null
output_display_kind             COMPLETE | HEAD_TAIL not null
output_source_coverage_reason   nullable closed reason
output_artifact_unavailability_reason nullable closed reason
```

Round 1的两类closed reason严格分属不同轴：

```text
ToolOutputSourceCoverageReason =
    TERMINAL_RETENTION_GAP

ToolOutputArtifactUnavailabilityReason =
    ARTIFACT_CONTENT_TOO_LARGE
  | BLOB_PUBLICATION_FAILED
  | BLOB_PUBLICATION_UNCONFIRMED
```

例如terminal只剩retained snapshot，且随后blob publication又失败时，同一row必须能同时表达：

```text
output_source_coverage = RETAINED_SNAPSHOT
output_source_coverage_reason = TERMINAL_RETENTION_GAP
output_artifact_disposition = UNAVAILABLE
output_artifact_unavailability_reason = BLOB_PUBLICATION_FAILED
```

数据库约束至少包括：

- `(session_id, workspace_id)`精确FK到sessions；
- `(output_artifact_blob_id, workspace_id)`精确FK到blobs；
- `output_artifact_id`使用partial unique constraint/index，且只是logical handle；
- disposition、coverage与display kind三轴物理上均为`NOT NULL`，不允许PostgreSQL `CHECK`因NULL结果为UNKNOWN而放行非法row；
- `COMPLETE` coverage当且仅当`output_source_coverage_reason IS NULL`；`RETAINED_SNAPSHOT`当且仅当coverage reason非NULL；
- `UNAVAILABLE`当且仅当`output_artifact_unavailability_reason`非NULL；其他disposition的unavailability reason必须为NULL；
- `NOT_REQUIRED`：artifact id/blob与unavailability reason均NULL，source coverage必须`COMPLETE`；
- `AVAILABLE`：artifact id/blob非NULL，source coverage=`COMPLETE`，unavailability reason为NULL；
- `INCOMPLETE`：artifact id/blob非NULL，source coverage=`RETAINED_SNAPSHOT`，unavailability reason为NULL；
- `UNAVAILABLE`：artifact id/blob均NULL，unavailability reason非NULL；source coverage及其独立reason保留tool owner能够证明的值；
- tool-result transcript entry必须用数据库约束保证canonical preview为inline content（例如`entry_kind = TOOL_RESULT -> inline_content IS NOT NULL`），不得让preview再依赖artifact所用的blob publication路径；
- 每个tool result最多一个primary output artifact edge；
- 任何application自由字符串都不能绕过disposition、coverage、display kind与两类reason的closed vocabulary。

`tool_results`不复制：

- blob digest、size、media type或codec；这些只从exact blob读取；
- head/tail/original/omitted计数；这些只属于写入时preview builder的验证值，不是durable parser contract；
- storage identity、private URL、callback或owner metadata。

### 7.3 Preview与artifact observation

`ToolResultAccepted`已经以exact result entry为subject。Gateway/query可从entry → tool result → optional blob读取typed projection。把artifact descriptor复制进event payload会造成row/event双真源，因此event payload保持现状。

Read-time projection可以返回：

```text
artifact_disposition
artifact_id?
source_coverage
display_kind
source_coverage_reason?
artifact_unavailability_reason?
exact blob descriptor?  # 仅AVAILABLE/INCOMPLETE且capability允许
```

Gateway直接从logical join后的`tool_results`读取三个typed dimensions与两类reason，preview文本则原样读取canonical entry bytes。不从marker/footer反向解析任何schema field；标点或文案调整不得改变durable语义。event不得用于证明tool result或artifact edge已经真实。

### 7.4 Relation与oracle数量

clean product relations保持24。以下Round 1 oracle为：

```text
product relations = 24
Committed events  = 26
Live events       = 23
subject slots     = 13
append guards     = 2
durable jobs      = 4
```

Round 1会改变`tool_results`列、CHECK/FK、expected catalog fingerprint与baseline identity，但不改变relation manifest成员或runtime table grant集合。

### 7.5 当前未发布基线的数据库策略

Pulsara不会在hard-cut与本轮补齐途中发布兼容版本，且当前PostgreSQL被用户明确授权完整reset。因此Round 1采用：

- 直接修订clean version-0 baseline中的`tool_results`；
- 重生成expected catalog、baseline contract与Binding v2 identity；grant artifact只有在真实grant fingerprint变化时才更新；
- 旧Round 1前local database得到typed `RESET_REQUIRED`；
- 按更新后的clean-baseline runbook reset并一次安装；
- 不为本轮建立compat dual write、online ALTER migration或通用migration receipt graph。

这是本轮pre-release cutover选择，不是“未来所有产品schema变化都必须reset”的长期承诺。首次对外兼容发布前，正常schema evolution策略需另行冻结。

## 8. 写入与接受顺序

### 8.1 Happy path

一条tool result必须严格按以下顺序：

1. complete assistant tool-request message已经canonical commit。
2. permission/capability decision完成。
3. tool attempt已经canonical accept。
4. physical tool adapter执行。
5. adapter返回已知tool outcome、public result与可选typed output candidate。
6. artifact processor先于任何lossy truncation选择exact source。
7. 若需归档且content未越过16 MiB，按workspace发布固定`media_type=text/plain, codec=utf-8` blob；ACK unknown只对同一content-addressed candidate做bounded retry/exact-confirm，绝不重跑tool。
8. 根据结果形成`NOT_REQUIRED | AVAILABLE | INCOMPLETE | UNAVAILABLE` disposition。publication失败或超限只改变artifact disposition，不改变已经取得的tool outcome。
9. 构造最终不超过65,536 UTF-8 bytes的inline preview，再固定全部result IDs/time、closed side branch与`PreparedToolResultAcceptance`。
10. 发出process-local ToolResult Delta/End；其正文是final preview，End仍不是canonical acceptance proof。
11. `accept_tool_result(candidate)`在一个Host writer transaction中写：
    - transcript preview entry；
    - 含nullable artifact edge、三轴typed state与两类reason的`tool_results`；
    - existing `ToolResultAccepted` occurrence。
12. write抛出任何可能代表ACK unknown的异常时，以同一个candidate调用stateless `confirm_tool_result_winner()`；只有exact winner成立才继续，absent才重试同一canonical write或中断，conflict必须fail closed。
13. commit后下一次provider input与detach/attach query都只读canonical rows。

### 8.2 `PreparedToolResultAcceptance`

当前runner只提前固定`result_entry_id`，却在repository调用参数中临时生成`result_id`与`occurred_at`，不足以exact-confirm。Round 1必须增加process-local immutable candidate，至少覆盖：

```text
PreparedToolResultAcceptance
  session_id
  workspace_id
  result_id
  result_entry_id
  turn_id
  assistant_entry_id
  tool_call_id
  attempt_id?
  result_state
  canonical_preview_content
  artifact_disposition
  artifact_id?
  artifact_blob_descriptor?
  source_coverage
  display_kind
  source_coverage_reason?
  artifact_unavailability_reason?
  actor_id
  occurred_at
  side_branch = NoToolResultSideBranch | PreparedMemoryProposalSideBranch
  candidate_fingerprint
```

当前`accept_tool_result()`只有一个可选atomic side branch：memory proposal与它的governance job。Round 1不用自由optional fields表达，而是冻结process-local closed union：

```text
NoToolResultSideBranch
  branch_kind = NONE

PreparedMemoryProposalSideBranch
  branch_kind = MEMORY_PROPOSAL
  memory_candidate_id
  proposal_kind
  frozen_proposal_payload
  candidate_semantic_digest
  governance_job_id
  job_handler_type = MEMORY_GOVERNANCE
  intent_schema_version = memory_governance.v1
  frozen_intent_payload
  intent_digest
  automatic_intent_key
  safety_class = RETRY_SAFE
  initial_status = PENDING
  retry_policy_id
  retry_policy_version
  maximum_attempts
  attempt_timeout_ms
  provider_input_token_limit_per_attempt
  provider_output_token_limit_per_attempt
  next_eligible_at
  JobQueued occurrence draft  # subject/payload/actor/time均固定
```

`candidate_semantic_digest`覆盖exact workspace、proposal kind/payload与source entry；`intent_digest`覆盖exact governance intent。两者、所有side-branch IDs、catalog导出的有限retry字段及`next_eligible_at`都必须在第一次write前固定，并进入外层`candidate_fingerprint`；writer不得在每次retry时重新读clock生成新的eligibility time。这只是把当前已存在的同事务写入纳入ACK-unknown确认，不重设计memory，不新增side-branch catalog，也不引入durable receipt。

所有session/workspace、ID、actor、time、preview bytes、artifact edge、side branch与candidate fingerprint必须在第一次write前固定。`artifact_id`推荐从stable `result_entry_id + role`派生，或使用在physical invocation前已固定的opaque id；不得从数据库自增序列、临时blob URL或callback identity生成。

`candidate_fingerprint`使用domain-separated canonical digest，例如`pulsara:prepared-tool-result-acceptance:v1`，覆盖上述closed semantic payload；大正文只以exact digest/size进入fingerprint，不复制body。callback、connection、clock function、writer对象与异常实例不得进入fingerprint。

同一result acceptance重试：

- exact相同candidate视为同一winner；
- 相同artifact id指向不同content必须fail closed；
- 不得创建第二个tool result、替换artifact edge或追加第二个`ToolResultAccepted`。

`PreparedToolResultAcceptance`只存在于当前调用栈。它不是durable receipt、repair record、replay carrier或crash-resumable state。

### 8.3 Stateless exact confirmation

`confirm_tool_result_winner(candidate)`是只读canonical确认，语义参照现有`confirm_assistant_message_winner()`：

- 用Host writer guard作session/generation scope校验，但不取得新的write authority；
- exact比较entry kind/content、tool result IDs/call/attempt/state、session/workspace、artifact disposition/edge/coverage/display/reasons、actor/time与event payload；
- `NoToolResultSideBranch`要求该candidate预期的memory candidate/job/`JobQueued`集合为空；不扫描或推断与本tool result无关的memory rows；
- `PreparedMemoryProposalSideBranch`要求exact memory candidate row、exact durable job row与exact `JobQueued` occurrence全部存在且字段/digest相同；任何部分存在、多出或漂移都是canonical conflict；
- canonical entry/result row是winner truth；同事务occurrence只作为一致性校验，event不得反向证明row；
- row不存在返回`None`；exact相同返回accepted winner；任一字段漂移抛`ConversationKernelConflict`；
- 不写confirmation receipt，不安装repair，不触发tool、artifact或event replay。

### 8.4 Blob不是接受事务的证明

blob publication可以先于canonical transaction，因为blob是immutable、content-addressed且可由GC回收。必须保持：

- blob存在不代表tool result accepted；
- `tool_results.output_artifact_blob_id`才表达accepted result与blob的canonical relation；
- event也不能反向证明artifact edge；
- transaction rollback后不得由scanner自动补写artifact edge；
- orphan GC不能读取event来决定blob引用。

### 8.5 Transaction与event预算

有artifact的tool happy path：

- 不增加canonical acceptance transaction数量；
- `accept_tool_result()`仍只插入现有entry与tool result rows，只扩展后者字段；
- shared blob publication是acceptance前的idempotent content write/confirm；
- committed event数量不增加；
- live Start/Delta/End数量与类型不增加；
- 不增加owner family、durable job或close drain owner。

## 9. `artifact_read`产品契约

### 9.1 Production binding

`artifact_read`只有在以下条件全部成立时才可进入model tool specs：

- session-scoped canonical artifact query port已经绑定；
- exact blob reader已经绑定；
- capability policy将它判定为read-only；
- descriptor、JSON schema与executor identity一致。

若绑定不可用：

- production tool specs隐藏descriptor，或返回typed `TOOL_UNAVAILABLE`；
- 不允许“descriptor可见但调用落入KeyError/静态说明”的半绑定状态。

建立通用architecture guard：每个advertised builtin descriptor必须有exact production executor；有executor但不advertise可以是内部能力，反向不允许。闭合比较不能只看tool name，至少必须覆盖：

```text
descriptor id/version
descriptor fingerprint
JSON Schema fingerprint
binding/executor identity
read-only/concurrency/permission contract
```

当前descriptor中`artifacts[]`、`output_preview`属于旧wire形状。本轮必须改成与canonical inline marker/footer一致的描述：tool result可能直接给出单一`artifact_id`与建议调用；`UNAVAILABLE` marker没有artifact id，不能调用read。

### 9.2 Request

Round 1保留两个mode：

```text
artifact_read(
  artifact_id,
  mode = "text" | "info",
  offset_chars = 0,
  max_chars = 20_000,
)
```

约束：

- `artifact_id`非空；
- `offset_chars >= 0`；
- `1 <= max_chars <= 32_000`；
- `mode=info`忽略offset/max但仍校验scope；
- 未知mode、负offset、超限max返回typed application error，不抛出未处理异常。

这些bound必须物理进入advertised JSON Schema，而不只在executor中二次校验。最小wire schema为：

```json
{
  "type": "object",
  "properties": {
    "artifact_id": {"type": "string", "minLength": 1},
    "mode": {"type": "string", "enum": ["text", "info"], "default": "text"},
    "offset_chars": {"type": "integer", "minimum": 0, "default": 0},
    "max_chars": {"type": "integer", "minimum": 1, "maximum": 32000, "default": 20000}
  },
  "required": ["artifact_id"],
  "additionalProperties": false
}
```

### 9.3 Scope与capability

读取必须在单次bounded read transaction中exact join：

```text
current session
  -> current workspace
  -> tool_results(session_id, workspace_id, output_artifact_id)
  -> exact blob descriptor
```

要求：

- current session与workspace都匹配；
- caller具备当前session的model-tool execution authority；
- artifact id本身不授权；
- cross-session、cross-workspace与不存在统一返回`not_found`，不形成枚举oracle；
- ordinary extension lease不自动继承artifact read；
- revocation在每次读取前重验，不能只在registration时校验；
- query不返回storage identity、physical table path或private URL。

### 9.4 Response

`mode=info`最少返回：

```text
status
artifact_id
role
media_type
size_bytes
artifact_disposition
source_coverage
display_kind
source_coverage_reason?
artifact_unavailability_reason?
```

`mode=text`额外返回：

```text
text
offset_chars
returned_chars
total_chars
has_more
next_offset_chars?
```

`artifact_read`只接受`AVAILABLE | INCOMPLETE`且有exact blob edge的handle。`INCOMPLETE` response必须持续携带`source_coverage=RETAINED_SNAPSHOT`与`source_coverage_reason`，不得因slice本身完整返回而改称原始source完整。`UNAVAILABLE | NOT_REQUIRED`没有可调用handle，因此正常read response的`artifact_unavailability_reason`为NULL；该字段仅为了保持typed projection闭合。

### 9.5 Bounded exact read

Round 1继续使用字符offset，保留旧产品API。实现可以在16 MiB artifact hard bound内执行一次exact digest/size校验后切char slice。`max_chars`是返回字符数上限，不是必须填满的承诺；response builder还必须把JSON/envelope算入后确保最终tool-result preview `<= 65,536` UTF-8 bytes。若requested slice中多字节code points使它超限，则只返回从`offset_chars`开始的最长UTF-8-safe prefix，并以实际`returned_chars`计算`has_more/next_offset_chars`。不得为了字符索引引入durable page receipt或artifact projection index。

若未来实测16 MiB decode成本不可接受，可另行增加disposable sparse character index；它不能成为artifact truth或读取gate。

### 9.6 不递归归档

`artifact_read`的tool result：

- 其新`tool_results` row使用`output_artifact_disposition=NOT_REQUIRED`，不创建新的artifact edge；
- 不复制source blob；
- response继续携带原`artifact_id`与`next_offset_chars`；
- 同时受32,000-char与65,536-byte两个上限；只从requested offset返回bounded prefix，不对read response再做head-tail或建立artifact；
- 仍按普通tool路径写attempt、result entry与`ToolResultAccepted`。

## 10. Provider、Live、TUI与Hook语义

### 10.1 Provider context

Provider input compiler继续只读取canonical transcript entry：

- small result看到原始完整output；
- medium result看到完整output与短artifact reference；
- large result看到head/tail、明确omission marker、artifact id与read instruction；
- artifact留存失败时仍看到已知tool outcome与bounded `UNAVAILABLE` marker，不会被降级成side-effect unknown；
- provider不会自动注入完整blob；只有模型显式调用`artifact_read`才读取更多；
- late tool result lowering与provider cut规则不变。

tool result的artifact edge不应被compiler独立拼成第二条tool message，否则会破坏provider call/result配对。

### 10.2 Live ToolResult

现有`ToolResult Start/Delta/End`保留：

- Start不可变；
- Delta与End只携带最终bounded preview，而非完整大正文；
- End携带final frozen preview text与digest；
- marker中的artifact id可以在commit前出现，但End不承诺artifact已canonical accepted；
- crash或GAP后TUI必须从canonical snapshot重建，不能用历史live End合成artifact edge；
- live observer overflow仍detach/GAP，不阻塞tool或blob publication。

本轮不新增`ArtifactStart/Delta/End`。

### 10.3 Committed observation与未来Go TUI

`ToolResultAccepted`到`ImmutableEntryProjection`的read-time projection可在未来包含bounded artifact descriptor，但必须通过exact subject join读取；stored event payload本身不复制descriptor。

本轮不要求实现Go artifact viewer。后端必须保证未来Go TUI可通过：

```text
canonical tool-result observation
  -> artifact_id
  -> bounded canonical content read
```

完成hydration，而无需raw SQL、EventLog replay或猜测blob id。

### 10.4 Hook

- ordinary post-commit hook默认看到typed/redacted result occurrence或preview；
- 完整artifact descriptor/正文需要具名capability；
- hook失败、timeout、overflow或detach不得否定canonical commit；
- 真正需要跨进程必达地处理artifact的extension必须升级为exact-four catalog中的明确durable product job，不能恢复通用hook receipt graph；
- Round 1不增加第五种job handler。

## 11. Failure与crash语义

| 故障点 | Canonical结果 | 对外行为 | 禁止行为 |
| --- | --- | --- | --- |
| tool在physical dispatch前失败 | 无attempt effect；按现有typed denial/unavailable结果 | provider得到闭合tool result | 伪造artifact |
| physical tool已返回、candidate/preview辅助处理异常 | 接受原tool outcome；artifact=`UNAVAILABLE`，正文使用bounded availability warning | provider知道tool执行结果与留存失败 | 改写成side-effect unknown、自动重跑tool |
| candidate chars在32,000内，但最终preview/envelope编码超过65,536 bytes | 已知outcome、coverage与artifact disposition不变；仅把display确定性降为UTF-8-safe `HEAD_TAIL` | canonical entry仍是inline且不超限 | 将preview再送入blob publisher、拒绝已知tool result |
| exact完整output超过16 MiB | 接受原tool outcome；artifact=`UNAVAILABLE/ARTIFACT_CONTENT_TOO_LARGE` | 可显示bounded head/tail，但明确省略内容不可读取 | 冒充完整artifact、自动重跑tool |
| terminal owner已发生8 MiB retention gap | 接受`INCOMPLETE/RETAINED_SNAPSHOT` edge | preview与read均显示`source_coverage_reason=TERMINAL_RETENTION_GAP`，offset相对retained body | 声称完整原始output或原始offset |
| terminal retention gap后blob publication又失败 | 接受`UNAVAILABLE + RETAINED_SNAPSHOT`，同时写coverage reason与unavailability reason | provider看到retained preview、coverage warning与不可读warning | 丢掉任一reason、写`INCOMPLETE`却无blob edge |
| blob publication明确失败 | 接受原tool outcome；artifact=`UNAVAILABLE/BLOB_PUBLICATION_FAILED`且无blob FK | provider获得闭合tool result；不自动重试effect | 降级成may-have-executed、写缺失blob FK |
| blob publication ACK unknown | 对同一content-addressed candidate做bounded retry/exact-confirm；仍不明则接受`UNAVAILABLE/BLOB_PUBLICATION_UNCONFIRMED` | 不重跑tool | 新建receipt/repair/job、换candidate content |
| existing blob与同一content identity发生deterministic descriptor/body conflict | 保留现有canonical corruption hard fence；不得吞成普通availability loss | 报typed integrity conflict，绝不重跑tool | 覆盖blob、换digest、伪造`UNAVAILABLE`继续写 |
| blob已发布、canonical transaction失败 | 用同一`PreparedToolResultAcceptance` exact-confirm或重试canonical write | orphan只在最终无edge时等待现有GC | scanner补写edge、重跑tool |
| artifact edge/FK/identity conflict | 整个result acceptance rollback并报canonical conflict | 不把冲突吞成正常availability loss | event单独commit、替换winner |
| result commit ACK unknown | 以完整prepared candidate调用stateless confirmation | exact相同视为成功，absent才重试同一write，冲突fail closed | 只比artifact id、再执行physical tool |
| crash在Live End后、commit前 | live消失；无canonical result | reopen根据attempt生成interruption closure | 从live历史合成result/artifact |
| crash在commit后 | row/event都已接受 | attach读取preview，artifact_read可读 | replayexecution |
| artifact_read cross-session | 无状态变化 | 与not found相同 | 泄露存在性 |
| blob缺失/digest/size不匹配 | 原tool result仍是accepted fact | 当前artifact_read返回typed content error；session不崩溃 | 修改canonical row“修复”、隐藏corruption |
| hook/TUI/observer失败 | canonical commit保持成功 | detach/GAP/typed request error | 回滚tool result |

必须区分两类unknown：

- tool已经返回、仅artifact retention失败：tool outcome是已知事实，必须接受bounded result与`UNAVAILABLE` disposition；
- 整个canonical result transaction因数据库不可用而始终无法write或confirm，随后进程崩溃：reopen只能看到attempt，此时才按现有规则派生may-have-executed。

artifact retention失败本身不得制造第二类unknown。

## 12. 实施切片

### R1-0：冻结inventory与negative guards

- 记录当前baseline commit与文档hash；
- 证明`artifact_read` descriptor存在但production不可达；
- 证明当前4 MiB loss发生在blob publication之前；
- 记录当前24 relations、26/23/13/2/4 oracle；
- 加入advertised descriptor → executor闭合test；
- 不修改production behavior。

### R1-A：process-local candidate与两态preview

- 收窄/重写当前残留artifact port；
- 加入最多一个primary output candidate；
- collapse `full | head_tail | head_tail_huge`为`COMPLETE | HEAD_TAIL`产品语义；
- 冻结`source coverage × artifact disposition × display kind`三个正交维度；
- primary blob physical media type统一为UTF-8 plain text，JSON只留process-local format hint；
- 实现UTF-8/字符边界、head/tail与marker unit tests；
- 删除或改名任何仍声称服务“model lifecycle recovery”的artifact port文案/依赖。

### R1-B：canonical artifact edge与read repository

- 修改clean v0 baseline；
- 将nullable artifact edge、三个NOT NULL typed dimensions与两类reason直接加入`tool_results`并增加constraints/FKs；
- 使用数据库约束保证tool-result canonical preview永远是inline，不走blob fallback；
- 保持24张relation与现有grant集合，更新expected catalog与Binding identity；
- 增加artifact prepare/query port；
- read path exact join session/workspace/result/blob；
- 本切片保持production model tool surface尚未advertise `artifact_read`。

### R1-C：acceptance transaction与runner ordering

- blob publication成功、失败或unconfirmed都先lower成closed artifact disposition；
- 构造覆盖session/workspace、全部ID/time/content/edge/reasons与closed side branch的`PreparedToolResultAcceptance`；
- 同一transaction插入entry、扩展后的tool result与existing occurrence；
- 增加stateless `confirm_tool_result_winner()`并复用同一candidate；
- 移除pre-artifact 4 MiB lossy truncation；
- Live ToolResult改为final preview；
- fault injection覆盖blob-before-row、event rollback与ACK unknown；
- event vocabulary保持不变。

### R1-D：`artifact_read` production activation

- 构造session-scoped read port；
- 把`ArtifactReadTool`加入`DirectKernelToolPort`真实tool set；
- 更新descriptor description与minimum/maximum JSON Schema；
- descriptor/schema fingerprint与executor binding closure guard全绿；
- info/text、pagination、not-found、cross-session、corruption与no-recursive tests通过。

### R1-E：Terminal primary candidate

- terminal/terminal_process从sanitized retained owner提供private candidate；
- preview保留结构字段；
- 完整foreground output在retention bound内可从artifact读取head/middle/tail；
- running snapshot不冒充final output；
- retention gap显式`RETAINED_SNAPSHOT + INCOMPLETE`，read offset相对stored body；
- 不实现cursor journal、monitor或跨Host重绑。

### R1-F：reset activation与证据

- clean reset安装更新后的single v0 baseline；
- fresh install与second verification通过；
- old binding得到typed reset-required且DDL=0；
- 更新active README/architecture中的artifact contract与baseline identity口径；relation/event数量保持不变，不重写历史activation evidence；
- 新增Round 1 activation evidence，记录baseline、relation/event数量、tests与known non-goals；
- 将[`POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md`](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)中的PHC-02更新为已恢复/剩余边界，而不是删除历史审计。

## 13. 主要修改面

预期production修改面：

- [`src/pulsara_agent/ports/tool_execution.py`](src/pulsara_agent/ports/tool_execution.py)
- [`src/pulsara_agent/ports/artifact.py`](src/pulsara_agent/ports/artifact.py)
- [`src/pulsara_agent/message/blocks.py`](src/pulsara_agent/message/blocks.py)（只保留仍有正式owner的DTO）
- [`src/pulsara_agent/tools/builtins/artifact.py`](src/pulsara_agent/tools/builtins/artifact.py)
- [`src/pulsara_agent/capability/builtin_catalog.py`](src/pulsara_agent/capability/builtin_catalog.py)
- [`src/pulsara_agent/conversation_kernel/tool_runtime.py`](src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [`src/pulsara_agent/conversation_kernel/runner.py`](src/pulsara_agent/conversation_kernel/runner.py)
- [`src/pulsara_agent/conversation_kernel/repository.py`](src/pulsara_agent/conversation_kernel/repository.py)
- [`src/pulsara_agent/conversation_kernel/blob.py`](src/pulsara_agent/conversation_kernel/blob.py)
- [`src/pulsara_agent/conversation_kernel/host.py`](src/pulsara_agent/conversation_kernel/host.py)
- [`src/pulsara_agent/terminal_process/manager.py`](src/pulsara_agent/terminal_process/manager.py)
- [`src/pulsara_agent/terminal_process/models.py`](src/pulsara_agent/terminal_process/models.py)
- [`src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql`](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)
- [`src/pulsara_agent/storage/migrations/manifest.py`](src/pulsara_agent/storage/migrations/manifest.py)
- clean catalog resource、binding identity与相应tests；grant artifact只有在observed fingerprint真实变化时才改。

若实现需要大幅修改名单之外的memory、job、subagent、Protocol v3或Go client，应先停止并说明scope expansion；不能借artifact恢复顺手重建旧runtime。

## 14. 必须有的tests

### 14.1 Preview unit matrix

- 0、阈值前一位、阈值、阈值后一位；
- archive byte阈值与display char阈值独立；
- ASCII、中文、emoji与组合字符；
- display `COMPLETE`不遗漏candidate body的任何字符，但不暗示source coverage；
- `HEAD_TAIL`同时保留真实head/tail；
- marker计入总预算；
- 最终preview/envelope计入后不超过65,536 UTF-8 bytes；20,000个emoji虽未超过32,000 chars，也确定性降为inline `HEAD_TAIL`；
- omitted/head/tail计数exact；
- suggested offset等于visible head boundary；
- 只有两种semantic display kind；
- `COMPLETE | RETAINED_SNAPSHOT` source coverage与display kind正交组合；
- `NOT_REQUIRED | AVAILABLE | INCOMPLETE | UNAVAILABLE` disposition约束；
- coverage、disposition、display kind均为NOT NULL；两类reason的iff CHECK对NULL与非法交叉组合均fail closed；
- `RETAINED_SNAPSHOT + UNAVAILABLE`可同时持有`TERMINAL_RETENTION_GAP`与`BLOB_PUBLICATION_FAILED`；
- `COMPLETE + UNAVAILABLE`文案只声明独立artifact不可用；`HEAD_TAIL + UNAVAILABLE`才声明省略内容不可读；
- read path不从marker反向解析typed dimensions或计数；
- retained snapshot marker不包含伪造的original total/start coordinate；
- current `head_tail_huge`残留不再成为第三种canonical enum。

### 14.2 Generic tool happy path

- small output保持原文且无artifact；
- 8k–32k输出完整展示，同时artifact可读；
- >32k输出head/tail，artifact读取head/middle/tail可重组为exact source；
- blob digest/size与source exact；
- same workspace相同bytes始终以固定UTF-8 plain-text media/codec dedupe，但artifact handle与tool result edge仍准确；
- >16 MiB与forced publication failure仍接受原tool outcome，写`UNAVAILABLE`且不自动重跑tool；
- forced publication failure + 20,000 emoji candidate仍生成`<=65,536` bytes的inline `HEAD_TAIL`并接受已知outcome；
- artifact creation不增加committed event；
- complete request commit与attempt-before-effect guards仍通过。

### 14.3 `artifact_read`

- info；
- default text slice；
- nonzero offset；
- end-of-content、has_more与next offset；
- max boundary；
- invalid mode/offset/max；
- unknown与cross-session indistinguishable；
- cross-workspace denied；
- binary/non-UTF8 typed unsupported（本轮不生产binary artifact）；
- missing/corrupt blob typed content error；
- result不创建新artifact；
- multibyte response中`max_chars`只是上限；实际slice按最终65,536-byte inline bound缩短，`returned_chars/next_offset_chars`精确；
- descriptor只有在executor绑定时advertise；
- advertised schema物理拒绝negative offset、zero/over-32000 max与additional property；
- descriptor/schema fingerprint与executor identity漂移使closure guard失败。

### 14.4 Terminal

- foreground output包含HEAD、中间sentinel与TAIL；public result触发head/tail，artifact可读三个位置；
- status/exit code/cwd/process id未被preview rewrite丢失；
- terminal_process running observation标明snapshot而非final；
- within retention bound `source_coverage=COMPLETE/disposition=AVAILABLE`；
- forced retention gap `source_coverage=RETAINED_SNAPSHOT/disposition=INCOMPLETE/source_coverage_reason=TERMINAL_RETENTION_GAP`；
- forced retention gap叠加publication failure时，`source_coverage_reason`与`artifact_unavailability_reason`同时保留；
- retained snapshot的offset 0精确指向stored body开头，不伪装成original stream offset 0；
- sensitive assignment、Bearer token与ANSI/CR normalization在artifact与preview保持同一sanitized contract；
- no `.pulsara/terminal-output` durable side store。

### 14.5 Transaction/crash

- blob published then transaction rollback → noartifact edge/noevent；
- artifact edge/FK conflict → entry/result/event全部rollback；
- event append conflict → row全部rollback；
- publication ACK unknown只重试/确认同一content-addressed candidate；
- result commit ACK unknown用完整`PreparedToolResultAcceptance` exact-confirm，不重跑tool；
- confirmation覆盖session/workspace、result id/time/state/preview/edge/coverage/display/reasons与closed side branch，不能只比artifact id；
- `NoToolResultSideBranch`的预期side-row/event集合为空；`PreparedMemoryProposalSideBranch`的candidate/job/`JobQueued`全部exact存在，任何部分存在均conflict；
- publication失败写`UNAVAILABLE`仍能闭合provider tool result；
- Live End before commit crash → attach only seesattempt closure；
- commit then Host crash → attach seespreview且artifact_read成功；
- live/hook/TUI failure不否定commit；
- orphan GC不删除仍被tool result edge引用的blob。

### 14.6 Historical product regression

从`5b7ad9f7`提炼而非原样搬运：

- `test_tool_executor_archives_generic_large_output`；
- `test_terminal_large_output_returns_preview_and_readable_artifact`；
- artifact slice与cross-session tests；
- source artifact read不递归归档；
- multibyte bytes-vs-chars；
- descriptor-lowered artifact policy与executor闭合。

旧EventLog、RuntimeSession、archive fixture与event-slice assembler不得成为新tests的依赖。

## 15. Static architecture guards

至少增加/更新以下guard：

```text
production advertised descriptor/schema fingerprint - production executor binding = empty
ToolResultAccepted event count delta = 0
new committed event types = 0
new live event types = 0
new subject slots = 0
new append guards = 0
new durable job handlers = 0
independent tool_result_artifacts relation = 0
tool_results artifact edge FK/UNIQUE/CHECK set present
tool_results disposition/coverage/display columns NOT NULL = true
tool_results source-coverage reason and artifact-unavailability reason are distinct = true
tool-result canonical preview always inline and <= 65,536 UTF-8 bytes = true
preview marker/footer reverse parser = 0
PreparedToolResultAcceptance side-branch union exhaustiveness = true
artifact event/receipt/hold/projection/replay owner = 0
event payload full tool output/blob body = 0
pre-artifact lossy tool truncation = 0
artifact_read recursive artifact creation = 0
cross-session artifact disclosure = 0
artifact publication failure converted to side-effect unknown = 0
blob physical media/codec variants for primary output = 1
durable PreparedToolResultAcceptance/confirmation receipt rows = 0
```

还必须证明：

- `RuntimeSession`、universal `event_log`、projection jobs、checkpoint/reducer/reconciliation生产import仍为0；
- Oxigraph/SPARQL/JSON-LD仍为0；
- Protocol v2仍为0；
- current Kernel仍是唯一production conversation authority；
- relation manifest保持exact 24，没有新增或未登记表；
- `tool_results` runtime grant不因artifact edge扩大；
- blob DELETE仍只属于现有bounded GC owner。

## 16. 验证命令

实施者应使用repository root的`uv`/`.venv`，至少执行：

```bash
uv run pytest -q tests/test_round1_tool_output_artifact.py
uv run pytest -q tests/test_stage2_conversation_runner.py
uv run pytest -q tests/test_stage2_conversation_kernel_postgres.py
uv run pytest -q tests/test_stage2_architecture.py tests/test_stage3_5_architecture.py
uv run pytest -q tests/test_stage5_clean_migration.py
uv run pytest -q

uv run ruff check .
uv run python -m compileall -q src tests tools
uv run python tools/generate_terminal_protocol_contract.py --check

(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)

git diff --check
```

若实际新增test文件名不同，应在activation evidence记录exact命令，不得用不存在的路径伪造gate。

PostgreSQL验证必须使用ephemeral或用户已授权reset的数据库，覆盖fresh install、reset-required、runtime grants与artifact happy path。

Real-provider dogfood不是unit gate的替代品，但最终应至少运行一次真实模型trajectory：让模型触发大tool output、看到head/tail marker、主动调用`artifact_read`读取中间sentinel并完成回答。证据不得记录API key或raw sensitive output。

## 17. Definition of Done

以下条件全部满足，Round 1才可宣称完成：

- 所有normal production tool result在artifact policy判断前仍保有完整process-local文本；
- 小输出保持原有直接体验；
- 中等输出完整展示且可有完整artifact；
- 大输出或最终UTF-8 envelope超过65,536 bytes的输出只使用`HEAD_TAIL`；artifact可用时给出可执行read instruction，不可用时明确说明省略内容不可读取；
- 完整artifact在当前16 MiB hard bound内逐byte等于sanitized source；
- terminal在其owner完整持有output时不再归档tail-clipped JSON；
- terminal retention gap显式`RETAINED_SNAPSHOT + INCOMPLETE`，不冒充完整原始stream；
- retention gap与artifact publication failure叠加时，coverage reason与artifact-unavailability reason同时保留；
- `artifact_read`真实出现在production model tool specs并可执行；
- info/text、offset/limit、cross-session与corruption语义闭合；
- detach/attach后artifact仍可读；
- accepted result、nullable artifact edge与existing occurrence同transaction；
- blob/event都不被用作canonical row proof；
- artifact publication failure保留已知tool outcome、写`UNAVAILABLE`，既不触发physical tool auto retry，也不降级成side-effect unknown；
- `COMPLETE + UNAVAILABLE`只声明独立artifact不可用，`HEAD_TAIL + UNAVAILABLE`才声明省略内容不可读；preview不被反向解析为schema；
- tool result ACK unknown由含session/workspace、三轴、两类reason与closed side branch的完整process-local prepared candidate及stateless canonical confirmation闭合；
- event/live/subject/guard/job数量没有为本功能扩张；
- clean schema保持24张product relations，独立`tool_result_artifacts`表为0；
- 没有新增receipt、hold、projection、repair、reducer、checkpoint、delivery ACK或execution replay；
- primary artifact blob统一UTF-8 plain-text media/codec，不修改global blob identity；
- capability descriptor、JSON Schema fingerprint与production executor exact闭合；
- full pytest、PostgreSQL、static architecture、Go既有gate与real-provider dogfood均有记录；
- PHC-02 gap index更新为“能力已恢复 + 明确剩余边界”，PHC-01/03/04等未被误报为已完成。

## 18. Coding handoff边界

Coding agent可以在本规格内自行决定：

- DTO与helper的具体文件拆分；
- SQL列顺序与index名称；
- preview builder的纯函数组织；
- test fixture名称；
- stable artifact id的安全编码细节。

但不得自行改变：

- 两种display策略；
- source coverage、artifact disposition与display kind三个正交closed union；
- 8k/32k/8k/65,536 bytes/16 MiB的初始contract值；
- session/workspace exact scope；
- one-result/one-primary-output-artifact边界；
- same-transaction acceptance；
- 已知tool outcome不得因artifact unavailable而变成side-effect unknown；
- `PreparedToolResultAcceptance`仅process-local，confirmation仅stateless read；
- existing `ToolResultAccepted`复用且event数量不增加；
- no-recursive artifact read；
- pre-release clean-reset策略；
- terminal gap必须显式incomplete；
- source coverage reason与artifact unavailability reason必须分开，并允许同时出现；
- prepared candidate的side branch只能是`NONE | MEMORY_PROPOSAL`的closed union；
- 不恢复旧EventLog/recovery machinery。

若review认为上述任何冻结项会破坏canonical Kernel、使physical effect被自动重试、或迫使恢复第二套durable state machine，应在编码前提出P1并暂停该slice。
