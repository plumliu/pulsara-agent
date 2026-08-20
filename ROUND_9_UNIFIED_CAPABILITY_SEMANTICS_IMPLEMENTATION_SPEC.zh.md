# Round 9：Unified Capability Semantics 与 MCP Direct/Meta Exposure 实施规格

> 状态：**DRAFT — NOT ACTIVATED**
>
> 记录日期：2026-08-17；本次架构校准：2026-08-19
>
> 编码基线：**待冻结**。必须先把Round 5A.2 durable replay与OpenAI function-tool wire contract v2形成经过review的clean checkpoint，再把该提交SHA写回此处；当前审阅输入HEAD `a39e537fa56f6685c677496d0eb11628337675c0`带有dirty worktree，只是规格修订证据，**不得**冒充coding baseline。
>
> hard-cut 前参考基线：`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`
>
> 上位契约：[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 provider-input prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5A.1 provider-neutral output termination](ROUND_5A_1_PROVIDER_NEUTRAL_MODEL_OUTPUT_TERMINATION_IMPLEMENTATION_SPEC.zh.md)、[Round 5A.2 durable provider replay](ROUND_5A_2_DURABLE_PROVIDER_REPLAY_AND_CROSS_RESTART_THREAD_CONTINUATION_IMPLEMENTATION_SPEC.zh.md)、[Round 6 MCP](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md)、[Round 7 model-visible observation](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 provider-visible ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Gap Index](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 直接下游：[Round 9.1 Agent Skills Standard](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)
>
> 后续但不属于本轮：[Round 9.2 Agent Plugin bundle 与 Hook lifecycle](ROUND_9_2_AGENT_PLUGIN_BUNDLE_AND_HOOK_LIFECYCLE_IMPLEMENTATION_SPEC.zh.md)、[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)

本文重新冻结 Pulsara 中 `capability` 的唯一产品含义，并把当前 Built-in tool、MCP tool 与 Skill 投影到同一个**纯语义规划边界**。本轮统一 discovery identity、semantic fact与provider exposure planning，但不统一 permission、physical binding、transport、Skill activation或文件执行方式。

本轮同时把原先寄放在 Round 5B 中、但与compaction无关的 MCP direct/meta 混合能力独立落地：cold epoch 建立时canonical-valid、native-wire eligible且完整可容纳的MCP cohort进入provider native `tools[]`；wire-incompatible或epoch中后到的MCP工具通过固定`inspect_new_mcp_tool`与`use_new_mcp_tool`使用，无法完整inspect的工具诚实标为unavailable。Round 9.1随后只需把Skill正文作为指导数据接入同一registry/catalog，不需要建立Skill→Tool dependency graph。

本轮不建立统一抽象基类，不恢复hard-cut前的durable capability graph，也不让“统一capability”成为新的execution authority。

Round 5A.2与Round 7.1都是本轮编码硬前置，而不只是引用文档：

- Round 5A.2必须已经冻结assistant/blocks/required replay row同事务接受、metadata/body两阶段hydration、replay target/placement/final wire plan exact join与Chat/Responses cross-restart native replay；Round 9不得以capability planning为由绕过、重排或重新合成durable replay carrier；
- Round 7.1必须已经冻结normal ToolResult projection；`list_mcp_servers`、`inspect_new_mcp_tool`、`use_new_mcp_tool`与所有direct MCP结果都进入该路径。Round 9不得复制40,000-byte logical FULL、HEAD_TAIL/COMPACT/REF_ONLY、artifact、FULL-delivery requirement或provider envelope逻辑；
- clean checkpoint还必须包含当前`OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION = v2-explicit-non-strict-prevalidated-lowering`及其Chat/Responses shared lowering tests。该版本只属于adapter wire contract，不能进入canonical capability semantic identity。

上述任一前置未ACTIVATED、activation evidence hash与代码不一致，或clean checkpoint尚未冻结时，本轮不得开始production slice。

---

## 0. 执行结论

### 0.1 Capability 的新定义

Pulsara中的`Capability`固定定义为：

> 可以被模型发现、理解或使用的有界语义能力描述；被发现或暴露不自动授予权限，也不必然具有独立执行器。

本轮只接受两个capability leaf kind：

| 对象 | Capability kind | Provider暴露 | Execution authority |
|---|---|---|---|
| Built-in tool | `TOOL` | native `tools[]` | Builtin registry、permission与exact binding |
| MCP tool | `TOOL` | native direct tool或new-MCP meta route | MCP supervisor、slot、dirty fence与effect policy |
| Skill | `SKILL` | `SKILL_CATALOG`与`ACTIVE_SKILL` | 无独立executor；只能指导或引用现有tool capability |

Plugin不属于本轮的capability leaf。后续Round 9.2把Plugin定义为`CapabilityBundle/CapabilitySource`：启用后把portable Skill物化到本文冻结的四个既有physical roots之一，并贡献MCP server definitions与process-local Hook definitions，同时保留一个dormant Subagent-spec inventory；Skill/MCP仍分别交还既有owner，Hook不进入capability leaf，Plugin自身不拥有通用`invoke()`。Plugin不得新增第五个Skill root或让Runtime扫描Plugin cache。

### 0.2 统一什么，不统一什么

三者只统一：

1. source registration与refresh policy；
2. immutable source snapshot/discovery cut；
3. leaf admission、stable capability identity与semantic fingerprint；
4. exact source provenance与frozen registry snapshot；
5. pure provider exposure planning；
6. same-epoch append-only与future cold-boundary input。

三者明确不统一：

- physical executor；
- tool permission/effect policy；
- MCP connection、slot lease、concurrency lane或dirty generation；
- Skill filesystem读取与activation；
- canonical ToolExecutionAttempt/ToolResult事务；
- Plugin enablement；
- compaction/rebase；
- durable recovery。

### 0.3 不使用行为型抽象基类

禁止引入以下形状：

~~~python
class Capability(ABC):
    def expose(self): ...
    def authorize(self): ...
    def invoke(self): ...
~~~

Skill没有`invoke()`，Plugin也不应进入provider tool surface。把它们塞入同一个开放继承层只能产生大量optional字段、空实现与绕过closed authority的plugin subclass。

本轮使用共享identity值对象与closed tagged union：

~~~text
CapabilityIdentity
    +-- FrozenToolCapabilityFact
    |       +-- BUILTIN origin
    |       +-- MCP origin
    +-- FrozenSkillCapabilityFact

FrozenCapabilityFact
    = FrozenToolCapabilityFact | FrozenSkillCapabilityFact
~~~

这里的统一发生在**注册与语义快照**，而不是`invoke()`。Built-in、MCP与Skill都先注册一个bounded source，再由source owner冻结完整snapshot并把leaf送入同一个pure registry admission：

~~~text
register source
    -> freeze/discover complete source snapshot
    -> admit closed capability facts
    -> freeze one registry snapshot
    -> plan provider exposure

TOOL fact  -> exact-join execution binding -> authorize/invoke
SKILL fact -> catalog/activation only       -> no executor
~~~

Built-in source的snapshot是零I/O、确定性、永不刷新的**退化discovery**，但它不是compiled descriptor catalog的无条件全集：Host open必须先把实际安装的executor bindings与catalog entries做scope-aware双向join，只有execution-backed inventory才能成为Built-in source leaf。MCP与Skill使用同一条注册管线，只额外允许在provider safe point产生successor source snapshot。不得因为Built-in没有外部I/O，就为它保留一套绕过registry的特殊leaf入口。

### 0.4 Prefix不变量

同一Host、同一exact ROOT/child scope、同一provider-input continuity epoch继续满足：

~~~text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
~~~

因此：

- Built-in tool集合只在cold epoch建立时冻结；
- cold epoch选中的direct MCP集合在该epoch内固定；
- late-ready MCP只能追加`MCP_CATALOG` observation并走meta route；
- direct MCP失联时不删除descriptor，只在local gate返回typed unavailable；
- Skill新增、修改、删除只追加`SKILL_CATALOG` successor snapshot；
- active Skill仍由`ACTIVE_SKILL`独立表达；
- 本轮不修改BASE_SYSTEM来塞入dynamic capability。

### 0.5 最终产品路径

~~~text
Host/cold epoch planning
  -> obtain three exact-scope owner-issued immutable snapshots
       Builtin: one sealed execution-backed snapshot
       MCP: one complete snapshot set covering every registered server
       Skill: one globally resolved LOCAL_SKILL_CATALOG snapshot
  -> central factory consumes the three named inputs and derives the complete registered source set
  -> pure registry admission and FrozenCapabilityRegistrySnapshot
  -> prepare exact model target/profile without opening provider
  -> adapter-owned pure native-wire preflight over canonical Tool facts
  -> freeze one parent FrozenCapabilityDispatchCut
  -> mechanically derive two narrow sibling dispatch views
       tool view  -> KernelToolCapabilityPlanner
       skill view -> KernelSkillProjectionComposer
  -> pure KernelToolCapabilityPlanner consumes frozen eligibility facts
  -> select fixed direct tool cohort and exact native wire projections
  -> build FrozenModelToolSurface
  -> exact join existing physical tool-surface access
  -> compiler / preflight / continuity install / provider open

same epoch: new MCP becomes READY_CLEAN
  -> MCP supervisor remains the only connection/catalog owner
  -> publish a complete successor source snapshot at safe point
  -> freeze a successor registry snapshot; do not mutate the installed one
  -> planner classifies tool as NEW_MCP_META_ONLY
  -> append MCP_CATALOG observation
  -> inspect_new_mcp_tool -> use_new_mcp_tool
  -> one ordinary canonical attempt/result
  -> never mutate provider tools

same epoch: Skill catalog changes
  -> publish one complete/unavailable successor LOCAL_SKILL_CATALOG snapshot at safe point
  -> freeze a successor registry snapshot; do not mutate the installed one
  -> append SKILL_CATALOG successor or invalidation
  -> never mutate provider tools
~~~

---

## 1. 范围、非目标与迁移纪律

### 1.1 本轮实施

- capability领域词汇与closed contracts；
- Built-in catalog、MCP server config与聚合local Skill catalog的统一source-registration adapter；
- `IMMUTABLE | SAFE_POINT_REFRESHABLE` source policy、complete source snapshot与pure registry admission；
- Built-in/MCP tool到统一`FrozenToolCapabilityFact`的无损adapter；
- current Skill projection到最小`FrozenSkillCapabilityFact`的adapter；
- `CapabilityIdentity`、version reference与MCP resolved route；
- process-local `FrozenCapabilityRegistrySnapshot`、父`FrozenCapabilityDispatchCut`与pure `KernelToolCapabilityPlanner`；
- cold MCP direct cohort的deterministic all-or-none selection；
- canonical Tool capability与adapter-native wire eligibility的显式分层；
- 单一pure native-wire projection contract、closed incompatibility reason与final wire exact join；
- late-ready MCP meta-only exposure；
- fixed `inspect_new_mcp_tool`与`use_new_mcp_tool` builtin descriptors；
- `list_mcp_servers`的DIRECT/NEW分类与bounded pagination；
- direct MCP disconnect/schema replacement gate；
- `CAPABILITY_CATALOG`到`SKILL_CATALOG`的clean-v0 rename；
- 当前skill-only类、protocol与diagnostic的诚实名词收窄；
- ROOT/child scope isolation、prefix、permission、effect与real-provider dogfood tests。

### 1.2 明确非目标

- 不实施Agent Skills standard parser或progressive resource contract；这些属于Round 9.1；
- 不修改ordinary `read_file`或实现任何Skill声明驱动的permission行为；Round 9.1将明确保持read schema无Skill intent；
- 不扫描`.claude/skills`、Plugin cache或新增Skill root；
- Round 9与Round 9.1共同冻结同一套四种physical root policy：workspace `.pulsara/skills`、workspace `.agents/skills`，以及启用user skills时的user `.pulsara/skills`与`.agents/skills`；`.claude/skills`不属于Pulsara root policy；
- 不实现Plugin manifest、install、enable、disable或bundle namespace；
- 不让Plugin动态加载Python代码；
- 不实施compaction summary、snapshot adoption、rebase或MCP promotion；
- 不把late MCP在same epoch热提升为native direct tool；
- 不增加provider-side tool search、BM25、dense retrieval或virtual descriptor filesystem；
- 不新增generic `search_capabilities`/`use_capability`；
- 不把MCP resources/prompts/elicitation重分类为新的capability leaf；它们继续由Round 6现有fixed tools访问；
- 不建立durable capability table、event、job、generation、receipt、checkpoint、projection或repair graph；
- 不承诺跨Host保持同一个capability exposure epoch；replacement Host仍cold build；
- 不修改canonical transcript schema；
- 不把OpenAI lowered schema写回MCP discovery fact、generic registry或capability semantic fingerprint；
- 不建立通用JSON Schema translator或第二套schema authority；native projection只允许本文冻结的closed、bounded transformation，不能诚实投影的MCP走meta/unavailable。

### 1.3 Clean-v0纪律

当前仓库允许clean-v0 reset。本轮可以删除或重命名尚未形成对外durable wire contract的旧Python类型，不建立双读或alias图。但必须保留真正被生产路径依赖的descriptor、permission、result、long-horizon与binding语义。

本轮尤其不得同时保留：

- 旧`CapabilityExposurePlan`与新`FrozenToolCapabilityExposurePlan`两套planner；
- 旧skill-only`CapabilityProjectionOutput`与新`SkillProjectionOutput`两套projection；
- 旧`CAPABILITY_CATALOG`与新`SKILL_CATALOG`两个source kind；
- MCP semantic fact与generic tool fact两份可独立漂移的schema truth。

---

## 2. 当前代码真值与减法依据

### 2.1 已存在且必须复用

当前代码已经拥有：

- `capability/builtin_catalog.py`中的builtin descriptor、availability、permission、result render与long-horizon policy；
- `_BUILTIN_TOOL_CATALOG`这一零I/O、确定性的descriptor/policy catalog；它当前是production executor inventory的超集，不能直接冒充provider-visible capability全集；
- `mcp_config.py::load_mcp_server_configs()`把user/workspace/Host override组合成显式server registrations，MCP supervisor再负责initialize/discovery/listChanged；
- `capability/local_skills.py::LocalSkillProvider`先使用固定root policy，再bounded scan/parse其中的`SKILL.md`；
- `model_input/contracts.py::FrozenToolSpec`与`FrozenModelToolSurface`，它们是provider-neutral canonical typed tool schema的唯一frozen truth；actual native wire projection由adapter另行拥有且不得回写；
- Round 5A.2 `provider_assistant_replay_fragments`、metadata/body hydration与`FrozenProviderWireInputPlan` actual-wire proof；
- `llm/adapters/openai/function_tools.py`中的共享Chat/Responses显式`strict:false`、bounded prevalidated lowering与`OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION`；
- `conversation_kernel/tool_surface.py::PreparedToolExecutionBinding`与`ProcessLocalToolSurfaceAccess`，它们把semantic descriptor exact join到Host authority与executor；
- `conversation_kernel/mcp/contracts.py::McpToolSemanticFact`、discovery snapshot与catalog snapshot；
- MCP supervisor作为唯一connection、slot、dirty generation与physical close owner；
- Round 6 `MCP_CATALOG`与`list_mcp_servers`；
- local Skill discovery、catalog projection、`ACTIVE_SKILL`和Round 3.1 append-only source head；
- structured compiler的64 tool / 1 MiB aggregate tool-schema bound；
- continuity owner的same-epoch SYSTEM/tools/messages proof；
- Round 7 provider-wire hygiene，不把internal fingerprint/generation写入模型正文。

当前Round 6 direct-only路径还在MCP discovery中直接import并调用OpenAI lowering；wire-incompatible tool会被既有`FAIL_SERVER | OMIT_INVALID`分支当作invalid schema处理。该临时边界在Round 9必须删除：canonical JSON Schema validity与native-wire compatibility是两个不同判断，generic registry与MCP discovery均不得import OpenAI adapter。否则合法但wire-incompatible的MCP capability会在进入meta planner前被永久丢弃。

`McpInvalidToolPolicy`的既有公开配置枚举固定保持`FAIL_SERVER | OMIT_INVALID`。本轮不得把`OMIT_INVALID`重命名为`SKIP_TOOL`，不得增加YAML alias、translator或兼容双读；文中的“omit/丢弃”只是行为描述，不是第三个配置值。

当前compiled builtin catalog共有31项，其中`create_agent_tasks`、`wait_agent_tasks`、`stop_agent_task`、`report_agent_phase`与`report_agent_result`尚无production executor；它们属于后续hierarchical task graph的dormant descriptor。`DirectKernelToolPort.snapshot_tool_surface()`当前正确地从实际安装的executor bindings出发，再逐项查询catalog并按ROOT/child scope过滤。因此Round 9必须继承这条真实方向：**binding-backed inventory -> catalog join -> semantic fact**，而不能反向把整个compiled catalog暴露为可执行能力。

### 2.2 当前名实不符

当前`capability`包同时包含两组互不一致的形状：

1. `CapabilityDescriptor`、`CapabilityRegistry`与`CapabilityExposurePlan`看似通用，实际描述和规划的是model-callable tools，并夹带旧registry generation/event projection语义；
2. `CapabilityProjectionOutput`、`KernelCapabilityComposer`与`CAPABILITY_CATALOG`看似通用，实际只处理local Skill catalog和active Skill。

`primitives/capability.py::CapabilityExecutionSurfaceIdentityFact`还保存descriptor artifact ID与binding contract identity；当前canonical Kernel已经由`FrozenModelToolSurface`和`ProcessLocalToolSurfaceAccess`提供更精确的semantic/physical split。除非M0证明仍有独立production consumer，否则该旧primitive也应删除，而不是成为新planner的第三份surface truth。

旧`CapabilityRegistry.register()`也不能被直接复活为新的long-lived mutable owner：它只接受tool-shaped descriptor，使用自增generation，并且没有MCP/Skill complete-source cut、safe-point refresh或continuity语义。Round 9保留“所有leaf经过一个注册入口”的思想，但实现为**一次性pure registry builder/factory + frozen snapshot**；动态变化通过重建successor snapshot表达，不在旧snapshot上原地register/unregister。

与此同时，production Kernel真正执行MCP与builtins时已经使用另一套更严格的`FrozenModelToolSurface + PreparedToolExecutionBinding + ProcessLocalToolSurfaceAccess`。

本轮不能在这三套形状上再包一层。最小做法是：

- 把tool-only descriptor命名收窄；
- 把skill-only projection命名收窄；
- 让新的pure planner直接组合现有frozen leaf facts；
- 删除未被production authority需要的旧registry/exposure generation语义；
- 保持permission与execution owner原位。

### 2.3 hard-cut前值得保留的结构

hard-cut前统一capability设计的正确部分是四层分离：

~~~text
Discovery / Registry
    -> Exposure / Advertisement
    -> Gate / Policy
    -> Execution / Result
~~~

它正确表达了descriptor不等于executor、Skill引用不能创建工具，以及direct/deferred/hidden disposition。错误部分是把exposure generation、working-set lineage、artifact、event与recovery做成durable graph。

Round 9保留前四层的**纯语义分离**，但用Round 3/3.1 process-local continuity epoch替代durable exposure lineage。

在此基础上，注册与发现还必须拆成两个正交步骤：

~~~text
Source composition / registration
    -> Source-owned discovery or zero-I/O snapshot
    -> Common leaf admission / frozen registry
    -> Exposure / Advertisement
    -> Gate / Policy
    -> Execution / Result
~~~

Built-in不是“没有discovery”的例外，而是`IMMUTABLE` source，其discovery退化为对Host-open时**execution-backed descriptor inventory**的纯snapshot。Compiled catalog仍可以保存尚未接入production composition的descriptor，但这些dead/dormant entries不是capability source leaf。MCP server与聚合`LOCAL_SKILL_CATALOG`是`SAFE_POINT_REFRESHABLE` source；它们可以反复发现新leaf，但每次都必须先形成完整、immutable source snapshot，再进入与Built-in相同的leaf admission。不得让MCP supervisor或filesystem scanner绕过registry直接修改planner集合。

---

## 3. Canonical词汇与authority边界

### 3.1 七个词的唯一含义

| 词 | 定义 | 是否拥有execution |
|---|---|---|
| `CapabilitySourceRegistration` | Host当前承认哪些bounded source及其refresh policy | 否 |
| `CapabilitySourceSnapshot` | 一个registered source在一次complete freeze/discovery后的immutable leaf集合 | 否 |
| `CapabilityFact` | 某项能力是什么的immutable semantic fact | 否 |
| `CapabilityRegistrySnapshot` | 对同一parent dispatch cut中三个owner-issued immutable source inputs的pure、closed合并 | 否 |
| `CapabilityExposure` | 该fact如何进入provider输入 | 否 |
| `CapabilityBinding` | tool capability如何exact绑定到本地执行器 | 仅tool有 |
| `CapabilityBundle` | 一组source/contribution的安装组合 | 否；留给Round 9.2 |

### 3.2 Authority矩阵

| 事实 | 唯一owner |
|---|---|
| Built-in source registration/snapshot input | Host tool composition/tool-surface owner + builtin catalog adapter |
| Built-in descriptor/policy | builtin catalog |
| Built-in physical binding | builtin ToolRegistry/ports |
| MCP server source registration | resolved MCP config composition |
| MCP discovery/catalog/status | MCP supervisor |
| MCP physical connection/slot | MCP supervisor |
| MCP dirty fence/effect policy | MCP supervisor + existing tool runtime |
| Local Skill catalog source registration/snapshot | Host local-Skill composition policy + `LocalSkillProvider` |
| Skill filesystem manifest与winning-root provenance | aggregate Skill source snapshot |
| Skill active body | existing `ACTIVE_SKILL` projection |
| Capability registry snapshot | pure central factory；没有long-lived mutable owner |
| Provider direct tool surface | `FrozenModelToolSurface` + continuity epoch |
| Provider native tool wire eligibility/projection | resolved model adapter的single pure factory + frozen planning value |
| Capability exposure selection | pure planner result；不是authority |
| Tool attempt/result | existing canonical repository transaction |

`KernelToolCapabilityPlanner`只能消费上述owner已经冻结的Tool事实。`KernelSkillProjectionComposer`只消费同一个父dispatch cut中的Skill view。二者都不能自行connect MCP、读Skill文件、检查permission、创建attempt或关闭slot。

### 3.3 Trust

- Built-in descriptor是Pulsara-owned schema，但是否可调用仍由permission与binding决定；
- MCP descriptor来自远端server，schema只经过bounded canonical validation/freeze且不成为本地policy；provider-specific lowering不属于discovery normalization；
- Skill是untrusted instructional data；
- provider exposure只表示“模型可以看到或引用”，不表示“物理操作已授权”；
- Runtime permission、Plan、memory opt-out、MCP dirty fence与effect confirmation始终优先。

### 3.4 注册/发现与执行是两个正交维度

| 类型 | 固定注册 | snapshot/discovery | refresh | execution |
|---|---|---|---|---|
| Built-in tool | Host-open execution-backed builtin source | 对已安装binding与catalog exact join后的inventory做零I/O pure snapshot | `IMMUTABLE` | Builtin binding |
| MCP tool | resolved MCP server source | bounded initialize/list tools/catalog snapshot | `SAFE_POINT_REFRESHABLE` | MCP slot/binding |
| Skill | one exact-scope local Skill catalog source | bounded ordered-root filesystem scan/parse snapshot | `SAFE_POINT_REFRESHABLE` | 无 |

这里的“固定注册”不是为MCP复制一份静态remote schema，也不是为Skill发明inline Python manifest：

- MCP固定注册的是server source；remote tool schema仍只由negotiated discovery拥有；
- Skill固定注册的是聚合`LOCAL_SKILL_CATALOG` source；current root集合、precedence与physical paths由Skill owner内部policy拥有，skill leaf仍只由winning root中的标准`SKILL.md`拥有；
- Built-in descriptor catalog与executor inventory不是同一事实；source adapter必须从Host-open时已安装、scope-visible且能与catalog exact join的binding inventory构造registration/snapshot，catalog-only entry不能进入registry；
- bundled Skill必须先物化到Skill owner当前root policy中的某个physical root再被普通discovery发现；不得走第二条“builtin Skill”leaf通道；
- future Plugin向本文只贡献MCP server registration，并由installer把portable Skill物化到四个既有physical roots之一；它不得修改local Skill catalog的root policy或让Runtime扫描Plugin cache。其Hook由独立process-local owner执行，Subagent spec暂时dormant，均不得注册第三种tool executor。

因此，三者在逻辑上共享同一条注册管线，而只在source-owned discovery与tool-only binding处分叉。注册成功只证明fact进入current registry；它不证明该fact已暴露、已授权或可物理执行。

---

## 4. 最小closed contracts

### 4.1 Identity与source

~~~python
class CapabilityKind(StrEnum):
    TOOL = "TOOL"
    SKILL = "SKILL"


class CapabilitySourceKind(StrEnum):
    BUILTIN_REGISTRY = "BUILTIN_REGISTRY"
    MCP_SERVER = "MCP_SERVER"
    LOCAL_SKILL_CATALOG = "LOCAL_SKILL_CATALOG"


class LocalSkillRootKind(StrEnum):
    WORKSPACE_PULSARA = "WORKSPACE_PULSARA"
    WORKSPACE_AGENTS = "WORKSPACE_AGENTS"
    USER_PULSARA = "USER_PULSARA"
    USER_AGENTS = "USER_AGENTS"


@dataclass(frozen=True, slots=True)
class CapabilitySourceRef:
    kind: CapabilitySourceKind
    stable_source_id: str
    source_identity_fingerprint: str


@dataclass(frozen=True, slots=True)
class CapabilityIdentity:
    kind: CapabilityKind
    source: CapabilitySourceRef
    stable_name: str
    identity_fingerprint: str
~~~

`source_identity_fingerprint`只覆盖`source.kind + stable_source_id`，不覆盖MCP discovery revision、filesystem digest或Host generation。`identity_fingerprint`使用domain-separated canonical encoding覆盖`kind + source.kind + stable_source_id + stable_name`。

`LocalSkillRootKind`属于Round 9的local Skill source policy，而不是Agent Skills文件格式或generic source kind。它exact区分本文冻结的四种physical roots，进入winner provenance与Skill fact semantic fingerprint，但不增加四个generic registrations；Round 9.1只能复用该enum，不能扩张第五种root或另建同名closed union。

身份规则：

- Built-in tool：`stable_source_id = pulsara-builtin-tools`，`stable_name = exact tool name`；
- MCP tool：`stable_source_id = exact server_id`，`stable_name = complete remote_tool_name`；
- Skill：`stable_source_id = pulsara-local-skill-catalog`，`stable_name = current winning skill name`；winning root/location只作为fact provenance，不改变public identity；
- MCP provider-mangled tool name不是canonical identity；
- filesystem absolute path不是public identity；
- owner epoch、slot generation、connection object、mtime与writer generation不进入identity。

同一identity可以在不同semantic fingerprint下出现新版本。Identity回答“是哪项能力”，semantic fingerprint回答“当前版本是什么”。

### 4.2 Source registration

~~~python
class CapabilitySourceRefreshMode(StrEnum):
    IMMUTABLE = "IMMUTABLE"
    SAFE_POINT_REFRESHABLE = "SAFE_POINT_REFRESHABLE"


@dataclass(frozen=True, slots=True)
class FrozenCapabilitySourceRegistration:
    source: CapabilitySourceRef
    refresh_mode: CapabilitySourceRefreshMode
    source_contract_fingerprint: str
    registration_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenCapabilitySourceRegistrationSet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    builtin_registration: FrozenCapabilitySourceRegistration
    mcp_registrations: tuple[FrozenCapabilitySourceRegistration, ...]
    local_skill_catalog_registration: FrozenCapabilitySourceRegistration
    registration_set_fingerprint: str

    @property
    def registrations(self) -> tuple[FrozenCapabilitySourceRegistration, ...]:
        """Derived deterministic BUILTIN + MCP + LOCAL_SKILL_CATALOG view."""
        return (
            self.builtin_registration,
            *self.mcp_registrations,
            self.local_skill_catalog_registration,
        )
~~~

closed matrix固定为：

| `source.kind` | `refresh_mode` | source-specific registration truth |
|---|---|---|
| `BUILTIN_REGISTRY` | `IMMUTABLE` | Host-open execution-backed builtin inventory + joined catalog contract |
| `MCP_SERVER` | `SAFE_POINT_REFRESHABLE` | one resolved server config identity |
| `LOCAL_SKILL_CATALOG` | `SAFE_POINT_REFRESHABLE` | one exact-scope aggregate catalog；ordered root policy由Skill owner内部持有 |

`source_contract_fingerprint`只引用source owner已经冻结的非秘密semantic registration identity。Builtin branch覆盖由完整binding input选出的ordered descriptor/catalog contract identities，但不覆盖executor object/identity或binding generation。Generic contract不保存MCP headers/auth/request state/transport object，也不保存Skill absolute private path、directory handle、root count或watcher。Owner-specific config/root policy仍由MCP supervisor或Skill provider持有；generic registration只证明“这个source被Host composition承认”。

仅凭可重算的semantic tuple仍不能证明现实owner inventory完整，因此保留三个**owner-issued、private-constructor、immutable snapshot carrier**；但删除共同attempt token、跨owner current-seal握手与“同一瞬间”的承诺：

| required owner snapshot | 唯一producer | 完整性的含义 |
|---|---|---|
| `SealedBuiltinCapabilitySnapshot` | Host tool composition + builtin adapter | exact scope下完整、immutable、execution-backed且catalog-joined的Builtin集合 |
| `PreparedMcpCapabilitySourceSnapshotSet` | resolved MCP config composition + MCP supervisor | exact scope下当前全部enabled server registrations，且每项恰有一个`COMPLETE | UNAVAILABLE` snapshot；允许合法空server tuple |
| `PreparedLocalSkillCatalogSourceSnapshot` | Host Skill composition + `LocalSkillProvider` | exact scope下按当前registered root policy全局解析precedence后的一个`LOCAL_SKILL_CATALOG` snapshot |

三种carrier各自在自身owner lock/safe-point内一次性签发，携带exact scope、generic registration/snapshot与不序列化的owner authenticity。Builtin carrier额外持有composition seal和完整execution binding input；MCP carrier持有resolved-config completeness proof；Skill carrier持有ordered-root policy fingerprint、bounded diagnostics与`LocalSkillDiscovery`。这些opaque字段使用`repr=False, compare=False`，不进入semantic fingerprint，也不形成跨owner lease、generation或新authority。

Production central API固定为三个named参数：

~~~python
def freeze_capability_registry_from_owner_snapshots(
    *,
    builtin: SealedBuiltinCapabilitySnapshot,
    mcp: PreparedMcpCapabilitySourceSnapshotSet,
    skills: PreparedLocalSkillCatalogSourceSnapshot,
) -> FrozenCapabilityRegistrySnapshot: ...
~~~

Central seam验证三个carrier均由其真实owner构造、exact scope一致，并分别满足自身完整性；它不重新询问owner“是否仍current”，也不声称获得跨owner原子瞬间。三项immutable snapshot按既有dispatch absolute deadline顺序冻结即可；freeze后发生的MCP/Skill变化只进入下一个safe-point successor。Production其他模块不得提交raw registration/snapshot tuple或自行重建owner carrier。

`FrozenCapabilitySourceRegistrationSet`由上述三个named carrier中的registrations机械派生，而不是调用者传入：exact one Builtin、bounded MCP tuple、exact one aggregate Skill catalog。其fingerprint覆盖exact scope、Builtin registration、按`server_id`排序的MCP registrations与Skill catalog registration；opaque owner authenticity不进入结果。

注册规则：

- exact同一`source + registration_fingerprint`重复输入是idempotent；
- 同一`source`在一个registration set中出现两个不同fingerprint是conflict；
- Built-in registration及其execution-backed leaf inventory在Host open后不得替换、删除或新增；compiled catalog中没有production binding的descriptor允许继续作为dormant catalog entry存在，但不得进入source snapshot；
- MCP server composition与Skill catalog snapshot只能在safe point采纳owner-issued complete cut；
- aggregate Skill registration始终恰有一个；owner root policy合法为空时发布`COMPLETE + facts=()`，不通过删除generic source表达空catalog；
- source registration的新增/删除不直接改provider输入，必须经过successor registry与exposure planner；
- 每个owner snapshot与最终registration set都exact绑定`conversation_scope_kind + scope_subagent_task_id`；ROOT snapshot不能复用于child，反之亦然；
- derived registration view固定为`Builtin singleton + MCP按server_id排序 + aggregate Skill singleton`且unique，并且exact等于三个named owner snapshot中的scope-filtered registrations；
- source-set变化产生新的immutable registration set，旧set不得原地修改；
- generic registry不执行config load、filesystem scan、network connect或secret resolution。

### 4.3 Tool fact

~~~python
class ToolCapabilityOrigin(StrEnum):
    BUILTIN = "BUILTIN"
    MCP = "MCP"


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityFact:
    identity: CapabilityIdentity
    origin: ToolCapabilityOrigin
    canonical_tool_spec: FrozenToolSpec
    semantic_fingerprint: str

    @property
    def fact_semantic_fingerprint(self) -> str:
        return self.semantic_fingerprint


@dataclass(frozen=True, slots=True)
class ToolCapabilityVersionRef:
    identity_fingerprint: str
    semantic_fingerprint: str
    provider_name: str
    version_fingerprint: str
~~~

Central factory规则：

- Built-in adapter从Host tool-surface owner在同一surface lock下冻结的exact-scope executor binding inventory出发，逐项exact join现有catalog entry后转换`FrozenToolSpec`；禁止从catalog keys正向枚举leaf；
- MCP adapter从`McpToolSemanticFact.provider_spec()`无损冻结`canonical_tool_spec`；现有owner方法名不改变这里的canonical归属；
- `semantic_fingerprint`覆盖identity、canonical public name/description/schema和descriptor fingerprint；
- 不复制effect、permission、availability、slot lease或executor；
- 同一identity/semantic fingerprint必须产生byte-identical `canonical_tool_spec`；
- 同一provider name不得映射到两个identity。

`FrozenToolCapabilityFact`不是`PreparedToolExecutionBinding`的父类。二者通过`canonical_tool_spec.descriptor_fingerprint`与capability identity exact join。

`canonical_tool_spec`是唯一capability schema truth：local argument validator、effect/permission descriptor与physical execution binding都必须exact join它。任何OpenAI/Chat/Responses lowering结果、`strict`成员、wire wrapper、provider profile或wire contract version均不得写回该字段、registry/source fingerprint或`semantic_fingerprint`。

#### 4.3.1 Adapter-owned native wire eligibility

Native exposure使用一个窄、pure、process-local的adapter contract，不把OpenAI adapter导入generic registry/planner，也不建立第二套schema authority：

~~~python
class NativeToolWireEligibilityKind(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"


class NativeToolWireIncompatibilityReason(StrEnum):
    ROOT_SHAPE_UNSUPPORTED = "ROOT_SHAPE_UNSUPPORTED"
    COMPOSITION_UNSUPPORTED = "COMPOSITION_UNSUPPORTED"
    CONSTRAINT_UNSUPPORTED = "CONSTRAINT_UNSUPPORTED"
    PROJECTION_OVERBOUND = "PROJECTION_OVERBOUND"


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireProjection:
    capability_version_fingerprint: str
    canonical_tool_spec_fingerprint: str
    native_function_tool_wire_contract_fingerprint: str
    wire_tool: FrozenJsonObjectFact
    projection_fingerprint: str

    @property
    def wire_utf8_bytes(self) -> int:
        return len(canonical_json_bytes(self.wire_tool))


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireIncompatibility:
    capability_version_fingerprint: str
    canonical_tool_spec_fingerprint: str
    native_function_tool_wire_contract_fingerprint: str
    reason: NativeToolWireIncompatibilityReason
    decision_fingerprint: str


NativeToolWireEligibility = (
    FrozenNativeToolWireProjection
    | FrozenNativeToolWireIncompatibility
)


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireEligibilitySet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    native_function_tool_wire_contract_fingerprint: str
    entries: tuple[NativeToolWireEligibility, ...]
    eligibility_set_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenNativeToolProjectionSet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    native_function_tool_wire_contract_fingerprint: str
    tool_versions: tuple[ToolCapabilityVersionRef, ...]
    projections: tuple[FrozenNativeToolWireProjection, ...]
    projection_set_fingerprint: str
~~~

Adapter-owned pure factory固定为以下语义：

~~~text
canonical Tool facts + exact resolved adapter profile
  -> NativeEligible(exact frozen wire projection, fingerprint)
   | NativeIncompatible(closed reason)
~~~

规则：

- resolved model target/profile必须在该factory前冻结，但factory不得打开provider、读取transport、调用MCP、查询repository或取得executor；
- cold/compatible planning时，eligibility set对current registry中每个`TOOL` fact恰有一项；installed contract-change reset时，还必须对central factory从predecessor `direct_projection_set.tool_versions + tool_surface`机械配对得到的每个retained direct canonical input恰有一项。同一exact version/spec在两组输入中只保留一项，任何同version不同spec均conflict；Skill没有entry；
- 每个entry exact绑定scope、capability version、canonical spec fingerprint与opaque `native_function_tool_wire_contract_fingerprint`。Retained direct projection input不是新的capability fact或schema truth，只是旧installed canonical surface的process-local typed view；caller不得补写当前registry中不存在的任意tool；
- OpenAI adapter以唯一factory产生该narrow fingerprint，覆盖`OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION`、wire API与tool-relevant request-shape contract，但不覆盖assistant replay、model target token budget或capability semantic fields；generic capability package只消费fingerprint和frozen结果，不import常量或adapter模块；
- projection可以是在non-strict native wire上诚实的受控superset，因为canonical local validator仍是attempt/invoke前的exact authority；projection不得更窄、静默删除canonical admissible arguments或改变tool identity；
- `wire_utf8_bytes`是`wire_tool`的机械派生值，固定等于`len(canonical_json_bytes(wire_tool))`；caller/factory不得单独提交、覆盖或fingerprint一个独立count，所有capacity quote都调用这一派生值；`projection_fingerprint`覆盖canonical wire bytes本身，不重复接受count作为独立语义输入；
- projection factory只允许closed、已测试的bounded transformation，不发展成任意JSON Schema translator。遇到无法证明受控projection的合法canonical schema，返回`INCOMPATIBLE`；
- selected direct cohort必须由central factory从eligibility set冻结成唯一`FrozenNativeToolProjectionSet`，逐项exact join Tool version、canonical spec与wire projection；predecessor、planner output与final wire plan只引用该对象，不得各自复制version/projection tuple；
- final `FrozenProviderWireInputPlan`必须复用或重新执行同一pure projection并对`wire_tool` canonical bytes、projection fingerprint、profile fingerprint逐项exact join；不能只比较version字符串；
- eligibility/projection只存于本次planning与installed process-local epoch view；不写registry、canonical rows、event、activation catalog或durable replay row。

Builtin与MCP策略不同：

- Pulsara-owned Builtin若存在精确的OpenAI-portable canonical表达，应直接收窄其authored canonical schema，减少projection工作；无精确portable等价时仍保留exact canonical schema，并允许上述受控wire superset；任何execution-backed Builtin返回`INCOMPATIBLE`都使整个cold planning在provider open前fail closed，不能skip或走MCP meta；
- MCP始终保留server发布且通过canonical validation的原始schema。Native-compatible工具可成为DIRECT候选；native-incompatible工具只有在完整`inspect_new_mcp_tool` DTO能通过Round 7.1 FULL quote时才成为`NEW_MCP_META_ONLY`，否则为`UNAVAILABLE/DESCRIPTOR_OVERBOUND`；
- canonical invalid与native-wire incompatible是互斥分类。只有前者进入既有`FAIL_SERVER | OMIT_INVALID`与`invalid_tool_count`；后者必须保留在complete canonical source snapshot中，供planner决定meta/unavailable。

MCP route matrix固定为：

| canonical schema | native wire | inspect FULL | route / policy |
|---|---|---|---|
| invalid | — | — | `FAIL_SERVER | OMIT_INVALID`；不产生capability fact |
| valid | incompatible | fits | `NEW_MCP_META_ONLY / NATIVE_WIRE_INCOMPATIBLE` |
| valid | incompatible | overbound | `UNAVAILABLE / DESCRIPTOR_OVERBOUND` |
| valid | compatible | 不适用 | native DIRECT cohort candidate；若aggregate fallback到meta，再要求inspect FULL |

表中“compatible”只表示当前adapter contract有诚实native projection，不授予permission或physical availability。Aggregate fallback后的compatible tool若inspect overbound，同样必须转为`UNAVAILABLE/DESCRIPTOR_OVERBOUND`，不能声称meta可用。

Built-in adapter的双向不变量固定为：每个进入snapshot的builtin fact恰有一个installed binding与一个matching catalog entry；每个该scope可见的installed builtin binding恰好产生一个fact。Catalog-only dormant descriptor不要求有binding，也不产生fact；binding没有catalog entry、descriptor fingerprint不一致或同名多binding均在provider open前fail closed。Round 9新增的`inspect_new_mcp_tool/use_new_mcp_tool`若要成为fixed tools，必须同时实现真实local executor binding，不能只在catalog加schema。

这条不变量由现有tool-surface owner签发的唯一process-local Builtin snapshot实现，而不是让pure registry读取executor，也不再建立“prepared source input → inventory → snapshot”三层相同tuple：

~~~python
@dataclass(frozen=True, slots=True)
class SealedBuiltinCapabilitySnapshot:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshot: "FrozenCapabilitySourceSnapshot"
    executor_bindings: tuple[ProductionBuiltinExecutorBinding, ...] = field(repr=False)
    builtin_composition_seal: object = field(repr=False, compare=False)
~~~

`DirectKernelToolPort`必须在同一surface lock下、复用当前ROOT/child过滤规则冻结这份完整binding tuple；adapter逐项读取matching catalog entry并一次性派生Builtin registration、generic facts与`COMPLETE` source snapshot。ROOT/child snapshot来自同一个sealed base tuple，但各自拥有exact scope envelope。`executor_bindings`与opaque seal只用于owner真实性及后续physical preflight join，不进入`FrozenToolCapabilityFact`、registry fingerprint或provider body；generic registry package也不得import该Host-facing carrier。

Builtin composition不能只依赖Host构造调用顺序，必须有显式process-local seal：

~~~python
class BuiltinCompositionState(StrEnum):
    PREPARING = "PREPARING"
    SEALED = "SEALED"
    CLOSED = "CLOSED"
~~~

状态与API规则固定为：

- `DirectKernelToolPort`初始为`PREPARING`；builtin tool objects、interaction、subagent、memory、MCP supervisor以及Round 9 fixed meta executors/ports都必须在该状态完成binding；
- Host在任何tool-surface/source snapshot/Skill allowlist读取前调用一次`seal_builtin_composition()`；它在同一surface lock下验证required bindings、冻结完整base `ProductionBuiltinExecutorBinding` tuple与seal fingerprint，然后原子进入`SEALED`；
- 所有composition-affecting `bind_*`、seal与sealed snapshot admission都由同一surface lock串行化；`bind_interaction_port`也不得继续作为无锁例外；
- 重复seal只返回同一个既有seal/input identity，不增加generation；
- `SEALED`后所有会改变Builtin executor inventory或其required port的`bind_*`调用typed拒绝；不存在unseal、late builtin registration或自动cold reset；
- ROOT/child `SealedBuiltinCapabilitySnapshot`都只能从同一个sealed base tuple做closed scope projection，不能重新读取mutable ports；
- MCP `install_pending_at_safe_point()`、same-schema reconnect与dynamic route变化仍可在seal后运行，因为它们只改变MCP owner state，不得修改sealed Builtin tuple；
- close把状态置为`CLOSED`，之后seal/bind/snapshot均拒绝。

Seal只是现有Host tool composition的一个bool/closed state与frozen tuple，不是新的capability generation、lease、durable owner或recovery mechanism。

### 4.4 Skill fact

Round 9只冻结足够让统一planner识别Skill的最小leaf；不在本轮规定Agent Skills文件格式，也不把legacy metadata转成dependency graph：

~~~python
@dataclass(frozen=True, slots=True)
class FrozenSkillCapabilityFact:
    identity: CapabilityIdentity
    public_name: str
    description: str
    location: str
    winning_root_provenance_fingerprint: str
    catalog_semantic_fingerprint: str
    activation_semantic_fingerprint: str
    fact_semantic_fingerprint: str
~~~

其中：

- `catalog_semantic_fingerprint`只覆盖provider catalog可见字段；
- `activation_semantic_fingerprint`可额外覆盖当前body/version，但body本身不进入catalog；
- `winning_root_provenance_fingerprint`由Skill owner覆盖logical root kind、precedence ordinal与stable location prefix；它只证明winner来源，不包含absolute path、不进入provider body，也不改变`CapabilityIdentity`；
- `fact_semantic_fingerprint`使用domain-separated canonical encoding覆盖identity fingerprint、catalog semantic fingerprint、activation semantic fingerprint与winning-root provenance fingerprint；central Skill adapter必须逐字段重算并拒绝caller-supplied mismatch。它是generic source snapshot唯一使用的Skill leaf version，不替代两项provider-visible fingerprint；
- 当前legacy parser通过adapter产生该fact；
- Round 9.1替换parser后必须保持此leaf contract，不得再造`AgentSkillCapabilityBase`；
- Skill没有execution binding、permission、tool requirement、CLI health或route字段；
- installed valid Skill默认进入model catalog并允许现有textual/configured activation；legacy `user_invocable/disable_model_invocation`不进入generic fact。

### 4.5 Source snapshot与统一registry

~~~python
class CapabilitySourceSnapshotDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


FrozenCapabilityFact = FrozenToolCapabilityFact | FrozenSkillCapabilityFact


@dataclass(frozen=True, slots=True)
class FrozenCapabilitySourceSnapshot:
    registration: FrozenCapabilitySourceRegistration
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    disposition: CapabilitySourceSnapshotDisposition
    facts: tuple[FrozenCapabilityFact, ...]
    source_snapshot_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenCapabilityRegistrySnapshot:
    registration_set: FrozenCapabilitySourceRegistrationSet
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...]
    registry_fingerprint: str
~~~

另外两个refreshable owner carrier的closed physical shape固定为：

~~~python
@dataclass(frozen=True, slots=True)
class PreparedMcpCapabilitySourceSnapshotSet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...]
    resolved_config_inventory_fingerprint: str
    owner_authenticity: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedLocalSkillCatalogSourceSnapshot:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshot: FrozenCapabilitySourceSnapshot
    discovery: LocalSkillDiscovery
    root_policy_fingerprint: str
    owner_authenticity: object = field(repr=False, compare=False)
