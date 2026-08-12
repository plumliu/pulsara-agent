# Round 3：Structured Model-Input / Context Compiler 实施规格

_状态：ACTIVATED（2026-08-12）；R3-0 至 R3-F 已在当前未提交工作树完成并通过全部 activation gate。机器证据见 [`round3_structured_model_input_compiler_activation.json`](benchmarks/suites/core/v1/round3_structured_model_input_compiler_activation.json)。_

## 0. 基线、目的与最终结论

### 0.1 两个代码基线

本轮必须同时对照两个Git tree。二者用途不同，不得把旧实现整体移植到当前Kernel：

| 基线 | Commit | 用途 |
| --- | --- | --- |
| hard-cut前产品真值 | `5b7ad9f7ffc8565bc572180b2bde0c81ab64473a` | 找回已经进入production model loop的typed source、channel、预算、render mode与provider-neutral compilation语义 |
| 当前减法Kernel | `242895dcfef1af1fcdcd1f433b28637c16020720` | Round 2完成后的实际基线；canonical cut、tool-result artifact、Terminal observation、model target和physical dispatch必须以此为唯一当前真值 |

起草时前置材料SHA-256如下：

```text
PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md
cb3e7b0a9f33e5e4c5b17850d47e1af580a3f23f094f868076351bb17a6a6e80

POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
fc32565440b94b95a90fa2266408c9d0ab914457a955d39c6a710a66d14ed71f

ROUND_1_TOOL_OUTPUT_ARTIFACT_IMPLEMENTATION_SPEC.zh.md
7b34caa305f5a5f9f5f9fda1dd1d1254bbd8d33c6116ca86f8c5bb22cbe4374b

ROUND_2_TERMINAL_RUNTIME_IMPLEMENTATION_SPEC.zh.md
0de90b7b926fa53080729b4462946a4715429e9c3530ed49524c5b5f3f4532c4

STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
8a30fb3db34bff7c152f3450ce5b18c7b403e3e657fb6f53d9e2e1d87b812b4a

STAGE_3_5_IMPLEMENTATION_SPEC.zh.md
c7a44c62857761f870532e2c6fec02de1a662d0d043854e2eff0df8c04427fbe

STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md
d58e1c585c0f718a516ab4b292061393c6d71f2e1fb2475c311ce11ac5ea82e5
```

`POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md`在起草时已有未提交修改，其中包含PHC-17及permission/Plan边界的最新讨论结论。实施者不得丢弃、覆盖或用HEAD中的旧版本替换它。实施开始前应记录实际checkpoint HEAD与所有输入文档的新hash；上面的hash只证明本规格的起草输入。

### 0.2 为什么Round 3选择Prompt Compiler

当前Kernel已经能把canonical transcript发给provider，也会在发送前估算token。但这并不等于存在Context Compiler。当前路径实质是：

```text
exact canonical transcript
    + base system / catalog / active skill字符串拼接
    + exact tool specs
    -> adapter构造完整LLMContext
    -> 超限则整体失败
```

hard-cut前production路径已经具备typed multi-source、channel、budget class、source-owned render mode、deterministic degradation与source-level attribution。它们与旧EventLog、exact-input audit、provider generation recovery共址，因而在hard-cut中一起被切掉。

Round 3恢复的是前一组产品能力，不恢复后一组durability machinery。它也是后续PHC能力的共同接入点：

```text
Plan / permission guidance
MCP diagnostics / resources
failure / interruption note
tool timing / freshness
compaction-selected canonical snapshot
        |
        v
typed source boundary
        |
        v
one provider-neutral compiler
```

如果没有这一边界，上述能力会分别在runner、tool renderer或provider adapter里重新拼字符串，最终再次形成多个隐式prompt owner。

### 0.3 最终拓扑

本轮目标拓扑冻结为：

```text
ProviderSafePoint freezes exact canonical cut
        |
        +--> CanonicalProviderInputReader
        |      canonical transcript/tool pairing/late outcome truth
        |
        +--> PreparedKernelToolSurface
        |      scope-filtered model surface + process-local binding access
        |
        +--> PreparedKernelModelCall
        |      transport-bearing call retained by adapter
        |      -> provider-neutral ModelInputCompileBinding
        |
        +--> KernelContextSourceCollector
               base/runtime/clock/capability facts
                         |
                         v
              StructuredModelInputCompiler
              - typed channel lowering
              - protected transcript structure
              - source-owned degradation
              - tool-result body degradation
              - exact final estimate
                         |
                         v
              FrozenCompiledModelInput
                         |
                         v
              DirectKernelModelPort
              exact join + thaw ephemeral LLMContext + dispatch only
```

唯一authority划分为：

- canonical reader拥有conversation cut、entry order、scope、tool pairing、closure与late-result lowering；
- domain/process-local source producer拥有其输入事实；
- compiler只拥有本次call的selection、render decision和provider-neutral carrier；
- transport-bearing prepared call留在Kernel adapter，拥有target、transport binding与model budget；
- provider-neutral compile binding只暴露call/target facts、estimator、budgets与scope-filtered frozen tool specs；
- Kernel tool surface access证明本次call真正advertised且可执行的binding，pure compiler永远看不到executor或live owner；
- provider adapter只做exact join、将frozen JSON thaw为一次性`LLMContext`并发送，不再决定source、channel、budget或tool surface。

### 0.4 Round 3子切片

| 子切片 | 目标 | 独立闭环 |
| --- | --- | --- |
| R3-0 | inventory与negative guards | 当前行为基线、旧machinery禁入、无schema/event增长 |
| R3-A | model prepare/dispatch split | target与tool surface先冻结，adapter不再临时编译 |
| R3-B | pure structured compiler | typed sources、channel、render mode、预算、report、fingerprint |
| R3-C | first-party source恢复 | runtime environment、clock、catalog、active skill与Terminal cwd |
| R3-D | runner hard switch | exact cut到compiled input再到physical dispatch的唯一路径 |
| R3-F | activation | 全量回归、multi-provider dogfood、evidence与PHC-17状态更新 |

R3-A可以先以dormant API和unit tests落地；在R3-D切换前，production adapter仍只能有一条有效发送路径。不得长期保留“legacy request”和“compiled request”两套可选composition。

## 1. 必须保持的上位架构约束

1. canonical relational rows继续拥有conversation semantic truth；compiler不读取`agent_events`，也不从event replay构造history。
2. selective committed journal只记录accepted product occurrence；本轮不新增`ContextCompiled`、`ModelInputPrepared`或任何proof event。
3. process-local LiveAgentEvent继续承载provider与semantic block实时体验；compiled prompt不是LiveAgentEvent。
4. reopen读取canonical rows并重新编译未来call；不承诺恢复过去某次call的逐byte input。
5. 不承诺exact context-input audit，不保存完整prompt、source pages、plan/root artifact、provider generation或prefix accumulator。
6. compiler不得成为Plan、permission、MCP、memory、tool、Terminal或subagent的第二authority。source只能投影其owner已经冻结的事实。
7. canonical transcript cut、scope和entry sequence仍由[`reader.py`](src/pulsara_agent/conversation_kernel/reader.py)拥有；compiler不得重新查询PostgreSQL。
8. tool-request message完整接受前physical tool不可达；Round 3不得改变assistant commit、attempt-before-effect或tool result acceptance顺序。
9. current user、assistant/tool-call/result pairing、provider-only unknown closure与late-result sequence不得被普通source allocation打断。
10. tool-result正文可以在本次provider projection中降级；canonical preview、artifact edge与accepted result不被改写。
11. tool schema只能来自当前Host中exact descriptor/executor closure；compiler不得从catalog名称推断tool，也不得advertise没有executor的descriptor。
12. provider target必须在compile前冻结；compiler只使用从`ResolvedModelCall`投影的provider-neutral facts/estimator seam，pre-send validator再与原transport-bearing call exact join；两侧estimator fingerprint与estimate必须相同。
13. provider adapter不得在dispatch时重新resolve model target、重新读取tool specs、重新拼system prompt或重新选择render mode。
14. source collection、compile plan与report全部process-local；Host crash后消失，不补写、不replay、不合成历史diagnostic。
15. ordinary hook、TUI、Inspector或recorder失败不得否定compile或provider call；pre-commit policy不通过ordinary hook实现。
16. compile失败发生在physical provider open之前；已接受的user entry保持canonical，当前turn按既有规则进入`INTERRUPTED`。
17. compiler不得自动创建context snapshot、删除history或推进binding revision。PHC-07拥有compaction和long-horizon continuation。
18. 本轮不修改Memory设计。Memory source不注册、不查询、不以空projection占位。
19. Plan与dynamic permission尚未恢复。本轮不得读取live mutable permission并生成“看似冻结”的permission prompt；未来必须消费send-time accepted run snapshot。
20. `AgentEvent`数量、subject slot、append guard、product relation与durable job oracle在本轮保持`27 / 23 / 13 / 2 / 24 / 4`。
21. Operational hook vocabulary可以增加一个bounded/redacted compile observation，但它不计入Committed/Live oracle，也不参与execution recovery。
22. job worker中的handler-specific JSON prompt不在本轮强制迁移；`AGENT_MODEL_LOOP`以及使用同一runner的ROOT/subagent turn是本轮production scope。

## 2. 当前代码真值

### 2.1 Canonical reader已经是正确的conversation owner

[`CanonicalProviderInputReader`](src/pulsara_agent/conversation_kernel/reader.py)在一个`REPEATABLE READ` transaction中：

- 校验exact `context_binding_revision_id`与`provider_input_through_sequence`；
- 按`conversation_scope_kind`和`scope_subagent_task_id`读取同scope entries；
- 读取inline/blob canonical content并校验size、digest、codec与UTF-8；
- 按`entry_sequence`形成user、assistant、tool request与tool result；
- 对缺失result的历史tool call，根据是否存在physical attempt生成`before_dispatch | may_have_partially_executed` provider-only closure；
- 保持late result的真实sequence，不倒插到早先assistant cut；
- 对context snapshot只读取binding已选择的base与delta。

这些行为必须保留。Round 3不能复制一个“Context Transcript Projector”，也不能为了预算重新读取raw tables。

当前reader仍有一个必须修正的semantic bug：assistant没有TEXT/DATA block时，会把parent entry中的block-manifest JSON当作assistant正文fallback。parent content只是storage carrier；Round 3不得把这一现状当作provider兼容真值。

当前reader的两个边界继续有效：

```text
maximum items          4096
maximum canonical body 16 MiB
```

它们是physical read bounds，不是model budget。reader成功只代表canonical内容可有界读取，不代表目标模型一定能容纳。

### 2.2 当前Direct model adapter承担了过多职责

[`DirectKernelModelPort`](src/pulsara_agent/conversation_kernel/direct_model.py)当前在`stream()`内部同时：

1. resolve model target；
2. resolve model call；
3. 读取构造时保存的tool specs；
4. 把reader items降为`LLMMessage`；
5. 拼入system prompt；
6. 构造`LLMContext`；
7. 估算与验证；
8. 打开physical transport。

因此在target/estimator未知时，runner无法进行真实budget allocation；compiler若放在adapter之外又无法证明最终发送的target/tools与编译时相同。

本轮必须把`prepare`与`execute`拆开，而不是在adapter里再嵌一层compiler callback。

### 2.3 当前Capability composer只是字符串拼接器

[`KernelCapabilityComposer`](src/pulsara_agent/conversation_kernel/capability.py)已经正确做到：

- 只用最后一个真实`USER` item做显式skill mention解析；
- 使用当前production tool names过滤skill projection；
- 返回catalog、active skill与diagnostics。

