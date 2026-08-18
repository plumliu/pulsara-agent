# Round 7.1：Provider-visible ToolResult Projection 与统一 FULL 边界实施规格

> 状态：**ACTIVATED — 2026-08-17**
>
> 记录日期：2026-08-17
>
> 实施checkpoint：`572798cf50650d670dfc2bfcb47e3e6e334d80aa`
>
> hard-cut 前参考基线：`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`
>
> 上位契约：[Round 1 ToolResult artifact](ROUND_1_TOOL_OUTPUT_ARTIFACT_IMPLEMENTATION_SPEC.zh.md)、[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 provider-input prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 7 model-visible observation](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Gap Index](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 直接下游：[Round 9 unified capability semantics](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 9.1 Agent Skills](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)、[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)
>
> 激活证据：[round7_1_provider_visible_tool_result_projection_activation.json](benchmarks/suites/core/v1/round7_1_provider_visible_tool_result_projection_activation.json)
>
> 下游收口注记（2026-08-18）：Round 9.1最终不增加`read_file` Skill intent。既有`SKILL_ACTIVATION` enum literal保留为无producer的reserved compatibility value，ordinary `read_file(SKILL.md)`恒为`BEST_AVAILABLE`；本轮activated production行为与其他三类FULL requirement不变。

本文只修订**普通、全局、provider-visible ToolResult投影**。它不实施compaction，不改变tool execution authority，也不建立任何ToolResult专用durable recovery machinery。R7.1-0～R7.1-F与activation gate均已闭合；实际代码/文档hash、测试、PostgreSQL、real-provider脱敏结果与最终oracle以机器证据为准。

本轮把此前误寄放在Round 5B中的全局阈值与artifact guidance独立出来。完成后，ordinary history、Round 5B retained tail、Builtin、Terminal、MCP、Plan、memory、ordinary Skill file reads与`artifact_read`全部复用同一个normal ToolResult pipeline；Round 5B不再拥有阈值或新的结果variant。

---

## 0. 执行结论

Pulsara已经有正确的两层边界：

~~~text
physical tool outcome
-> ToolResult acceptance时冻结canonical preview + optional artifact
-> compiler从同一canonical preview选择provider FULL / COMPACT / REF_ONLY / OMITTED_BODY
~~~

当前问题不是缺少新架构，而是几个阈值彼此错位：

- canonical `COMPLETE`仍按`32,000 characters`判断，中文、emoji与ASCII得到不同byte风险；
- provider `FULL`没有独立final-message byte cap；
- `COMPACT`的8 KiB预算被误当成普通完整结果的展示上限；
- artifact guidance使用无条件“Use artifact_read”，容易诱导模型偏离主线；
- Round 5B重复声明了这些全局语义，导致compaction看似拥有普通ToolResult contract。

本轮冻结：

~~~text
MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES = 40_000
CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES       = 65_536   # 保持
ARTIFACT_ARCHIVE_THRESHOLD_BYTES               = 8_000    # 保持
HEAD_TAIL_PREVIEW_CHARS                        = 8_000    # 保持
TOOL_RESULT_COMPACT_MAX_UTF8_BYTES             = 8 KiB    # 保持降级职责
TOOL_RESULT_REF_ONLY_MAX_UTF8_BYTES             = 2 KiB    # 保持
ARTIFACT_READ_DEFAULT_CHARS / HARD_CHARS        = 20_000 / 32_000  # 保持请求上限
~~~

其中只有前两个“40,000”判断共享同一semantic常量：

1. sanitized candidate body能否成为canonical `COMPLETE`的soft decision；
2. 最终provider-visible `FULL` message能否进入variant集合。

`HEAD_TAIL`与`COMPACT`不得抬到40,000。它们是主动缩小context的降级形状；模型需要被省略的正文时，才按visible artifact handle读取。`REF_ONLY`、artifact archive threshold、canonical hard bound与artifact分页窗口也各自拥有不同职责，不能因“统一阈值”被机械改成40,000。

同一Host、同一scope、同一continuity epoch仍满足：

~~~text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
~~~

本轮通过cold contract bump采用新projection；不得在已经安装的epoch中热切换阈值或重写旧ToolResult。

---

## 1. 范围与非目标

### 1.1 本轮实施

- 一个provider-neutral 40,000-byte logical FULL semantic常量；
- canonical `COMPLETE | HEAD_TAIL`决策改用UTF-8 bytes；
- provider `FULL` variant按最终typed message计量；
- 保留8 KiB `COMPACT`、2 KiB `REF_ONLY`和`OMITTED_BODY` ladder；
- UTF-8-safe deterministic head/tail；
- artifact guidance改为conditional；
- `artifact_read`在既有分页语义内保证自身完整provider result可进入FULL；
- ordinary、retained与全部tool origin使用同一variant builder；
- live final preview、repository canonical preview与reader lowering exact一致；
- lowering/preview contract cold bump及完整测试。

### 1.2 明确非目标

- compaction、summary、protected tail选择或snapshot adoption；
- 提高Terminal retained raw bytes、MCP frame/body/schema、blob或compiler working-set hard bound；
- 为某个tool family设置更大的provider-visible特权；
- 为Skill正文、MCP inspect或最后一个tool result创建专用大正文通道；
- 枚举所有artifact、自动读取artifact或让模型恢复整个artifact集合；
- 修改tool permission、effect、attempt、late-outcome或unknown-effect语义；
- 删除canonical ToolResult或完整artifact；
- 动态环境变量、per-turn knob或同epoch热配置；
- durable projection、receipt、checkpoint、repair、replay或第二套ToolResult authority。

### 1.3 Prior art只决定量级，不决定Pulsara分层

Codex普通tool output采用约10,000 token量级；grok-build通用tool output使用40,000-byte量级，并为bash/MCP保留更低特例。Pulsara吸收40,000-byte作为统一normal FULL量级，但不照搬per-tool lowering特例：

- physical tool自己仍可有更低输入/输出安全bound；
- canonical artifact owner仍决定完整内容是否可恢复；
- provider compiler统一决定FULL/COMPACT/REF_ONLY/OMITTED_BODY；
- tool origin不改变normal projection ladder。

因此“统一”指同一provider contract，不是把所有相关常量改成40,000。

---

## 2. 当前代码真值

### 2.1 已有且必须复用

当前production已经拥有：

- `ToolOutputArtifactProcessor.prepare()`：唯一canonical preview与artifact preparation owner；
- `PreparedToolOutputProjection`：冻结preview、display kind、artifact disposition与coverage；
- `CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES = 65_536`；
- `ARTIFACT_ARCHIVE_THRESHOLD_BYTES = 8_000`；
- `HEAD_TAIL_PREVIEW_CHARS = 8_000`与65/35 head/tail；
- `FrozenProviderInputItem.tool_result_context/body`；
- `_tool_result_variants()`：唯一normal provider variant builder；
- `ToolResultProviderRenderMode.FULL | COMPACT | REF_ONLY | OMITTED_BODY`；
- compiler对variant ladder的budget选择与decision report；
- content-addressed artifact、`artifact_read`与bounded分页；
- Round 7 closed outer ToolResult envelope、timing、freshness、origin与citation handle。

本轮不得复制上述DTO或另建`CompactionToolResultProjection`。

### 2.2 当前错位

当前`tool_artifacts.py`仍使用：

~~~text
COMPLETE_DISPLAY_BODY_CHARS = 32_000
~~~

而`StructuredModelInputLimits`只冻结：

~~~text
maximum_tool_result_compact_bytes = 8 KiB
maximum_tool_result_ref_only_bytes = 2 KiB
~~~

`FULL` variant会无条件加入；它是否最终被选只由总token budget间接决定，没有“单条provider-visible result不得超过normal FULL边界”的closed contract。结果是：

- 同样32,000 characters的ASCII与中文具有完全不同的UTF-8体积；
- canonical COMPLETE与provider FULL不是同一计量域；
- 大result可能在compile working set里长期携带一个本不应成为FULL的variant；
- Round 5B retained history若另写阈值，会与ordinary history产生不同bytes。

### 2.3 两层projection不能合并

canonical preparation回答：

> ToolResult被接受后，永远保留哪一个有界preview；完整正文是否另存artifact？

provider lowering回答：

> 对当前model call，在不改canonical row的前提下选择哪一个已有representation？

`COMPLETE`不保证每次都选`FULL`：candidate body可能刚好40,000 bytes，但加入Round 7 outer envelope、timing、citation handle后超过40,000；此时FULL不进入variant集合，compiler选择COMPACT/REF_ONLY/OMITTED_BODY。反过来，8,001-byte正文可以同时拥有artifact并以FULL发送。

当前compiler还有一个必须随本轮修复的实现假设：`_ToolState.selected`从ordinal `0`开始，而decision把`selected == 0`等同于`SELECTED_FULL`。本轮允许合法variant tuple从`COMPACT`、`REF_ONLY`甚至`OMITTED_BODY`开始，因此ordinal不再表达render mode；fresh、compatible append与replay/retained三条路径都必须按**实际selected mode**分类。

---

## 3. Closed constants与contract identity

### 3.1 唯一共享40K常量

在provider-neutral模块`src/pulsara_agent/primitives/tool_observation.py`定义：

~~~python
MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES = 40_000
~~~

它被`tool_artifacts.py`和provider-neutral logical ToolResult renderer直接import。Production不得再出现第二个40,000 literal、等价`39_999 + 1`或独立config default。

`StructuredModelInputLimits`**不得**再增加一个同值实例字段。40K没有合法的per-call、per-provider或test-only第二取值；canonical preparation与logical renderer都直接引用同一provider-neutral constant。Compiler quote/report可以记录固定projection contract version，但不得保存一个可由调用者单独覆盖的`maximum_tool_result_full_message_bytes` mirror。测试使用真实39,999/40,000/40,001边界，不靠缩小一层limit制造假环境。

### 3.2 保持独立的bounds

以下常量不合并：

| bound | owner | reason |
|---|---|---|
| 8,000-byte artifact threshold | artifact preparation | 决定是否保存完整可恢复正文 |
| 8,000-character HEAD_TAIL preview | canonical preview | 约束接受时永久inline preview |
| 65,536-byte canonical preview hard | canonical preview | envelope与physical row安全余量 |
| 8-KiB COMPACT final message | provider lowering | context-pressure降级形状 |
| 2-KiB REF_ONLY final message | provider lowering | 最小typed artifact reference |
| 20k/32k artifact read chars | artifact tool request | 分页请求上限，不是wire byte承诺 |
| 16-MiB Terminal retained bytes | Terminal owner | physical retained stream安全 |
| MCP pre-parse/schema bounds | MCP owner | 不可信transport/discovery安全 |

### 3.3 Contract bump

以下contract/domain version必须一次性升级：

- canonical tool-result preview preparation；
- provider ToolResult lowering；
- provider-input compiler/lowering compatibility contract；
- live ToolResult final preview golden。

这是cold adoption：clean-v0可reset，旧Host必须关闭；不得尝试在installed continuity epoch里把已存在FULL variant重新lower成新bytes。

---

## 4. Canonical preparation

### 4.1 COMPLETE decision

`ToolOutputArtifactProcessor.prepare()`先对sanitized `candidate.text`执行一次UTF-8编码：

~~~text
candidate_utf8_bytes <= 40_000
and final canonical preview envelope <= 65_536
    -> display_kind = COMPLETE

otherwise
    -> display_kind = HEAD_TAIL
~~~

禁止继续使用character count决定COMPLETE。`candidate_chars`仍可作为diagnostic保存，但不参与branch。

### 4.2 HEAD_TAIL保持8,000 characters

candidate超过40,000 UTF-8 bytes时：

- 最大可见正文仍为8,000 Unicode code points；
- 65% head、35% tail；
- 只在code-point边界切分，最终UTF-8始终合法；
- omission marker包含exact omitted characters；如可廉价计算，也可包含omitted UTF-8 bytes，但不能从字符数猜；
- final canonical envelope仍必须不超过65,536 bytes；超出时沿现有binary search确定性缩小visible characters；
- artifact AVAILABLE/INCOMPLETE时保留exact handle；UNAVAILABLE时明确正文不可恢复且禁止自动重跑tool。

`HEAD_TAIL_PREVIEW_CHARS`不得改成40,000，也不得按tool origin变化。

### 4.3 Artifact关系不变

artifact archive threshold继续为8,000 UTF-8 bytes。因此：

~~~text
candidate 0..8,000 bytes
    -> artifact通常NOT_REQUIRED

candidate 8,001..40,000 bytes
    -> canonical COMPLETE可能成立
    -> artifact同时可以AVAILABLE

candidate >40,000 bytes
    -> canonical HEAD_TAIL
    -> artifact按现有规则AVAILABLE / INCOMPLETE / UNAVAILABLE
~~~

“完整发送给模型”和“另存可恢复artifact”是正交事实，不能互相推导。

### 4.4 Live与canonical必须同源

Runner不得先把32,000-char旧preview发成`LiveToolResultEnd`，再向repository提交40,000-byte新preview。唯一`PreparedToolOutputProjection.canonical_preview`同时进入：

- live authoritative End；
- stable ToolResult acceptance candidate；
- repository row/content；
- canonical reader；
- later provider lowering。

ACK unknown只确认同一prepared candidate，不重新运行tool或重新决定preview。

---

## 5. Provider variant builder

### 5.1 Variant顺序

每个provider-visible ToolResult只生成下列ladder的一个**非空、有序、去重子序列**：

~~~text
FULL
-> COMPACT
-> REF_ONLY   # only when readable artifact exists and tool is visible
-> OMITTED_BODY
~~~

`FULL`不是必有元素。完整logical message超过40K时，合法tuple可以从`COMPACT`开始；COMPACT framing也超界时可以从`REF_ONLY`或`OMITTED_BODY`开始。`LoweredCanonicalItem` validator只验证mode唯一、顺序保持、至少一个variant及各mode自己的closed bound，绝不能要求`variants[0].mode == FULL`。

不存在`COMPACTION_FULL`、`LAST_RESULT_FULL`、`MCP_FULL`、`SKILL_FULL`或parallel-batch专用variant。

### 5.2 FULL按provider-neutral logical message计量

40K约束的对象不是Chat Completions或Responses最终HTTP JSON bytes，而是Round 7已经冻结的**provider-neutral ToolResult logical message**。本轮把现有outer encoder与quote抽成一个无I/O、无provider profile、无compiler反向依赖的低层pure helper；canonical lowering、artifact page factory、Round 9 MCP directory page与inspect schema都只能调用这一helper，禁止各自复制字段清单或byte公式。

该helper必须先构造exact Round 7 carrier，再返回同一个message value与logical UTF-8 quote：

- ordinary/native ToolResult的实际scalar fields只有`content`与`tool_call_id`；
- late outcome user observation只有实际`content`；
- `content`内部继续使用Round 7 exact closed envelope：`body`、optional `citation_handle`、model-visible memory IDs、observation timing/origin/freshness与result state；
- model-visible memory provenance继续服从canonical relation已冻结的`<= 50 items`与canonical JSON aggregate `<= 8 KiB`；该约束由shared renderer/decoder与repository candidate复用同一constant，不能在lowering另加通用2 KiB metadata fence；
- tool name、display、coverage或任何其他字段只有在现有renderer**确实发出**时才存在并计量；本轮不得为了满足quote文档把它们新增进wire；
- logical bytes等于上述provider-neutral message中实际存在的string scalar values的UTF-8 bytes之和；role enum、adapter JSON key、HTTP framing不计入这条40K；
- outer envelope自身的canonical JSON key、quote与framing已经位于`content`字符串内，因此计入；Chat/Responses adapter随后对`content`再次JSON escaping产生的物理膨胀不在此处重复估算。

只有：

~~~text
provider-neutral tool-result logical message <= 40_000 UTF-8 bytes
~~~

才加入FULL。禁止先按body判定FULL、随后让actual envelope把logical message推过40,000。

Chat Completions与Responses的真实role/key/escaping/item shape及最终request总量继续由Round 5A.1 `FrozenProviderWireInputPlan`逐adapter exact materialize、quote并加入continuity CAS；logical 40K不能替代wire proof，wire proof也不能反向改写normal ToolResult ladder。

### 5.3 COMPACT仍是8 KiB降级形状

COMPACT从已冻结canonical preview确定性生成UTF-8-safe head/tail，最终typed message不超过现有8 KiB bound。它继续服务“FULL合法但当前context不足”的降级场景；不得把COMPACT target抬到40,000，否则一个30 KiB FULL将失去有意义的中间representation而直接跳到REF_ONLY/OMITTED_BODY。

COMPACT metadata至少准确表达：

- canonical preview bytes/characters；
- included bytes/characters；
- omitted bytes/characters；
- projection kind；
- artifact disposition/handle是否可读；
- conditional artifact guidance。

若canonical preview本身已经小于COMPACT cap，或者生成结果与FULL body相同，则不重复加入等价variant。

### 5.4 REF_ONLY与OMITTED_BODY

只有artifact disposition为AVAILABLE/INCOMPLETE、`artifact_read`在当前tool surface可见且最终message不超过2 KiB时才有REF_ONLY。它必须保留：

- known ToolResult state；
- exact artifact ID；
- source coverage；
- timing/freshness/origin的必要typed字段；
- conditional read instruction。

artifact不可用时禁止伪造REF_ONLY。OMITTED_BODY仍保留tool pairing与known/unknown结果语义，不能让provider history出现orphan tool call。

### 5.5 Compiler只选variant，不重算canonical preview

Compiler可以因预算从FULL降到COMPACT/REF_ONLY/OMITTED_BODY，但不得：

- 重跑artifact publication；
- 从raw physical output重新截取；
- 修改canonical display kind；
- 重新解释source coverage；
- 为retained tail使用不同阈值；
- 因tool origin重排variant。

decision/report继续描述本次suffix的选择；完整历史decision不持久累积。Compiler与`CompiledToolResultDecision`必须按actual selected mode分类，不能再用variant ordinal推导语义：

~~~text
selected.mode == FULL
    -> SELECTED_FULL

selected is first legal variant and first.mode != FULL
    -> FULL_INELIGIBLE_RESULT_BOUND

selected ordinal > 0
    -> DEGRADED_FOR_BUDGET
~~~

report同时保留actual mode与first legal mode。因而“tuple从COMPACT开始且选择index 0”是合法`FULL_INELIGIBLE_RESULT_BOUND + COMPACT`，不是`SELECTED_FULL + COMPACT` validator conflict。Fresh compile、compatible append、canonical replay与Round 5B retained lowering必须调用同一个分类helper；`compiler.py`、`contracts.py`都属于本轮明确修改面。

### 5.6 通用FULL-delivery requirement

“存在FULL variant”只证明单条logical message合法，不能证明aggregate compiler最终选择了FULL。对那些把**完整page/schema/body**作为工具成功语义的结果，本轮增加一个纯process-local closed requirement：

~~~python
class ToolResultDeliveryRequirement(StrEnum):
    BEST_AVAILABLE = "BEST_AVAILABLE"
    FULL_REQUIRED = "FULL_REQUIRED"


class ToolResultFullDeliveryReason(StrEnum):
    ARTIFACT_PAGE = "ARTIFACT_PAGE"
    MCP_DIRECTORY_PAGE = "MCP_DIRECTORY_PAGE"
    MCP_INSPECT_SCHEMA = "MCP_INSPECT_SCHEMA"
    SKILL_ACTIVATION = "SKILL_ACTIVATION"  # reserved; Round 9.1 has no producer
~~~

它的closed classifier只从exact canonical tool request、accepted result state和已经版本化的Builtin/binding contract派生：

- successful `artifact_read` text page；
- successful `list_mcp_servers` server/tool directory page；
- successful `inspect_new_mcp_tool` closed schema/ref result；
- typed failure、unavailable、invalid arguments及其他普通ToolResult仍为`BEST_AVAILABLE`；
- ordinary `read_file`（包括读取`SKILL.md`）始终为`BEST_AVAILABLE`；Round 9.1不增加Skill-specific intent或delivery requirement。

未知、第三方或MCP tool body不能通过返回字段自行宣称`FULL_REQUIRED`。Reader rehydrate必须由exact canonical request/result与同一versioned classifier重建；不得按当前mutable registry、mangled MCP prefix或正文字符串猜测。Frozen requirement进入该provider input item的lowering/compiler identity与dispatch fingerprint，但不写ToolResult row、不新增relation/event/receipt。

Compiler规则冻结为：

1. `FULL_REQUIRED` item必须拥有FULL variant并且只能选择FULL；`_ToolState.advance()`对它恒为false；
2. ordinary `BEST_AVAILABLE` sibling仍可按既有priority降级；parallel batch的call/result order与pairing不变；
3. 单条requirement没有FULL时返回typed `FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE`；
4. 单条FULL合法但aggregate/provider target budget装不下时返回typed `FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET`，provider open为0；不得用COMPACT/REF_ONLY/OMITTED伪装成功分页或完整inspect；
5. requirement与selected FULL必须进入exact compile result、append candidate、dispatch attachment及continuity CAS证明；CAS conflict后从fresh predecessor重建，不得把旧选择提升到新attempt。

Canonical ToolResult可以先按既有事务可靠接受；这不等于模型已经收到完整结果。只有含该exact result FULL representation的continuity candidate成功安装后，Runtime才可发布相应process-local ref或允许依赖该交付的后续meta invocation。`inspect_new_mcp_tool` ref在此之前保持dormant；caller cancellation只能detach waiter，不能把requirement降为BEST_AVAILABLE或提前激活ref。Host close/epoch close可释放未发布process-local attachment；Host restart后opaque ref本来就按Round 9契约stale，不需要durable handoff。

这一规则有意把“单条40K内但当前aggregate放不下”定义为typed provider-input resource boundary。它不回滚canonical result、不重跑工具，也不预先提交compiler receipt；后续Round 5B可通过合法cold rebase创造空间，但Round 7.1本身不实现compaction。

---

## 6. Artifact guidance与`artifact_read`

### 6.1 Conditional guidance

所有provider-visible guidance必须表达等价语义：

> If the omitted content is necessary for the current task, read the retained artifact with artifact_read. Otherwise continue from the visible result without opening the artifact.

禁止无条件：

~~~text
Use artifact_read ...
You must inspect the full output ...
Read all retained artifacts ...
~~~

首尾已经足以判断成功、失败、主要错误或下一步时，模型应继续主线。Runtime不得在普通call或compaction handoff中列出artifact inventory。

### 6.2 COMPLETE正文带artifact也不催促读取

8,001..40,000-byte COMPLETE result可同时带artifact ID，用于未来budget降级或精确回查。Footer只说明“完整retained output可用”，并使用conditional wording；不能因为artifact存在就暗示当前inline正文不完整。

### 6.3 `artifact_read`不递归归档

`artifact_read`继续：

- 不发布新的artifact；
- 默认请求最多20,000 chars，hard request最多32,000 chars；
- 返回原artifact ID、requested/returned range、total/has_more/next offset；
- 读取范围以retained artifact body为坐标。

为同时满足多字节文本与40,000-byte logical FULL边界，`max_chars`语义明确为**maximum**而不是必须精确返回的数量。Artifact tool在构造canonical JSON result时，确定性选择不超过requested `max_chars`、且由§5.2唯一logical renderer/quote证明在最大合法call-local augmentation下仍有FULL variant的最长UTF-8-safe prefix，并准确返回`returned_chars/next_offset_chars/has_more`。这不是silent truncation：分页坐标与remaining state必须显式。

Artifact execution时尚不知道未来model call是否分配memory citation handle，因此不得反向调用compiler或依赖一次偶然的actual envelope。Shared helper以artifact page的closed result shape、Round 7实际outer fields、各实际字段既有maximum bytes与128-byte citation-handle上界纯计算保守body capacity；它没有I/O、provider profile或per-tool配置。`artifact_read`使用同一renderer做binary search，而不是复制“metadata + body”公式。任何合法后续citation/timing/origin组合都必须仍有FULL variant；若actual envelope更小，Runtime也不扩大已经canonicalized的page。

`artifact_read`正常成功结果必须标记`FULL_REQUIRED/ARTIFACT_PAGE`，不再对一个已分页slice生成HEAD_TAIL或第二个artifact。Page factory保证单条FULL eligible；aggregate compiler再保证实际选择FULL。若固定logical envelope加一个code point仍无法容纳，返回typed resource boundary；若aggregate不fit，则provider open为0并保留canonical result，不得断言崩溃、推进一个模型未见的page或输出invalid JSON。

### 6.4 不新增全局artifact浏览器

本轮不新增：

- `list_all_artifacts`；
- `read_compacted_history`；
- automatic artifact expansion；
- summary中的artifact inventory；
- artifact relevance ranking。

只有当前可见ToolResult携带的exact handle可引导模型按需读取。

---

## 7. Origin与carrier一致性

### 7.1 全部tool origin共用同一投影

以下结果全部进入同一个builder：

- Builtin filesystem/search/terminal；
- `terminal_process`、`terminal_monitor`；
- direct MCP与`use_new_mcp_tool` outer result；
- Plan工具；
- memory tools；
- ordinary `read_file`，包括`SKILL.md`；
- `inspect_new_mcp_tool`；
- `artifact_read`。

Tool owner可以在进入pipeline前拥有更小physical/product bound，但进入accepted ToolResult后不得绕过normal projection或申请更大的provider cap。

### 7.2 Structured JSON不允许破坏outer carrier

JSON tool output仍作为opaque body或既有source-format envelope处理。Head/tail不能让正文逸出Round 7 closed outer envelope；body中仿造`contract`、closing marker、timing或artifact字段不会取得Runtime authority。

本轮不递归清洗用户、MCP或tool正文，也不删除actionable IDs。Runtime只拥有outer framing与自己生成的metadata。

### 7.3 Tool grouping保持不变

Parallel tool request/result batch仍按provider有效的完整group lower。某一result降级不能：

- 拆掉sibling result；
- 改变call/result order；
- 把一个result artifact预算转给另一个；
- 引入compaction-only公平配额；
- 产生tool call而无matching result。

---

## 8. Prefix continuity与采用边界

### 8.1 只对新cold epoch生效

40K constant、preview contract与lowering contract共同进入provider-input compatibility。Activation后：

- 新Host/cold epoch使用新contract；
- 已安装旧epoch不得热切换；
- same epoch新ToolResult按该epoch冻结的contract处理；
- 旧canonical row不回写、不重新artifact化；
- clean-v0测试数据库可reset，不建立历史migration/reindex machinery。

### 8.2 Strict-prefix证明

相邻calls仍证明：

~~~text
SYSTEM exact equal
tools exact equal
old messages byte-for-byte prefix of new messages
~~~

新结果只作为完整tool group suffix追加。Compiler后续降级只作用于尚未安装suffix；已安装ToolResult representation不能在same epoch从FULL改成COMPACT。

### 8.3 Round 5B只复用

Round 5B protected tail选择完整tool group后，直接复用本轮唯一pure builder。这里的等价性必须带上call-local augmentation输入，不能错误要求跨epoch raw bytes恒等：

~~~text
same canonical ToolResult item
+ same lowering contract
+ same call-local augmentation inputs
  (exact scope/epoch citation mapping, visible artifact-read availability, result observation inputs)
-> byte-identical ordered variants
~~~

Memory citation handle是`scope + epoch_nonce`绑定的opaque capability。Compaction successor必须使用successor epoch自己的citation mapping重新调用builder；它可以合理地没有旧handle或取得新handle。Runtime不得迁移、复制或为了golden伪造旧epoch `tool:N`。除closed call-local augmentation外，canonical body、artifact/timing/result-state等base projection保持相同。

Round 5B不得：

- 再定义40K常量；
- 提高最后一个result阈值；
- 为retained result重新读取artifact；
- 建立compaction-specific ToolResult variant；
- 枚举artifact handles。

若某个retained group在successor dry compile中不fit，Round 5B只能按3→2→1→0减少完整group数量或让normal compiler选择本轮允许的variant；带`FULL_REQUIRED`结果的group不能降级，只能完整保留或从protected suffix中整体移除。不能拆pair或改变projection contract。

---

## 9. Failure matrix

| failure | canonical effect | provider/runtime effect |
|---|---|---|
| candidate body恰好40,000 bytes且canonical envelope fit | COMPLETE | FULL仅在最终message也fit时存在 |
| candidate body 40,001 bytes | HEAD_TAIL | ordinary ladder |
| COMPLETE + logical outer envelope >40,000 | unchanged COMPLETE row | 无FULL；从COMPACT开始，decision=`FULL_INELIGIBLE_RESULT_BOUND`而非伪`SELECTED_FULL` |
| HEAD_TAIL canonical preview <40,000 final | HEAD_TAIL | FULL可以展示该完整preview |
| artifact publication known failure | accepted preview + UNAVAILABLE | no fake REF_ONLY；明确loss warning |
| artifact publication ACK unknown | exact reissue/confirm同一blob candidate | 不重跑tool |
| COMPACT minimum framing >8 KiB | unchanged row | 跳过COMPACT，继续REF_ONLY/OMITTED |
| REF_ONLY framing >2 KiB | unchanged row | 跳过REF_ONLY，使用OMITTED |
| artifact_read requested chars导致logical message过大 | no recursive artifact | 返回largest FULL-eligible page与next offset |
| FULL_REQUIRED单条没有FULL variant | canonical result保持 | typed `FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE`；provider open=0 |
| FULL_REQUIRED单条FULL合法但aggregate不fit | canonical result保持 | typed `FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET`；不得降级，provider open=0 |
| BEST_AVAILABLE variant tuple从COMPACT开始 | unchanged row | index 0合法选择COMPACT，reason=`FULL_INELIGIBLE_RESULT_BOUND` |
| artifact_read fixed logical envelope自身过大 | typed resource boundary | no invalid/truncated JSON |
| adapter JSON escaping使physical request大于logical quote | unchanged result | 由`FrozenProviderWireInputPlan`拒绝；不得改写40K逻辑variant |
| FULL安装前caller cancel | canonical result保持；无工具重跑 | waiter detach；ref/activation保持未发布，不能改走degraded result |
| final quote mismatch | no provider open | discard dispatch candidate |
| same-epoch contract mismatch | no provider open | typed continuity conflict；不得rebase |

---

## 10. 实施修改面

### 10.1 `primitives/tool_observation.py`

- 定义唯一`MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES`；
- 导出给canonical preparation与provider lowering；
- bump provider observation/projection domain；
- 不importconversation Host、repository或artifact store。

### 10.2 `conversation_kernel/tool_artifacts.py`

- 删除`COMPLETE_DISPLAY_BODY_CHARS`；
- COMPLETE按candidate UTF-8 bytes决定；
- HEAD_TAIL 8,000 chars与65/35保持；
- footer/marker改成conditional artifact guidance；
- live/repository继续消费同一`PreparedToolOutputProjection`；
- artifact_read不递归artifact。

### 10.3 低层logical renderer、`model_input/contracts.py`与`lowering.py`

- 把Round 7 exact outer encoder与logical quote抽入一个可供lowering、artifact与MCP page factory共同调用的provider-neutral pure module；它只返回实际logical message及quote，不知道HTTP adapter或compiler；
- `lowering.py`直接引用唯一40K constant并按它执行FULL logical-message bound；`contracts.py`升级相关contract/fingerprint且允许合法variant tuple没有FULL，不增加同值可配置字段；
- 保留8-KiB COMPACT与2-KiB REF_ONLY；
- FULL在最终provider-neutral typed logical message计量后才加入；
- UTF-8-safe compact helper与conditional guidance；
- contract/fingerprint bump；
- 不新增variant enum。

### 10.4 `model_input/compiler.py`

- `_ToolState`只保存selected ordinal，任何decision都按`variants[selected].mode`判断；
- 增加process-local `FULL_INELIGIBLE_RESULT_BOUND` report reason并同步DTO validator；
- fresh、compatible append、replay/retained调用同一decision helper；
- 为item携带`BEST_AVAILABLE | FULL_REQUIRED`，required item禁止`advance()`；
- aggregate不fit时返回closed resource boundary并确保provider open=0；
- ordinary siblings仍可降级，parallel group pairing/order不变。

### 10.5 `tools/builtins/artifact.py`

- `max_chars`解释为upper bound；
- 通过shared exact logical renderer/quote构造largest safely fitting complete page，不反向import compiler；
- 保留exact range metadata与原artifact ID；
- successful page附加`FULL_REQUIRED/ARTIFACT_PAGE`；
- fixed envelope overbound返回typed result，不抛未结算AssertionError。

### 10.6 Runner、reader、continuity与tests

- Runner live End与repository candidate共用preview object；
- reader不重算threshold，只hydratecanonical fields，并从exact canonical tool request/result与versioned classifier重建delivery requirement；
- ordinary/retained path调用同一lowering helper；
- prepared dispatch把requirement与任何dormant ref/activation attachment exact join continuity candidate；只有selected FULL成功安装后才发布attachment；
- cancellation、CAS conflict与Host close遵守§5.6，不增加durable settlement owner；
- 删除Round 5B中的全局projection实现与tests，只保留retained-equivalence regression。

---

## 11. 测试规格

### 11.1 UTF-8 boundary golden

- ASCII 39,999/40,000/40,001 bytes；
- 中文、emoji与combining characters在byte边界正确；
- 不切断UTF-8 code point；
- 32,000 chars不再作为semantic branch；
- final canonical preview始终<=65,536 bytes。

### 11.2 Canonical preview

- <=40K candidate COMPLETE；>40K HEAD_TAIL；
- HEAD_TAIL仍最多8,000 chars、65/35；
- JSON envelope合法且body不能逸出；
- artifact AVAILABLE/INCOMPLETE/UNAVAILABLE/NOT_REQUIRED矩阵；
- 8,001..40,000-byte COMPLETE同时拥有artifact；
- live End、prepared candidate、repository hydrate fingerprints exact相等。

### 11.3 Provider variants

- FULL provider-neutral logical message 39,999/40,000/40,001-byte边界；
- body<=40K但outer envelope>40K时FULL不存在；
- variant tuple从COMPACT/REF_ONLY/OMITTED_BODY开始均通过validator；index 0选择COMPACT产生`FULL_INELIGIBLE_RESULT_BOUND`而非`SELECTED_FULL`；
- fresh、append与replay/retained对同一tuple给出相同actual-mode decision；
- COMPACT final<=8KiB且明显小于一个30KiB FULL；
- REF_ONLY final<=2KiB；
- no readable artifact时没有REF_ONLY；
- duplicate variants去重；
- OMITTED保持tool pairing与known result；
- citation handle/timing/freshness/origin按实际Round 7 envelope计入logical bytes；不存在的tool name/display/coverage不被凭空计入；
- 包含quote、反斜杠与非ASCII的同一logical message可证明Chat/Responses物理JSON bytes不同，logical 40K不冒充wire quote，两个adapter wire plan仍各自exact通过或拒绝。

### 11.4 Artifact guidance

- COMPLETE、HEAD_TAIL、COMPACT、REF_ONLY都只使用conditional read wording；
- 不出现无条件`Use artifact_read`/`must inspect`；
- artifact unavailable不伪造ID；
- 没有artifact inventory；
- visible handle仍可真实分页读取。

### 11.5 Artifact read

- ASCII/中文/emoji下，requested 20k/32k chars均按shared exact logical renderer与最大合法call-local augmentation返回largest safely fitting完整JSON page；
- returned/next/has_more坐标精确；
- provider-neutral logical FULL final<=40K；
- 加入最大合法128-byte citation handle、timing/freshness/origin后仍FULL；actual较小envelope不得让canonical page在compile时膨胀；
- successful page被标记`FULL_REQUIRED/ARTIFACT_PAGE`；aggregate压力下不降级且provider open=0，模型未见page时不得把next offset当成已交付；
- 不生成第二artifact；
- next page无重叠、无遗漏；
- fixed envelope异常走typed failure而不是AssertionError。

### 11.6 FULL-delivery requirement

- 三类successful result分别得到`ARTIFACT_PAGE / MCP_DIRECTORY_PAGE / MCP_INSPECT_SCHEMA`，typed failure仍为BEST_AVAILABLE；`SKILL_ACTIVATION`仅是无producer的reserved enum value；
- 普通/MCP正文不能通过伪造字段自行取得requirement；rehydrate只从exact request/result与versioned classifier重建；
- 一个required result与多个可降级sibling并行时，只有siblings可降级，call/result order与pairing不变；
- 两个或更多required siblings合计超budget时typed resource boundary且provider open=0；
- FULL variant缺席、aggregate压力、continuity CAS conflict、FULL安装前cancel与Host close矩阵；
- inspect ref在FULL安装前不可用于`use_new_mcp_tool`；成功安装后exact一次发布，失败不重跑原工具。Ordinary Skill read不发布activation state。

### 11.7 Origin与group

- Builtin、Terminal、MCP、Plan、memory、ordinary Skill read、inspect与artifact_read全部同一ladder；
- parallel 1/2/3-call group顺序与pairing保持；
- origin不同不改变阈值；
- tool-specific更低physical cap仍在candidate形成前生效。

### 11.8 Continuity与Round 5B readiness

- Chat Completions与Responses相邻call证明SYSTEM/tools相等、messages suffix-only；
- installed suffix不重新降级；
- cold contract bump不热改旧epoch；
- 同canonical item、同lowering contract、同call-local augmentation输入的ordinary与模拟retained group得到byte-identical ordered variants；
- successor epoch使用自己的citation mapping；旧`tool:N`不迁移，只有该augmentation允许导致variant bytes不同；
- Round 5B source tree不再定义第二个40K常量、ToolResult variant或artifact inventory。

### 11.9 Retained与全量

至少覆盖：

~~~bash
uv run pytest -q tests/test_round1_tool_output_artifact.py
uv run pytest -q tests/test_round3_structured_model_input_compiler.py
uv run pytest -q tests/test_round3_1_provider_input_prefix_continuity.py
uv run pytest -q tests/test_round7_model_visible_failure_and_tool_observation.py
uv run pytest -q tests/test_round7_1_provider_visible_tool_result_projection.py
uv run pytest -q
uv run ruff check src tests
uv run python -m compileall -q src tests
uv lock --check
git diff --check
go test ./...
go vet ./...
go mod verify
~~~

实际测试文件名以coding agent落地为准；activation evidence固定写入`benchmarks/suites/core/v1/round7_1_provider_visible_tool_result_projection_activation.json`，并保存exact node IDs、code/doc hashes、contract versions与oracle。

---

## 12. Architecture guards

机器拒绝：

1. production出现第二个40,000 ToolResult FULL literal；
2. `HEAD_TAIL_PREVIEW_CHARS`或COMPACT cap被改成40,000；
3. `StructuredModelInputLimits`、provider config或tool config再暴露一个可覆盖的40K FULL字段；
4. tool origin拥有不同normal provider threshold；
5. compaction package定义ToolResult variant/cap/artifact inventory；
6. FULL只按body计量、复制logical字段公式、把不存在的字段加入quote，或把logical 40K冒充Chat/Responses physical wire bytes；
7. character count继续决定COMPLETE；
8. invalid UTF-8切分；
9. compiler从raw physical output重建preview；
10. artifact_read递归发布artifact；
11. artifact guidance无条件要求读取；
12. canonical ToolResult因provider budget被改写；
13. 已安装suffix在same epoch更换representation；
14. 新增ToolResult relation/event/job/guard/receipt/checkpoint/repair/replay；
15. Round 9 MCP inspect或Round 9.1 Skill read建立专用大正文通道；
16. compiler以`selected == 0`推导FULL、要求variant tuple第0项恒为FULL，或生成`SELECTED_FULL + non-FULL mode`；
17. `artifact_read`或MCP directory/inspect成功结果在aggregate压力下被降级后仍宣称page/schema完整交付；
18. provider/第三方tool正文自行声明FULL_REQUIRED，或reader依赖current mutable catalog猜测该requirement；
19. inspect ref在exact FULL continuity installation之前发布生效，或ordinary Skill read错误发布Runtime activation state；
20. 为FULL delivery新增relation、event、job、receipt、checkpoint或recovery graph。

---

## 13. PostgreSQL与real-provider验证

PostgreSQL测试允许使用本机专门用于Pulsara开发、已安装所需扩展且可随时reset的真实库：

~~~text
PULSARA_POSTGRES_ADMIN_DSN=postgresql://plumliu@localhost:5432/pulsara
PULSARA_POSTGRES_DSN=postgresql://pulsara:pulsara@localhost:5432/pulsara
~~~

该授权只适用于host为localhost/loopback且database name精确为`pulsara`的resolved DSN。执行reset前必须机械验证目标，不得泛化到远端、其他database、空目标或生产环境。Evidence不得记录DSN、credential、完整tool output、artifact正文或provider prompt。

Real-provider smoke至少证明：

1. 一个接近40K的完整result可被模型直接理解，无额外artifact read；
2. 一个大result以8K级HEAD_TAIL/COMPACT出现，模型在首尾足够时不读取artifact；
3. 只有任务确实依赖省略中段时，模型才调用一次或少量分页`artifact_read`；
4. Chat与Responses adapter均保持valid tool pairing；
5. cache token只作operational evidence，不成为correctness gate。

---

## 14. Definition of Done

只有以下全部成立才可标记ACTIVATED：

1. 40,000-byte logical FULL semantic常量在production只有一个定义，canonical preparation与provider-neutral logical renderer共同引用。
2. canonical COMPLETE按candidate UTF-8 bytes判断，不再按32,000 chars判断。
3. provider FULL按Round 7实际provider-neutral typed logical message计量且不超过40,000 bytes；Chat/Responses physical bytes仍由各自wire plan证明。
4. HEAD_TAIL保持8,000 chars、65/35；COMPACT保持8 KiB；REF_ONLY保持2 KiB。
5. artifact threshold保持8,000 bytes，8,001..40,000-byte COMPLETE可同时拥有artifact。
6. canonical hard bound保持65,536 bytes。
7. artifact guidance全部conditional，不诱导遍历artifact。
8. artifact_read返回largest logical-FULL-eligible完整page、准确next offset且不递归归档。
9. 合法variant tuple可以没有FULL；compiler按actual mode报告`SELECTED_FULL | FULL_INELIGIBLE_RESULT_BOUND | DEGRADED_FOR_BUDGET`，不从ordinal猜测。
10. artifact page、MCP directory page与inspect schema/ref共享一个process-local FULL-delivery requirement；aggregate不fit时typed终止且provider open=0。`SKILL_ACTIVATION` reserved value无producer，ordinary Skill read恒为BEST_AVAILABLE。
11. FULL安装前ref不生效；cancel/CAS conflict不降级、不重跑工具且不产生durable settlement machinery。
12. 全部tool origin、ordinary history与retained history复用一个logical renderer、quote与variant builder。
13. live End、canonical row与reader hydrate共用同一prepared preview。
14. Round 3.1 strict prefix保持，采用只发生在cold contract boundary。
15. Round 5B不再拥有普通ToolResult阈值、variant或artifact implementation gate；跨epoch equivalence显式参数化call-local citation mapping。
16. 不新增schema、relation、event、job、guard、subject、receipt、checkpoint、repair或replay。
17. Oracle保持`31/23/13/2/25/1`。
18. targeted、retained、full pytest、PostgreSQL、static、Go与real-provider smoke全部通过。

---

## 15. 最终产品语义

用户可感知行为固定为：

> 普通工具结果在Round 7 provider-neutral logical message不超过40,000 UTF-8 bytes时可以完整展示；更大结果保留一个约8,000字符的canonical首尾预览，并在可用时保存完整artifact。Context紧张时compiler通常可降为约8 KiB的COMPACT、2 KiB的REF_ONLY或仅保留pairing的OMITTED_BODY；只有artifact page、MCP目录页与inspect schema/ref这类“完整交付就是成功语义”的结果必须保持FULL，放不下时明确停止本次provider open而不是伪装已经交付。普通Skill file read仍是BEST_AVAILABLE。Artifact只在省略内容确实影响当前任务时按需读取；Runtime不会向模型倾倒artifact清单，也不会因为compaction、工具来源或“最后一个结果”改变这套规则。