~~~

MCP carrier必须证明`source_snapshots`的registrations exact等于resolved config inventory；Skill carrier必须证明`source_snapshot`是唯一`LOCAL_SKILL_CATALOG` registration的完整global-scan结果，且`discovery`与facts逐项exact join。它们都不提供renew/current-check API；owner后续变化形成新carrier。

`freeze_capability_registry_snapshot(...)`是唯一leaf admission factory。它是pure、一次性、bounded的builder，不是Host-lived registry owner，也没有自增generation、callback、`register/unregister` side effect或跨Host identity。

`source_snapshot_fingerprint`使用closed domain覆盖registration fingerprint、exact scope、disposition与ordered `fact.fact_semantic_fingerprint`；`registry_fingerprint`覆盖derived registration-set fingerprint与ordered source-snapshot fingerprints。`FrozenToolCapabilityFact.fact_semantic_fingerprint`是既有`semantic_fingerprint`的机械property，`FrozenSkillCapabilityFact`则保存上述closed组合值；generic factory不按leaf kind临场选择catalog/activation/provenance字段。任何owner-specific physical identity都不进入这两个semantic fingerprints。

Source snapshot规则：

- 每个snapshot必须exact绑定registration set的scope；snapshot fingerprint覆盖scope，ROOT snapshot不能在child registry复用；
- Built-in snapshot必须`COMPLETE`，只从Host-open时冻结的scope-visible、execution-backed且catalog-joined inventory确定性产生；这就是零I/O退化discovery；
- MCP每个registered server只在**全部远端分页/枚举成功读取**、aggregate/bounds验证完成且既有include/exclude/canonical-invalid-tool policy被完整应用后发布`COMPLETE`。`OMIT_INVALID`只丢弃未通过MCP dialect/schema/bounds/local-validator canonical admission的schema；include/exclude过滤仍属于对完整raw listing的确定性normalization，所得集合是`COMPLETE`而不是partial。Native-wire lowerability不在discovery或source snapshot阶段判断，不触发`FAIL_SERVER | OMIT_INVALID`、不增加`invalid_tool_count`，合法但wire-incompatible的fact必须保留到adapter eligibility与planner。只有连接失败、分页未完成、frame/parse/aggregate/bounds失败或无法证明完整枚举时才发布`UNAVAILABLE + facts=()`；
- Skill owner对当前已注册root集合执行一次bounded ordered scan并全局解析precedence，只发布一个exact-scope `LOCAL_SKILL_CATALOG` snapshot；invalid/duplicate loser不注册leaf。完整scan/parse/aggregate成功时发布`COMPLETE + globally resolved winners`，任何会破坏全局completeness的失败发布单个`UNAVAILABLE + facts=()`，不得先按root拆分再重组；
- `COMPLETE + facts=()`表示合法空source，和`UNAVAILABLE`不同；
- registry factory必须从三个named owner carrier取得exact one Builtin snapshot、每个derived MCP registration的exact one snapshot，以及exact one aggregate Skill snapshot；不得接受unregistered snapshot。Owner-issued carrier证明现实inventory完整，generic factory只证明derived registration与snapshot exact覆盖；
- 每个fact的`identity.source`必须exact equal其snapshot registration的`source`；
- source-kind、leaf-kind与origin使用以下closed matrix，任何其他组合均拒绝：

  | snapshot source kind | legal leaf | required identity/origin |
  |---|---|---|
  | `BUILTIN_REGISTRY` | `FrozenToolCapabilityFact` | `identity.kind=TOOL`且`origin=BUILTIN` |
  | `MCP_SERVER` | `FrozenToolCapabilityFact` | `identity.kind=TOOL`且`origin=MCP` |
  | `LOCAL_SKILL_CATALOG` | `FrozenSkillCapabilityFact` | `identity.kind=SKILL`，且不存在execution origin/binding |