但最终输出只是：

```python
"\n\n".join(base_system, catalog_prompt, active_skill_prompt)
```

workspace root/kind、Terminal current cwd与clock没有进入model-visible input；catalog和active skill也没有独立budget/attribution。

当前child objective同样被编码为`USER_MESSAGE`；若只沿用“最后一个USER”的判断，parent model生成的`$skill`文本会意外触发child capability。Round 3必须以closed activation subject区分human prompt与delegated objective。

Round 3应保留skill resolution owner，拆掉最终字符串拼接责任。Terminal observation、runtime clock或其他untrusted source仍不得激活skill。

### 2.4 当前estimator与final validator可直接复用

[`llm/estimator.py`](src/pulsara_agent/llm/estimator.py)已有唯一V1 estimator，并对以下部分提供exact additive breakdown：

- system prompt；
- each message；
- tool specs；
- request envelope。

[`llm/validation.py`](src/pulsara_agent/llm/validation.py)已经校验：

- context与resolved call identity；
- target fingerprint和transport binding；
- tool support；
- provider message closed shape；
- final estimate不超过target budget；
- compiler estimate与pre-send estimate完全相等。

[`ResolvedModelCall`](src/pulsara_agent/llm/resolution.py)的target持有真实transport，full validator也会读取该binding。因此它们只能留在adapter，不得作为pure compiler DTO。

本轮不得新增第二种heuristic或用`len(text) / 4`在compiler中另算。allocation使用从resolved call投影的provider-neutral estimator seam；final validation使用原transport-bearing call。两者必须共享同一estimator contract/fingerprint并对同一frozen carrier给出exact相等结果。

### 2.5 当前runner ordering正确但缺少compile boundary

[`ConversationKernelRunner.run_accepted_turn()`](src/pulsara_agent/conversation_kernel/runner.py)当前顺序是：

```text
consume human steer
freeze provider-safe exact cut
reader.rematerialize(cut)
capability compose(latest real user)
build KernelModelRequest
begin_model_operation()
open provider stream
```

Round 3只在`reader/capability`与`begin_model_operation()`之间插入target preparation、source snapshot和pure compile。safe point owner、turn scheduler和canonical transaction顺序不变。

### 2.6 当前tool surface已有exact binding truth

[`DirectKernelToolPort.executor_bindings`](src/pulsara_agent/conversation_kernel/tool_runtime.py)已经为每个production tool保存：

- descriptor ID/version/fingerprint；
- input schema fingerprint；
- catalog/binding/availability/permission contract fingerprint；
- executor identity与binding fingerprint。

这是冻结本次tool surface的正确材料。`builtin catalog`中的dead descriptor不能进入snapshot；tool name列表也不能单独充当closure proof。

当前[`ToolSpec`](src/pulsara_agent/llm/input.py)的`parameters`仍是mutable dict，canonical reader的tool arguments也会复制为dict-backed mapping。仅把外层dataclass设为frozen或做一次deepcopy，不能阻止fingerprint之后的原地修改；第6.1节必须把两者收敛到现有frozen JSON primitive。

但当前invocation context没有advertised surface identity，invoke主要按“当前同名binding”校验；同时`terminal_monitor`会在SUBAGENT_TASK scope拒绝。Round 3因此不能宣称ROOT/child surface相同，也不能只冻结model-visible specs而让dispatch重新查当前binding。第6.2节的scope filter与process-local exact join是本轮必须补齐的安全闭环。

### 2.7 当前Runtime environment的可用事实

当前Host已持有[`ResolvedWorkspace`](src/pulsara_agent/workspace_identity.py)：

- `workspace_kind`；
- absolute resolved `workspace_root`；
- display label与workspace key。

Round 2的[`TerminalSessionManager`](src/pulsara_agent/terminal_process/manager.py)已持有same-Host default Terminal session的`current_cwd`。foreground command完成后可推进它；yielded process不会推进。

目前没有安全的窄read port把这些事实冻结给model input，也没有session timezone/clock snapshot DTO。本轮应增加process-local snapshot seam，不允许compiler伸入Terminal manager内部字段。

## 3. hard-cut前产品真值与禁止移植面

### 3.1 必读旧代码

实施与review至少读取：

```bash
PRE_HARD_CUT=5b7ad9f7ffc8565bc572180b2bde0c81ab64473a

git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_engine/types.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/compiler.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/policy.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/snapshot.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/transcript.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/provider_projection.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/sources/input.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/sources/registry.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/sources/builder.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/context_input/sources/render.py"
```

旧回归至少参考：

```text
tests/test_context_candidates.py
tests/test_context_input_facts.py
tests/test_context_transcript_projection.py
tests/test_context_input_architecture.py
tests/test_provider_input_hard_cut.py
```

归档材料：

- [`archived_docs/PULSARA_CONTEXT_ENGINEERING_COMPILER_DESIGN.zh.md`](archived_docs/PULSARA_CONTEXT_ENGINEERING_COMPILER_DESIGN.zh.md)；
- [`archived_docs/PULSARA_CONTEXT_COMPILER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_CONTEXT_COMPILER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md)；
- [`archived_docs/PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md)；
- [`archived_docs/PULSARA_PROMPT_CACHE_CONTRACT.zh.md`](archived_docs/PULSARA_PROMPT_CACHE_CONTRACT.zh.md)。

### 3.2 必须找回的产品语义

旧代码中应保留的核心语义是：

- compiler输入是已经冻结的facts，不读取mutable loop state；
- source producer、source candidate、allocation与provider lowering职责分离；
- source有closed channel、placement、degradation priority、budget class和render modes；
- system、messages、tool schemas与envelope共同参与最终budget；
- current user和tool pairing有结构保护；
- optional source在budget压力下确定性compact/ref/omit；
- tool result正文可以降级，但tool-result closure message不能消失；
- final carrier由同一个target estimator重新测量；
- compile result提供source/decision/budget attribution；
- compiler不拥有domain truth。

旧代码中下列具体测试思想仍有价值：

- candidate provider placement与degradation分别按各自closed ordinal，而非input field order；
- optional/degradable source先于required source被省略；
- malformed tool arguments原样、确定性地保留给provider；
- assistant tool call/result pairing不被source message插入破坏；
- tool body不能伪造timing或其他typed metadata；
- system prompt保留per-source ownership；
- compiler omission就是本次final provider payload truth；
- provider dispatch deep-copies/freeze exact tool schema。

### 3.3 明确禁止恢复的旧machinery

下列旧机制即使代码曾经完整，也不得进入Round 3：

- `ContextCompiledEvent`、`ContextBudgetReportEvent`或其他durable compile event；
- context plan/page/root artifact与exact request audit；
- provider-input generation、prefix accumulator、resident generation或continuation store；
- event slice、receipt、checkpoint、reducer、repair、projection ready或delivery ACK；
- historical source head/disposition replay；
- source lifecycle cache作为correctness条件；
- runtime observation durable carrier；
- provider cache命中作为semantic truth；
- old`RuntimeSession`、`RunActivationWorkingState`、`Msg` facade或EventLog transcript projector；
- source exhaustive registry要求所有未来source一次性注册；
- compile output跨Host恢复；
-为Inspector持久化完整prompt。

本轮可以使用process-local semantic fingerprint做同一次call的binding和diagnostic，但它不能被包装成新的durable audit graph。

## 4. 本轮范围与非目标

### 4.1 必须完成

Round 3必须完成：

1. model target resolution与physical stream dispatch拆分；
2. exact production tool surface按call与conversation scope冻结，并闭合到后续dispatch；
3. provider-neutraltyped source/candidate/render contract；
4. base system、runtime environment、runtime clock、capability catalog与active skill五类source；
5. current canonical reader item的唯一provider lowering owner；
6. Round 1 tool result artifact-aware `FULL | COMPACT | REF_ONLY | OMITTED_BODY` provider projection；
7. deterministic source/tool-result budget allocation；
8. final exact estimate与pre-send estimate equality；
9. bounded/redacted compile report和operational projection；
10. ROOT与SUBAGENT_TASK runner共用同一compiler contract，但使用各自scope-filtered tool surface与activation subject；
11. compile失败时zero provider physical send；
12. no-schema/no-durable-vocabulary architecture guards；
13. real-provider dogfood覆盖当前两类normalized transport。

### 4.2 明确不做

本轮不做：

- Memory source、memory recall设计或memory budget；
- Plan workflow、plan source、read-only overlay或dynamic permission snapshot；
- MCP server lifecycle、MCP resources/prompt/tool source；
- failure/interruption note产品语义；
- tool timing/freshness推导；
- context snapshot生成、compaction策略或history删除；
- subagent handoff/result的新domain projection；
- historical exact prompt查询；
- provider prefix continuation或local prompt cache；
- cross-Host compile recovery；
- Go TUI compile inspector；
- dedicated compile table、blob、job、event或subject；
- durable job handler的single-string JSON prompt重构；
-通过compiler实施tool permission。

未实现的future source不得以空字符串candidate、`UNKNOWN` JSON或dormant database row提前占位。

## 5. 最终owner与package拓扑

### 5.1 推荐package边界

新增provider-neutral纯模块：

```text
src/pulsara_agent/model_input/
    __init__.py
    contracts.py
    compiler.py
    lowering.py
    diagnostics.py
```

当前Kernel适配层：

```text
src/pulsara_agent/conversation_kernel/context_sources.py
```

职责冻结为：

| owner | 可以做 | 禁止做 |
| --- | --- | --- |
| `model_input.contracts` | provider-neutral immutable enum/DTO、frozen JSON、model-call facts、bounds | Kernel DTO、live transport、DB、filesystem、clock |
| `model_input.lowering` | canonical input snapshot与source variant到immutable provider-neutral messages | import reader/repository、查询artifact、修改canonical row |
| `model_input.compiler` | validate、allocate、estimate、build frozen carrier/report | 读取Host state、持有`ResolvedModelCall`、调用transport-aware validator、open transport |
| `model_input.diagnostics` | bounded/redacted report projection | 保存raw body或secret |
| `conversation_kernel.context_sources` | 冻结workspace/cwd/temporal/capability source，将Kernel事实适配为pure DTO | 决定model budget、发送provider |
| canonical reader | exact transcript cut与tool-result metadata，直接产出provider-neutral immutable snapshot | context source selection |
| direct model port | 持有transport-bearing prepared call；投影pure binding；最终thaw/verify/send | 把transport capability交给compiler、拼prompt、选择source/render mode |

不得重新创建`runtime/context_input/`、`runtime/context_engine/`或其他与旧graph同名的package。

### 5.2 Pure compiler定义

Pure表示：

- 输入相同且显式identity/time相同，输出字节、decision与fingerprint相同；
- 无database、filesystem、clock、environment、network或callback I/O；
- 不读取global mutable registry；
- 不生成domain fact；
- 不持有跨call cache；
- 不启动thread/task。

Pure package的import graph冻结为：

```text
model_input
  -> primitives.model_call / primitives.context frozen facts
  -> llm estimator protocol / immutable message values

conversation_kernel
  -> model_input
  -> llm resolution / live transport
```

`model_input`不得反向import`conversation_kernel`。因此compile request中不得出现`PreparedProviderInputCut`、`RematerializedProviderInput`、`PreparedKernelModelCall`或`ResolvedModelCall`；这些Kernel/transport-bearing DTO必须先由composition adapter投影成第6.3与第10.1节的provider-neutral值。

UUID、clock和source observation time必须由caller冻结后传入。compiler内部不得调用`uuid4()`或`datetime.now()`。

### 5.3 Process-local work execution

