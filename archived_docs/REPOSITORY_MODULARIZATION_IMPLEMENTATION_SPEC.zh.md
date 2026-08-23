# Pulsara Conversation Kernel Repository 模块化实施规格

> 状态：**ACTIVATED — 2026-08-14（纯结构重构，产品与持久化语义不变）**
>
> M0–M6 已闭合：薄 facade 保留原 import/FQCN/pickle identity，128 methods、29 top-level functions、542 database-call shapes、36+3 checkout sites 与全部 baseline pytest node IDs 经机器门控证明等价。
>
> 记录日期：2026-08-14
>
> 当前编码基线：`edbe7aea5518085028657aedc161d8fcbe88bb6b`
>
> 当前真源文件：[`src/pulsara_agent/conversation_kernel/repository.py`](src/pulsara_agent/conversation_kernel/repository.py)
>
> 当前真源 SHA-256：`43669989c424012e84874d15d85ca3d6842f216d025fa2ae2be293166b2b915e`
>
> 上位架构：[`PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md`](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 当前产品恢复索引：[`POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md`](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)

---

## 0. 执行结论

当前 `ConversationKernelRepository` 已经成为 canonical relational conversation kernel 的唯一 PostgreSQL 产品仓储入口，但它的物理实现集中在一个 12,826 行文件中。继续在该文件上恢复 hierarchical subagent、memory 或 compaction，会让代码审阅、冲突隔离和事务边界核查越来越困难。

本次工作的目标只有一个：

~~~text
把 repository.py 的实现机械拆入一个内部目录，
同时保持 repository.py 作为唯一兼容 facade，
不改变任何可观察行为、事务、SQL、identity、exception 或 import path。
~~~

本次不是产品 Round，不恢复任何缺失能力，也不借机重新设计 repository API。它不得改变：

- canonical row 或 selective committed journal 的语义；
- Host writer / job-attempt claim 两类 append authority；
- canonical row 与对应 committed occurrence 的同事务提交；
- ACK-unknown 的 `FULL | NONE | CONFLICT` confirmation；
- prompt、tool、Plan、subagent、job、memory 的状态机；
- PostgreSQL schema、migration、relation 或 index；
- `34 / 23 / 15 / 2 / 26 / 4` activation oracle；
- provider-input、LiveAgentEvent、MCP、Terminal 或 Go Protocol 行为。

最终物理形状冻结为：

~~~text
src/pulsara_agent/conversation_kernel/
├── repository.py                 # 薄兼容 facade；公开 import path 与 class identity 保持
└── _repository/                  # internal implementation package
    ├── __init__.py               # internal marker，不建立第二套公开 API
    ├── contracts.py              # exceptions、DTO、prepared candidates、pure builders
    ├── matching.py               # stateless matchers + pure _MatchingOperations mixin
    ├── kernel.py                 # 唯一 provider/transaction/guard/event/entry primitive owner
    ├── authority.py              # workspace、Host writer acquire/renew
    ├── conversation.py           # turn、assistant、snapshot、terminal observation、rehydrate/close
    ├── tools.py                  # capability decision、attempt、remote identity、result/interaction
    ├── plans.py                  # Plan workflow、question、draft、continuation及其私有SQL helper
    ├── prompts.py                # ingress、queue、steer、reject/cancel/head
    ├── subagents.py              # flat task/turn/child/query
    ├── external_results.py       # subagent/job result进入ROOT的shared safe-point acceptance
    ├── jobs.py                   # enqueue、claim、provider-call、settlement、job source/result
    └── memory.py                 # 当前memory candidate/governance/index实现的原样搬迁
~~~

`repository.py` 必须继续存在。它不再承载大段SQL，但仍定义公开的 `ConversationKernelRepository` facade并重导出现有符号。这样可以同时保持：

- `pulsara_agent.conversation_kernel.repository` import path；
- `ConversationKernelRepository.__module__`；
- 当前文档和代码链接；
- 已盘点的downstream subclass与故障注入override seam；
- 不需要修改production caller import。

不得用同名 `repository/` package替换 `repository.py`；该做法会改变class module identity、破坏现有本地代码链接，并扩大无关caller diff。

---

## 1. 当前代码真值

### 1.1 物理规模

当前基线的AST与引用探针结果为：

| 项目 | 当前值 |
|---|---:|
| `repository.py` 行数 | 12,826 |
| 顶层class | 43 |
| 顶层function | 29 |
| `ConversationKernelRepository`方法 | 128 |
| 其中public方法 | 81 |
| 其中private方法 | 47 |
| 当前`__all__`条目 | 34 |
| 当前production/tests直接import到的不同符号 | 41 |
| SQL字面量启发式inventory | 315 |
| `pulsara:` domain literal occurrence | 37 |

上述数值用于发现机械迁移遗漏，不是永久产品oracle。实施前必须由baseline inventory工具在实际checkpoint上重新生成；若实际HEAD已经变化，应记录新值并解释差异，不能把本文数值硬编码成错误真值。

### 1.2 当前职责簇

当前单类实际包含以下职责：

1. connection provider、Host writer与job claim transaction；
2. entry/event sequence分配和selective journal append；
3. ROOT turn、provider cut、assistant message与context snapshot；
4. Terminal observation canonical acceptance；
5. tool permission、attempt、remote identity、result与human decision；
6. Plan workflow、question、draft review与automatic continuation；
7. prompt ingress、future turn queue与active-turn steer；
8. flat subagent task、task-scoped turn与child message/result；
9. subagent/job external result进入ROOT；
10. durable job enqueue/claim/settlement；
11. current memory candidate/governance/FTS/vector mutation；
12. session rehydrate、command query、close与journal suffix query；
13. prepared candidate、semantic fingerprint及confirmation matching。

拆分只改变这些职责的文件归属，不改变职责本身或owner数量。

### 1.3 公开import面不能只按`__all__`迁移

当前调用方通过以下稳定路径导入：

~~~python
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
~~~

调用方还直接导入大量contracts/builders。当前观察到41个不同符号，其中一部分没有列入`repository.py.__all__`，例如Plan DTO、`PromptDeliveryMode`以及部分historical compatibility import。实施者必须从AST caller inventory冻结实际import面，不能只复制34项`__all__`后宣称兼容。

至少必须保持以下类别：

- `ConversationKernelRepository`；
- `ConversationKernelConflict`及现有stable exception；
- assistant block DTO；
- accepted/prepared/confirmation DTO；
- Plan control与resolution DTO；
- root/subagent/tool prepared candidate builders；
- 当前调用方实际依赖的transitive compatibility imports。

最终`repository.py.__all__`默认保持现有34项完全相等。是否将其他7个实际import符号补入`__all__`不是本轮范围；它们只需继续可被direct import。

### 1.4 subclass与故障注入是现有测试契约

现有测试通过继承 `ConversationKernelRepository` 覆盖下列方法注入真实故障：

~~~text
_writer_transaction
_append_events
commit_assistant_message
consume_prepared_prompt_steer
accept_tool_result
accept_root_turn
confirm_root_turn_admission
accept_subagent_turn
confirm_subagent_turn_admission
publish_tool_remote_identity
confirm_tool_remote_identity
~~~

这些不是应删除的测试技巧，而是在没有durable receipt/recovery graph的前提下验证ACK unknown、cancellation与transaction failure的必要seam。

最终facade必须继续可被正常继承。domain mixin中的调用必须保持当前虚拟分派语义：当前通过`self.method()`调用的地方不能改成module-level直调，从而绕过subclass override。

当前少量显式 `ConversationKernelRepository._helper(...)` 调用属于非虚拟分派；机械迁移必须记录并保留其原行为，不能无意改成可override或指向错误mixin。

### 1.5 当前path-based architecture guards

以下测试直接读取 `repository.py` 源码：

- [`tests/test_stage2_architecture.py`](tests/test_stage2_architecture.py)
- [`tests/test_round2_terminal_architecture.py`](tests/test_round2_terminal_architecture.py)
- [`tests/test_round4_architecture.py`](tests/test_round4_architecture.py)
- [`tests/test_round6_mcp_production.py`](tests/test_round6_mcp_production.py)

拆分时不得删除或弱化这些断言。它们应改为读取：

~~~text
repository.py
+ sorted(_repository/**/*.py)
~~~

并在aggregate source/AST上继续证明原有架构结论。仅因token不再位于facade就删除guard，属于验收失败。

---

## 2. 冻结的不变量

### 2.1 单一repository authority

最终只能存在一个public concrete repository：

~~~text
ConversationKernelRepository
~~~

禁止新增：

- `PlanRepository`、`ToolRepository`、`MemoryRepository`等可独立实例化的public repository；
- 每个domain各自持有connection provider；
- domain module新增未被baseline physical-path manifest记录的direct mutation checkout；
- domain module自己分配event sequence或建立第二个event appender；
- facade与domain service双写或镜像状态。

内部文件按实现职责拆开，不意味着authority被拆成多个owner。

### 2.2 transaction与physical checkout边界完全保持

当前代码并不是“所有Host mutation都先有Host guard、所有Job mutation都先有Job claim”的三入口模型。writer与job claim自身必须先有bootstrap transaction，guard才能被签发。迁移前后必须保持以下五类真实物理路径：

| physical path | 当前入口/代表方法 | guard与event batch | 必须冻结的事实 |
|---|---|---|---|
| writer bootstrap / renew | `acquire_host_writer()`、`renew_host_writer()`直接checkout | acquire在guard产生前管理event batch并可interrupt prior generation；renew使用既有guard字段做CAS但不进入`_writer_transaction` | `HOST_CONTROL` lane、row factory、deadline、session lock/CAS、generation与batch行为逐方法保持 |
| guarded writer mutation | `_writer_transaction(HostWriterGuard)` | transaction入口`_require_writer(lock=True)`，统一begin/finish event batch | lane、row factory、deadline、lock order、exception settlement保持 |
| job-claim bootstrap | `claim_due_job()`通过`_event_transaction(BACKGROUND_WORK)` | 在claim存在前锁定/revalidate job，创建或接管attempt，transaction内签发guard；event batch由`_event_transaction`拥有 | candidate lookup、session/job/attempt lock order、reaper generation与event顺序保持 |
| guarded job mutation | `_job_transaction(JobAttemptClaimGuard)` | origin session lock、`_require_job_claim(lock=True)`、cancel check与event batch保持 | lane、row factory、deadline、allow-cancel分支保持 |
| direct read / preflight / confirmation | 当前36个public/private operation中的direct`self._provider.connection(...)` site | 不因“confirmation”名称自动获得event batch；部分public operation随后另开guarded writer transaction，这是当前既定顺序 | 每个site的lane、row factory、deadline、query/参数/fetch shape和后续分支逐项保持 |

baseline inventory按**每个physical checkout site**分类，而不是只给public method贴一个标签；`resolve_plan_question()`等同时含direct preflight与guarded mutation的方法必须记录两段路径。最终provider调用总inventory还包括三个transaction helper内部checkout，因此当前基线是36个direct operation site加3个shared transaction-helper site。

禁止为了让模块更“独立”而把一个现有transaction拆成两次repository调用。尤其必须保持：

- assistant entry + ordered blocks + occurrence；
- tool result entry + result row + artifact edge + memory side branch + occurrence；
- Plan transition + interaction + continuation turn + occurrences；
- prompt consume/reject + turn transition + occurrences；
- external result entry + exact source edge + occurrence；
- memory candidate/governance + job enqueue/terminal occurrence；
- job attempt/settlement与aggregate terminal occurrence。

### 2.3 SQL与锁顺序保持

本轮不得：

- 改写SQL为ORM/query builder；
- 合并或拆开SQL statement；
- 重排`SELECT ... FOR UPDATE`；
- 改变table lock顺序；
- 改变CAS predicate；
- 修改isolation level、read/write lane或deadline source；
- 修改schema-qualified relation名；
- 顺手增加index、constraint、column或migration。

移动时应尽量保持每个SQL expression及其参数字节/AST相等。仅比较SQL literal不足以覆盖动态`f"...{lock_clause}..."`、先选择query再执行、参数tuple顺序或fetch shape。若仅因Python缩进造成三引号字符串前导空白变化，必须证明发送给PostgreSQL的字符串等价，并由database-call manifest采用明确的canonicalization口径；不得人工忽略任意SQL drift。

### 2.4 identity与confirmation保持

以下内容逐字保持：

- candidate fingerprint domain separator；
- stable ID domain与字段顺序；
- event ID派生；
- occurred_at/actor冻结时机；
- content digest与canonical JSON编码；
- `FULL | NONE | CONFLICT`判定；
- stateless exact confirmation查询；
- compatible winner与semantic conflict规则。

不得在拆分时重新生成candidate、改用随机ID、改变hash载荷，或让重试绑定新的physical attempt metadata。

### 2.5 row/event truth关系保持

拆分后仍必须满足：

~~~text
canonical row = 当前semantic truth
committed event = transition occurrence/audit truth

row与对应event由同一owner在同一PostgreSQL transaction接受
event不得用于证明row已经真实
reopen读取canonical rows，不通过event replay恢复execution
~~~

### 2.6 对外Python行为保持

必须保持：

- module import path；
- `ConversationKernelRepository`的class name、`__module__`与`__qualname__`；
- constructor signature；
- 128个方法的名称、sync/async属性、decorator、signature与default；
- exception type及inheritance；
- dataclass field顺序、frozen/slots配置与validation；
- return DTO及tuple/order；
- subclass override行为；
- current `__all__`顺序；
- 当前41个observed direct-import symbol可解析到同一对象语义；
- 其中当前由`conversation_kernel.repository`自身定义的39个class/function，其`__module__`与`__qualname__`保持；另外两个transitive compatibility symbol保持各自baseline identity。

不要求保持源码line number、private实现文件路径或traceback内部frame布局。

---

## 3. 目标模块与owner边界

### 3.1 `repository.py`：唯一公开facade

最终facade只允许承担：

1. 从`_repository.contracts`重导出现有public/compatibility symbols，并为closed 39-symbol manifest恢复原facade metadata identity；
2. 导入internal operation mixins与`_RepositoryKernel`；
3. 定义唯一public `ConversationKernelRepository`；
4. 保持当前`__all__`。

建议最终形状：

~~~python
class ConversationKernelRepository(
    _MatchingOperations,
    _AuthorityOperations,
    _ConversationOperations,
    _ToolOperations,
    _PlanOperations,
    _PromptOperations,
    _SubagentOperations,
    _ExternalResultOperations,
    _JobOperations,
    _MemoryOperations,
    _RepositoryKernel,
):
    """PostgreSQL repository for the canonical conversation kernel."""
~~~

最终facade不得包含SQL、transaction实现或数百行delegator。使用mixin是为了机械保留现有`self`调用与subclass override，不是建立新的多authority继承架构。

public symbol可以实际定义在facade，也可以从internal module重导出后，以显式closed manifest把`__module__`恢复为`pulsara_agent.conversation_kernel.repository`；不得用wrapper class/function制造第二个Python对象。pickle/FQCN probe必须能从facade解析回同一对象。

所有internal mixin class均以下划线开头，不进入`__all__`，不得由production caller直接实例化。

### 3.2 `contracts.py`

拥有：

- stable exceptions；
- accepted/prepared/confirmation dataclass与closed enum；
- assistant block DTO；
- root/subagent admission builders；
- tool remote identity/result builders；
- Plan semantic fingerprint/candidate builders；
- pure content/candidate manifest helper。

禁止：

- import psycopg connection provider；
- checkout connection；
- 执行SQL；
- 持有repository实例；
- 启动task或做I/O。

### 3.3 `matching.py`

拥有迁移后仍为纯函数的confirmation matcher，以及一个不持有状态、不做I/O的`_MatchingOperations` mixin：

- prepared steer row matching；
- event row与draft matching；
- required scalar decoding；
- `_MatchingOperations._content_from_row()`：原样保留当前repository `@staticmethod`的signature、decorator与method identity，作为唯一canonical content row decoder。

`_MatchingOperations`不得定义constructor或字段，也不能独立实例化成repository authority；facade MRO必须显式包含它。这样现有`self._content_from_row(...)`调用、128-method数量和staticmethod分派全部保持。

该模块可以接受row/value并返回pure结果，不得查询数据库，也不得反向引用公开facade class。当前顶层`_prompt_steer_row_matches_resource_rejection()`对`ConversationKernelRepository._content_from_row()`的引用必须closed-remap为`_MatchingOperations._content_from_row()`；除此之外现有`self._content_from_row(...)`调用保持原样。这一项是baseline manifest中唯一明确允许的`_content_from_row` owner rename，不改变已盘点的subclass/override seam。

当前`_load_root_transcript_cut()`不是matcher：它执行transcript/blob多表查询、`fetchall()`及row/byte bound。它及两个compaction/job调用方整体迁入`jobs.py`，不得放进`matching.py`。

### 3.4 `kernel.py`

这是唯一共享物理仓储内核，拥有：

- `__init__`与connection provider字段；
- `connection_provider()`；
- `_writer_transaction()`；
- `_job_transaction()`；
- `_event_transaction()`；
- event batch thread-local bookkeeping；
- `_require_writer()` / `_require_job_claim()`；
- prior generation interruption primitive；
- workspace/session generic lookup；
- provider-safe-turn in-transaction predicate；
- entry/event sequence allocation；
- `_append_events()` / `_insert_event()`；
- generic entry/block insert与row decode；
- permission column/frozen snapshot等被多个domain共享的无独立authority primitive。

只有该模块可以保存`VerifiedPostgresConnectionProviderProtocol`实例。domain operation通常通过三类shared transaction helper取得connection；writer bootstrap/renew、job claim bootstrap及36个baseline direct read/preflight/confirmation checkout可以继续访问继承的`self._provider`，但只允许出现在physical-path manifest已经冻结的exact method/site中。不得新增第37个direct operation site或改变现有site的lane/row factory/deadline。

### 3.5 domain operation modules

| 模块 | 迁移的主要public operation | 必须留在同一模块的private helper |
|---|---|---|
| `authority.py` | workspace读取、Host writer acquire/renew | writer lease与generation校验相关helper |
| `conversation.py` | ROOT admission、provider cut/safe point、Terminal observation、snapshot、assistant commit/confirm、interrupt、turn status、rehydrate、command query、session close、events suffix | assistant/terminal confirmation与conversation-only query helper |
| `tools.py` | capability decision、attempt、remote identity、tool result、tool interaction decision | artifact blob exact join、tool-result memory side branch confirmation |
| `plans.py` | Plan batch、question、enter/force-exit/exit/review、continuation inspection/content read | 全部Plan workflow/interaction/continuation insert与confirmation helper |
| `prompts.py` | ingress confirm/enqueue、head consume、steer read/consume/confirm/reject、cancel/head query | terminal steer head cleanup与resource rejection helper |
| `subagents.py` | task accept/status、task turn admission、child accept、query/list | subagent-only candidate/confirmation helper |
| `external_results.py` | subagent result与job result进入ROOT | exact external target preparation与safe-point rules |
| `jobs.py` | enqueue/cancel/claim、provider call start、settlement、compaction/extraction job source/result | job candidate/claim confirmation，以及有真实SQL I/O的`_load_root_transcript_cut()` |
| `memory.py` | extraction bundle、candidate/governance、FTS/vector snapshot/apply | 当前memory SQL与validation helper |

这是最终owner表。若一个现有private helper被多个domain调用，应优先移入`kernel.py`，或在确属pure row matching时作为`_MatchingOperations` method保留；不得复制两份。若helper明显只表达Plan、tool或memory语义，应保留在相应domain，并通过现有`self`调用从facade组合访问，不能为了消除一次cross-call复制SQL。

### 3.6 internal dependency DAG

最终import方向必须满足：

~~~text
contracts.py       matching.py (_MatchingOperations)
      \                 /
        kernel + domain operation mixins
                  |
           repository.py facade
~~~

具体规则：

- `_repository`内部模块不得import公开`conversation_kernel.repository` facade；
- domain module之间原则上不直接import implementation class；
- `contracts.py`和`matching.py`不得import domain module；
- `kernel.py`不得importfacade；
- `repository.py`是唯一汇聚点；
- 不得用lazy import掩盖循环依赖；
- 不得在`_repository/__init__.py`重导出整套public API。

---

## 4. Baseline inventory与architecture guards

### 4.1 第一个production move之前必须生成baseline

新增一个测试/开发工具，例如：

~~~text
tools/repository_modularization_inventory.py
tests/fixtures/repository_modularization_baseline.json
~~~

baseline fixture至少记录：

- checkpoint HEAD；
- `repository.py` SHA-256；
- 当前`__all__`有序列表；
- production/tests observed direct-import symbol集合；
- 41个observed symbol的`__module__`、`__qualname__`与object-kind，其中39个当前owned symbol必须保持facade FQCN；
- public top-level class/function kind与必要signature；
- `ConversationKernelRepository` constructor；
- 128个方法的名称、sync/async、decorator与signature；
- 全部128个method与29个top-level function的normalized AST body digest；
- exception inheritance；
- dataclass field顺序与frozen/slots属性；
- 每个database-call site的owner definition、ordered SQL expression AST、parameter AST、`fetchone/fetchall` shape、lane、isolation、row factory与deadline expression；
- 动态lock f-string、runtime-selected query及有序branch的normalized AST digest；
- 36个direct operation checkout与三个shared transaction-helper checkout的closed site inventory；
- per-method stable/domain-separator literal digest；
- class-qualified nonvirtual helper call inventory；
- subclass override seam inventory；
- physical checkout site到五类transaction path、guard check和event-batch owner的closed classification；
- 当前完整pytest collection的有序node-ID集合。

normalized AST body digest使用`ast.dump(..., include_attributes=False)`或等价稳定编码，忽略文件位置/行号，但不忽略语句顺序、call target、参数顺序、分支、fetch shape或异常路径。唯一允许的语法变化是fixture明确列出的closed owner rename，例如`ConversationKernelRepository._content_from_row`改绑`_MatchingOperations._content_from_row`；每项rename必须在before/after manifest中一一对应，不能使用任意字符串替换。

该fixture只证明机械等价，不进入production package，不成为runtime registry。

baseline一旦从干净checkpoint生成，实施过程中不得为让gate变绿而重新生成。若确有baseline probe bug，必须先说明并单独修正probe，不能同时修改implementation和expected fixture。

### 4.2 最终architecture gate

新增：

~~~text
tests/test_repository_modularization_architecture.py
~~~

至少证明：

1. `repository.py`仍存在且只承担facade；
2. `_repository/`不存在`_monolith.py`、`legacy.py`或完整复制文件；
3. 最终只有一个public concrete `ConversationKernelRepository`；
4. internal mixin不能独立构造provider；
5. connection provider字段与三类transaction primitive只有一份；
6. 每个baseline method在最终aggregate AST中恰好定义一次；
7. public signature、exception/dataclass、`__all__`、observed import surface以及39个owned symbol的FQCN与baseline一致；
8. normalized method/function AST、database-call、SQL/parameter/fetch与identity manifest除closed owner rename外无漂移；
9. path-based旧architecture guards已改为aggregate source且语义未弱化；
10. oracle仍为`34 / 23 / 15 / 2 / 26 / 4`；
11. 没有新增migration、relation、event、subject、guard或job；
12. production caller不直接import `_repository.*`；
13. baseline pytest node-ID集合是最终collection集合的子集。

### 4.3 import compatibility probe

必须至少执行：

~~~python
import pulsara_agent.conversation_kernel.repository as repository

assert repository.ConversationKernelRepository.__module__ == (
    "pulsara_agent.conversation_kernel.repository"
)
~~~

并逐项导入baseline中所有observed symbols，比较`__module__`/`__qualname__`/object-kind；39个owned class/function必须继续从facade FQCN解析为同一对象。对象从internal module重导出是允许的，但外部import路径、pickle identity、名称与语义必须保持。

---

## 5. 实施切片

### M0：Dormant baseline gate

只新增inventory工具、baseline fixture和architecture test，不改production代码。

Exit gate：

- baseline HEAD/SHA与当前干净checkpoint一致；
- inventory覆盖128个方法、29个top-level function、34项`__all__`、41个observed direct imports、全部database-call site和pytest node IDs；
- 新gate在monolith状态下通过；
- full collection无变化；
- 不改变任何product behavior。

### M1：建立内部package与薄facade骨架

新增`_repository/`目录。此切片可先把现有实现完整移动到一个临时internal module，以便建立facade与import compatibility，但该临时monolith只能作为迁移中间态，不能进入最终activation。

本切片必须先证明：

- 所有production caller无需修改import；
- 39个owned public symbol（其中包括repository facade class）的module/qualname identity保持；
- subclass故障注入仍生效；
- path-based architecture gate已采用aggregate source；
- targeted与PostgreSQL测试保持。

不得把临时monolith提交为最终完成状态。

### M2：迁移pure contracts与matching

优先移动不执行I/O的内容：

- exceptions/enums/dataclasses；
- prepared candidate builders；
- semantic fingerprint/payload builders；
- row/event/candidate pure matcher；
- bounded content decoding helper。

Exit gate：

- contracts/matching不import psycopg provider；
- `_content_from_row`仍是facade可达的staticmethod，aggregate repository method数量仍为128；
- dataclass与signature manifest完全相等；
- candidate fingerprints有golden equivalence；
- ACK confirmation targeted tests通过。

### M3：冻结唯一repository kernel

移动constructor、provider、transaction、guard、event/entry primitive到`kernel.py`。

Exit gate：

- `_writer_transaction`、`_job_transaction`、`_event_transaction`各只有一个定义；
- event/entry sequence allocator各只有一个定义；
- `_append_events` override seam仍可注入失败；
- 五类physical path、39个provider checkout site及transaction/event-batch manifest完全相等；
- PostgreSQL corruption/fencing测试保持fail closed。

### M4：逐domain机械迁移

建议顺序：

~~~text
authority
memory
jobs
subagents
external_results
prompts
tools
conversation
plans
~~~

顺序只用于降低依赖冲突，不授权改变语义。每移动一个domain：

1. 从临时monolith删除原方法；
2. 在目标mixin定义相同方法；
3. 确认aggregate AST中该方法恰好一次；
4. 运行该domain targeted tests；
5. 比较SQL/identity/transaction manifest；
6. 再进行下一个domain。

Plan最后迁移，因为它同时使用permission、interaction、turn admission、continuation和event primitive；不得为降低文件长度拆散Plan事务。

### M5：删除迁移脚手架

最终必须删除：

- `_monolith.py`或等价临时整文件；
- duplicate compatibility wrappers；
- unused imports；
- migration-only alias；
- 为旧文件位置保留的source concatenation hack。

`repository.py`最终只能是薄facade；`_repository`目录是唯一implementation owner。

### M6：最终验证与机器证据

新增：

~~~text
benchmarks/suites/core/v1/repository_modularization_activation.json
~~~

机器证据至少记录：

- baseline/final HEAD与source manifest；
- 最终module列表与每个文件职责；
- method/import/signature/SQL/identity equivalence结果；
- transaction owner与oracle结果；
- targeted/full/PostgreSQL/静态验证；
- production diff只包含facade/internal package；
- 无schema、protocol或产品行为变化。

---

## 6. 关键交叉边界

### 6.1 ToolResult与memory side branch

`accept_tool_result()`可以在同一transaction内接受memory proposal side branch。拆分后不得先提交tool result再调用memory repository。

正确边界：

~~~text
tools.py owns accept_tool_result transaction
    -> same connection validates/inserts optional memory proposal branch
    -> same event batch commits all accepted occurrences
~~~

`_confirm_memory_proposal_side_branch()`可留在`tools.py`，因为它服务于tool-result atomic candidate；不能为了文件命名纯度把它变成第二次memory transaction。

### 6.2 Plan与new-turn admission

ROOT new-turn producer必须继续读取OPEN Plan interaction/admission fence。conversation、prompt、Terminal wake、external result等调用路径不能因Plan代码移入`plans.py`而绕过该约束。

共享的`_require_root_admission_open()`可以进入`kernel.py`；Plan-specific classification与handoff仍留在`plans.py`。无论物理归属如何，现有lock顺序与同事务check保持。

### 6.3 Prompt steer与turn interruption

resource rejection、Plan conflict及turn interruption当前有原子组合路径。拆分后不得变为：

~~~text
prompts.py commits rejection
conversation.py later interrupts turn
~~~

正确实现仍是prompt operation持有同一个writer transaction并写完queue/turn/event changes。

### 6.4 External results

subagent result与job result共享ROOT safe-point target preparation，但各自source identity不同。`external_results.py`可以承载shared algorithm；不得把两种source降成自由字符串union，也不得允许result倒插已经冻结的provider cut。

### 6.5 Job与memory

memory governance/extraction会创建或结算durable job。模块化后exact-four job catalog、attempt claim与terminal occurrence保持不变。memory operation可以在其当前transaction内写job rows；不能通过新的异步callback或process-local queue延迟canonical acceptance。

### 6.6 class-qualified helper调用

当前源码包含显式：

~~~text
ConversationKernelRepository._permission_columns(...)
ConversationKernelRepository._insert_entry(...)
ConversationKernelRepository._content_from_row(...)
~~~

实施者必须逐项决定新physical owner，并保持非虚拟调用语义。`_content_from_row`已经冻结为`_MatchingOperations` staticmethod；另外两项可以按最终唯一owner改绑，例如`_RepositoryKernel._insert_entry(...)`，但不得不经审查改成`self._insert_entry(...)`，因为这会改变subclass override面。

本轮不承诺任意private helper都继续响应对`ConversationKernelRepository`的monkeypatch。兼容范围只包括M0实际盘点出的subclass/override故障注入seam；上述三个class-qualified helper当前不在该集合中，因此可以使用closed owner rename移到唯一internal owner，而无需在facade安装compatibility descriptor。若实施前发现真实production/test monkeypatch调用，必须把它加入baseline seam再决定，不得凭假设扩展兼容机制。

---

## 7. Failure matrix

| 场景 | 必须行为 |
|---|---|
| public import缺失或对象类型漂移 | 阻塞切片，不增加compat fallback package |
| owned public symbol的module/qualname漂移 | 阻塞；保持facade FQCN或明确证明它不在closed public manifest |
| method signature/decorator漂移 | 阻塞；恢复baseline，不修改fixture |
| normalized AST或database-call manifest漂移 | 仅closed owner rename可接受；其余阻塞并人工核对 |
| SQL/parameter/fetch/lane/deadline漂移 | 阻塞；本轮默认不允许 |
| identity/domain separator漂移 | 阻塞；不得生成新winner兼容旧candidate |
| transaction owner/lock order漂移 | 阻塞；不得以测试仍绿为理由接受 |
| subclass override不再命中 | 阻塞；保持`self`虚拟分派或原class-qualified语义 |
| import cycle | 调整internal dependency，不使用lazy import掩盖 |
| old architecture test只因文件移动失败 | 改为aggregate AST/source，保留原断言 |
| targeted regression | 修正机械迁移，不顺手改变产品expectation |
| full-suite existing test需要删除/skip/xfail | 禁止；该重构要求零删除、零新增skip/xfail |
| PostgreSQL环境不可用 | 记录为未验证，不能标记ACTIVATED |
| final tree仍存在monolith或重复SQL | 不满足DoD |

---

## 8. 测试与验证

### 8.1 最小targeted gate

至少覆盖：

~~~bash
uv run pytest -q \
  tests/test_repository_modularization_architecture.py \
  tests/test_stage2_architecture.py \
  tests/test_stage3_5_architecture.py \
  tests/test_round2_terminal_architecture.py \
  tests/test_round4_architecture.py \
  tests/test_stage2_conversation_runner.py \
  tests/test_round1_tool_output_artifact.py \
  tests/test_round4_plan_workflow.py \
  tests/test_round5_long_horizon_execution_envelope.py \
  tests/test_round6_mcp_production.py
~~~

### 8.2 PostgreSQL gate

必须覆盖当前repository的主要事务族：

~~~bash
uv run pytest -q \
  tests/test_stage2_conversation_kernel_postgres.py \
  tests/test_round2_terminal_postgres.py \
  tests/test_round4_plan_postgres.py \
  tests/test_round5_long_horizon_postgres.py
~~~

以及项目当前统一PostgreSQL marker gate。必须使用ephemeral/test database，不得依赖生产数据。

### 8.3 全量与静态gate

~~~bash
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src tests tools
uv run python tools/generate_terminal_protocol_contract.py --check
(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)
(cd clients/terminal && go mod verify)
uv lock --check
git diff --check
~~~

本轮不需要real-provider dogfood：provider、MCP、tool或wire行为均不应变化。若实现者认为必须依赖dogfood证明正确，通常说明本次改动已经越过纯结构重构边界。

### 8.4 测试数量口径

当前Round 6 checkpoint已报告541项full pytest通过。M0必须通过`pytest --collect-only`保存每一个完整node ID，而不是只保存数量。新增repository architecture test会增加collection数量，因此最终不要求exact仍为541；要求：

- baseline node-ID集合是最终node-ID集合的子集；
- 不删除或改名掩盖原测试；
- 新增测试只验证模块化等价与architecture；
- 新增skip/xfail为0；
- full suite零失败。

---

## 9. 修改面

### 9.1 允许的production修改

~~~text
src/pulsara_agent/conversation_kernel/repository.py
src/pulsara_agent/conversation_kernel/_repository/**
~~~

原则上不应修改其他production文件，因为公开import path保持不变。

### 9.2 允许的非production修改

~~~text
tools/repository_modularization_inventory.py
tests/fixtures/repository_modularization_baseline.json
tests/test_repository_modularization_architecture.py
现有四个path-based architecture tests
benchmarks/suites/core/v1/repository_modularization_activation.json
REPOSITORY_MODULARIZATION_IMPLEMENTATION_SPEC.zh.md
~~~

现有测试只允许为source location/aggregate AST适配做窄修改；不能重写产品断言。

### 9.3 禁止修改

- migrations、clean baseline SQL与catalog/grant manifest；
- README产品宣称；
- Protocol schema或Go client；
- event vocabulary、job catalog、subject slots与append guards；
- runner/Host/tool/Plan/MCP/Terminal业务逻辑；
- pyproject、lockfile或依赖；
- Gap Index产品状态；
- memory、subagent或compaction产品行为。

若机械迁移暴露真实既有bug，应单独记录，不能混入本次diff修复。

---

## 10. Definition of Done

只有同时满足以下条件，才可把本文标记为`ACTIVATED`：

1. `repository.py`已缩为薄facade，公开module/class identity保持；
2. implementation全部位于`_repository/`的closed owner modules；
3. final tree无monolith、duplicate SQL或temporary shim；
4. baseline `__all__`与41个observed import symbols保持；
5. 128个method及29个top-level function的signature/normalized AST、sync/async、decorator和定义唯一性保持；
6. exception/dataclass/candidate contract及39个owned public symbol的FQCN保持；
7. database-call、SQL expression、parameter、fetch、lane、deadline与identity manifest无未解释漂移；
8. subclass override/ACK-unknown fault injection继续生效；
9. writer bootstrap/renew、guarded writer、job claim bootstrap、guarded job及direct read/confirmation五类physical path与锁顺序保持；
10. canonical row/event同事务关系保持；
11. path-based architecture guards已聚合扫描且没有弱化；
12. 没有修改schema、migration、Protocol、product docs或依赖；
13. oracle保持`34 / 23 / 15 / 2 / 26 / 4`；
14. targeted、PostgreSQL、full pytest与静态gate全部通过；
15. baseline pytest node-ID集合完整保留，无测试删除、无新增skip/xfail；
16. 机器证据记录最终文件owner与equivalence结果；
17. 未stage、commit或push，除非用户另行明确要求。

完成后应能准确描述本次结果：

> `ConversationKernelRepository`仍是唯一canonical PostgreSQL repository；本次只把其实现按domain与共享transaction kernel机械拆入internal modules。所有public import、SQL、transaction、candidate、confirmation、event与product behavior保持不变。

不能描述为：

- “引入多个domain repositories”；
- “重写canonical persistence layer”；
- “优化了SQL/事务”；
- “恢复了subagent/memory/compaction能力”；
- “建立了新的repository plugin architecture”。

---

## 11. 后续边界

本次完成后，后续产品能力应按最终owner落点增长：

- PHC-13/14的model-visible failure/timing facts主要进入canonical reader/compiler，不应重新膨胀repository facade；
- hierarchical/batch subagent task graph进入`_repository/subagents.py`及必要的新canonical schema/event规格；
- memory专项重设计替换`_repository/memory.py`内部实现，但不建立第二个repository authority；
- Round 5B compaction使用`_repository/jobs.py`、conversation snapshot与compiler rebase边界，不回到universal EventLog。

模块化的价值是让这些未来变更各自落在清楚的owner中；它本身不预先决定这些产品设计。