- visibility不由generic registry按名字或当前配置重算：Builtin adapter复用现有ROOT/child binding过滤；MCP adapter按owner fact中的`root_visible/subagent_visible`为exact scope筛选并与owner projection exact join；Skill owner同样先按exact scope冻结聚合catalog。Generic fact无需再复制visibility字段，因为scope-bound snapshot就是其唯一admission envelope；脱离该snapshot不得复用leaf。
- 一个registry内source identity、capability identity均unique；Tool provider name全局unique；Skill同名winner必须在进入generic factory前由closed root precedence确定；
- facts按`(kind, identity_fingerprint, fact_semantic_fingerprint)`确定性排序；同输入得到byte-identical snapshot/fingerprint；
- registry的Tool/Skill flattened view由`source_snapshots`纯派生，禁止再保存一份caller可独立传值的`builtin_tools/mcp_tools/skill_facts`。

Safe-point refresh不修改旧registry：owner冻结新的complete/unavailable source snapshot，central factory重建新的`FrozenCapabilityRegistrySnapshot`，planner再结合installed epoch决定append-only successor。Watcher/listChanged只负责唤醒；它们不是registry truth。

### 4.6 MCP tool reference 与 route

~~~python
class CapabilityRouteReasonCode(StrEnum):
    DIRECT_NATIVE_SURFACE = "DIRECT_NATIVE_SURFACE"
    NEW_NOT_IN_NATIVE_SURFACE = "NEW_NOT_IN_NATIVE_SURFACE"
    NEW_COLD_COHORT_META_FALLBACK = "NEW_COLD_COHORT_META_FALLBACK"
    NATIVE_WIRE_INCOMPATIBLE = "NATIVE_WIRE_INCOMPATIBLE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SCOPE_INVISIBLE = "SCOPE_INVISIBLE"
    DIRTY_FENCED = "DIRTY_FENCED"
    SCHEMA_REPLACED = "SCHEMA_REPLACED"
    REMOVED = "REMOVED"
    UNKNOWN_TARGET = "UNKNOWN_TARGET"
    AMBIGUOUS_LEGACY_TARGET = "AMBIGUOUS_LEGACY_TARGET"
    DESCRIPTOR_OVERBOUND = "DESCRIPTOR_OVERBOUND"


@dataclass(frozen=True, slots=True)
class McpToolCapabilityRef:
    server_id: str
    remote_tool_name: str

~~~

`ToolCapabilityVersionRef`是Tool当前semantic version的唯一小型引用，使用fact `fact_semantic_fingerprint + canonical_tool_spec.name`；其既有字段名`semantic_fingerprint`保存的就是该机械property值，不另建第二份Tool version input。`version_fingerprint`只覆盖上述三个独立字段，不覆盖scope、status、policy、registry/catalog fingerprint、executor、native-wire projection或physical generation。Skill已有catalog/activation identity，不为了形式统一复用Tool version DTO。

`McpToolCapabilityRef`只服务Round 9 MCP direct/meta route与`NewMcpToolRef`签发，不由Skill manifest构造。Round 9 adapter必须停止按startup available-tool allowlist删除或过滤整个Skill，但无需保存legacy `provides_tools/suggested_tools`：这些字段行为inert，并在Round 9.1 clean cut从parser/DTO删除。

### 4.7 Resolved route

~~~python
class ToolCapabilityRouteKind(StrEnum):
    DIRECT = "DIRECT"
    NEW_MCP_META_ONLY = "NEW_MCP_META_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FrozenMcpToolExposure:
    target: McpToolCapabilityRef
    version: ToolCapabilityVersionRef
    route: ToolCapabilityRouteKind
    public_reason_code: CapabilityRouteReasonCode
    route_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenMcpRouteProjection:
    routes: tuple[FrozenMcpToolExposure, ...]
    joined_catalog_semantic_fingerprint: str
    projection_fingerprint: str
~~~

两个MCP route DTO的closed coverage固定为：

- `FrozenMcpToolExposure.route_fingerprint`覆盖exact `server_id + remote_tool_name`、version fingerprint、route与closed reason；它是tool-specific route proof，不覆盖scope-wide registry/catalog/status，也不覆盖execution policy；
- `FrozenMcpRouteProjection.projection_fingerprint`覆盖ordered tool-specific route fingerprints与joined catalog semantic fingerprint；它服务catalog renderer/current lineage，可以因无关server状态变化而变化，但不进入native compatibility或`NewMcpToolRef`。

`DIRECT | NEW_MCP_META_ONLY`必须有exact target version。MCP exposure只能引用`TOOL/MCP` version。所有ordered tuple由central factory排序，caller顺序不进入语义。

`NATIVE_WIRE_INCOMPATIBLE`只表示canonical-valid MCP schema无法由当前adapter contract诚实投影到native `tools[]`，但完整inspect DTO仍可FULL交付；它不能表示canonical invalid，也不能与aggregate overbound的`NEW_COLD_COHORT_META_FALLBACK`混用。Canonical-valid但inspect DTO overbound使用`UNAVAILABLE + DESCRIPTOR_OVERBOUND`。

`NEW_MCP_META_ONLY` route只冻结exact target version与公开`server_id/remote_tool_name` locator。Planning、Skill catalog与MCP catalog均不得预先准备或持有`NewMcpToolRef`；只有模型实际调用`inspect_new_mcp_tool`后，Runtime才为该次inspect result准备process-local dormant ref，并在其Round 7.1 exact FULL成功安装后使其callable。

### 4.8 MCP semantic projection input

MCP supervisor增加一个只读、process-local的semantic projection：

~~~python
@dataclass(frozen=True, slots=True)
class FrozenMcpCapabilityProjectionInput:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshot_fingerprints: tuple[str, ...]
    catalog_semantic_fingerprint: str
    projection_fingerprint: str
~~~

它由MCP adapter从当前supervisor已经拥有的discovery/catalog facts构造，不复制MCP schema leaf，也不把`McpCatalogSnapshot`类型反向导入pure capability package。完整Tool facts只存在于`FrozenCapabilityRegistrySnapshot`对应的MCP source snapshots；这里保存其ordered fingerprint refs与catalog-specific identity。完整catalog仍由MCP source owner持有；provider renderer必须exact join这里的`catalog_semantic_fingerprint`。该input不包含client、transport、slot、lease、secrets、headers或private request state。

`source_snapshot_fingerprints`只引用current registry中当前scope可见的registered MCP sources，按`server_id`排序且unique。Central planning-cut factory必须证明这些refs exact resolve到registry中的MCP snapshots；`COMPLETE` snapshots内的Tool facts就是current READY_CLEAN leaf集合。Projection fingerprint覆盖scope、catalog semantic fingerprint及ordered source snapshot refs。Installed epoch中缺失的old direct tool由predecessor与current registry Tool view的差集推导为typed unavailable，而不是伪造current ready fact。

### 4.9 Skill projection input

当前skill-only `FrozenKernelCapabilityProjectionInput`重命名为：

~~~python
@dataclass(frozen=True, slots=True)
class FrozenSkillProjectionInput:
    discovery: LocalSkillDiscovery
    source_snapshot_fingerprint: str
    snapshot_fingerprint: str
~~~

Round 9 adapter保持当前visible body/catalog behavior，不借机实现Agent Skills parser。`source_snapshot_fingerprint`必须exact引用同一registry中唯一`LOCAL_SKILL_CATALOG` snapshot；Skill facts不在projection与registry各保存一份。`LocalSkillDiscovery`保留renderer/activation需要的source-specific parsed carrier，central factory必须证明其中winning manifests、root provenance与registry Skill view exact join。Round 9.1随后以portable Agent Skills manifest替换legacy parser、保留本文冻结的四种physical root policy并删除旧metadata，不改变Round 9 registry接口。

### 4.10 Epoch predecessor

~~~python
@dataclass(frozen=True, slots=True)
class EmptyCapabilityEpochPredecessor:
    expected_continuity_revision: Literal[0]


@dataclass(frozen=True, slots=True)
class InstalledCapabilityEpochPredecessor:
    expected_continuity_revision: int
    continuity_epoch_nonce: str
    tool_surface: FrozenModelToolSurface
    direct_projection_set: FrozenNativeToolProjectionSet


CapabilityEpochPredecessor = (
    EmptyCapabilityEpochPredecessor
    | InstalledCapabilityEpochPredecessor
)
~~~