source collection和compile继续通过session-owned[`KernelSessionIO`](src/pulsara_agent/conversation_kernel/io.py)执行，以复用现有thread admission、deadline与Host close drain；本轮不新增第二个worker pool。由于`KernelSessionIO.run()`会向同步operation注入deadline参数，runner应使用一个只负责适配调用形状的窄helper调用pure compiler；helper不得改变compile input或结果。

compiler是bounded CPU work而非durable job。它必须：

- 不读取wall/monotonic clock；deadline由外层`KernelSessionIO`实施；
- 外层deadline后不打开provider；
- 若to-thread已开始，logical cancellation必须等待该bounded work退出；
- Host close继续由`KernelSessionIO.aclose()`证明无遗留thread。

## 6. Exact tool surface与model-call preparation

### 6.1 Pure frozen JSON与model-visible tool surface

外层`frozen=True`不足以冻结`ToolSpec.parameters: dict`。pure graph必须改用由[`primitives.context`](src/pulsara_agent/primitives/context.py)公开的现有`FrozenJsonObjectFact`，并冻结为：

```python
class ModelInputScopeKind(StrEnum):
    ROOT = "ROOT"
    SUBAGENT_TASK = "SUBAGENT_TASK"


@dataclass(frozen=True, slots=True)
class FrozenToolSpec:
    name: str
    description: str
    parameters: FrozenJsonObjectFact = field(repr=False)
    descriptor_fingerprint: str
    executor_binding_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenModelToolSurface:
    conversation_scope_kind: ModelInputScopeKind
    tool_specs: tuple[FrozenToolSpec, ...] = field(repr=False)
    surface_fingerprint: str
```

约束：

- tool specs按name排序且name唯一；
- Kernel composition必须把canonical `ConversationScopeKind`逐值投影为pure `ModelInputScopeKind`并exact join；pure package不得import Kernel enum；
- JSON schema在进入surface时递归freeze，fingerprint覆盖canonical JSON bytes；
- model-visible surface必须先按conversation scope过滤，不能把“会在invoke时拒绝”的tool继续advertise给模型；
- ROOT可包含当前合法的`terminal_monitor`，`SUBAGENT_TASK`必须排除该ROOT-only tool；未来scope-restricted tool也必须由同一个closed scope policy过滤；
- pure compiler与source collector只看到`FrozenModelToolSurface`，看不到executor、callback、registry或tool owner；
- thaw只允许发生在最终adapter exact join之后，每次产生一份全新的`dict`，不得把mutable object写回frozen graph或缓存。

canonical reader中的tool-call arguments也必须使用`FrozenJsonObjectFact`。lowering通过同一canonical JSON encoder形成provider-neutral arguments string；不得将reader当前的dict-backed mapping原样带入compiler。

### 6.2 Kernel-private surface access

descriptor/executor closure仍由`DirectKernelToolPort`拥有。每次model call在capability collection之前，通过一个原子方法冻结：

```python
@dataclass(frozen=True, slots=True)
class ProcessLocalToolSurfaceAccess:
    owner_epoch: int
    surface_generation: int
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    surface_fingerprint: str
    _authority: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedKernelToolSurface:
    model_surface: FrozenModelToolSurface
    executor_binding_fingerprints: tuple[str, ...]
    access: ProcessLocalToolSurfaceAccess = field(repr=False)
```

这里的`access`是当前Host内的窄revocable capability，不是新durability机制：

- 不入库、不进event metadata、不序列化、不跨Host恢复；
- 没有renewal、receipt、ACK、repair或通用lease graph；
- snapshot必须一次性join descriptor、schema、scope policy与production executor binding；任何一侧缺失都fail closed；
- owner close先禁止新borrow并取消active runner，再bounded drain已经取得的borrow和physical effect owner；
- 一个model response进入tool-call authorization前取得同一surface的bounded borrow。borrow只pin住“模型看到的binding仍是随后要执行的binding”，不能延长Host生命周期或授予额外tool；
- 每个返回tool call在authorize、attempt acceptance和physical invoke前都exact join `surface_fingerprint + binding_fingerprint + scope`；
- join在attempt acceptance前失败时返回typed `TOOL_UNAVAILABLE`，不得创建attempt或执行physical effect；取得borrow后binding不得在attempt与physical invoke之间被替换；
- ordinary hook/plugin不能取得、伪造或发布该access。

本轮不得为上述约束增加durable generation、schema column、receipt或第三类append guard。`DirectKernelToolPort.tool_specs`与`executor_bindings`应收敛为上述单一原子snapshot/borrow owner，避免两次property read之间的组合漂移。

### 6.3 Provider-neutral compile binding

pure compiler不接收`ResolvedModelCall`。Kernel adapter先把transport-bearing resolved call投影为：

```python
class ModelInputTokenEstimator(Protocol):
    fact: TokenEstimatorFact

    def estimate_text(self, text: str) -> int: ...
    def estimate_message(self, message: LLMMessage) -> int: ...
    def estimate_frozen_tool_spec(self, tool: FrozenToolSpec) -> int: ...
    def estimate_frozen_input(
        self,
        *,
        system_prompt: str,
        messages: tuple[LLMMessage, ...],
        tools: tuple[FrozenToolSpec, ...],
    ) -> TokenEstimate: ...


@dataclass(frozen=True, slots=True)
class ModelInputCompileBinding:
    call_fact: ResolvedModelCallFact
    target_fact: ResolvedModelTargetFact
    estimator: ModelInputTokenEstimator = field(repr=False, compare=False)
    estimator_fingerprint: str
    effective_input_budget_tokens: int
    effective_output_tokens: int
    tool_surface: FrozenModelToolSurface
    binding_fingerprint: str
```

`ModelInputTokenEstimator`只能暴露provider-neutral frozen carrier的estimate接口，不能暴露transport target。它必须使用与最终adapter相同的canonical tool-schema bytes与message accounting。compile binding不得包含：

- `NormalizedLLMTransport`或任何transport method；
- `ResolvedModelCall` / `ResolvedModelTarget` live object；
- callback、provider client、tool executor或Host owner；
- mutable effective options或dict-backed tool schema。

estimator implementation来自closed first-party estimator registry，必须是stateless/bounded compute owner；不得把任意plugin callback包装成estimator，也不得持有transport、filesystem、network或Host引用。其`fact.estimator_fingerprint`必须exact等于binding字段。

pure compiler只执行structural/budget validation。它不得调用当前会读取transport binding的`validate_model_context_for_call()`；transport-aware final validation保留在第12.4节的adapter。

### 6.4 Transport-bearing prepared call

Kernel-private DTO冻结为：

```python
@dataclass(frozen=True, slots=True)
class KernelModelPreparationRequest:
    session_id: str
    turn_id: str
    model_call_index: int
    purpose: ModelCallPurpose
    maximum_input_tokens: int
    maximum_output_tokens: int
    tool_surface: PreparedKernelToolSurface


@dataclass(frozen=True, slots=True)
class PreparedKernelModelCall:
    call: ResolvedModelCall = field(repr=False)
    tool_surface: PreparedKernelToolSurface = field(repr=False)
    compile_binding: ModelInputCompileBinding
    preparation_fingerprint: str
```

`KernelModelPort.prepare_call()`必须：

1. resolve target一次；
2. resolve call一次；
3. 验证`AGENT_MODEL_LOOP`、model call index与output cap；
4. 若scope-filtered surface非空，验证target fact声明支持tools；
5. 冻结`effective_input_budget_tokens = min(runner maximum, resolved target input budget)`；
6. 建立provider-neutral estimator seam和`ModelInputCompileBinding`；
7. fingerprint exact call fact、target fact、estimator、surface与两项effective budget；
8. 不构造`LLMContext`，不打开transport或发送网络请求。

resolved target的effective output超过runner hard cap时保持当前fail-closed行为，不在compiler中截断或重resolve另一个model。

### 6.5 Execution DTO

```python
@dataclass(frozen=True, slots=True)
class KernelModelExecutionRequest:
    session_id: str
    turn_id: str
    cut: PreparedProviderInputCut
    model_call_index: int
    prepared_call: PreparedKernelModelCall = field(repr=False)
    compiled_input: FrozenCompiledModelInput = field(repr=False)
```

`stream()`只接受该DTO。旧`KernelModelRequest(provider_input, system_prompt, maximum_*)`在R3-D必须物理删除，不保留optional union或legacy branch。Kernel DTO可以持有canonical cut与transport capability；它们不得反向进入pure compile request。

## 7. ContextSource typed protocol

### 7.1 Closed vocabulary

```python
class ContextSourceKind(StrEnum):
    BASE_SYSTEM = "BASE_SYSTEM"
    RUNTIME_ENVIRONMENT = "RUNTIME_ENVIRONMENT"
    RUNTIME_CLOCK = "RUNTIME_CLOCK"
    CAPABILITY_CATALOG = "CAPABILITY_CATALOG"
    ACTIVE_SKILL = "ACTIVE_SKILL"


class ContextChannel(StrEnum):
    SYSTEM = "SYSTEM"
    LEADING_OBSERVATION = "LEADING_OBSERVATION"
    TRAILING_OBSERVATION = "TRAILING_OBSERVATION"


class ContextTrustClass(StrEnum):
    ROOT_INSTRUCTION = "ROOT_INSTRUCTION"
    AUTHORIZED_CAPABILITY_CONTEXT = "AUTHORIZED_CAPABILITY_CONTEXT"
    TRUSTED_RUNTIME_FACT = "TRUSTED_RUNTIME_FACT"
    UNTRUSTED_OBSERVATION = "UNTRUSTED_OBSERVATION"


class ContextBudgetClass(StrEnum):
    MUST_KEEP = "MUST_KEEP"
    IMPORTANT = "IMPORTANT"
    OPTIONAL = "OPTIONAL"
    DEBUG = "DEBUG"


class ContextRenderMode(StrEnum):
    FULL = "FULL"
    COMPACT = "COMPACT"
    SUMMARY = "SUMMARY"
    REF_ONLY = "REF_ONLY"
```

`OMITTED`是compiler decision，不是producer提供的文本variant。source kind初始只有五种；未来增加kind需要按第7.6节审查，但五不是长期架构上限。

### 7.2 Candidate与variant

```python
@dataclass(frozen=True, slots=True)
class ContextRenderVariant:
    mode: ContextRenderMode
    text: str = field(repr=False)
    utf8_bytes: int
    semantic_fingerprint: str


@dataclass(frozen=True, slots=True)
class ContextSourceCandidate:
    source_kind: ContextSourceKind
    source_instance_id: str
    source_contract_version: str
    source_contract_fingerprint: str
    source_semantic_fingerprint: str
    channel: ContextChannel
    trust_class: ContextTrustClass
    budget_class: ContextBudgetClass
    placement_ordinal: int
    degradation_priority: int
    variants: tuple[ContextRenderVariant, ...] = field(repr=False)
```

约束：

- `source_instance_id`只在本次call内唯一，不是durable identity；
- `placement_ordinal`范围`0..999`，只决定同一channel中的provider-visible稳定位置；
- `degradation_priority`范围`0..999`，数值越小越受保护，只决定预算不足时的退化顺序；
- 两者正交；不得通过改变degradation policy意外重排instruction precedence；
- variant按`FULL -> COMPACT -> SUMMARY -> REF_ONLY`的子序列排列；
- mode不得重复；
- compiler使用本次prepared call的exact estimator验证每个后续variant token cost不得高于前一个，否则candidate invalid；producer不得自行声明或缓存“估算值”充当证明；
- text必须严格UTF-8；
- semantic fingerprint覆盖typed payload、contract、所有variant bytes和policy fields；
- `MUST_KEEP`永远不能OMIT，但可以使用producer明确提供的更小variant；
- 非`MUST_KEEP`在最后一个variant后可以进入`OMITTED`；
- compiler不得对source text执行通用字符截断以伪造compact；
- raw text字段`repr=False`，不得提供通用event serializer。