`InstalledCapabilityEpochPredecessor.tool_surface`与`direct_projection_set`必须直接引用continuity owner已有epoch view中的同一frozen值或fingerprint-exact value，不允许caller重建第二份surface、version或projection tuple。`ToolCapabilityVersionRef`只保存identity fingerprint、semantic fingerprint与provider name；不保存executor binding。

Round 9不建立公开native transition enum，也不向`ProviderInputEpochCompatibility`增加第二个native compatibility字段。分支由已有真值机械派生：

~~~text
EMPTY predecessor
  -> cold install

installed predecessor + existing compatibility says same target/lowering
  -> exact reuse predecessor direct_projection_set and actual wire tool_items

installed predecessor + existing MODEL_TARGET_CHANGED | PROVIDER_LOWERING_CHANGED reset
  -> reproject exact predecessor canonical direct cohort under the new adapter contract
  -> install only through that existing cold-reset/CAS path
~~~

窄`native_function_tool_wire_contract_fingerprint`继续由adapter拥有，但它必须无条件进入既有`ProviderInputEpochCompatibility.provider_message_lowering_contract`：compatibility factory使用domain-separated hash覆盖既有message-lowering contract与该narrow function-tool wire contract。不得只把它放入final provider-wire profile，也不保留“二者任选其一”的实现自由。该narrow contract变化必须机械产生既有`PROVIDER_LOWERING_CHANGED` reset；若没有形成matching reset，属于internal contract conflict，不得靠Round 9新增状态字段补偿。

### 4.11 Parent dispatch cut 与两个consumer view

~~~python
@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityPlanningInput:
    predecessor: CapabilityEpochPredecessor
    native_wire: FrozenNativeToolWireEligibilitySet
    mcp: FrozenMcpCapabilityProjectionInput
    tool_view_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenCapabilityDispatchCut:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    registry: FrozenCapabilityRegistrySnapshot
    tools: FrozenToolCapabilityPlanningInput
    skills: FrozenSkillProjectionInput
    dispatch_cut_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityDispatchView:
    parent_dispatch_cut_fingerprint: str
    registry_fingerprint: str
    registry_tool_facts: tuple[FrozenToolCapabilityFact, ...]
    planning_input: FrozenToolCapabilityPlanningInput
    view_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenSkillCapabilityDispatchView:
    parent_dispatch_cut_fingerprint: str
    registry_fingerprint: str
    registry_skill_facts: tuple[FrozenSkillCapabilityFact, ...]
    projection_input: FrozenSkillProjectionInput
    view_fingerprint: str


def freeze_capability_dispatch_cut_and_views(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    registry: FrozenCapabilityRegistrySnapshot,
    tools: FrozenToolCapabilityPlanningInput,
    skills: FrozenSkillProjectionInput,
) -> tuple[
    FrozenCapabilityDispatchCut,
    FrozenToolCapabilityDispatchView,
    FrozenSkillCapabilityDispatchView,
]: ...
~~~

Parent cut是一次provider dispatch planning的immutable value，不是durable snapshot。它不查询数据库，不读取filesystem，不打开MCP连接。唯一central factory必须先冻结parent fingerprint，再从该parent机械派生两个narrow view；production caller不得分别提交fingerprint、registry与planning input：

~~~text
FrozenCapabilityDispatchCut
  ├─ FrozenToolCapabilityDispatchView
  │    -> KernelToolCapabilityPlanner
  └─ FrozenSkillCapabilityDispatchView
       -> KernelSkillProjectionComposer
~~~

`registry_tool_facts`与`registry_skill_facts`只引用parent registry已经冻结的对应closed-union leaf，不复制schema/body，也不能由caller另传tuple。Tool view fingerprint覆盖parent fingerprint、registry fingerprint、ordered Tool fact `fact_semantic_fingerprint`与`planning_input.tool_view_fingerprint`；Skill view fingerprint同样覆盖parent、registry、ordered Skill fact `fact_semantic_fingerprint`与`projection_input.snapshot_fingerprint`。Factory逐项证明两组facts恰好等于parent registry的derived Tool/Skill view。

Tool exposure result和Skill projection result都必须exact引用同一个parent fingerprint及各自view fingerprint，随后一起进入compiler/continuity candidate。最终组合validator同时取得parent与两个结果，拒绝wrong-parent、wrong-view或只替换一侧的混合。Tool planner不得读取Skill discovery/catalog lineage；Skill composer不得读取MCP route、native projection、executor或physical binding。

Central factory必须证明：

- exact scope一致；
- registry exact scope与planning scope一致；
- native-wire eligibility exact scope与dispatch scope一致，并对registry中的每个Tool fact恰有一项；若predecessor为installed，还对其retained direct version/spec pair恰有一项，且该扩展集合只能由central factory从predecessor机械派生；
- eligibility的native-function-tool contract exact等于本次已经resolved的model target/adapter tool-wire contract；
- identity unique；
- provider tool names不冲突；
- Built-in/MCP/Skill facts都只来自registry中三个owner-issued carrier的complete source snapshots；
- registry必须包含exact one immutable Built-in source snapshot；
- MCP projection refs是registry中MCP snapshots的closed子集；Skill projection exact引用唯一aggregate Skill snapshot；二者分别与owner-specific catalog/discovery carrier exact join；
- predecessor revision/nonce与continuity owner一致；
- 三项owner snapshot均在本次absolute dispatch deadline内从各自合法lock/safe-point冻结；不要求虚构跨owner原子瞬间；
- fingerprint覆盖全部独立输入。

### 4.12 Tool exposure plan

~~~python
@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityExposurePlan:
    dispatch_cut_fingerprint: str
    tool_dispatch_view_fingerprint: str
    direct_tool_surface: FrozenModelToolSurface
    direct_projection_set: FrozenNativeToolProjectionSet
    mcp_catalog_route_projection: FrozenMcpRouteProjection
    exposure_plan_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenSkillCapabilityProjectionResult:
    dispatch_cut_fingerprint: str
    skill_dispatch_view_fingerprint: str
    output: SkillProjectionOutput
    result_fingerprint: str
~~~

`direct_projection_set`是canonical Tool version到exact native wire projection的唯一selected mapping；plan不得再次保存独立versions/projections tuple。`exposure_plan_fingerprint`覆盖parent/view、direct canonical surface、projection set与MCP route projection，只证明本次Tool planning result完整，不成为新的continuity compatibility key。Skill result fingerprint覆盖parent/view与existing bounded `SkillProjectionOutput`的closed semantic encoding；internal free-text diagnostics若不进入provider source input则不得影响该fingerprint。

`ProviderInputEpochCompatibility.tool_surface_fingerprint`继续表达provider-neutral canonical `FrozenModelToolSurface`；model target与既有provider-lowering compatibility继续拥有reset分类。Final `FrozenProviderWireInputPlan`逐项比较`direct_projection_set.projections[*].wire_tool`与actual materialized `tool_items`并重算`wire_tools_fingerprint`。因此actual wire plan承担最终strict-prefix byte proof，projection set承担canonical-version→wire exact join；二者职责互补，但不需要第三个compatibility字段。

Late MCP或server status变化只改变`mcp_catalog_route_projection`并追加MCP source successor；Skill refresh只改变独立Skill composer output。两者都不能触发same-epoch rebase或重写provider `tools[]`，Tool plan也不再保存Skill lineage。

`FrozenMcpRouteProjection.routes`是MCP tool-specific route的唯一tuple；Exposure plan不得再保存第二份`mcp_routes`。Projection只额外保存所join的existing catalog semantic fingerprint；完整server instructions/resources/prompts/status仍由MCP catalog owner/renderer拥有。该plan是pure value，不持有authority。它的字段不包含：

- `PreparedToolExecutionBinding`；
- `McpSlotLease`；
- provider transport；
- permission snapshot；
- filesystem handle；
- continuity install permit；
- canonical repository connection。

Tool exposure plan不保存`skill_versions`、Skill snapshot或Skill lineage。`KernelSkillProjectionComposer`只接受`FrozenSkillCapabilityDispatchView`与既有closed activation subject，形成`SKILL_CATALOG`/`ACTIVE_SKILL`并返回上述result。这样same-call join由parent/view双重证明，而Tool planner不获得Skill renderer、CLI health或user-message interpretation authority。

### 4.13 KernelToolCapabilityPlanner

~~~python
class KernelToolCapabilityPlanner:
    def plan(
        self,
        *,
        view: FrozenToolCapabilityDispatchView,
    ) -> FrozenToolCapabilityExposurePlan: ...


class KernelSkillProjectionComposer:
    def compose(
        self,
        *,
        view: FrozenSkillCapabilityDispatchView,
        activation_subject: SkillProjectionResolveContext,
    ) -> FrozenSkillCapabilityProjectionResult: ...
~~~

Planner与composer必须pure、deterministic且bounded。相同view与activation subject必须产生byte-identical结果。二者不得接受分离的parent fingerprint、registry、leaf tuple或自由`dict`/callback/resolver object；所有capability输入均来自central factory签发的frozen view。Planner只能读取registry-derived Tool view，不能读取或fingerprint Skill facts；composer反之亦然。

Planner不执行OpenAI lowering：adapter-owned factory已经把每个Tool fact冻结为eligible projection或closed incompatibility。Planner只验证eligibility set的exact coverage/join并进行route与capacity决策；generic registry/planner package对OpenAI、Chat、Responses及具体provider name均为零import。

### 4.14 Physical exact join

Direct surface允许两种process-local access leaf：

~~~python
@dataclass(frozen=True, slots=True)
class PreparedUnavailableDirectMcpGate:
    capability_identity_fingerprint: str
    tool_semantic_fingerprint: str
    provider_tool_name: str
    unavailable_reason_code: str
    supervisor_authority_identity: object
    gate_fingerprint: str


DirectToolAccessLeaf = (
    PreparedToolExecutionBinding
    | PreparedUnavailableDirectMcpGate
)
~~~

Tool execution exact join对两种origin完全同构：

| Registered fact | required live access | authority owner |
|---|---|---|
| `TOOL/BUILTIN` | exact `PreparedToolExecutionBinding` | builtin registry/ports |
| `TOOL/MCP` | exact `PreparedToolExecutionBinding`，或仅对old installed direct使用typed unavailable gate | MCP supervisor/slot |
| `SKILL` | 必须不存在execution binding | none |

因此Built-in与MCP不仅都叫`TOOL`，还都必须通过同一“registered semantic fact -> exact live binding -> authorize/attempt/invoke”结构。不同之处只在binding的closed policy leaf与physical owner，不能让MCP走generic registry callback，也不能让Builtin绕过binding exact join。

Unavailable gate只允许已安装epoch中的old DIRECT MCP identity。它没有executor、slot或attempt authority；调用必须在authorize/attempt前返回typed unavailable。它仍exact绑定当前Host supervisor authority，same-shape foreign object不能通过。Builtin与尚未安装的cold direct candidate不得用unavailable gate伪造可执行surface。

Provider preflight前，existing tool-surface owner执行：

~~~text
plan.direct_tool_surface.surface_fingerprint
    == ProcessLocalToolSurfaceAccess.semantic_surface_fingerprint

every plan.direct_tool_version
    -> exactly one PreparedToolExecutionBinding or typed unavailable MCP gate

no extra binding
no foreign Host authority
~~~

这一join不进入plan fingerprint，也不允许planner签发physical borrow。Same-schema MCP reconnect可以替换physical slot/binding，但必须继续exact匹配同一个tool capability identity与semantic fingerprint。

---

## 5. Provider exposure channels

### 5.1 Native `tools[]`

只包含：

- fixed Built-in tool capabilities；
- cold epoch选中的direct MCP tool capabilities。

同一continuity epoch中逐项byte-identical。Skill永远不进入`tools[]`。

`FrozenModelToolSurface`保存ordered canonical Tool specs；adapter-derived `direct_projection_set.projections`保存本epoch实际发送的ordered native definitions。二者必须一一exact join但职责不同：前者服务canonical capability/local validation/execution binding，后者服务Chat/Responses actual-wire prefix。Native projection不得写回前者，final wire planner也不得绕过前者直接赋予执行权限。

### 5.2 `MCP_CATALOG`

继续复用Round 6的stateful runtime observation，但增加epoch-relative route：

~~~text
DIRECT
NEW_MCP_META_ONLY
UNAVAILABLE
~~~

Catalog变化只追加VALUE/CLEARED/UNAVAILABLE successor，不修改SYSTEM/tools/旧messages。Provider carrier必须使用Round 7五字段observation envelope，`trust=UNTRUSTED_OBSERVATION`；MCP server instructions、description、status与failure detail均不得进入SYSTEM authority。

唯一provider renderer冻结以下closed body，而不是自由拼接提示词：

~~~text
McpCatalogProviderBody
    total_server_count
    omitted_server_count
    direct_tool_count
    new_tool_count
    unavailable_tool_count
    servers: tuple[McpCatalogProviderServerRow, ...]
    new_tool_usage: fixed inspect_new_mcp_tool -> use_new_mcp_tool guidance

McpCatalogProviderServerRow
    server_id
    public_status
    public_status_detail | null
    direct_tool_names: tuple[qualified name, ...]
    new_tool_names: tuple[qualified name, ...]
    unavailable_tool_names: tuple[qualified name, ...]
    total_direct_tool_count / omitted_direct_tool_count
    total_new_tool_count / omitted_new_tool_count
    total_unavailable_tool_count / omitted_unavailable_tool_count
    resource_count / resource_template_count / prompt_count
~~~

Provider wire使用canonical JSON string field承载所有server/tool文字；不得依赖delimiter或让正文逸出carrier。小catalog必须完整列出每个admitted row的qualified tool name。达到现有MCP catalog bound时，只能按`server_id`与每类tool的确定性顺序保留**完整row**：

- 不得缩短、摘要或prefix-truncate tool identity；
- 每个保留server必须给出exact total/omitted counts；
- 若整个server row未进入body，顶层`omitted_server_count`必须准确；
- 固定提示使用`list_mcp_servers(server_id=..., cursor=...)`分页读取全集；
- 禁止BM25、dense embedding、top-k、recent-use ranking或无提示截断；
- `DIRECT`、`NEW_MCP_META_ONLY`、`UNAVAILABLE`必须显式区分；不能把native surface之外的tool描述成可直接调用。

`new_tool_usage`的固定语义必须前后呼应两个meta descriptor：只为当前native `tools[]`中不存在的NEW MCP tool先inspect、后use；Builtin或DIRECT MCP必须直接调用。Body不得包含catalog/route/policy fingerprint、slot generation、UUID、private header、requestState、raw exception或absolute private path。

### 5.3 `SKILL_CATALOG`

当前`ContextSourceKind.CAPABILITY_CATALOG`在clean-v0中重命名为`SKILL_CATALOG`：

- source contract：`pulsara.skill-catalog.v1`；
- collector contract：`pulsara.skill-catalog-collector.v1`；
- lifecycle保持`SNAPSHOT_ON_CHANGE`；
- budget class与FULL/COMPACT/REF_ONLY行为保持；
- body只表达Skill routing entry，不混入MCP server总目录或builtin descriptor。

Round 9.1因采用Agent Skills standard与新renderer，可再升级到`pulsara.skill-catalog.v2`。

### 5.4 `ACTIVE_SKILL`

本轮保持现有activation行为和source kind。它是Skill-specific context，不进入generic tool surface。Round 9.1负责标准化其body与activation intent；Skill声明不获得permission语义。

### 5.5 为什么不合并两个catalog

`MCP_CATALOG`与`SKILL_CATALOG`具有不同owner、变化频率、预算和invalidation语义。统一capability不等于把它们渲染进一个巨大消息：

- MCP status/dirty/reconnect由supervisor拥有；
- Skill file变化由filesystem snapshot拥有；
- 任一source失败不能让另一source失去可见性；
- compiler应能独立降级和append successor。

---

## 6. MCP Direct/Meta 混合语义

### 6.1 Fixed meta tools

以下descriptor是固定Built-in tool capabilities，即使没有MCP配置也存在：

~~~text
list_mcp_servers
inspect_new_mcp_tool
use_new_mcp_tool
~~~

它们必须在cold built-in surface中永久存在，避免启用第一个MCP时改变tool schema。

`list_mcp_servers`保留总目录语义，不改名为`list_new_mcp_servers`。`new`是tool相对当前epoch的exposure，不是server identity。

`list_mcp_servers`的provider schema固定为：

~~~text
list_mcp_servers(
    server_id?: string,
    cursor?: string,
    limit: integer = 50, minimum 1, maximum 200
)
~~~

返回closed union：

~~~text
McpServerDirectoryPage
    page_kind = SERVER_PAGE
    servers: tuple[McpServerDirectoryRow, ...]
    total_server_count
    next_cursor | null

McpServerToolPage
    page_kind = SERVER_TOOL_PAGE
    server: McpServerDirectoryRow
    tools: tuple[McpServerToolDirectoryRow, ...]
    total_tool_count
    direct_tool_count
    new_tool_count
    unavailable_tool_count
    next_cursor | null

McpServerDirectoryRow
    server_id
    public_status
    public_status_detail | null
    bounded_public_instructions | null
    tool_count / resource_count / resource_template_count / prompt_count

McpServerToolDirectoryRow
    server_id
    remote_tool_name
    provider_tool_name
    route = DIRECT | NEW_MCP_META_ONLY | UNAVAILABLE
    public_status
    public_reason_code
~~~

规则冻结为：

- 无`server_id`时按`server_id`排序返回server page；有`server_id`时只返回该exact scope-visible server的tool page；
- tool page按`(remote_tool_name, provider_tool_name)`排序，不返回完整input/output schema；
- 空参数旧调用继续合法；`cursor`与显式filter不匹配时typed stale，不偷偷从第一页重启；
- opaque cursor内部exact绑定current catalog semantic fingerprint、current epoch installed direct projection-set fingerprint、ROOT/child exact scope、server filter、page kind、limit与next offset；这些内部字段不得进入provider正文；
- catalog/surface/scope/filter变化使旧cursor返回`STALE_CURSOR`，unknown/invisible server返回`NOT_FOUND`，非法limit/cursor shape返回`INVALID_ARGUMENTS`；
- 它只读取一次已经安装的local catalog/route snapshot，不连接server、不refresh、不触发discovery、不取得MCP physical operation lane；
- `limit`是maximum row count，不是必须返回的数量；page factory复用Round 7.1唯一logical ToolResult renderer/quote，按ordered rows选择不超过limit且在最大合法call-local augmentation下必有FULL variant的最长prefix，并据此准备exact `next_cursor`；它不反向调用compiler，也不依赖未来actual citation碰巧更短；
- successful page标记Round 7.1 `FULL_REQUIRED/MCP_DIRECTORY_PAGE`。Response明确给出returned count、总数与`next_cursor`；不得靠HEAD_TAIL/COMPACT截断一个directory page后仍称其为成功分页。单个已bounded row连同actual logical envelope都无法FULL容纳时返回typed `MCP_DIRECTORY_ROW_OVERBOUND`且不推进cursor；单条page FULL合法但aggregate input不fit时由通用compiler返回resource boundary、provider open=0，Runtime不得把模型未见的`next_cursor`声称为已交付；
- resources/templates/prompts/status/instructions保留总目录诊断语义，但任何secret、raw exception或private URL必须继续服从Round 6 redaction。

### 6.2 Cold direct cohort selection

在`EMPTY` predecessor上：

1. 冻结fixed builtin surface；
2. 对每个execution-backed builtin exact join adapter eligibility；任一Builtin为`INCOMPATIBLE`、canonical aggregate超限或exact wire projection超限时，provider open=0并返回typed internal tool-contract/resource boundary；Builtin不得skip或走MCP meta；
3. 由MCP config既有`exposure_policy.include/exclude`、scope policy与**canonical** schema validity得到完整可暴露集合；被配置排除的tool既不direct也不meta，canonical invalid仍独占`FAIL_SERVER | OMIT_INVALID`；
4. 取得其中当前READY_CLEAN的完整MCP Tool fact cohort，并逐项exact join adapter eligibility；
5. canonical-valid、native-wire `INCOMPATIBLE`且完整inspect DTO可通过Round 7.1 FULL quote的tool直接归为`NEW_MCP_META_ONLY + NATIVE_WIRE_INCOMPATIBLE`；若inspect overbound则归为`UNAVAILABLE + DESCRIPTOR_OVERBOUND`；
6. 其余native-eligible MCP构成唯一direct candidate cohort，与builtins合并后按provider name排序；同时计算provider-neutral canonical 64-tool/1 MiB bounds与adapter exact lowered wire 64-tool/1 MiB bounds；二者是数值相同但独立计量的hard fences；
7. 若完整native-eligible cohort同时满足两组bounds，全部eligible MCP为`DIRECT`；
8. 若任一aggregate bound超出，全部native-eligible MCP退为`NEW_MCP_META_ONLY + NEW_COLD_COHORT_META_FALLBACK`，但每项仍须通过inspect FULL quote；无法inspect FULL的单项改为`UNAVAILABLE + DESCRIPTOR_OVERBOUND`；仅builtins进入native surface；
9. final direct surface、唯一`FrozenNativeToolProjectionSet`与两组quote必须由同一plan冻结。

禁止按发现时序、前N个、词法排名或embedding挑选部分native-eligible MCP。Native-wire incompatibility是逐tool closed分类，不属于任意挑选；aggregate fallback仍对全部native-eligible MCP all-or-none。用户若需要缩小集合，只能使用Round 6既有per-server exposure include/exclude；这会缩小整个可暴露集合，而不是把明确排除的工具偷偷保留在meta gateway。

该all-or-none fallback保证同一server早1毫秒READY或晚1毫秒READY不会分别造成Host失败与成功：过大的native-eligible MCP集合无论cold或late都经meta使用；wire-incompatible schema也不会毒化整个provider tool surface。Canonical aggregate与actual wire aggregate必须分别计量，任何一方不得被另一方的quote替代。

### 6.3 Installed epoch

在`INSTALLED` predecessor上：

- 无论本次adapter contract是否变化，都保留predecessor exact `FrozenModelToolSurface + direct_projection_set.tool_versions` canonical cohort；late/current MCP不得借epoch reset进入direct cohort；
- existing compatibility没有reset：必须直接复用predecessor `direct_projection_set`与actual wire `tool_items`；adapter revalidation只能证明byte-equal，不能产生新projection替换已安装值；
- existing compatibility给出`MODEL_TARGET_CHANGED | PROVIDER_LOWERING_CHANGED` reset：只对predecessor direct canonical version/spec cohort执行同一pure adapter projection，冻结一组完整new-contract `FrozenNativeToolProjectionSet`；不得沿用任一old-contract wire item；
- reset reprojection中任一predecessor direct Builtin或MCP无法诚实native projection、actual wire bounds不fit、retained version/spec无法exact配对，均在provider open前typed fail closed。不得drop旧direct tool、将其改为meta、缩窄schema或用current late MCP补位；operator若要采用不同cohort，必须创建replacement Host进行真正cold planning；
- successful reprojection必须与compiler已有`MODEL_TARGET_CHANGED | PROVIDER_LOWERING_CHANGED` epoch reset exact join：新epoch nonce、revision与完整wire plan经continuity CAS安装后才可provider open。该分支由existing reset reason机械派生，不新增公开transition enum、relation、event、receipt、checkpoint或recovery owner；
- 不重新选择direct cohort；
- current MCP fact与predecessor direct versions exact相同：`DIRECT`；
- current新增identity：若完整inspect DTO可FULL交付则`NEW_MCP_META_ONLY`，否则`UNAVAILABLE/DESCRIPTOR_OVERBOUND`；其native-wire eligibility只决定closed reason，不允许same-epoch提升为DIRECT；
- predecessor direct identity消失或连接不可用：provider descriptor仍为DIRECT，local execution state为typed unavailable；
- predecessor direct identity发生schema replacement：旧descriptor继续保留但禁止physical dispatch，新版本不能通过meta绕过；下个cold epoch才可采用新schema；
- same-schema reconnect只换physical binding，不改semantic route。

这里不把model target/API profile冻结为Host-lifetime常量。既有compiler已经把model target/provider lowering变化定义为合法epoch reset；Round 9只补齐reset前的native projection ownership与exact join。该reset开启新epoch，因此不违反“同Host、同scope、同epoch”的strict-prefix契约；但它不等价于一次新的capability cold discovery，故不能promotion late MCP。

### 6.4 Late-ready observation

当新的MCP tool在safe point进入`NEW_MCP_META_ONLY`：

- `MCP_CATALOG` successor列出bounded qualified tool names、server status和明确使用说明；
- observation必须写明先调用`inspect_new_mcp_tool`取得exact schema/ref，再调用`use_new_mcp_tool`；
- 不创建USER_MESSAGE canonical row；
- 不修改native tools；
- 不创建durable exposure receipt。

若名字列表超过catalog bound，body必须包含exact total/omitted count与`list_mcp_servers(server_id, cursor)`分页路径，不能把bounded overview冒充全集。

### 6.5 `inspect_new_mcp_tool`

Provider schema：

~~~text
inspect_new_mcp_tool(
    server_id: string,
    tool_name: string
)
~~~

描述必须明确：

> Inspect a tool announced through the new-MCP channel because it is not in the current native tools array. This tool cannot inspect built-in tools or MCP tools already exposed directly.

成功结果必须是以下closed canonical JSON DTO；不得返回自由文本前缀或省略关键字段：

~~~text
InspectedNewMcpToolProviderResult
    access_mode = NEW_MCP_META_ONLY
    server_id
    remote_tool_name
    provider_tool_name
    description
    input_schema
    output_schema | null
    effect_kind = READ_ONLY | EXTERNAL_EFFECT
    permission_notice = fixed "availability does not grant permission"
    invocation_notice = fixed "invoke only with use_new_mcp_tool"
    tool_ref
~~~

所有文字与schema均作为canonical JSON value编码。`input_schema`必须完整；`output_schema`只有remote descriptor实际携带时才出现为object，否则为`null`。不得截断JSON schema、把schema换成artifact handle、只返回摘要或要求模型猜参数。

完整DTO必须通过Round 7.1 shared logical ToolResult renderer/quote并满足共享`MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES`。计量覆盖实际进入Round 7 outer envelope的完整DTO、最大合法citation handle与fixed logical fields，而不只是schema正文；不能依赖未来actual envelope碰巧更小。无法形成FULL variant时，在准备ref和创建remote MCP attempt前返回typed `MCP_DESCRIPTOR_OVERBOUND`；inspect不得建立Skill式大正文特权、专用artifact escape hatch或第二个provider channel。

只有当前scope、当前continuity epoch、当前semantic descriptor为`NEW_MCP_META_ONLY`时成功。拒绝矩阵固定为：

| input/current state | result | physical/canonical effect |
|---|---|---|
| Builtin或DIRECT MCP | `INVALID_ARGUMENTS` + “call native tool directly” | 无ref、无MCP attempt |
| unknown、removed或scope-invisible | `NOT_FOUND` | 无ref、无MCP attempt |
| dirty/reconcile中 | `MCP_SNAPSHOT_STALE` | 无ref、无MCP attempt |
| descriptor/schema/result overbound | `MCP_DESCRIPTOR_OVERBOUND` | 无ref、无MCP attempt |
| current exact NEW | 完整DTO + dormant opaque ref | read-only builtin ToolResult，标记`FULL_REQUIRED/MCP_INSPECT_SCHEMA`；无remote MCP attempt |

Inspect本身仍是普通read-only Builtin call，因此保留一组outer canonical request/attempt/result；上表“无MCP attempt”是指不得进入目标remote MCP operation lane或创建nested attempt。Exact inspect result可以准备opaque token并写入canonical body，但对应process-local ref先保持dormant；只有下一次compile实际选择该exact result的FULL、且continuity CAS成功安装后才转为callable。Provider result构造失败释放prepared ref；aggregate budget不fit时保留canonical result与dormant ref、provider open=0，不得退成COMPACT后激活ref。

Inspect从current `FrozenMcpRouteProjection.routes` exact定位该tool的`FrozenMcpToolExposure`，并把其tool-specific `route_fingerprint`写入ref。Scope-wide catalog fingerprint只用于证明本次inspect读取的是current renderer/catalog cut，不进入token identity或后续stale判断；无关server变化不得撤销该tool ref。

### 6.6 `NewMcpToolRef`

内部prepared record：

~~~python
@dataclass(frozen=True, slots=True)
class NewMcpToolRef:
    opaque_token: str
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    continuity_epoch_nonce: str
    capability_identity_fingerprint: str
    tool_semantic_fingerprint: str
    mcp_execution_policy_fingerprint: str
    tool_route_fingerprint: str
    issued_by_host_authority: object
    ref_fingerprint: str
~~~

Provider只看到bounded opaque token，不看到Host authority、fingerprint或generation。

Ref只由成功的`inspect_new_mcp_tool`调用准备，并且只有该exact FULL ToolResult按Round 7.1成功安装后才视为已向模型签发。FULL安装前`use_new_mcp_tool`必须返回typed local rejection且不得创建remote attempt；caller cancellation只detach waiter，不能提前激活ref或把result降级。Ref是process-local：

- Host restart后自然失效；
- scope/epoch不匹配即stale；
- schema replacement失效；
- current tool-specific route不再是fingerprint-exact `NEW_MCP_META_ONLY`时失效；
- effect override、timeout、parallel-safety或classification source变化导致policy fingerprint变化时失效，模型必须重新inspect；
- same-schema且same-policy的纯物理reconnect可exact rebind当前compatible slot；
- 不持有slot lease，不阻止server正常reconnect/close；
- 不写数据库、artifact或event。

同一Host/scope/epoch/identity/tool-semantic/policy/tool-route fingerprint重复inspect应准备同一个token；其`DORMANT | CALLABLE`状态由exact FULL delivery settlement唯一推进，不进入token identity。无关server status、instructions或catalog count变化不能改变token。Schema、policy、该tool route或epoch变化不得复用。`ref_fingerprint`必须覆盖上述全部字段与Host authority identity（authority不进入provider token正文），明确禁止覆盖scope-wide catalog/registry/projection fingerprint。Ref在epoch close时整体释放，不做会让仍在canonical transcript中的recent ref随机失效的LRU eviction。

### 6.7 `use_new_mcp_tool`

Provider schema：

~~~text
use_new_mcp_tool(
    tool_ref: string,
    arguments: object
)
~~~

描述必须明确：

> Invoke only an MCP tool announced through the new-MCP channel because it is absent from the current native tools array. Obtain tool_ref and the exact schema from inspect_new_mcp_tool. Never use this tool for built-in tools or MCP tools already present in the native tools array; call those directly.

执行顺序冻结为：

~~~text
parse bounded outer arguments
-> resolve exact process-local ref
-> prove ref is CALLABLE because its exact inspect FULL was installed
-> recompute current exact tool-specific route and prove scope/epoch + NEW_MCP_META_ONLY + route fingerprint
-> prove descriptor semantic fingerprint unchanged
-> prove current MCP execution policy fingerprint unchanged
-> acquire generation-bound dispatch admission permit
-> evaluate MCP effect policy and current permission
-> optional existing human confirmation
-> accept exactly one existing ToolExecutionAttempt
-> enter MCP physical concurrency lane
-> invoke exact remote tool once
-> settle exactly one existing ToolResult
~~~

Invalid/stale/foreign/dormant/builtin/DIRECT/dirty ref必须在attempt前拒绝。`use_new_mcp_tool`不能接受裸tool name，不能自行搜索，也不能成为generic gateway。

Generation-bound dispatch admission permit只冻结“该semantic generation仍允许未来dispatch”，不占physical outbound lane，也不产生effect。Permission DENY、human confirmation拒绝/取消、waiter detach、server disable或Host close必须释放permit；ALLOW后才沿用现有decision+attempt原子事务并进入physical lane，避免dirty期间创建确定未执行却被误判unknown的attempt。

### 6.8 Canonical settlement

一次model-visible`use_new_mcp_tool`调用只产生一组canonical request/attempt/result：

- canonical assistant tool request仍保留模型实际调用的outer tool与arguments；
- attempt remote identity必须覆盖resolved server、remote tool、semantic fingerprint与arguments identity；
- 不为内部remote call再创建nested assistant call或第二个attempt；
- Round 7 `observation_origin_kind`必须冻结为`MCP_REMOTE`；
- permission/effect/timeout/concurrency/input-required复用Round 6；
- late exact outcome、unknown effect与artifact复用Round 1/5A/7；
- 不自动重跑effectful call。

### 6.9 Direct tool失效

已进入native surface的MCP descriptor在same epoch内不得删除。失联、dirty或schema replacement时：

- provider仍看到原descriptor；
- authorize/invoke exact gate返回typed unavailable/stale；
- ToolResult明确建议检查`list_mcp_servers`或等待same-schema reconnect；
- 禁止改走`use_new_mcp_tool`；
- same-schema reconnect后恢复exact binding；
- schema replacement等待future cold epoch。

确定尚未开始physical dispatch的known-unavailable ToolResult必须使用稳定、可本地化但语义等价的说明：

~~~text
This MCP tool was available when the current context began, but its server
connection is currently unavailable. The native tool definition remains
frozen to preserve context continuity. Do not route it through
use_new_mcp_tool. Check list_mcp_servers or wait for a same-schema reconnect.
~~~

若旧DIRECT identity发生schema/description/scope semantic replacement，current epoch仍只保留旧native descriptor并在local gate返回`SCHEMA_REPLACED_PENDING_COLD_ADOPTION`；current catalog可以报告pending cold adoption，但不得把同一个`server_id + remote_tool_name`的新schema同时签发为NEW meta ref。这样模型不会同时面对同一逻辑identity的两个不兼容调用契约。未来若需要零停机replacement，必须先建立显式version-addressed public identity，不能放宽本轮规则。

若physical attempt已经开始后才断线，必须沿Round 5A effect-aware known/unknown outcome结算；不能伪装成上述“确定未dispatch”的local denial，也不能自动重跑effectful operation。

### 6.10 Resources与prompts

MCP resources/templates/prompts继续使用Round 6 fixed tools，不变成generic capability leaf，也不通过`use_new_mcp_tool`访问。`list_mcp_servers`可以继续展示其counts/status。

---

## 7. Skill在统一体系中的关系

### 7.1 Skill是指导型leaf

Skill可以：

- 被catalog发现；
- 在满足trigger时形成active body；
- 指导模型如何组合现有工具。

Skill不能：

- 创建或删除tool schema；
- 获得physical executor；
- 把UNAVAILABLE route改成callable；
- 安装MCP server；
- 绕过permission/Plan/memory/MCP fence；
- 把文档命令当成已执行事实。

### 7.2 Round 9 adapter

本轮把当前`LocalSkillManifest/ResolvedSkillCatalogEntry/ActiveSkillInjection`适配到最小Skill capability fact，但不改变当前parser格式与root集合。Legacy private metadata只为读取旧文件而暂存于source-specific carrier，不进入generic fact、planner或provider route；Round 9.1会clean cut删除它。

Adapter必须：

- 保持当前catalog/active body可见语义；
- 不把legacy `provides_tools/suggested_tools`转换为dependency、route或tool identity；
- 不按startup available-tool allowlist过滤整个Skill或其provider-visibledescription/body；
- 不把filesystem path放入generic capability identity；
- 不读取supporting resources；
- 不实现新permission overlay。

Round 9.1随后一次性替换legacy manifest为Agent Skills standard，并保留Round 9 identity/fact/planner接口。

Round 9 adapter必须停止让startup available-tool allowlist改变Skill leaf。这里不接受Agent Skills新字段；Round 9.1 clean cut时删除legacy `provides_tools/suggested_tools`及其他private metadata语法。

### 7.3 与Tool/MCP route正交

Skill fact和`SKILL_CATALOG`不保存或渲染DIRECT/META_ONLY/UNAVAILABLE依赖状态。Skill正文提到的Builtin、MCP或CLI只是指导；模型根据独立的native tools、`MCP_CATALOG`与runtime observation选择真实调用路径，真实调用时再authorize。MCP route变化不得改变Skill catalog semantic fingerprint，Skill变化也不得改变MCP route。

### 7.4 Source rename

`CAPABILITY_CATALOG`只装Skill，继续保留该名称会让统一语义再次名实不符。本轮clean cut为`SKILL_CATALOG`。MCP仍使用独立`MCP_CATALOG`。

### 7.5 Active Skill

`ACTIVE_SKILL`不是ToolCapability，也不进入direct/meta cohort。它是Skill capability的一种context exposure。Textual/configured activation矩阵保持当前Round 3.1契约，Round 9.1再标准化其manifest与`read_file`路径。

---

## 8. Planning与dispatch顺序

### 8.1 Cold/EMPTY

~~~text
start one provider-dispatch absolute planning deadline
-> freeze exact scope and EMPTY continuity predecessor
-> obtain SealedBuiltinCapabilitySnapshot from the sealed base
-> obtain PreparedMcpCapabilitySourceSnapshotSet at an MCP safe point
-> obtain one PreparedLocalSkillCatalogSourceSnapshot from a complete global root scan
-> central factory validates owner authenticity/exact scope and freezes registry snapshot
-> freeze MCP and Skill owner-specific projections referencing that registry
-> resolve exact model target/profile without opening provider
-> adapter-owned pure native-wire preflight produces exact eligibility/projection set
-> construct one parent FrozenCapabilityDispatchCut
-> mechanically derive FrozenToolCapabilityDispatchView and FrozenSkillCapabilityDispatchView
-> KernelToolCapabilityPlanner selects direct/meta/unavailable and exact native projection set from Tool view
-> KernelSkillProjectionComposer independently renders the Skill view from Skill dispatch view
-> prepare exact physical tool-surface access for direct surface
-> collect remaining runtime sources from same dispatch planning attempt
-> compile semantic provider input
-> hydrate Round 5A.2 replay metadata/body against the same cut and target
-> build provider-wire plan / DirectModel preflight; exact-join planned native wire projections
-> continuity candidate exact joins canonical tool surface + parent/view-bound Tool/Skill outputs + replay hydration proof + actual wire plan
-> continuity CAS install
-> provider open_once
~~~

若physical exact join失败，candidate必须discard；不得安装一个没有可验证direct surface的epoch。

### 8.2 Installed epoch

~~~text
freeze exact installed continuity epoch view and one absolute planning deadline
-> obtain the exact-scope immutable Builtin/MCP/aggregate-Skill owner snapshots
-> reuse identical immutable Built-in source snapshot
-> freeze current complete/unavailable MCP and aggregate-Skill successor snapshots
-> build a new frozen registry snapshot; never mutate predecessor registry
-> exact join MCP/Skill owner-specific projections to that registry
-> resolve exact model target/profile and existing compatibility reset reason
-> adapter preflight classifies current facts and the central-derived retained direct version/spec pairs
-> construct one parent FrozenCapabilityDispatchCut and mechanically derive the two sibling views
-> if existing compatibility is unchanged: reuse predecessor canonical surface/direct_projection_set/tool_items byte-for-byte
-> if existing MODEL_TARGET_CHANGED | PROVIDER_LOWERING_CHANGED reset applies: reproject only the exact predecessor direct canonical cohort
-> prove exact reuse, or freeze a complete new-contract projection set without direct cohort change
-> Tool planner derives current NEW MCP routes/catalog successor from Tool view
-> Skill composer derives current Skill catalog successor from Skill view
-> compiler compatible append, or exact MODEL_TARGET_CHANGED/PROVIDER_LOWERING_CHANGED reset matching the derived branch
-> Round 5A.2 replay hydration + final wire plan exact join / continuity CAS / provider open
~~~