### 7.3 Registry不是第二authority

`ContextSourceRegistry`是Host composition时构造的process-local closed binding set：

- active binding按source kind唯一；
-只注册本轮真正有producer的五种source；
- registry fingerprint覆盖每个binding contract与implementation contract version；
- runtime校验“candidate与binding一致”，但不要求`set(ContextSourceKind)`永远exhaustive；
- plugin/hook不能直接注册source；
- ordinary extension principal不能自授`SYSTEM` channel；
- source binding不持有repository、event writer或provider transport；
- source collection完成后，compiler只接收immutable candidates，不回调producer。

### 7.4 Channel与trust矩阵

| trust class | SYSTEM | LEADING_OBSERVATION | TRAILING_OBSERVATION |
| --- | --- | --- | --- |
| ROOT_INSTRUCTION | only first-party base binding | 禁止 | 禁止 |
| AUTHORIZED_CAPABILITY_CONTEXT | only capability resolver binding | 禁止 | 禁止 |
| TRUSTED_RUNTIME_FACT | closed first-party runtime binding | 允许 | 允许 |
| UNTRUSTED_OBSERVATION | 禁止 | 允许 | 允许 |

除此通用矩阵外，每个source binding还必须冻结exact channel。trust class合法不代表caller可以任意换channel。

任何包含workspace/skill/tool生成内容的SYSTEM source都必须使用source-owned escaping/container，不能直接把free-form map拼成YAML或指令句。

### 7.5 Collection result与public diagnostic

内部capability/source diagnostic可以带调试message与path，但进入compiler graph或operational projection前必须经过中央closed projector：

```python
class ContextPublicDiagnosticCode(StrEnum):
    OPTIONAL_SOURCE_UNAVAILABLE = "OPTIONAL_SOURCE_UNAVAILABLE"
    CAPABILITY_DISCOVERY_INCOMPLETE = "CAPABILITY_DISCOVERY_INCOMPLETE"
    ACTIVE_SKILL_NOT_FOUND = "ACTIVE_SKILL_NOT_FOUND"
    ACTIVE_SKILL_UNAVAILABLE = "ACTIVE_SKILL_UNAVAILABLE"
    CATALOG_TRUNCATED = "CATALOG_TRUNCATED"
    RUNTIME_CLOCK_UNAVAILABLE = "RUNTIME_CLOCK_UNAVAILABLE"
    SOURCE_DEGRADED = "SOURCE_DEGRADED"
    SOURCE_OMITTED = "SOURCE_OMITTED"
    TOOL_RESULT_DEGRADED = "TOOL_RESULT_DEGRADED"
    TOOL_RESULT_BODY_OMITTED = "TOOL_RESULT_BODY_OMITTED"
    SOURCE_VARIANT_NON_PROGRESS = "SOURCE_VARIANT_NON_PROGRESS"
    DECISION_SAMPLE_TRUNCATED = "DECISION_SAMPLE_TRUNCATED"
```

这是本轮初始closed union；实现中若现有typed capability failure确需另一个public distinction，必须在该enum、映射表与security test中同时显式增加，不能退回自由字符串。

```python
@dataclass(frozen=True, slots=True)
class ContextSourceCollectionDiagnostic:
    code: ContextPublicDiagnosticCode
    severity: Literal["INFO", "WARNING", "ERROR"]
    source_kind: ContextSourceKind | None


@dataclass(frozen=True, slots=True)
class CollectedContextSources:
    candidates: tuple[ContextSourceCandidate, ...]
    diagnostics: tuple[ContextSourceCollectionDiagnostic, ...]
    collection_fingerprint: str
```

public diagnostic没有`message/path/public_detail`字段。中央projector只按内部diagnostic kind映射closed code；未知或携带自由文本的capability diagnostic统一投影为`CAPABILITY_DISCOVERY_INCOMPLETE`，不得复制原message。diagnostic不得含raw source text、path、environment value、tool arguments或secret。必要的真实路径只能存在于已授权、bounded且provider-visible的candidate正文，不能进入public carrier。

### 7.6 Future source准入清单

未来PHC source接入前必须逐项冻结：

1. domain truth owner；
2. typed source payload；
3. observed/frozen linearization point；
4. trust class与exact channel；
5. budget class、placement ordinal、degradation priority和允许render modes；
6. byte/token hard bounds；
7. sensitivity与redaction；
8. source failure disposition；
9. deterministic renderer与fingerprint；
10.是否会激活skill/capability；默认答案必须是否；
11.是否影响physical permission；默认答案必须是否；
12. canonical/reopen语义；
13. unit/integration/security tests。

以下做法禁止：

- plugin传任意`system_prompt`字符串；
- source读取另一个domain的raw tables；
- source依据event occurrence证明canonical row存在；
- source在compile期间执行network/tool；
- source failure创建durable retry job，除非对应产品明确要求跨Host必达；
- source callback或owner object进入candidate metadata。

## 8. 首批五类source契约

### 8.1 总表

| source | channel | trust | budget | placement | degrade priority | modes | failure |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| BASE_SYSTEM | SYSTEM | ROOT_INSTRUCTION | MUST_KEEP | 0 | 0 | FULL | fail closed |
| RUNTIME_ENVIRONMENT | SYSTEM | TRUSTED_RUNTIME_FACT | MUST_KEEP | 10 | 10 | FULL, COMPACT | fail closed |
| CAPABILITY_CATALOG | SYSTEM | AUTHORIZED_CAPABILITY_CONTEXT | IMPORTANT | 20 | 30 | FULL, COMPACT, REF_ONLY |可omit |
| ACTIVE_SKILL | SYSTEM | AUTHORIZED_CAPABILITY_CONTEXT | MUST_KEEP | 30 | 20 | FULL | resolved injection存在时必保留 |
| RUNTIME_CLOCK | LEADING_OBSERVATION | TRUSTED_RUNTIME_FACT | OPTIONAL | 0 | 80 | FULL, COMPACT |可omit |

provider-visible SYSTEM相对顺序明确保持当前`base -> runtime -> catalog -> active`。degrade priority只影响谁先缩减，不代表provider placement、durable sequence或instruction precedence。

### 8.2 BASE_SYSTEM

owner：Host构造参数`system_prompt`或[`DEFAULT_SYSTEM_PROMPT`](src/pulsara_agent/ports/system_prompt.py)。

规则：

- exactly one candidate；
- non-empty strict UTF-8；
- FULL only；
- 不允许compiler自动summary/ref/omit；
- custom prompt若超过physical source bound，在provider open前fail closed；
- 不把permission mode、Plan state或current date拼入base source。

### 8.3 单次`RuntimeTemporalCapture`

Host open时冻结session display timezone identity；每次model call最多采集一次时间事实：

```python
@dataclass(frozen=True, slots=True)
class RuntimeTemporalCapture:
    observed_at_utc: datetime
    local_date: date
    timezone_name: str
    utc_offset_minutes: int
    capture_fingerprint: str
```

- `observed_at_utc`必须timezone-aware并规范成UTC；
- local date、timezone与offset必须由同一instant导出，environment与clock producer不得分别重采样；
- IANA zone可证明时使用IANA ID，否则Host冻结固定`UTC+HH:MM | UTC-HH:MM` identity，不得只输出`CST`等歧义缩写；
- source collector失败后不得第二次读clock；environment仍使用Host冻结的timezone identity并省略call-specific offset，optional clock则omit并产生closed diagnostic；
- DST边界测试必须证明两个source不会出现互相矛盾的local date/offset。

### 8.4 RUNTIME_ENVIRONMENT

新增最小snapshot：

```python
@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentSnapshot:
    workspace_kind: Literal["project", "transient"]
    workspace_root: str
    terminal_current_cwd: str
    timezone_name: str
    utc_offset_minutes: int | None
    snapshot_fingerprint: str
```

来源与线性化：

- workspace fields来自Host已解析的`ResolvedWorkspace`；
- cwd通过Terminal manager的窄snapshot method在其state lock内读取；
- default Terminal session尚未创建时，cwd确定为workspace root，不为读取而创建session；
- cwd必须等于root或位于root之下；
- timezone来自Host冻结的session display identity；offset如存在只能来自本call唯一`RuntimeTemporalCapture`；
- 不读取或展示environment values、shell profile、PATH、proxy、SSH、DBus或credential。

FULL rendering必须以固定container和JSON-escaped values表达：

```text
<runtime_environment contract="pulsara.runtime-environment.v1">
workspace_kind=<json string>
workspace_root=<json string>
terminal_current_cwd=<json string>
timezone=<json string>
utc_offset_minutes=<integer|null>
relative_workdir_base="terminal_current_cwd"
</runtime_environment>
```

COMPACT仍必须包含`workspace_kind/workspace_root/terminal_current_cwd/timezone`；只能删除解释性字段，不能把cwd退化为root或省略containment事实。单次temporal capture失败不使required environment source失败；它确定性使用`utc_offset_minutes=null`，且不得重采样。

### 8.5 RUNTIME_CLOCK

```python
@dataclass(frozen=True, slots=True)
class RuntimeClockSnapshot:
    observed_at_utc: datetime
    local_date: date
    timezone_name: str
    utc_offset_minutes: int
    temporal_capture_fingerprint: str
```

规则：

- 只能从第8.3节同一次`RuntimeTemporalCapture`投影，不得另调Clock port；
- compiler内部不重新读clock；
- FULL包含UTC instant、local date、timezone、offset；
- COMPACT只含local date和timezone/offset；
- 作为leading observation出现在canonical transcript之前；
- 使用固定“runtime observation, not human instruction”边界；
- temporal capture失败时clock omission只产生`RUNTIME_CLOCK_UNAVAILABLE`，不阻塞provider，也不允许fallback重新采样。

### 8.6 CAPABILITY_CATALOG与ACTIVE_SKILL

现有skill provider继续拥有discovery、显式mention、health diagnostics与rendering。`KernelCapabilityComposer`重塑为source collector，输出两类候选而不是一个system字符串。

冻结规则：

- capability resolution只接收scope-filtered `FrozenModelToolSurface`中的exact tool names；
- textual mention activation使用closed `CapabilityActivationSubjectKind`，不能以reader role恰好相同来推断调用者身份；
- `ROOT_HUMAN_PROMPT`允许最后一个真实human `USER_MESSAGE | USER_STEER`中的`$skill` / `skill:name`激活skill；
- `SUBAGENT_OBJECTIVE`由parent model生成，本轮禁止通过objective正文触发textual skill activation；configured active skills仍可按Host显式配置进入child；
-未来如需delegated skill，必须增加typed `requested_skill_names`并经过显式policy validation，不能重新解析free-form objective；该能力不属于Round 3；
- `TERMINAL_OBSERVATION`、clock、runtime environment、tool result或future failure note不得激活skill；
- configured active skills仍由Host显式输入；
- catalog entries和active injections分别fingerprint；
- active skill存在时形成一个聚合`ACTIVE_SKILL` FULL-only candidate，并保持在catalog之后；
- active candidate不得因budget被省略；无法容纳则call失败；
- catalog FULL使用现有bounded renderer；
- catalog COMPACT由source renderer生成，保留sorted name与bounded description；
- catalog REF_ONLY只保留sorted skill names及固定的skill activation/read guidance；
- missing/oversized/invalid skill继续产生现有typed capability diagnostic，不伪造空active candidate；
- capability resolver发生未处理异常时fail closed，因为catalog与active injection已无法证明一致；
- diagnostics中的filesystem path不得进入operational hook public projection。