Compatible reuse若生成不同native surface/projection/tool items，属于internal contract conflict，provider open=0。Existing reset branch允许wire projection变化，但canonical surface/version cohort必须exact equal predecessor；任一late MCP promotion、old direct omission或compiler未形成matching reset都使provider open=0。分支不作为caller字段或第二套状态机持久存在。

### 8.3 Safe point

MCP catalog/Skill rescan与refreshable source-registration set采纳只能在既有provider safe point进行：

- 不在provider stream中途替换；
- 不在并行tool batch中途让后续call看到新policy；
- 已开始的MCP physical operation可drain；
- listChanged后新dispatch受dirty fence；
- source append发生在完整tool group之后；
- source owner不能在registry freeze后继续向该snapshot增量`register()` leaf；
- 新发现的leaf必须等待下一次完整source snapshot与registry successor。

### 8.4 Lock与borrow

- filesystem扫描、database I/O、network await不得持有Host lock；
- MCP semantic snapshot borrow只保护当前normalized facts，不把planner变成close owner；
- provider/tool-surface physical borrow仍由现有Host owner管理；
- planner input freeze与physical binding可以分层，但provider open前必须exact join；
- adapter native-wire eligibility/projection必须在同一dispatch absolute deadline内完成；不得在final wire阶段重新发现另一组tool或启动第二个planning deadline；
- Round 5A.2 metadata/body hydration继续使用同一dispatch deadline，并exact join已经冻结的native tools；capability planner不得读取或改写durable replay carrier；
- waiter cancellation不得遗留prepared continuity candidate或MCP permit。

---

## 9. Permission、effect与execution

### 9.1 Exposure不是授权

任一capability进入DIRECT、META或catalog都不改变permission preset。Planner不得读取当前permission来增删native tool schema，否则same-epoch permission变化会破坏prefix。

### 9.2 Built-in

Built-in调用继续使用现有builtin descriptor、action classifier、hard deny、Plan/memory run policy、human confirmation与binding。

### 9.3 Direct MCP

Direct MCP调用继续exact join当前advertised semantic descriptor与MCP execution policy。Same-schema reconnect只允许compatible physical rebind。

### 9.4 Meta MCP

Meta gateway在attempt前解析真实MCP capability，再使用该MCP tool的effect classification、timeout、parallel safety、scope与permission；不能按outer builtin `use_new_mcp_tool`的静态policy误判为普通builtin effect。

### 9.5 Skill

本轮及Round 9.1的Skill都没有authorization效果。Skill frontmatter不得改变permission、effect policy、Round 9 route或tool surface；Round 9.1只恢复portable standard filesystem、discovery、activation与progressive resources，不解释Skill-authored dependency metadata。

---

## 10. Lifecycle与failure matrix

| 场景 | Native tools | Append-only source | Execution结果 |
|---|---|---|---|
| Builtin installed binding缺catalog/mismatch/漏入snapshot | no provider open | none | internal contract conflict；catalog-only dormant entry本身合法 |
| Builtin canonical fact无法由current adapter contract诚实投影 | no provider open | none | typed internal native-tool contract conflict；不得skip/meta |
| Builtin composition seal后再次bind fixed/support port | installed surface不变 | none | typed local composition rejection；不增generation |
| 三项named owner snapshot任一缺失、foreign、错scope或owner内部inventory不完整 | no provider open | none | planning conflict |
| duplicate identical source registration | 按唯一项处理 | none | idempotent |
| same source在同一cut出现不同registration | no provider open | none | planning conflict |
| refreshable source完成相同snapshot | 不变 | no-op | current route保持 |
| MCP/aggregate Skill source无法证明complete snapshot | installed surface不变 | catalog UNAVAILABLE/invalidation | 不发布partial leaf truth |
| no MCP config | fixed builtins | empty/cleared MCP catalog | list返回空 |
| cold READY native-eligible MCP cohort双quote fits | builtins + all eligible MCP | eligible catalog标DIRECT；incompatible按meta/unavailable | direct invoke或inspect/use |
| MCP canonical schema invalid | 该tool不进入surface | 按`FAIL_SERVER | OMIT_INVALID` | 仅canonical invalid count增加 |
| MCP canonical-valid但native-wire incompatible，inspect FULL fits | builtins/其他eligible cohort不变 | catalog标NEW/meta + native-incompatible | inspect/use |
| MCP canonical-valid但native-wire incompatible，inspect overbound | 不变 | catalog标UNAVAILABLE/descriptor-overbound | 无ref、无remote attempt |
| cold native-eligible cohort aggregate overbound | builtins only | eligible MCP catalog标NEW/meta；inspect-overbound单项UNAVAILABLE | inspect/use或typed unavailable |
| optional MCP late-ready | 不变 | MCP_CATALOG VALUE | inspect/use |
| new remote tool added | 不变 | MCP_CATALOG successor | inspect/use |
| meta tool removed | 不变 | MCP_CATALOG successor |旧ref stale，无attempt |
| meta tool schema不变但execution policy变化 | 不变 | catalog/status successor按需 |旧ref stale，必须重新inspect |
| 无关MCP server catalog/status变化 | 不变 | MCP_CATALOG successor按需 | 未变tool的ref/token继续有效 |
| direct same-schema reconnect | 不变 | status可更新 | compatible rebind |
| direct connection unavailable | 不变 | status update | typed unavailable |
| direct schema replacement | 不变 | catalog说明pending cold adoption | old direct fenced；new schema不可meta绕过 |
| Skill新增/修改/删除 | 不变 | SKILL_CATALOG successor/invalidation | 无physical effect |
| Skill正文提到direct MCP | 不变 | Skill catalog no-op |模型依据native tool独立调用 |
| Skill正文提到late MCP | 不变 | Skill catalog no-op；MCP catalog追加NEW |模型依据MCP observation inspect/use |
| Skill正文提到unknown能力 | 不变 | Skill catalog no-op |真实调用typed unavailable；不创建tool |
| planner/source fingerprint mismatch | no provider open | none | discard candidate |
| planned native projection与final actual wire tool不一致 | no provider open | none | discard candidate；不回写canonical schema |
| existing target/provider-lowering compatibility要求reset，retained direct cohort可完整重投影 | predecessor canonical cohort、new-contract exact wire projections | current catalog successor按需 | compiler/CAS安装matching新epoch后provider open |
| contract变化但retained direct cohort无法完整重投影、超界或发生late promotion | no provider open | none | typed compatibility/resource conflict；不得drop/meta/替换旧direct，需replacement Host cold planning |
| 无existing matching reset却重投影，或compatible reuse时wire bytes变化 | no provider open | none | discard candidate；不得same-epoch替换wire |
| physical surface foreign/missing | no provider open | none | typed preflight conflict |
| invalid meta ref | 不变 | none | no attempt |
| meta effect outcome unknown | 不变 | ordinary ToolResult/turn interruption contract | never retry automatically |

### 10.1 Catalog overbudget

`MCP_CATALOG`与`SKILL_CATALOG`各自沿Round 3.1 stateful-source policy处理：

- 完整replacement能容纳：append VALUE；
- 完整replacement不能容纳且旧VALUE存在：append最小UNAVAILABLE invalidation；
- explicit empty：CLEARED；
- 连续CLEARED/UNAVAILABLE按presence/fingerprint no-op；
- 只有最小invalidation也无法容纳才形成typed resource boundary。

Advisory catalog不得让旧snapshot永久冒充current，也不得因为完整catalog太大永久阻塞普通对话。

---

## 11. Physical bounds

本轮复用现有bounds，不为Capability另建无限registry：

- provider native tool count：64；
- aggregate canonical tool schema：1 MiB；
- aggregate actual native wire tool definitions：1 MiB，并由provider-wire plan独立quote；canonical 1 MiB通过不代表wire quote通过，反之亦然；
- compiler working set：64 MiB；
- configured MCP servers：64；
- generic source registrations：exact 1个Builtin + 最多64个MCP server + exact 1个聚合`LOCAL_SKILL_CATALOG`；固定four-root policy及Round 9.1 parser替换只属于Skill owner内部bounded实现，不改变generic source数量；
- discovered tools per MCP server：512；
- MCP discovery page/item/body/schema与Host aggregate：继续服从Round 6；
- MCP catalog FULL/COMPACT/REF：32/8/2 KiB；
- single inspected schema必须在既有schema working-set bound内形成exact JSON，并且完整closed inspect DTO必须通过Round 7.1 provider-neutral logical FULL 40,000-byte quote；
- process-local issued new-MCP refs：每scope每epoch最多1,024个unique live token；不做LRU eviction，达到上限时inspect typed capacity failure；
- capability planner canonical fingerprint input：不得复制schema/body；引用既有frozen facts后的额外framing最多4 MiB；
- native-wire eligibility set只为每个Tool保存exact one frozen projection或小型closed incompatibility；其wire bytes计入provider-wire working set与Host planning budget，不得在registry再复制；
- registry snapshot只组合既有bounded source facts，不把MCP catalog或Skill manifest body复制为第二份generic payload；
- Skill discovery/body bounds保持现状，Round 9.1另行收紧。

所有count/byte检查在provider open或MCP physical attempt前完成。禁止静默截断schema、arguments、identity或ref。

---

## 12. 实施修改面

### 12.1 `capability/contracts.py`

新增纯DTO：

- `CapabilityKind`；
- `CapabilitySourceKind/Ref`；
- four-value `LocalSkillRootKind`；它是local Skill owner provenance contract，不产生四个generic sources；
- `CapabilitySourceRefreshMode`、`FrozenCapabilitySourceRegistration`与三个named branch的derived registration set；process-local owner-issued snapshot carrier不放入pure contracts模块；
- `CapabilityIdentity`；
- `FrozenToolCapabilityFact`；
- `FrozenToolCapabilityFact.canonical_tool_spec`明确保存未lower的provider-neutral canonical schema；
- `FrozenSkillCapabilityFact`；
- `CapabilitySourceSnapshotDisposition`、`FrozenCapabilitySourceSnapshot`与`FrozenCapabilityRegistrySnapshot`；
- `ToolCapabilityVersionRef`、`McpToolCapabilityRef`、`FrozenMcpToolExposure`、`FrozenMcpRouteProjection`；
- adapter-neutral `FrozenNativeToolWireProjection | FrozenNativeToolWireIncompatibility`、eligibility/projection set、父dispatch cut、两个central-derived narrow dispatch views、Tool planning input/plan与Skill projection result wrapper。

该模块只能依赖primitives与`model_input` frozen contracts；禁止import conversation repository、`conversation_kernel.mcp`（包括其pure-looking DTO）、MCP transport、Host、tool runtime或compaction。MCP adapter只能向内构造这里的neutral generic facts。

### 12.2 `capability/registry.py`

现有mutable tool-descriptor registry执行clean replacement：

- 不保留Host-lived自增generation或原地`register/unregister` authority；
- registration set只能由Host central seam从三个named owner snapshot中的registration机械派生；不提供接受raw `registrations` tuple的production API；
- 提供pure `freeze_capability_registry_snapshot(...)` central factory；
- internal pure helper只接受central seam提取的complete/unavailable frozen source snapshots；
- exact验证三个named branch完整性、每项derived registration恰有一个scope-bound snapshot、source-kind/leaf-kind/origin matrix、source/fact join、identity/provider-name uniqueness与deterministic ordering；
- flattened Tool/Skill view由source snapshots纯派生；
- Built-in、MCP与Skill均必须经过该factory，禁止planner接受旁路leaf tuple；
- 不读取builtin module、MCP supervisor、filesystem、Host或repository。

### 12.3 `capability/planner.py`

- 实现pure `KernelToolCapabilityPlanner`；
- 消费adapter签发的frozen eligibility set；禁止import/call OpenAI lowerer或接收callback；
- Builtin native eligibility fail-closed；MCP native-incompatible/inspectable进入meta、inspect-overbound进入unavailable；
- cold native-eligible MCP cohort aggregate all-or-none；
- canonical aggregate与actual native-wire aggregate独立quote；
- installed canonical surface/version cohort exact reuse；existing target/provider-lowering reset只允许重投影同一cohort；
- 输出唯一`FrozenNativeToolProjectionSet`与MCP route projection，不读取或fingerprint Skill view；
- deterministic ordering/fingerprint/bounds；
- 无I/O、无callback、无mutable dict。

### 12.4 Tool-only旧命名收窄

现有：

~~~text
CapabilityDescriptor
CapabilityProviderKind
CapabilityDescriptorSnapshotOutput
CapabilityExecutionSurfaceProvider
~~~

实际均为tool descriptor/builtin domain语义。迁移为诚实名称，例如：

~~~text
BuiltinToolDescriptor
BuiltinToolDomainKind
BuiltinToolDescriptorSnapshot
BuiltinToolDescriptorProvider
~~~

若某类型确实被MCP与builtin共同消费，则改为`ToolCapability...`，不能继续用看似包含Skill/Plugin的开放名称。

### 12.5 删除旧planner authority

审计并删除/替换：

- `CapabilityRegistry` generation及long-lived mutable registration作为另一个exposure owner；
- 旧`build_exposure_plan()`；
- `CapabilityExposurePlan.to_event_value()`；
- dormant `CapabilityExecutionSurfaceIdentityFact`及descriptor-artifact identity（若M0确认只有legacy Skill/tests消费）；
- dormant descriptor artifact/event projection；
- permission gate对旧plan的optional分支。

真实permission必须改为消费当前exact tool surface/binding与builtin/MCP policy，不能为了删除旧plan而失去hard deny或call classifier。

### 12.6 Skill-only命名收窄

~~~text
CapabilityProjectionOutput          -> SkillProjectionOutput
CapabilityProjectionResolveContext  -> SkillProjectionResolveContext
KernelCapabilityComposer            -> KernelSkillProjectionComposer
FrozenKernelCapabilityProjectionInput -> FrozenSkillProjectionInput
RenderedCapabilityPrompt            -> RenderedSkillPrompt
~~~

`CapabilityDiagnostic`若仍只由Skill discovery产生，也重命名为`SkillDiagnostic`。真正跨source的public diagnostics继续使用model-input closed code。

### 12.7 MCP contracts/supervisor

- 将每个resolved MCP server config适配为`SAFE_POINT_REFRESHABLE` source registration；
- discovery candidate按server发布complete/unavailable source snapshot；只有完整wire enumeration经过既有include/exclude与canonical `FAIL_SERVER | OMIT_INVALID` normalization后才能发布COMPLETE，未完成分页/parse/aggregate/bounds验证不得进入registry；
- 删除MCP supervisor/discovery对`llm.adapters.openai.function_tools`的import与`lower_openai_function_parameters()`调用；native-wire incompatibility不得再触发invalid policy或删除canonical fact；
- 暴露`FrozenMcpCapabilityProjectionInput`；
- 复用`McpToolSemanticFact`生成generic tool fact；
- projection只引用registry-owned MCP source snapshot fingerprints，不再持有第二份Tool facts；
- catalog entry增加epoch-relative route；
- current catalog分页返回完整DIRECT/NEW分类；
- same-schema reconnect与schema replacement按§6处理；
- supervisor继续唯一close owner。

### 12.8 Builtin catalog

- 以一个`IMMUTABLE` Built-in source registration和零I/O source snapshot接入统一registry；
- compiled catalog保留descriptor/policy catalog职责，不再被解释为executable leaf全集；
- Host tool-surface owner通过唯一`SealedBuiltinCapabilitySnapshot`在同一surface lock下冻结exact-scope installed builtin binding inventory、derived registration与generic source snapshot；adapter逐项join catalog并从binding侧穷尽构造facts，catalog-only dormant entries不进入snapshot；
- `DirectKernelToolPort`增加`PREPARING | SEALED | CLOSED` composition state与`seal_builtin_composition()`；seal前完成所有fixed/support port binding，seal后surface-changing bind typed拒绝，scope snapshot只投影sealed base tuple；
- 永久加入`inspect_new_mcp_tool`与`use_new_mcp_tool`；
- `list_mcp_servers`保留；
- 新fixed descriptor进入其closed scope允许矩阵并必须同时存在真实local executor binding；
- availability即使无MCP配置也保持descriptor存在；实际调用返回empty/unavailable；
- build generic tool facts的central adapter。
- 审计每个production builtin canonical schema：存在精确portable等价时直接使用该canonical shape；需要受控wire superset时保留exact canonical schema与local validator；任何adapter incompatibility在composition/preflight fail closed，不新增skip/meta分支。

### 12.9 Tool runtime

- 增加inspect/use fixed binding；
- `list_mcp_servers`使用closed page factory，从current installed local snapshot选择Round 7.1 logical FULL可容纳的最长ordered page，并将successful result标记`FULL_REQUIRED/MCP_DIRECTORY_PAGE`；不得把normal HEAD_TAIL/COMPACT当作成功分页；
- inspect从current route projection定位tool-specific route并准备process-local、policy/route-bound dormant ref；successful result标记`FULL_REQUIRED/MCP_INSPECT_SCHEMA`，只有exact FULL continuity install后ref才callable；scope-wide catalog fingerprint不进入ref；
- use在attempt前解析到MCP policy/binding；
- one attempt/result；
- origin冻结MCP_REMOTE；
- 复用Round 6 admission permit、dirty fence、concurrency与input-required；
- direct unavailable gate不删除descriptor。

### 12.10 Context source/compiler

- `CAPABILITY_CATALOG` clean rename为`SKILL_CATALOG`；
- 更新source policy、collector、renderer、BASE_SYSTEM source说明与golden；
- MCP renderer加入DIRECT/NEW指导；
- 本轮代码部署时global lowering、BASE_SYSTEM、tool-surface与source contract/domain version执行一次cold bump；activation只支持clean-v0/新Host，不在旧进程中热替换Python contract。部署后的同一Host若resolved model/API profile令narrow tool-wire contract变化，则复用compiler既有reset机制并严格走§6.3 reproject branch；两者不得混为migration/compat逻辑；
- 禁止internal identity/fingerprint进入provider body。
- `FrozenModelToolSurface`与compiler继续持有canonical specs；不得用lowered wire projection替换tool-surface semantic fingerprint或local validation input。

### 12.11 Continuity/runner/Host

- Host composition不直接手写registration tuple；
- Host分别从tool-surface owner、resolved MCP config/supervisor与Skill composition owner取得`SealedBuiltinCapabilitySnapshot`、`PreparedMcpCapabilitySourceSnapshotSet`与`PreparedLocalSkillCatalogSourceSnapshot`；不创建共同attempt token；
- 新增窄Host-facing `capability_composition.py` seam（或同等现有Host composition模块），只验证三个private-constructor carrier的owner authenticity、exact scope与各自inventory completeness，并派生registration set/registry；不做跨owner current-seal二次握手；
- 只有该seam能把三项named snapshot交给registry internal pure helper；其他production调用点直接使用raw frozen snapshot或caller tuple必须由architecture gate拒绝；
- Host必须在interaction/subagent/memory/MCP support与fixed meta binding完成后、任何capability/source/tool snapshot前seal Builtin composition；动态MCP safe-point refresh不属于Builtin unseal；
- capability registry snapshot与父`FrozenCapabilityDispatchCut`加入provider dispatch planning；
- exact model target/profile在native-wire preflight前冻结；adapter以唯一pure factory把registry Tool facts投影为eligibility set，generic registry/planner不import adapter；
- Built-in退化discovery、MCP discovery与聚合Skill scan在同一absolute planning deadline内顺序freeze，再由central factory合并；不要求共同capture瞬间；
- parent dispatch cut通过唯一pure factory机械派生`FrozenToolCapabilityDispatchView`与`FrozenSkillCapabilityDispatchView`；planner/composer只接受对应view，二者输出都exact引用parent及view fingerprint，Tool plan不保存Skill lineage；
- cold continuity candidate exact join parent cut、两项view-bound Tool/Skill outputs、canonical tool surface、Round 5A.2 replay hydration proof与actual wire plan；
- 不向`ProviderInputEpochCompatibility`新增native-wire字段；compatibility factory必须把base message-lowering contract与narrow function-tool contract组合进唯一`provider_message_lowering_contract`，从而让任一narrow contract变化机械产生既有`PROVIDER_LOWERING_CHANGED`；final wire profile不能单独承担reset authority；
- `FrozenProviderWireInputPlan`逐项exact join唯一`FrozenNativeToolProjectionSet`中的wire bytes、opaque native-function-tool contract与Round 5A.2 selected hydration；final planner不得独立重lower成不同shape；
- installed epoch的reuse/reproject分支由predecessor kind、existing compatibility reset reason与installed direct cohort机械派生；registry/MCP route/Skill snapshot变化只能驱动append-only source successor，不能触发reproject或late promotion；
- reproject branch必须exact joincompiler `MODEL_TARGET_CHANGED | PROVIDER_LOWERING_CHANGED` reset reason、新epoch nonce、predecessor canonical direct cohort与new-contract projection set；失败时provider open=0；
- safe-point current catalog变化只成为append-only source；
- cancellation/discard释放ref preparation/physical borrow；
- ROOT/child各有独立scope exposure。

### 12.12 Provider adapters、DirectModel与Round 5A.2

- `llm/adapters/openai/function_tools.py`继续是Chat/Responses共享wire projection的唯一实现与contract-version owner；显式`strict:false`保持；
- adapter暴露窄pure preflight factory，输入canonical frozen Tool specs与exact resolved profile，输出frozen eligibility/projection set；不接受MCP supervisor、registry owner、executor或transport；
- factory允许central planning seam把predecessor direct version/spec pair作为retained projection input，用于contract-change reset；该输入只能从installed predecessor机械派生，不能成为caller新增tool入口；
- same-contract exact reuse与contract-change full reprojection共享同一factory/goldens；任何部分复用、部分新投影、旧direct omission或late MCP promotion均拒绝；
- 当前closed transformation之外的canonical schema返回typed incompatibility，不扩展成任意JSON Schema翻译器；
- adapter新增窄`native_function_tool_wire_contract_fingerprint`作为`OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION`、wire API与tool-relevant request-shape的唯一hash owner；现有`_provider_wire_profile_fingerprint`可继续组合该值与assistant replay contract服务完整wire plan，但generic capability代码只能接收前者，不能因assistant replay变化伪造tool-surface变化；
- DirectModel final wire materialization复用或重算同一pure projection并byte-exact确认；Chat/Responses wrapper差异由adapter拥有，不进入capability semantic fingerprint；
- Round 5A.2 selected replay metadata/body hydration、placement join与final wire prefix证明保持原顺序；native tools在hydration前冻结，historical assistant carrier不得因route/catalog变化重写；
- capability planning、native projection、replay hydration与final wire plan共用同一个absolute dispatch planning deadline。

### 12.13 Inspector/CLI

bounded inspect可以展示：

- capability kind/source；
- public name；
- DIRECT/META/UNAVAILABLE route；
- closed reason code；
- Skill source/location与catalog/activation version；
- MCP server status。

不得展示secret、headers、slot object、raw exception、absolute private path或process-local ref token。

---

## 13. 测试规格

### 13.1 Contract tests

- closed source-kind/refresh matrix：Builtin只能IMMUTABLE，MCP/Skill只能SAFE_POINT_REFRESHABLE；
- source-kind/leaf-kind/origin closed matrix：Builtin snapshot只能接纳`TOOL/BUILTIN`，MCP只能`TOOL/MCP`，aggregate Skill catalog只能`SKILL`；
- exact duplicate source registration idempotent，同source不同registration fingerprint conflict；
- Host composition seam缺少三项named owner snapshot中的任一项、carrier foreign、scope不一致、source kind错位或owner内部inventory不完整均拒绝；调用方重算的普通frozen tuple不能冒充private-constructor carrier；
- freeze后MCP/Skill owner发生变化不使本次immutable cut retroactively stale；变化只能进入下一safe-point snapshot。Internal pure registry helper继续拒绝derived registration遗漏、同source两个snapshot或unregistered snapshot，只有Host composition seam可作为production caller；
- ROOT source snapshot不能复用于child registry；MCP scope visibility与current owner fact exact join；
- Built-in zero-I/O snapshot与scope-visible installed binding inventory byte-identical；catalog-only dormant descriptor不进入snapshot；binding缺catalog、descriptor mismatch或duplicate binding均拒绝；
- Builtin composition在PREPARING完成全部bind后seal；seal前snapshot拒绝，seal后late subagent/memory/MCP-support/fixed-tool bind拒绝，重复seal幂等，ROOT/child均投影同一sealed base；dynamic MCP refresh仍可运行且不改seal；
- complete empty与unavailable source snapshot严格区分；
- current Round 9 four-root policy完整扫描当前启用的两个workspace bindings与可选两个user bindings，且只产生一个`LOCAL_SKILL_CATALOG` snapshot；不扫描`.claude/skills`、不增加第五个root；全局precedence、winner provenance、complete-empty与whole-catalog UNAVAILABLE均有golden；
- unavailable snapshot禁止携带facts；MCP分页/聚合未完成不得发布，但完整listing经include/exclude或`OMIT_INVALID`确定性过滤后仍发布COMPLETE并保留exact invalid count；
- canonical-invalid MCP触发既有`FAIL_SERVER | OMIT_INVALID` policy；公开YAML中的`OMIT_INVALID`保持可解析且不存在`SKIP_TOOL` alias；同一canonical-valid schema的native-wire incompatibility仍保留在COMPLETE snapshot、invalid count不变；
- source snapshot中的每个fact exact join同一source registration；
- Tool与Skill leaf都暴露统一`fact_semantic_fingerprint`；Tool property等于既有semantic fingerprint，Skill closed value精确覆盖identity/catalog/activation/provenance，任一漏项或伪造值拒绝；
- Skill仅body变化、仅catalog字段变化或same-name winner移到另一root时，`fact_semantic_fingerprint`与aggregate source snapshot均变化；provider catalog/active fingerprints仍只按各自可见字段变化；
- pure registry snapshot跨三类source确定性排序，flattened view无第二份caller输入；
- Tool/Skill closed union穷尽；
- Plugin不是leaf kind；
- no public `Capability(ABC)`；
- identity与provider-mangled name分离；
- same MCP remote identity在same-schema reconnect后semantic fingerprint不变；
- `FrozenToolCapabilityFact`只保存canonical schema；同一canonical fact在不同adapter contract下semantic fingerprint不变；
- eligibility set对每个Tool fact exact one、Skill exact zero，wrong scope/profile/version/canonical-spec fingerprint均拒绝；
- generic registry/planner AST guard禁止import OpenAI adapter、`OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION`或MCP physical package；
- native projection不得比canonical schema更窄；无法证明受控superset时返回closed incompatibility而非静默改写；
- `wire_utf8_bytes`只能由`len(canonical_json_bytes(wire_tool))`派生；不存在可伪造constructor字段，multibyte/escaping near-bound quote与实际wire bytes相等；
- meta ref exact绑定MCP execution policy fingerprint；policy变化使旧ref stale，same-schema/same-policy物理reconnect仍可rebind；
- unrelated MCP server status/instructions/count变化只改变scope-wide catalog projection，不改变未变tool的route fingerprint或重复inspect token；
- legacy Skill private tool/binary/service fields不进入generic fact、planner或MCP identity；
- mutabledict不能进入facts；
- duplicate identity/provider name拒绝；
- fingerprints fixed-point。

### 13.2 Planner tests

- cold builtin-only；
- execution-backed Builtin wire-compatible成功；任一Builtin incompatible时provider open=0且不skip/meta；
- planner/composer只能消费central factory从parent cut机械派生的对应narrow view，拒绝直接消费registry、分离planning input或旁路builtin/MCP/Skill leaf tuples；
- parent cut只能在registry、exact model target/profile及adapter native-wire preflight全部冻结后构造；placeholder eligibility、post-freeze mutation或第二个replacement cut均拒绝；
- 相同source snapshots无论producer构造顺序如何都得到同一registry/plan fingerprint；
- safe-point refresh构造successor registry但不修改旧snapshot；
- cold native-eligible MCP cohort且canonical/actual-wire双quote fit -> all eligible direct；
- canonical-valid/native-wire incompatible且inspect FULL fits -> meta + `NATIVE_WIRE_INCOMPATIBLE`；
- canonical-valid/native-wire incompatible且inspect overbound -> unavailable + `DESCRIPTOR_OVERBOUND`；
- canonical-invalid与native-incompatible不共享policy、count或reason；
- native-eligible count overbound -> all eligible MCP meta；
- canonical byte overbound或actual lowered wire byte overbound -> all eligible MCP meta；两种quote分别有near-bound golden；
- existing exposure include/exclude缩小完整visible cohort后fit，excluded tool不进入meta；
- installed epoch无论current catalog变化都复用exact canonical direct cohort；same contract复用exact wire projection；
- narrow contract变化且retained cohort可投影时，既有provider-lowering compatibility必须形成matching reset，compiler/CAS安装新epoch且不promotion late MCP；
- `provider_message_lowering_contract` golden必须由base message-lowering contract与narrow function-tool contract共同确定；只改变后者必定形成`PROVIDER_LOWERING_CHANGED`。与model target同时变化时接受`MODEL_TARGET_CHANGED` precedence，其他reset reason不能授权wire重投影；
- reproject中任一old direct无法投影、actual-wire超界、遗漏/替换old direct、混入late MCP或compiler reset reason不匹配均provider open=0；
- registry/MCP route/Skill catalog变化只改变各自sibling output，不触发cold reset；Tool exposure plan不得保存Skill lineage；
- base lowering contract、narrow function-tool contract或任一actual wire projection变化不改变capability semantic fingerprint；前两者的合法变化只能经composed `provider_message_lowering_contract`、existing compatibility reset与新epoch安装，final wire profile的其他字段变化不能授权Tool重投影；
- `ToolCapabilityVersionRef/FrozenMcpToolExposure/FrozenMcpRouteProjection`字段与fingerprint golden固定，禁止status/policy/registry进入Tool version；
- Tool planner与Skill composer分别只接受central-derived narrow view；分离传入parent fingerprint/registry/planning input的旧API不存在。输出exact引用同一parent dispatch-cut fingerprint及各自view fingerprint，wrong-parent/wrong-view/foreign registry facts混合在compiler/CAS前拒绝；
- deterministic order与plan fingerprint；
- pure planner禁止I/O/import physical packages。

### 13.3 MCP lifecycle

- optional server在initial freeze前READY与freeze后READY都可用；overbound时均走meta，不出现时序性Host failure；
- MCP discovery不import/callOpenAI lowerer；合法但native-incompatible schema不会被`OMIT_INVALID`删除或毒化其他tool dispatch；
- late new tool追加MCP_CATALOG且tools byte-equal；
- 小catalog完整列出所有qualified names；overbound只截完整row并给出逐server exact total/omitted counts与分页指引，不使用ranking或缩短identity；
- `MCP_CATALOG` provider carrier是canonical JSON、trust为UNTRUSTED_OBSERVATION，正文含DIRECT/NEW/UNAVAILABLE与固定inspect→use guidance，且不含内部fingerprint/generation/secret；
- direct disconnect不删schema，调用typed unavailable；
- direct same-schema reconnect恢复；
- direct schema replacement只报告pending cold adoption，旧native call typed stale，新schema不能meta绕过；
- removed meta ref stale且无attempt；
- dirty/listChanged fence在attempt前生效。

### 13.4 Meta tool

- fixed descriptors在no-config场景仍存在；
- `list_mcp_servers`的server/tool两种page、1/50/200 limit边界、deterministic order、exact counts与opaque cursor golden；cursor exact绑定catalog/native-surface/scope/filter/page/limit/offset，任一漂移typed stale且不连接/refresh MCP；requested limit只作maximum，page factory返回能以Round 7.1 FULL完整容纳的最长ordered prefix，next cursor无遗漏/重复，单row overbound typed拒绝；
- directory page为`FULL_REQUIRED/MCP_DIRECTORY_PAGE`；parallel sibling可降级但page不可降级，aggregate不fit时provider open=0且next cursor不算已交付；
- bounded catalog overview遗漏的每个tool均可由server tool page无重复、无遗漏遍历；resources/templates/prompts/status/instructions继续可见且secret-safe；
- inspect仅接受current exact NEW tool；
- inspect拒绝builtin/DIRECT/unknown/dirty；
- inspect success使用closed canonical JSON DTO，完整input/output schema、effect kind、fixed permission/invocation notice与tool_ref逐字段golden；
- inspect最终provider-neutral ToolResult logical envelope达到39,999/40,000 bytes时存在FULL，40,001 bytes typed descriptor-overbound且不准备ref、不进入remote lane；schema不得截断或artifact化；
- inspect success为`FULL_REQUIRED/MCP_INSPECT_SCHEMA`；aggregate压力下不得降级，FULL continuity安装前ref保持dormant，cancel/CAS conflict不激活ref且不创建remote attempt；
- ref scope/epoch/Host/schema/policy exact join；
- ref exact绑定tool-specific NEW route而不绑定scope-wide catalog；无关server变化后重复inspect返回同token；该tool转DIRECT/UNAVAILABLE/removed后旧ref无attempt；
- policy override/timeout/parallel-safety变化后旧ref stale且必须重新inspect；
- same-schema且same-policy reconnect允许compatible rebind；
- use valid ref只执行一次remote effect；
- canonical attempt/result只有一组；
- observation origin=MCP；
- effect confirmation使用remote MCP policy；
- invalid ref、arguments或permission denial无attempt；
- ACK unknown复用existing tool-result settlement；
- state-only input_required保持Round 6；
- unknown effect绝不重跑。

### 13.5 Skill relationship

- current fixed root policy由Skill owner一次性冻结并完成global scan，再以单一aggregate source snapshot进入registry；
- current Skill适配成SKILL fact；
- legacy private metadata不产生Builtin/MCP/CLI requirement或route；
- Skill catalog只来自name/description/location，active来自exact body；
- MCP direct/meta/status变化不改变未修改Skill的catalog semantic fingerprint；
- Skill不能创建tool spec/binding；
- Skill catalog/active不随permission preset变化；
- Skill body不进入generic planner tool surface。

### 13.6 Source rename与prefix

- clean-v0只收集`SKILL_CATALOG`，不存在`CAPABILITY_CATALOG`；
- source ordering仍是base/runtime/catalog/active的既有placement；
- catalog变化只追加successor；
- direct disconnect只追加status；
- Chat Completions与Responses均证明SYSTEM/tools相等、messages只追加suffix；
- provider actual-wire plan/continuity proof同时包含固定canonical tool surface、ordered actual native wire projections与opaque adapter contract；
- Chat与Responses分别证明planned projection与最终wire tools byte-exact；same-contract CAS/projection mismatch时provider open=0；
- Chat↔Responses或narrow function-tool contract变化必须先改变composed `provider_message_lowering_contract`，再通过matching compiler reset重投影同一predecessor canonical direct cohort并安装新epoch；late MCP仍为meta/unavailable。无法完整重投影时provider open=0；
- direct MCP tool call/result提交后fresh Host读取Round 5A.2 durable replay carrier，metadata/body hydration、assistant placement、native tool surface与final wire plan exact join后继续；
- meta `inspect/use`作为fixed builtin native call提交后fresh Host同样exact replay，不合成placeholder reasoning或改写historical tool call；
- foreign physical binding在provider open前拒绝。

### 13.7 Scope

- ROOT与child只看到各自允许的MCP facts；
- child不继承ROOT-only direct MCP；
- ref不能跨scope；
- aggregate Skill catalog snapshot按exact scope注册/投影；
- 一个scope新增MCP/Skill不改变另一scope native surface或catalog head。

### 13.8 Bounds与security

- 64 tool/1 MiB schema bound；
- actual native wire tool aggregate独立64-tool/1 MiB bound；canonical与wire quote任一超限都在provider open前处理；
- large remote name仍使用bounded provider name与full canonical identity；
- schema不能静默截断；
- ref live cap；
- catalog omitted count与pagination完整；
- unknown JSON/result type继续fail closed；
- server annotation不能提升permission；
- provider body无contract version/fingerprint/generation/path/secret。

### 13.9 Retained tests

- Round 3/3.1 compiler/continuity；
- Round 5A/5A.1 ownership与wire proof；
- Round 5A.2 transaction、metadata/body hydration、fresh-process Chat/Responses native replay、bounds/deadline/corruption/privacy与actual-wire CAS proof；
- Round 6 MCP全部retained；
- Round 7 ToolResult observation；
- Round 8 memory/permission；
- full PostgreSQL suite；
- Protocol v3 Python contract/generator与architecture gates；
- clean-v0 fresh/repeat/deep verify。

### 13.10 Real provider dogfood

至少覆盖：

1. 一个cold direct stdio MCP tool，模型直接调用一次；
2. 一个late-ready或forced-meta MCP tool，模型读取observation后依次inspect/use；
3. provider actual tools数组在late-ready前后byte-identical；
4. meta invocation canonical attempt/result count精确为1；
5. 断连direct tool得到typed unavailable而非Host整体失败。
6. 一个canonical-valid但native-wire incompatible且inspectable的MCP tool不进入native数组，模型经inspect/use成功调用；
7. Chat与Responses各完成一次direct或meta tool call、fresh Host hydration与manual full-history continuation；证明不依赖remote response ID；
8. dogfood只记录非敏感canonical/native projection fingerprints与route，不记录schema正文、arguments或replay private carrier。

Dogfood不得记录API key、DSN、完整prompt、MCP arguments/body、headers、tool ref或secret。

---

## 14. Architecture gates

本轮activation必须证明：

- capability package不importMCP physical transport、repository、Host或compaction；
- Built-in/MCP/Skill均只通过一个pure registry factory进入planner；
- registration set只能由exact-scope Builtin/MCP/aggregate-Skill三个owner-issued private snapshot carrier派生；不得接受raw registration tuple、共同attempt token或跨owner current-seal握手；
- no mutable Host-lived generic capability registry/generation；
- Built-in source snapshot不执行I/O且只包含execution-backed catalog-joined bindings；MCP/Skill source refresh只在safe point发布完整successor；
- Builtin inventory只能从sealed Host composition投影；seal后不存在late builtin bind或第二个builtin generation；
- source scope与source-kind/leaf-kind/origin matrix由central factory fail closed；
- planner无async/I/O/path open/subprocess/network；
- MCP supervisor仍唯一slot/close owner；
- Skill无execution binding；
- Plugin不存在production contract；
- no second tool registry/exposure generation；
- no durable capability relation/event/job/guard/subject；
- `FrozenModelToolSurface`仍唯一provider tools semantic carrier；
- `PreparedToolExecutionBinding`仍唯一direct execution binding leaf；
- `McpToolSemanticFact`与generic fact通过central adapter，schema不重复声明；
- canonical Tool schema只存在于owner fact/generic fact无损view；lowered OpenAI schema只存在于adapter-owned process-local projection，registry、semantic fingerprint与MCP discovery不得保存它；
- MCP supervisor/generic capability package对OpenAI adapter与function-tool contract常量为零import；
- native-wire projection factory是pure、bounded、closed且无provider-name分支；没有mutable translator registry、callback、generation或第二套schema authority；
- Builtin native incompatibility fail closed；MCP native incompatibility只能meta/unavailable，不能进入canonical invalid policy；
- no arbitrary provider tool gateway；
- no permission-based tool-surface mutation；
- New-MCP ref只绑定tool-specific semantic/policy/route proof，不绑定scope-wide catalog lineage；
- existing continuity compatibility继续覆盖canonical tool surface、model target与provider lowering；registry/MCP route/Skill source变化不能触发same-epoch rebase；
- selected `FrozenNativeToolProjectionSet`保留canonical Tool version到wire projection的exact join，final wire plan逐项证明actual tool bytes；不得新增第二个native compatibility字段或transition enum；
- parent cut只在target/profile-aware native preflight之后冻结；Tool/Skill consumers只接受central-derived sibling views，两个结果的parent/view fingerprints必须在compiler/continuity assembly处成对验证；
- native wire contract变化只能通过既有process-local continuity reset重投影predecessor direct cohort；没有第三个epoch owner、durable generation或replacement receipt；
- Round 5A.2 durable replay relation/hydration owner保持唯一，capability package不能import repository/replay codec，final wire plan必须exact join selected hydration与native projections；
- no compaction import；
- oracle保持`31 Committed / 24 Live / 13 subjects / 2 guards / 26 product relations / 1 durable job`。