本轮保持catalog/active skill位于SYSTEM，以不改变当前happy path的instruction precedence。未来若要迁移到user observation channel，必须另立兼容与安全评审，不能由provider adapter自行决定。

## 9. Canonical transcript与tool-result lowering

### 9.1 唯一lowering owner

`_to_llm_message()`从`direct_model.py`迁入`model_input.lowering`，成为唯一provider-neutral lowering实现。canonical reader直接返回`CanonicalModelInputSnapshot`，其中只有scalar、immutable message facts、frozen tool-call arguments与第9.2节metadata；它不暴露repository/Kernel DTO。

映射保持：

| reader item | provider-neutral lowering |
| --- | --- |
| CONTEXT_SNAPSHOT | user message，固定`[CONTEXT_SNAPSHOT]`边界 |
| USER | ordinary user message |
| TERMINAL_OBSERVATION | user-role observational carrier，固定untrusted边界 |
| ASSISTANT | assistant message |
| ASSISTANT_TOOL_REQUEST | assistant turn + ordered tool calls |
| TOOL_RESULT | matching tool result message |
| TOOL_RESULT_CLOSURE | matching tool result message，body不可省略 |
| LATE_TOOL_OUTCOME | user-role typed runtime observation，保持真实sequence |

`LLMMessage.SYSTEM`仍不得出现在ordered messages；所有privileged source通过frozen compiled carrier的`system_prompt`表达，并由adapter最后一次性构造`LLMContext`。

assistant正文必须只来自ordered semantic `TEXT | DATA` blocks。`transcript_entries.content`中的parent block manifest只是storage/integrity carrier，永远不得作为provider-visible assistant fallback：

- pure tool-call assistant的正文是empty string，保留ordered tool calls；
- text + tool-call assistant只使用TEXT/DATA正文；
- multi-tool且没有TEXT/DATA时正文仍为空；
- parent manifest JSON无论当前adapter是否曾泄漏，都不得冻结为兼容语义。

这是Round 3必须修复的现有happy-path回归，而不是“保持当前字节等价”的例外。reader/lowering必须有pure tool-call、text+tool-call和multi-tool三条golden。

### 9.2 Reader最小metadata扩展

为支持artifact-aware tool-result degradation，reader在同一个repeatable-read transaction中为`TOOL_RESULT`与`LATE_TOOL_OUTCOME`附加：

```python
@dataclass(frozen=True, slots=True)
class ProviderToolResultContextMetadata:
    result_state: str
    display_kind: ToolResultDisplayKind
    artifact_disposition: ToolOutputArtifactDisposition
    artifact_id: str | None
    source_coverage: ToolOutputSourceCoverage
    source_coverage_reason: ToolOutputSourceCoverageReason | None
    artifact_unavailability_reason: (
        ToolOutputArtifactUnavailabilityReason | None
    )
```

并为所有entry item暴露`source_turn_id`，以区分current-turn与prior-turn tool result。assistant tool-call arguments在reader transaction结束前递归freeze为`FrozenJsonObjectFact`；lowering只使用同一canonical encoder生成argument JSON，不能在compiler graph中保存mutable mapping。

`ProviderInputItem`还应增加closed optional字段`tool_result_context`与`tool_result_body_text`：

- ordinary `TOOL_RESULT`二者必填，`tool_result_body_text == text`；
- `LATE_TOOL_OUTCOME`二者必填，同时保留typed `LateToolOutcomeObservation`；`text`可以继续是当前完整late envelope，而`tool_result_body_text`是exact accepted result preview；
- 其他item二者必须为空；
- compact/ref lowering只读取这些typed字段，绝不从late JSON或preview marker反向解析result/artifact语义。

这些字段来自现有`tool_results`与`transcript_entries` exact join：

- 不新增table；
- 不复制blob id/body；
- 不从preview text解析metadata；
- `artifact_id`仍是scope-checked handle，不是bearer capability；
- metadata非法时reader fail closed；
- artifact缺失不会被compiler推断成side-effect unknown。

### 9.3 Protected transcript structure

以下内容永远不可由Round 3省略或重排：

- current user message；
- context snapshot item；
- user/assistant chronological order；
- assistant tool request；
-每个tool call对应的result或provider-only closure message；
- tool call id/name/arguments；
- closure kind；
- late outcome的真实sequence placement；
- Terminal observation的untrusted carrier边界。

prior user/assistant text也不在本轮裁剪。若这些protected内容本身超过budget，compiler返回typed budget failure；PHC-07后续负责生成/采用新的canonical snapshot，而不是Round 3偷偷删history。

### 9.4 Tool-result body variants

每个`TOOL_RESULT`生成process-local render unit：

```python
class ToolResultProviderRenderMode(StrEnum):
    FULL = "FULL"
    COMPACT = "COMPACT"
    REF_ONLY = "REF_ONLY"
    OMITTED_BODY = "OMITTED_BODY"
```

规则：

- FULL是canonical preview exact text；
- COMPACT由compiler-owned deterministic UTF-8-safe head/tail renderer生成，最大`8 KiB`；
- COMPACT的固定envelope必须包含result state、source coverage、display kind、是否存在artifact、omitted byte/char count；这些count只相对本次accepted canonical preview，不能冒充original tool output或artifact body的坐标；
- artifact存在时，COMPACT必须完整保留`artifact_id`；只有scope-filtered exact `FrozenModelToolSurface`同时包含已闭合的`artifact_read` binding时才渲染read guidance；
- REF_ONLY只在`AVAILABLE | INCOMPLETE`、`artifact_id`存在且本次exact tool surface包含`artifact_read`时允许，最大`2 KiB`；
- REF_ONLY必须明确`INCOMPLETE/RETAINED_SNAPSHOT`不能恢复retention之前已经丢失的bytes；
- artifact unavailable或本次surface不可读取时不得生成虚假可读reference；surface没有`artifact_read`时跳过REF_ONLY；
- OMITTED_BODY仍保留一个matching tool-result message及result state/coverage/loss reason固定marker；
- OMITTED_BODY不等于tool result缺失，不改变side-effect outcome，也不授权retry；
- source preview中看起来像artifact marker的任意tool body不能覆盖typed metadata；
- `artifact_read`自身result保持普通tool result，可降级但不得递归产生新artifact reference。

Round 1的`source_coverage`继续是原始/retained body完整性的唯一typed说明；Round 3只描述provider projection相对canonical preview又发生了何种降级。两者不得复用同一个coverage字段或loss reason。

`LATE_TOOL_OUTCOME`可以使用同样的body projection，但必须从typed late observation与`tool_result_body_text`重建外层carrier，并保留late-outcome kind、result state、tool call ID和真实sequence。不得解析原JSON寻找字段，也不得把late result变成历史matching result。

### 9.5 Tool-result degradation价值顺序

每个tool-result unit归入：

| unit | budget class | degradation priority | 相对保护 |
| --- | --- | ---: | --- |
| prior-turn tool result | OPTIONAL | 50 | 旧sequence先降级；同class中晚于clock |
| current-turn tool result | IMPORTANT | 10 | 旧sequence先降级；同class中晚于catalog |
| provider-only closure | MUST_KEEP | 0 | 不降级 |

source与tool result统一进入第11节的deterministic allocation，不形成独立“tool renderer budget owner”。由此得到的初始价值顺序是：clock先于prior-turn tool result降级；catalog先于current-turn tool result降级；所有可省略unit耗尽后才尝试`MUST_KEEP` source自己声明的更小variant。

## 10. Compiler input/output contract

### 10.1 Compile request

```python
@dataclass(frozen=True, slots=True)
class CanonicalModelInputIdentity:
    session_id: str
    turn_id: str
    context_binding_revision_id: str
    provider_input_through_sequence: int
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    identity_fingerprint: str


@dataclass(frozen=True, slots=True)
class CanonicalModelInputSnapshot:
    identity: CanonicalModelInputIdentity
    items: tuple[FrozenProviderInputItem, ...] = field(repr=False)
    snapshot_fingerprint: str


@dataclass(frozen=True, slots=True)
class StructuredModelInputCompileRequest:
    context_id: str
    model_call_index: int
    canonical_input: CanonicalModelInputSnapshot = field(repr=False)
    compile_binding: ModelInputCompileBinding = field(repr=False)
    sources: CollectedContextSources = field(repr=False)
```

校验：

- canonical identity与`call_fact`中的session/turn/call purpose完全一致；
- canonical snapshot fingerprint、compile binding fingerprint、surface fingerprint各自self-consistent；
- source registry fingerprint与collector composition一致；
- `context_id`non-empty且仅本次call使用；
- compile request中不得出现Kernel cut、reader result、prepared call、resolved call、transport或executor object；
- no mutable list/map；tool schema与tool-call arguments必须已经递归freeze；
- compile开始后caller修改catalog/executor state不能改变request。

### 10.2 Compile decision

```python
@dataclass(frozen=True, slots=True)
class CompiledSourceDecision:
    source_kind: ContextSourceKind
    source_instance_fingerprint: str
    channel: ContextChannel
    selected_mode: ContextRenderMode | None
    included: bool
    estimated_tokens: int
    reason_code: str


@dataclass(frozen=True, slots=True)
class CompiledToolResultDecision:
    source_entry_fingerprint: str
    current_turn: bool
    selected_mode: ToolResultProviderRenderMode
    estimated_tokens: int
    reason_code: str
```

decision不含raw text、tool arguments、artifact body、filesystem path或callback。

### 10.3 Budget report

```python
@dataclass(frozen=True, slots=True)
class ContextCompileBudgetReport:
    compiler_contract_version: str
    estimator_fingerprint: str
    target_fingerprint: str
    tool_surface_fingerprint: str
    effective_input_budget_tokens: int
    system_tokens: int
    message_tokens: int
    tool_tokens: int
    envelope_tokens: int
    total_input_tokens: int
    protected_transcript_tokens: int
    context_source_tokens: int
    degraded_source_count: int
    omitted_source_count: int
    degraded_tool_result_count: int
    omitted_tool_result_body_count: int
    decision_digest: str
```

report的total与final `TokenEstimate`必须exact equal。component attribution使用同一estimator；不得从raw char count推断token。

### 10.4 Frozen provider-neutral carrier与compile output

```python
@dataclass(frozen=True, slots=True)
class FrozenCompiledModelInput:
    context_id: str
    canonical_input_identity: CanonicalModelInputIdentity
    system_prompt: str = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    tools: tuple[FrozenToolSpec, ...] = field(repr=False)
    final_estimate: TokenEstimate
    source_decisions: tuple[CompiledSourceDecision, ...]
    tool_result_decisions: tuple[CompiledToolResultDecision, ...]
    budget_report: ContextCompileBudgetReport
    diagnostic_codes: tuple[ContextPublicDiagnosticCode, ...]
    source_collection_fingerprint: str
    compiled_semantic_fingerprint: str
    compile_binding_fingerprint: str
```

其中：

- `LLMMessage`中的文本与tool-call argument JSON是immutable strings；任何dict-backed argument不得进入output；
- `compiled_semantic_fingerprint`覆盖canonical input identity、target/call facts、frozen tool surface、source decisions、provider-neutral system/messages/tools和final estimate，但排除callback/transport object；
- `compile_binding_fingerprint`必须exact等于request binding，Kernel execution层再把它与transport-bearing preparation fingerprint做join；
- 两者都不写durable storage；
- dataclass repr不得显示system/messages/tool schema正文；
- output没有通用JSON/event serializer。

pure compiler永远不构造或返回`LLMContext`。Direct adapter完成所有fingerprint/surface exact join后，才把每个`FrozenToolSpec.parameters` thaw为一份新dict并建立一次性`LLMContext`；该mutable context只存在于一次transport调用栈内。

tool-result decisions最多4096项，与reader item hard bound相同。对operational hook只投影aggregate与最多64个deterministic samples，不复制整个tuple。

## 11. Deterministic allocation算法

### 11.1 Physical limits

新增集中式`StructuredModelInputLimits`，初始hard values：

```text
source candidates                    32
variants per source candidate         4
single source variant UTF-8 bytes     1 MiB
aggregate FULL source UTF-8 bytes     2 MiB
aggregate all source variants bytes   4 MiB
tool specs                            64
aggregate tool spec canonical bytes   1 MiB
total compile text working-set bytes 64 MiB
compile diagnostics                   64
public diagnostic bytes              32 KiB
tool-result COMPACT bytes              8 KiB
tool-result REF_ONLY bytes             2 KiB
```

这些是physical safety bounds，不是默认token allocation。实现前若当前内置skill/catalog真实样本超过单项bound，可在R3-0用负载探针上调，但必须保持finite并更新规格/evidence；不得让实现者临场使用unbounded string。

source byte count使用strict UTF-8；tool spec canonical byte count使用`name/description/parameters`的sorted-key、compact、finite UTF-8 JSON。`total compile text working-set`覆盖canonical input body、所有source variants、所有生成的tool-result FULL/COMPACT/REF/marker carrier、frozen tool schema canonical JSON与固定envelope文本；它约束逻辑UTF-8 bytes，不试图精确等同Python RSS。

collector完成与tool-result variant collection完成后、allocator开始前，必须一次性验证single、aggregate FULL、aggregate all variants与total working-set四层bound。不得只检查最终被选择的variant，从而先构造unbounded候选集再丢弃。它们只做physical admission，token allocation仍完全服从provider-neutral estimator。

### 11.2 Initial layout

compiler先构造最高保真layout：

```text
system_prompt:
  included SYSTEM source fragments
  sorted by (placement_ordinal, source_kind, source_instance_id)
  joined by exactly "\n\n"

messages:
  included LEADING_OBSERVATION sources in deterministic order
  + canonical transcript lowering in exact reader order
  + included TRAILING_OBSERVATION sources in deterministic order

tools:
  exact FrozenModelToolSurface.tool_specs
```

source observation使用closed carrier：

```text
[PULSARA_CONTEXT_OBSERVATION source=<kind> trust=<class> contract=<version>]
<source-owned rendered text>
[/PULSARA_CONTEXT_OBSERVATION]
```

该carrier是provider-neutralcompiler的一部分，provider adapter不得改写source body或自行选择role。

### 11.3 Allocation order

当最高保真layout超出effective budget时，每次只推进一个unit到下一个较小mode。候选排序冻结为：

1. budget class从最可牺牲到最受保护：`DEBUG -> OPTIONAL -> IMPORTANT -> MUST_KEEP`；
2. 同class中degradation priority数值较大的先降级；
3. tool-result unit中prior-turn先于current-turn；
4. 同类tool result按`source_entry_sequence`从旧到新；
5. 其余按`source kind / instance ID / entry ID`词法序稳定裁决。

source的next state：

```text
next declared variant
    -> ...
    -> OMITTED（仅非MUST_KEEP）
```

tool-result next state：

```text
FULL
  -> COMPACT（确实节省token时）
  -> REF_ONLY（artifact可读时）
  -> OMITTED_BODY
```

artifact不可读时跳过REF_ONLY。任何next state没有减少exact estimated tokens时跳过该state并记录bounded diagnostic，避免non-progress loop。

### 11.4 Exact estimate实现

V1 estimator提供per-message additive breakdown。compiler应：

1. 对所有source/tool-result variant预计算exact message/text cost；
2. 对tool specs和fixed transcript messages只计算一次；
3. source selection变化时重新计算joined system prompt；
4. 用component delta选择mode，避免对4096项执行O(n²) full-context重估；
5. 冻结`FrozenCompiledModelInput`前构造完整provider-neutral carrier；
6. 通过`ModelInputCompileBinding.estimator`做一次完整exact estimate；
7. 验证component prediction与full estimate完全一致；
8. 把exact total写入`compiler_estimated_input_tokens`并完成pure structural/budget validation；
9. compiler不调用transport-aware `validate_model_context_for_call()`；
10. adapter在exact join和thaw后调用该validator，fresh validation estimate必须与compiler estimate再次exact equal。

provider-neutral estimator对`FrozenToolSpec`的计费必须使用与最终thaw后`ToolSpec`相同的canonical schema bytes。若实现选择在estimator内部临时thaw，它只能创建私有ephemeral dict，调用结束即丢弃，且不得让mutable object逃逸到compile graph。

若未来estimator不提供current additive contract，必须先扩展estimator fact与compiler算法；不得在不证明component identity的情况下沿用delta allocation。

### 11.5 Budget failure分类

closed failure kind：

```python
class ModelInputCompileFailureKind(StrEnum):
    MODEL_TARGET_PREPARATION_FAILED = "MODEL_TARGET_PREPARATION_FAILED"
    TOOL_SURFACE_INVALID = "TOOL_SURFACE_INVALID"
    REQUIRED_SOURCE_UNAVAILABLE = "REQUIRED_SOURCE_UNAVAILABLE"
    SOURCE_CONTRACT_INVALID = "SOURCE_CONTRACT_INVALID"
    SOURCE_PHYSICAL_BOUND_EXCEEDED = "SOURCE_PHYSICAL_BOUND_EXCEEDED"
    COMPILE_WORKING_SET_EXCEEDED = "COMPILE_WORKING_SET_EXCEEDED"
    PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET = "PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET"
    REQUIRED_CONTEXT_EXCEEDS_BUDGET = "REQUIRED_CONTEXT_EXCEEDS_BUDGET"
    TOOL_SCHEMA_EXCEEDS_BUDGET = "TOOL_SCHEMA_EXCEEDS_BUDGET"
    FINAL_ESTIMATE_MISMATCH = "FINAL_ESTIMATE_MISMATCH"
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"
```

分类规则：

- candidate collection超过all-variant或total working-set physical bound时使用`COMPILE_WORKING_SET_EXCEEDED`，在allocator和provider open前fail closed；
- 如果移除所有可省略source并把tool result降到minimum后，protected transcript本身超限，使用`PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET`；
- 若tools+envelope单独超限，使用`TOOL_SCHEMA_EXCEEDS_BUDGET`；
- 若required source minimum使总量超限，使用`REQUIRED_CONTEXT_EXCEEDS_BUDGET`；
- 多项同时成立时按`tool schema -> protected transcript -> required context`顺序选择public primary kind，report仍保留aggregate；
- 不自动创建snapshot或重试另一个model；
- 不把compile failure伪装成provider transport failure。

## 12. Runner与provider happy path

### 12.1 Exact sequence

每个foreground model call冻结为：

```text
1. consume pending human steer
2. ProviderSafePoint.freeze_provider_input()
3. CanonicalProviderInputReader.read_frozen_snapshot(exact cut)
4. PreparedKernelToolSurface.snapshot(scope-filtered)
5. KernelModelPort.prepare_call(exact surface + caps)
6. determine closed capability activation subject
7. collect one temporal capture + base/runtime/clock/capability sources
8. StructuredModelInputCompiler.compile()
9. offer bounded operational compile projection
10. exact-join surface and acquire bounded model-response borrow
11. prepared safe point begin_model_operation()
12. DirectKernelModelPort.stream(exact prepared call + frozen input)
13. assemble complete assistant message
14. existing canonical assistant commit / exact-surface tool path
15. release borrow after all calls from this response are rejected or handed to pinned physical owners
```

第11步之前发生任何错误时，transport `open_stream()`调用次数必须为0。surface borrow不得跨下一次model call复用。

### 12.2 Skill activation

capability activation不再只用`ProviderInputItemKind.USER`推断调用者，而是显式传入：

- ROOT只扫描human accepted `USER_MESSAGE | USER_STEER`并标记为`ROOT_HUMAN_PROMPT`；
- child objective标记为`SUBAGENT_OBJECTIVE`，不得通过free-form正文激活skill；
-不扫描Terminal observation、runtime clock、late outcome、tool result或context source；
- ROOT同一turn后续tool loop仍可基于最后一个真实human user message保持active skill；
-source message的`$skill`或`skill:name`文本不能触发capability resolution。

### 12.3 Safe-point与source freshness

canonical cut与process-local source snapshot不是一个数据库transaction，也不应伪装成一个：

- cut先冻结；
- runtime/capability source随后在provider open前snapshot；
-每个process-local source的semantic fingerprint/observed time证明本次看到的值；
- source collection完成后不再变；
- cwd或skill file在compile后变化只影响下一次model call；
- 不为此新增source receipt或global lock。

### 12.4 Direct adapter最终职责

`DirectKernelModelPort.stream()`必须在open transport之前校验：

- execution request identity与prepared call一致；
- compile binding、target/call facts与scope-filtered surface fingerprint一致；
- process-local surface borrow仍有效且exact join prepared access；
- frozen tools exact equal prepared model-visible specs；
- input/output caps仍满足；
- transport binding/contract未变化。

之后adapter才：

1. thaw每个frozen tool schema为新的ephemeral dict；
2. 构造一次性`LLMContext`；
3. 调用transport-aware `validate_model_context_for_call()`；
4. 验证fresh estimate与compiler `final_estimate` exact equal；
5. 打开transport。

通过后只执行：

```text
open_stream(call, compiled llm_context)
read typed provider payloads
report operational usage
close and wait physical completion
```

adapter不得调用reader、source collector、capability resolver或compiler。

provider返回的每个tool call由runner附加本次`surface_fingerprint + exact binding fingerprint + borrow identity`，该attribution进入process-local invocation context，不进入provider payload或durable event。authorize、attempt acceptance与invoke依次重验；任何阶段都不得按当前tool name重新选择另一个binding。

### 12.5 ROOT与SUBAGENT_TASK

ROOT与current same-Host subagent runner使用同一compiler实例/contract，但各自：

- 使用自己的scope-specific canonical reader cut；
- 使用自己的closed activation subject；
- 从同一Host tool owner取得不同的scope-filtered surface；ROOT与child surface无需相等，child不得advertise ROOT-only `terminal_monitor`；
- 使用同一workspace/runtime snapshot；
- 获得不同context ID与model call identity。

不得跨scope读取parent transcript或通过compiler注入child result。subagent result进入ROOT仍走已存在的canonical acceptance和safe point。

### 12.6 Durable job模型明确排除

[`DirectKernelJobModel`](src/pulsara_agent/conversation_kernel/job_model.py)的handler-specific single prompt不读取conversation transcript，也不属于本轮PHC-17 happy path。本轮：

- 保留其现有per-attempt budget与final validation；
- 不让job调用conversation source collector；
- 不把job prompt包装成fake user conversation；
- 可以复用未来抽出的“prepared target + final validate”utility，但不能引入conversation context source。

## 13. Failure、crash与cancellation矩阵