---

## 15. 分片实施顺序

### Slice C0：机器基线

- 首先形成并记录Round 5A.2 + OpenAI function-tool wire contract v2的reviewed clean checkpoint；若仍是dirty/TBD baseline立即停止；
- 验证Round 5A.2与Round 7.1均已ACTIVATED，冻结各自public DTO/constant/schema/replay manifest、activation hash与retained node IDs；
- 保存pytest node-ID集合、architecture oracle、source enum、tool descriptors与module/FQCN manifest；
- 保存Chat/Responses fixed-prefix golden；
- 保存Round 6 direct MCP happy path；
- 记录旧capability registry/exposure真实调用点，区分production与dormant。

### Slice C1：Pure semantics与命名减法

- 新建contracts/planner；
- 用pure frozen registry factory替换旧mutable descriptor registry；
- 三项owner-issued process-local snapshot carrier、fixed named registration-set derivation、execution-backed zero-I/O Builtin snapshot与scope/leaf closed admission；不实现共同attempt token/current-seal handshake；
- Builtin composition seal、聚合`LOCAL_SKILL_CATALOG` source与closed Tool-version/route/projection DTO；
- canonical Tool fact字段与adapter-neutral native eligibility/projection DTO；
- 统一`fact_semantic_fingerprint`与parent-derived Tool/Skill dispatch views；
- tool/skill facts与adapters；
- 删除开放行为基类可能性；
- skill-only/tool-only旧名收窄；
- `SKILL_CATALOG` clean rename；
- pure unit tests通过。

### Slice C2：MCP exposure planning

- MCP server registrations、per-server source snapshots与projection refs；
- 删除discovery中的OpenAI lowerer gate；完整raw discovery与仅canonical-invalid normalization policy的COMPLETE/UNAVAILABLE分界；
- adapter-owned native preflight、Builtin fail-closed与MCP direct/meta/unavailable分类；
- cold native-eligible cohort all-or-none及canonical/actual-wire双quote；
- installed canonical cohort reuse、same-contract wire reuse与contract-change reset reprojection；
- catalog DIRECT/NEW；
- physical exact join；
- final wire projection byte proof与existing continuity reset/CAS exact join；
- Round 5A.2/6 retained通过。

### Slice C3：Meta gateway

- fixed descriptors；
- inspect/tool-specific route ref/use；
- policy-bound ref与policy-change stale semantics；
- permission/effect/attempt/result；
- direct unavailable/schema replacement；
- ACK/cancel/close tests。

### Slice C4：Skill relationship

- current registered root policy的complete global scan、单一aggregate snapshot与Skill adapter；
- Skill catalog/activation identity与catalog/active projection join；不复用Tool version DTO；
- legacy private metadata不进入generic capability语义；
- honest Skill names；
- catalog/active placement与prefix；
- 不实施Round 9.1 parser。

### Slice C5：Activation证据

- targeted、full、PostgreSQL、Protocol与architecture；
- real provider direct/meta dogfood；
- README/Gap Index更新；
- activation evidence hashes与oracle；
- 标记Round 9 ACTIVATED后，Round 9.1才可编码。

---

## 16. Definition of Done

以下全部成立才可激活：

1. `Capability`拥有本文唯一语义定义，代码中不存在公开行为型generic base class。
2. Built-in、MCP与Skill共享source registration、complete source snapshot、leaf admission与frozen registry流程；registration set由三个既有owner的exact-scope private snapshot carrier派生，不能由调用方手写不完整集合，也不存在共同attempt token/current-seal handshake。
3. Built-in以IMMUTABLE零I/O、execution-backed退化discovery接入，且在Host open显式seal composition；catalog-only dead descriptor与seal后的late binding不进入provider surface。MCP server snapshots与单一aggregate Skill catalog以SAFE_POINT_REFRESHABLE discovery接入，三者均不能旁路registry进入planner。
4. Built-in与MCP共享只含canonical schema的`FrozenToolCapabilityFact`，但各自execution authority不变；lowered wire projection不进入registry或semantic fingerprint。
5. Skill是独立`FrozenSkillCapabilityFact`，没有executor；Tool/Skill leaf均暴露统一`fact_semantic_fingerprint`，Skill版本机械覆盖identity、catalog、activation与winner provenance。
6. Plugin未进入leaf union、runtime或provider surface。
7. 当前legacy Skill仅通过adapter接入；其private tool/binary/service metadata不进入generic fact/planner，Agent Skills standard与彻底删除这些字段明确留给Round 9.1。
8. Cold native-eligible MCP cohort在canonical与actual-wire bounds均完整fit时all direct，任一aggregate overbound时all eligible MCP meta；native-incompatible MCP在inspect FULL合法时meta、否则unavailable；builtins任何incompatibility都fail closed。
9. Late-ready MCP只追加catalog并经inspect/use执行。
10. Direct MCP断连/dirty/schema replacement不修改native tools且不能用meta绕过。
11. `NewMcpToolRef` exact绑定inspect时展示的真实MCP policy与tool-specific NEW route，但不绑定scope-wide catalog；无关server变化保持token稳定，真实tool/policy/route变化要求重新inspect。`use_new_mcp_tool`只有一次canonical attempt/result。
12. `MCP_CATALOG`使用唯一closed、untrusted renderer：小catalog完整列名，overbound只按完整row确定性截断并给出exact omitted counts与分页指引；不使用ranking，不泄露内部identity。
13. `list_mcp_servers`以closed server/tool page union完整表达DIRECT/NEW/UNAVAILABLE、status/resources/prompts、exact counts与pagination；cursor exact绑定catalog/native surface/scope/filter/page/limit/offset且只读local snapshot。Requested limit是maximum，successful page标记Round 7.1 `FULL_REQUIRED/MCP_DIRECTORY_PAGE`，必须完整安装后才算交付，不得用HEAD_TAIL/COMPACT伪装成功分页。
14. `inspect_new_mcp_tool`只在完整closed DTO满足Round 7.1普通ToolResult logical 40,000-byte边界后准备route/policy-bound dormant ref；successful result标记`FULL_REQUIRED/MCP_INSPECT_SCHEMA`，只有exact FULL continuity install后ref才callable。Schema不截断、不artifact化，所有拒绝均不进入remote MCP lane。
15. 已进入DIRECT的MCP在known-down时返回固定typed说明；same-schema reconnect恢复，schema replacement只等待cold adoption且不能meta绕过。
16. `CAPABILITY_CATALOG`已彻底clean rename为`SKILL_CATALOG`。
17. 当前generic Skill类和tool-only类名实相符。
18. `KernelToolCapabilityPlanner`与`KernelSkillProjectionComposer`分别pure、bounded、deterministic、无physical/durable authority；parent cut只在registry、exact target/profile与native preflight完成后冻结，二者只接受central-derived sibling view，输出exact引用同一parent及各自view fingerprint。Tool planner只消费adapter签发的frozen eligibility，不import/callOpenAI lowerer，也不拥有Skill lineage。
19. Provider preflight exact join canonical Tool surface、唯一native projection set、opaque adapter contract与current physical access；`wire_utf8_bytes`只能从canonical wire JSON派生，final wire bytes不一致时provider open=0。
20. ROOT/child scope、foreign ref、foreign borrow均fail closed。
21. same epoch SYSTEM/tools不变、messages只追加suffix；MCP route或Skill catalog successor不得被误作continuity compatibility变化。Adapter wire contract变化不得在same epoch静默替换tools，只能重投影predecessor exact direct cohort并经existing matching compiler/CAS reset安装新epoch；不得借reset promotion late MCP，也不得新增native transition状态机。
22. 没有新增schema、event、job、guard、subject、receipt、checkpoint、projection或repair。
23. Oracle保持`31/24/13/2/26/1`。
24. Round 5A.2与Round 7.1已ACTIVATED且其public contract/schema/replay manifest与activation hash exact匹配；Round 3/3.1/5A/5A.1/5A.2/6/7/7.1/8 retained与全量tests通过。
25. Round 9.1不再需要从Round 5B反向导入MCP meta DTO。
26. `McpInvalidToolPolicy`公开值仍精确为`FAIL_SERVER | OMIT_INVALID`；不存在`SKIP_TOOL` rename、alias或兼容解析分支。

---

## 17. 下游边界

### 17.1 Round 9.1

Round 9.1只负责：

- Agent Skills canonical manifest；
- standard resources/progressive disclosure；
- 保持Skill owner内部由Round 9冻结的四种physical root bindings，并继续执行一次global precedence scan；
- ordinary `read_file` progressive disclosure、2,000-line default window与无content-suppressing dedup；
- Agent Skills标准filesystem与activation；
- 删除legacy Pulsara metadata且不引入namespaced替代；
- append-only Skill catalog/active body。

Round 9.1不得把四个physical root暴露成四个generic capability sources，也不得新增第五个root或扫描`.claude/skills`。它保持`LocalSkillProvider`内部的ordered four-root policy，并把完整global scan结果作为本文唯一`SAFE_POINT_REFRESHABLE + LOCAL_SKILL_CATALOG` successor snapshot进入registry。它必须复用本文的identity、fact、registry与parent dispatch cut，不能创建Skill-private registry、MCP identity、dependency graph或第二个meta gateway。

### 17.2 Round 9.2 Plugin

后续[Round 9.2](ROUND_9_2_AGENT_PLUGIN_BUNDLE_AND_HOOK_LIFECYCLE_IMPLEMENTATION_SPEC.zh.md)固定Plugin是bundle/source：

~~~text
Enabled Plugin
  -> installer materializes portable Skills into one of the four existing roots
  -> MCP server source registrations
  -> process-local Hook definitions
  -> dormant Subagent-spec inventory
  -> existing four-root Skill owner observes installed files at the next safe point
  -> normalize MCP definitions into existing registrations
  -> existing source owners discover and publish ordinary snapshots
~~~

Plugin不成为第三种tool binding，不贡献第五个Skill root，也不让Runtime扫描任意private cache。Hook不进入本文capability leaf union；它由Round 9.2独立的process-local lifecycle owner执行。Subagent spec在PHC-10完成层次化/批量编排前只允许dormant discovery。具体portable manifest、Codex/Claude compatibility、namespace、enablement、trust与dynamic invalidation由Round 9.2冻结。

### 17.3 Round 5B

未来compaction/rebase只能消费本文已经存在的：

- current `FrozenCapabilityRegistrySnapshot`、其registration/source snapshots及owner-specific MCP/Skill projections；
- current `FrozenCapabilityDispatchCut`、Tool exposure与Skill projection result；
- installed direct tool exposure；
- current MCP catalog；
- current Skill projection/source heads。

Compaction可以在cold successor boundary重新选择MCP direct cohort，但本文不实施promotion/adoption。Round 5B不得复制成`CompactionCapabilityGraph`或durable exposure receipt。

---

## 18. 产品示例

### 18.1 Built-in与cold MCP

~~~text
Host cold open
-> obtain exact-scope Builtin/MCP/aggregate-Skill owner-issued snapshots
-> derive immutable Builtin、docs MCP registrations与one LOCAL_SKILL_CATALOG registration
-> builtin zero-I/O snapshot emits read_file / terminal / memory_search facts
-> docs MCP discovery snapshot emits 4 tool facts
-> one global Skill scan snapshot emits current winning Skill facts
-> one frozen registry snapshot
-> aggregate fits
-> all four MCP tools进入native tools
-> model直接调用
~~~

### 18.2 Late MCP

~~~text
epoch已经安装
-> GitHub MCP late-ready
-> native tools保持不变
-> MCP_CATALOG列出github:create_issue等NEW工具
-> model inspect_new_mcp_tool
-> receives exact schema + opaque ref
-> model use_new_mcp_tool
-> Runtime按MCP effect policy确认并exact调用一次
~~~

### 18.3 Skill正文指导使用MCP

~~~text
Skill body says: use GitHub MCP create_issue when available

if cold direct:
  model sees native github tool and calls it directly

if late/meta:
  independent MCP observation lists the new tool
  model inspect/use through the fixed meta route

if unavailable:
  real call path returns unavailable
  Skill remains valid but cannot create or pretend the tool exists
~~~

`SKILL_CATALOG`在三种情况下保持相同；route truth只属于MCP planner/supervisor。

### 18.4 Direct connection失效

~~~text
native tools仍含github:create_issue
-> connection fails
-> model call can continue
-> if tool is invoked, local gate returns typed MCP unavailable
-> list_mcp_servers reports status
-> same-schema reconnect restores binding
-> tools prefix never changes
~~~

---

## 19. 最终判断

Round 9不是恢复hard-cut前的统一durable capability registry，而是给当前已经存在的Builtin、MCP与Skill三条production路径建立一个诚实、closed、process-local的共同语义边界。

最终拓扑是：

~~~text
CapabilitySourceRegistration
  <- derived from three exact-scope owner-issued immutable snapshot carriers
  -> scope-bound complete CapabilitySourceSnapshot
  -> pure FrozenCapabilityRegistrySnapshot
  -> FrozenCapabilityFact views
  -> exact model target/profile + adapter native-wire preflight
  -> one parent FrozenCapabilityDispatchCut
       -> central-derived Tool dispatch view -> pure Tool exposure plan
       -> central-derived Skill dispatch view -> pure Skill projection
  -> provider channel
  -> TOOL only: existing local gate/binding/execution owner
  -> SKILL only: catalog/activation
~~~

Built-in与MCP统一为Tool capability，Skill作为Instructional capability引用Tool，但不拥有Tool。三者的注册、snapshot与leaf admission逻辑完全共用：Builtin把discovery退化为execution-backed immutable snapshot，MCP按server发布refreshable snapshots，Skill以一个聚合catalog source发布global-precedence snapshot；三项输入分别由现有owner签发，而不是generic caller自报。它们不需要共同attempt token或跨owner原子瞬间。Plugin未来只作为Bundle/Source input contributor加入。这个形状既能为Round 9.1提供稳定依赖，也能让Round 5B在真正需要rebase时消费当前事实，而无需恢复任何durable capability/recovery graph。

---

## 20. 证据锚点

### 20.1 当前Pulsara

- `src/pulsara_agent/capability/descriptor.py`：当前tool-only `CapabilityDescriptor`、provider/availability/advertise字段；
- `src/pulsara_agent/capability/registry.py`：当前mutable、tool-shaped、自增generation registry；本轮只保留其统一admission意图并改为pure frozen builder；
- `src/pulsara_agent/capability/exposure.py`：旧`CapabilityExposurePlan`混合tool与Skill projection；
- `src/pulsara_agent/capability/provider.py`：execution descriptor与skill projection protocol；
- `src/pulsara_agent/capability/types.py`：当前skill-only DTO使用generic capability命名；
- `src/pulsara_agent/conversation_kernel/capability.py`：current skill composer与startup allowlist交集；
- `src/pulsara_agent/model_input/contracts.py`：`FrozenToolSpec`、`FrozenModelToolSurface`与64/1 MiB bounds；
- `src/pulsara_agent/model_input/continuity.py`与`conversation_kernel/input_continuity.py`：canonical surface、actual-wire plan与same-epoch CAS；
- `src/pulsara_agent/llm/adapters/openai/function_tools.py`：Chat/Responses共享显式non-strict native wire projection与唯一contract version owner；
- `src/pulsara_agent/conversation_kernel/direct_model.py`：opaque provider-wire profile、final wire materialization与Round 5A.2 hydration exact join；
- `src/pulsara_agent/llm/provider_replay.py`、`model_input/provider_replay.py`与`provider_assistant_replay_fragments`：durable native replay codec/target/hydration truth；
- `src/pulsara_agent/conversation_kernel/tool_surface.py`：semantic surface与exact physical binding/Host authority；
- `src/pulsara_agent/conversation_kernel/mcp/contracts.py`：MCP semantic tool/discovery/catalog facts；
- `src/pulsara_agent/conversation_kernel/mcp/supervisor.py`：唯一connection/slot/discovery/close owner；
- `src/pulsara_agent/conversation_kernel/context_sources.py`：current Skill/MCP stateful sources；
- `src/pulsara_agent/capability/builtin_catalog.py`：builtin descriptor、binding、permission、effect与long-horizon truth。
- `src/pulsara_agent/mcp_config.py`：user/workspace/Host MCP server source composition；
- `src/pulsara_agent/capability/local_skills.py`：固定root policy与bounded filesystem discovery。

### 20.2 hard-cut前Pulsara

- `5b7ad9f7:src/pulsara_agent/capability/exposure.py`：direct/deferred/hidden/callable planning；
- `5b7ad9f7:src/pulsara_agent/capability/runtime.py`：continuation exact reuse/monotonic narrowing；
- `5b7ad9f7:src/pulsara_agent/capability/providers/mcp.py`：MCP descriptor contribution；
- `5b7ad9f7:src/pulsara_agent/tools/adapters/mcp.py`：semantic descriptor到physical executor；
- `archived_docs/PULSARA_UNIFIED_CAPABILITY_SURFACE_RESEARCH.zh.md`：Skill是prompt capability、MCP是typed execution capability的历史结论。

### 20.3 已有规格

- [Round 3.1](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)：same-epoch strict prefix、tool surface borrow与stateful source successor；
- [Round 5A.2](ROUND_5A_2_DURABLE_PROVIDER_REPLAY_AND_CROSS_RESTART_THREAD_CONTINUATION_IMPLEMENTATION_SPEC.zh.md)：assistant native replay同事务接受、两阶段hydration、final wire plan/CAS exact join与cross-restart continuation；
- [Round 6](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md)：MCP supervisor、bounded discovery、dirty fence、effect、scope与direct execution；
- [Round 5B draft](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)：其中§2的non-compaction direct/meta设计被本轮提取；Round 5B后续应删除重复实现，只保留rebase promotion消费；
- [Round 9.1](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)：本轮激活后的直接下游。

### 20.4 Provider wire协议锚点

- [OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)：`strict:true`的required/`additionalProperties:false`约束，以及Responses省略strict时尝试strict normalization、Chat默认non-strict、显式`strict:false`统一选择best-effort的协议差异；
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)：root必须是object且不能是root `anyOf`、supported subset及nested union限制。

这些外部文档只定义adapter native wire contract，不定义Pulsara canonical MCP schema validity。Activation以checked-in `OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION`、golden与real-provider conformance evidence冻结当时行为；外部页面后续变化不能静默改变same-epoch projection。