| 场景 | 本次call | canonical turn | provider physical I/O | durable副作用 |
| --- | --- | --- | --- | --- |
| optional clock source失败 | omit + diagnostic |继续RUNNING |允许 | 0 |
| capability typed diagnostic |按现有projection继续 |继续RUNNING |允许 | 0 |
| capability resolver异常 | compile失败 | existing interrupt | 0 |只保留既有turn terminal transition |
| runtime environment snapshot非法 | compile失败 | existing interrupt | 0 |无compile event |
| tool surface join漂移 | prepare失败 | existing interrupt | 0 | 0 |
| surface在borrow前被revoked | typed tool-surface failure | existing interrupt | 0 | 0 |
| close发生于active surface borrow |取消runner并drain pinned effect owner | interrupted |按既有effect规则 |无surface receipt |
| target resolve失败 | prepare失败 | existing interrupt | 0 | 0 |
| optional sources全部omit仍超限 | typed budget失败 | existing interrupt | 0 | 0 |
| protected transcript超限 | typed budget失败 | existing interrupt | 0 | 0 |
| final estimate mismatch | fail closed | existing interrupt | 0 | 0 |
| operational hook overflow/failure | compile不受影响 |不受影响 |允许 | 0 |
| cancellation during source collection | wait admitted thread exit | interrupted | 0 | 0 |
| cancellation during compile | wait bounded thread exit | interrupted | 0 | 0 |
| Host crash after compile before open | compiled state消失 |按canonical reopen规则 | 0或未确认open |不补写 |
| Host crash duringprovider stream | live stream消失 |turn interrupted |可能已发送 |不replay compiled input |
| cwd/skill在compile后变化 |本call用frozen snapshot |不受影响 |允许 |下一call重采集 |

compile failure发生时current user entry已经accepted。重新attach可以看到它与interrupted turn；系统不得删除该entry，也不得自动重复provider call。

## 14. Diagnostics、extension与sensitivity

### 14.1 Internal report与public projection分开

Internal `FrozenCompiledModelInput`必然包含provider-visible内容，只允许runner/model adapter持有。Operational extension只接收：

```text
compiler_contract_version
model_call_index
target_fingerprint
tool_surface_fingerprint
effective_input_budget_tokens
total_input_tokens
component token counts
degraded/omitted counts
bounded diagnostic codes
decision digest
```

public `decision digest`只覆盖source kind、selected mode、count与closed reason code；不得覆盖raw source/body bytes、path、artifact ID或可对低熵secret做离线猜测的semantic fingerprint。full compiled/source fingerprints只留在同一process的runner/adapter binding中。

不得包含：

- system/source正文；
- user/assistant/tool result正文；
- raw thinking；
- tool arguments；
- artifact正文或private storage identity；
- workspace root/cwd；
- skill file path/content；
- API key、endpoint userinfo、MCP secret或environment value；
- callback、recorder、compiler/model/source owner。

### 14.2 Operational hook

允许增加：

```python
OperationalHookType.MODEL_INPUT_COMPILE_OBSERVED
```

payload使用`disposition=COMPILED | FAILED`与上述public字段。它：

- 不进入`agent_events`；
- 不进入Live serializer/schema registry；
- delivery failure/timeout/overflow沿用ordinary operational hook isolation；
- 不阻塞provider或canonical transition；
- 无durable cursor/catch-up；
- Host crash后丢失。

如果实现者选择暂不公开operational hook，可以只保留internal report；但不得为了观测而写database或committed event。Activation evidence必须记录最终选择。

### 14.3 Logging

production log只允许closed failure kind、counts、fingerprints与target-safe identity。任何异常`repr(request)`、`repr(compiled_input)`或dump `LLMContext`的做法必须由test/guard禁止。capability/source内部diagnostic必须先经过第7.5节中央projector；日志/extension不得复制其free-form message或`Path`。

## 15. Schema、event、transaction与Protocol预算

### 15.1 Physical schema

Round 3 schema变化：

```text
new product relations           0
new columns                     0
new migrations                  0
new blobs                       0
new durable jobs                0
```

Reader读取Round 1已经存在的`tool_results`artifact/coverage字段即可。clean-v0 baseline不应因本轮改变。

### 15.2 Vocabulary oracle

```text
Committed AgentEvent types      27
Live AgentEvent types           23
subject slots                   13
append guards                    2
product relations               24
durable jobs                     4
```

`MODEL_INPUT_COMPILE_OBSERVED`如启用，仅是OperationalHookType，不改变上述oracle。

### 15.3 Transaction budget

每次model call新增：

```text
canonical write transactions     0
canonical read transactions      0  # 仍复用reader已有的一次read
committed events                 0
live events                      0
durable job attempts             0
```

text/tool turn的既有canonical transaction与committed occurrence budget不得增长。

### 15.4 Protocol v3与Go

Round 3不需要Protocol v3 wire变化。Go TUI不接收full prompt或compile report。若实现者为了diagnostic修改proto，即视为scope expansion，必须停止并另行review。

## 16. 实施切片

### R3-0：Inventory、baseline与negative guards

- 记录checkpoint HEAD、dirty status、文档hash与当前full pytest baseline；
- 列出所有production model call entrypoints；
- 证明foreground ROOT/subagent只走`DirectKernelModelPort`；
- 记录current tool surface names/binding fingerprints；
- 记录current canonical reader fixture与provider payload golden；将tool-only parent manifest fallback单列为待修known regression，不得纳入兼容golden；
- 新增negative architecture guard，禁止旧context/event/recovery package返回；
- 不修改production behavior。

Exit：inventory闭合且Stage 3–5/Round 1/2 retained tests全绿；baseline失败如有必须记录disposition，不得用skip隐藏。

### R3-A：Dormant model prepare/execute split

- 引入`FrozenToolSpec`、scope-filtered `FrozenModelToolSurface`与Kernel-private `PreparedKernelToolSurface`；
- 引入provider-neutral `ModelInputCompileBinding`与transport-bearing prepare/execution DTO；
- 将tool schema和canonical tool-call arguments改为`FrozenJsonObjectFact`；
- Direct model可prepare exact target但旧runner尚不切换；
- 加入target/tool fingerprint、scope filter、surface borrow、output cap与zero-open failure tests；
- scripted model fixture实现同一prepare contract，不允许测试绕过target/budget。

Exit：dormant path完整验证；pure DTO没有transport/mutable JSON；production仍只有旧路径。

### R3-B：Pure compiler与reader metadata

- 新建`model_input`纯package；
- reader直接产生provider-neutral frozen snapshot，同transaction附加tool-result metadata/source turn；
- 修复assistant parent manifest泄漏，增加pure-tool/text+tool/multi-tool semantic goldens；
- 实现source DTO、registry validation、lowering、tool-result variants；
- 实现placement/degradation正交的deterministic allocator、all-variant/workset bounds、report、fingerprints与failure kind；
- 纯unit tests覆盖所有mode/budget/order/security。

Exit：compiler无Kernel/DB/provider I/O import，不接收`ResolvedModelCall`且不调用transport-aware validator；同输入golden deterministic。

### R3-C：First-party source collector

- capability composer拆为typed catalog/active candidates；
- Terminal manager增加same-lock cwd snapshot；
- Host冻结timezone identity，每call只采集一个`RuntimeTemporalCapture`；
- 实现base/runtime/clock candidates；
- scope-filtered exact tool surface驱动capability resolution；
- closed activation subject拒绝child objective的textual skill activation；
- internal capability diagnostics通过closed central projector进入public DTO；
- required/optional failure disposition闭合。

Exit：五类source在真实Host composition可冻结；无Memory/Plan/MCP占位。

### R3-D：Production hard switch

- runner执行第12.1节exact sequence；
- direct adapter只接收compiled execution request；
- adapter exact join后thaw ephemeral `LLMContext`并执行transport-aware final validation；
- tool calls携带prepared surface attribution，authorize/attempt/invoke均exact join；
- 删除旧`KernelModelRequest.system_prompt/provider_input/maximum_*`materialization branch；
- 删除`direct_model._to_llm_message`和composer字符串join路径；
- ROOT/subagent/scripted fixtures全部切换；
- compile failure保证provider open count 0。

Exit：production composition只有一个structured compiler owner。

### R3-F：Activation与证据

- 全量Python/PostgreSQL/Go gates；
- real-provider dogfood；
- 更新README中model-input事实；
- 更新PHC-17为已恢复并保留future sources说明；
- 生成`benchmarks/suites/core/v1/round3_structured_model_input_compiler_activation.json`；
- evidence记录source/tool/result modes、budget decisions、zero schema/event delta与non-goals；
- 将本规格状态改为`ACTIVATED`。

Round 3不得在R3-F之前宣称完成。

## 17. 主要修改面

预期production修改：

```text
src/pulsara_agent/model_input/
src/pulsara_agent/conversation_kernel/context_sources.py
src/pulsara_agent/conversation_kernel/runner.py
src/pulsara_agent/conversation_kernel/direct_model.py
src/pulsara_agent/conversation_kernel/capability.py
src/pulsara_agent/conversation_kernel/reader.py
src/pulsara_agent/conversation_kernel/tool_runtime.py
src/pulsara_agent/conversation_kernel/host.py
src/pulsara_agent/conversation_kernel/extensions.py
src/pulsara_agent/conversation_kernel/limits.py
src/pulsara_agent/terminal_process/manager.py
src/pulsara_agent/llm/input.py
src/pulsara_agent/llm/estimator.py
src/pulsara_agent/llm/validation.py
```

预期tests：

```text
tests/test_round3_structured_model_input_compiler.py
tests/test_stage2_direct_model.py
tests/test_stage2_conversation_runner.py
tests/test_stage2_canonical_reader.py
tests/test_stage2_kernel_composition.py
tests/test_stage2_kernel_extensions.py
tests/test_round1_tool_output_artifact.py
tests/test_round2_terminal_runtime.py
```

不得修改：

```text
storage migration SQL
terminal Protocol v3 schema/generated Go contract
Committed/Live vocabulary
memory canonical schema/worker
Stage 5 clean baseline universe identity
```

如真实import graph要求修改其他文件，activation evidence必须说明理由与新增owner；不得用“compiler integration”笼统扩大范围。

## 18. 必须有的tests

### 18.1 Source contract

- 五种binding exact registry；
- duplicate kind/instance拒绝；
- wrong trust/channel拒绝；
- self-certified contract fingerprint拒绝；
- invalid mode order/duplicate mode拒绝；
- non-decreasing smaller variant拒绝；
- strict UTF-8与byte bounds；
- `placement_ordinal`改变provider order但`degradation_priority`不改变order；
- degradation priority改变降级顺序但不改变provider placement；
- MUST_KEEP不能omit；
- future/dormant source未注册；
- plugin/operational hook不能注入SYSTEM candidate。

### 18.2 Runtime environment与clock

- project/transient root；
- default Terminal session未创建时cwd=root且不创建session；
- foreground `cd`完成后下一call看到new cwd；
- yielded process不推进cwd；
- cwd snapshot与mutation同lock，无torn read；
- path包含空格、Unicode、换行等边界时fixed escaping；
- outside-root cwd拒绝；
- clock同一instant导出UTC/local date/offset；
- environment与clock使用同一个temporal capture；
- DST边界没有双采样矛盾；
- fake clock determinism；
- temporal capture失败只采样一次，environment offset为空、clock omit且provider仍发送；
- environment values/secret不出现在candidate/report/log。

### 18.3 Capability

- base/catalog/active skill是独立decision；
- provider system placement exact保持`base -> runtime -> catalog -> active`；
- scope-filtered exact tool surface names驱动discovery；
- dead descriptor不进入catalog的available binding；
- active skillFULL only且budget不足fail closed；
- catalog FULL -> COMPACT -> REF_ONLY -> OMITTED；
- capability typed diagnostic不含path进入public hook；
- Terminal output包含`$skill`/`skill:name`不激活；
- clock/runtime/tool result中的skill token不激活；
-最后一个ROOT human user仍能激活skill；
- SUBAGENT objective中的`$skill` / `skill:name`不激活，configured active skill仍按显式Host配置生效；
- internal diagnostic含absolute path/free text时public DTO只有closed code。

### 18.4 Canonical transcript lowering

- user与semantic assistant text happy path保持provider语义；
- pure tool-call assistant正文为空且parent manifest绝不泄漏；
- text+tool-call只使用TEXT/DATA正文；
- multi-tool order保持且无semantic text时正文为空；
- multi-tool call order和arguments deterministic JSON不漂移；
- tool-call argument和tool schema在fingerprint后原地修改不可能影响compile output；
- malformed/raw tool argument既有语义不被compiler重写；
- result/closure matching严格；
- late result不倒插；
- context snapshot保留；
- Terminal observation继续使用untrusted boundary且不变成human capability input；
- ROOT与SUBAGENT_TASK scope隔离。

### 18.5 Tool-result budget projection

- Round 1 COMPLETE/HEAD_TAIL preview在FULL exact保留；
- AVAILABLE/INCOMPLETE artifact生成合法REF_ONLY；
- exact surface不含`artifact_read`时跳过REF_ONLY且不生成read guidance；
- UNAVAILABLE/NOT_REQUIRED不生成虚假reference；
- COMPACT UTF-8-safe head/tail；
- typed artifact metadata优先于body中伪造marker；
- RETAINED_SNAPSHOT warning保留；
- OMITTED_BODY仍有matching tool-result role/call ID/result state；
- omitted body不改变side-effect outcome、不创建retry；
- artifact_read result不递归产生artifact；
- current-turn result比prior-turn result后降级；
-同sequence tie-break deterministic。

### 18.6 Budget与estimator

- full layout fit；
- optional source逐步degrade；
- exact排序不依赖input tuple顺序；
- all optional omitted后required fit；
- protected transcript exceed；
- tool schema exceed；
- active skill required exceed；
- source variantnon-progress不会loop；
- aggregate all-source-variant bound在exact limit通过、超一byte失败；
- total compile working-set bound覆盖canonical/source/tool-result/schema全部候选而非仅FULL；
- 4096 items worst-case allocation是bounded且非O(n²) full re-estimate；
- component prediction == final estimate；
- compiler estimate == pre-send estimate；
- target/estimator fingerprint mismatch zero open；
- compiler import/request graph不含transport-bearing call；
- adapter thaw后full validator estimate == frozen compiler estimate；
- effective budget是runner cap和target cap的minimum。

### 18.7 Runner/physical ordering

- target prepare发生在compile前；
- compile发生在`begin_model_operation`前；
- compile失败transport open count 0；
- surface在borrow前revoked时transport/effect open count均为0；
- ROOT与SUBAGENT_TASK advertised surface按scope过滤，child无`terminal_monitor`；
- provider返回tool call在authorize/attempt/invoke exact join同一advertised binding；
- binding变化不会按name替换执行，Host close可bounded drain active borrow；
- optional hook failure不阻塞；
- source collection/compile cancellation等待thread exit；
- successful text turn transaction/event counts不增长；
- successful one-tool turn transaction/event counts不增长；
- assistant commit和attempt-before-effect仍成立；
- subagent runner走同一compiler；
- durable job handler不意外读取conversation source。

### 18.8 Sensitivity与diagnostic

- internal DTO repr不含prompt/body；
- operational projection不含system/user/tool/path/secret正文；
- public diagnostic type没有free-form code/detail/path carrier；
- report decision count超限时有digest/sample/omitted count；
- extension queue overflow只detach/GAP；
- no full prompt serialization helper；
- logs不出现API key、private URL、MCP secret、env value或tool args。

### 18.9 Historical product regression

从`5b7ad9f7`移植测试语义而非旧fixture：

- placement/degradation not input field order；
- required source survives optional omission；
- system per-source ownership；
- final omission equals provider payload；
- source renderer cannot forge authority；
- transcript/tool pairing preserved；
- final tool schema deep-frozen；
- runtime clock one snapshot percall。

禁止复制依赖EventLog、context pages/root、provider generation、source head replay、cache receipt或RuntimeSession的测试。

## 19. Static architecture guards

必须增加或更新guard证明：

1. production adapter不import canonical repository/reader/source collector；
2. pure compiler不importPostgreSQL、filesystem、clock、Host、conversation kernel、event writer、Live bus或transport；compile request不含Kernel/transport-bearing DTO；
3. source collector不importprovider adapter；
4. no `ContextCompiledEvent`或compile schema/table；
5. no `runtime.context_input`/`runtime.context_engine` package复活；
6. no provider input generation/prefix/receipt/reducer/checkpoint import；
7. direct model不存在`_to_llm_message`或system prompt join；
8. runner不存在legacy `KernelModelRequest` branch；
9. tool surface snapshot按scope exact join descriptor/executor；ROOT-only tool不进入child，authorize/attempt/invoke绑定同一process-local surface access；
10. compiler不读取Memory/Plan/MCP/permission state；
11. event/table/job/guard/subject oracle保持；
12. Protocol major仍只有v3；
13. schema migration universe仍clean v0；
14. all provider physical open callers接受prepared+frozen compiled binding，且只有adapter可以thaw schema/构造`LLMContext`；
15. no raw prompt/report durable serializer；
16. all production `AGENT_MODEL_LOOP` callers经过structured compiler；
17. compiler graph中的tool schema与tool-call arguments只能是frozen JSON；
18. canonical assistant lowering没有parent manifest fallback；
19. public context diagnostic code为closed enum且没有free-form detail/path；
20. source physical guard同时覆盖FULL、all variants和total working set。

## 20. 验证命令

实施者应以实际文件名补全targeted nodes，但至少运行：

```bash
uv run pytest -q \
  tests/test_round3_structured_model_input_compiler.py \
  tests/test_stage2_direct_model.py \
  tests/test_stage2_conversation_runner.py \
  tests/test_stage2_canonical_reader.py \
  tests/test_stage2_kernel_composition.py \
  tests/test_stage2_kernel_extensions.py \
  tests/test_round1_tool_output_artifact.py \
  tests/test_round2_terminal_runtime.py

uv run pytest -q

uv run pytest -q \
  --postgresql \
  tests/test_stage2_canonical_reader.py \
  tests/test_stage2_conversation_runner.py \
  tests/test_round3_structured_model_input_compiler.py

uv run ruff check .
uv run python -m compileall -q src tests tools
uv run python tools/generate_terminal_protocol_contract.py --check

(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)
(cd clients/terminal && go mod verify)

uv lock --check
git diff --check
```

还必须执行：

- pytest collection 0 error；
- Markdown fence闭合；
- duplicate heading检查；
- active local Markdown link存在性检查；
- forbidden import/string guard；
- event/schema/job/transaction oracle；
- source/decision fingerprint golden；
- worst-case allocation performance probe；
- no-secret log scan。

不得通过删除仍验证当前产品行为的tests、增加skip/xfail或缩小collection来取得全绿。确属旧authority-only的fixture只能在manifest中逐项证明successor后删除。

## 21. Real-provider dogfood

### 21.1 最小场景

至少运行以下真实Agent loop：

1. user询问当前workspace root、cwd与local date；
2. provider回答必须来自typed runtime sources；
3. user触发一个真实skill并调用至少一个tool；
4. tool产生足以触发`COMPACT`或`REF_ONLY`的result；
5. model使用`artifact_read`继续读取；
6. assistant完成canonical response；
7. evidence只记录fingerprint/count/mode/sentinel，不记录prompt、API key或大段tool正文。

### 21.2 Provider覆盖

至少要求：

- 一次`openai_responses` normalized transport成功；
- 一次`openai_chat_completions` normalized transport成功。

`.env`中其他已配置provider各跑一次；外部auth/model availability/ratelimit错误可记录为environment disposition，不要求在本轮原地修provider。但Round 3 activation至少需要上述两种transport各一个真实成功证据。

不得为dogfood修改或打印`.env` secret。临时database必须使用可重置ephemeral database并在结束后清理。

### 21.3 Budget场景

dogfood还应使用测试only deterministic large optional source或tool result迫使至少一次真实degradation，并证明：

- provider实际收到的是selected compact/ref projection；
- required base/runtime/active skill仍存在；
- final estimate与transport前estimate一致；
-未打开第二次provider请求做“试探性重编译”。

## 22. Definition of Done

Round 3只有同时满足以下条件才可标记`ACTIVATED`：

1. production ROOT与subagent foreground model calls全部走structured compiler；
2. canonical reader仍是唯一transcript truth owner；
3. direct provider adapter不再编译prompt；
4. exact target、provider-neutral estimator binding和scope-filtered tool surface在compile前冻结，pure compiler无transport capability；
5.五类初始source以typed candidate进入；
6. workspace root/kind、Terminal cwd与clock真实model-visible；
7. capability catalog/active skill不再与base prompt不可分地拼接；
8. Terminal/clock/tool正文与SUBAGENT objective不能触发textual skill activation；
9. source和tool-result在budget压力下确定性degrade；
10. current user、history order、tool pairing、closure与late outcome不漂移；
11. Round 1 artifact reference在compact/ref mode可用且scope安全；
12. protected input超限时typed fail并zero provider open；
13. frozen compiler estimate与adapter thaw后的transport-aware validation exact equal；
14. optional source/hook failure不阻塞happy path；
15.无compile table/event/job/blob/receipt/replay/cache correctness graph；
16. `27 / 23 / 13 / 2 / 24 / 4` oracle保持；
17. canonical transaction/event budget不增长；
18. full pytest、PostgreSQL targeted、Go、ruff、compileall、protocol、lock与diff gates全绿；
19. no新增skip/xfail；
20. real-provider两类transport dogfood成功；
21. activation evidence可机器读取且不含敏感正文；
22. PHC-17更新为已恢复，但future Plan/MCP/failure/timing/Memory仍分别保持open；
23. frozen tool schema/arguments、assistant semantic lowering、single temporal capture与closed public diagnostics均有architecture regression；
24. implementation未stage、commit或push，除非用户另行明确要求。

## 23. Coding handoff边界

交给coding agent时应强调：

- 这是产品能力复原，不是回滚hard-cut；
- 从`5b7ad9f7`提取纯compiler/source/budget语义，不移植durability/recovery graph；
- 先完成R3-0 inventory，之后按R3-A/B/C/D/F推进；
- 不把旧测试夹具强行修到旧EventLog形状；测试必须改写为当前canonical Kernel事实；
- 不为通过budget tests裁剪current user或破坏tool pairing；
- 不为观测保存full prompt；
- 不把`ResolvedModelCall`、transport或mutable dict塞进pure compiler DTO；
- tool surface一致性只使用第6.2节窄process-local access/borrow，不把它扩成durable lease、receipt或repair graph；
- child objective不得因被编码成USER而获得human skill activation authority；
- 不顺手实现Memory、Plan/permission、MCP、failure note、timing或compaction；
- 不把exact count oracle误当未来source数量上限；
- 任何新增durable relation/event/job、Protocol变更、第二个model scheduler或第二个source authority都必须停止并回报；
- implementation结束时必须报告真实diff、test collection、full pytest、PostgreSQL、real-provider dogfood、known gaps与activation evidence位置。

本轮理想终局不是“旧compiler代码回来了”，而是：

> 当前canonical conversation Kernel拥有一个小而正式的provider-neutral model-input compiler。它能把exact canonical history与当前已授权facts编译为typed、可预算、可解释的process-local input，同时不让prompt compilation重新成为execution recovery或durable replay state machine。
