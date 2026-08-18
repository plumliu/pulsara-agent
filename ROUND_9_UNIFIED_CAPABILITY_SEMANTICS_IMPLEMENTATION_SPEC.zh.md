# Round 9：Unified Capability Semantics 与 MCP Direct/Meta Exposure 实施规格

> 状态：**DRAFT — NOT ACTIVATED**
>
> 记录日期：2026-08-17
>
> 当前代码基线：`ffd0d146f8d7991ff3d1e92dc9ca75e8abf894e8`
>
> hard-cut 前参考基线：`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`
>
> 上位契约：[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 provider-input prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5A.1 provider-neutral output termination](ROUND_5A_1_PROVIDER_NEUTRAL_MODEL_OUTPUT_TERMINATION_IMPLEMENTATION_SPEC.zh.md)、[Round 6 MCP](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md)、[Round 7 model-visible observation](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 provider-visible ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Gap Index](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 直接下游：[Round 9.1 Agent Skills Standard](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)
>
> 后续但不属于本轮：[Round 9.2 Agent Plugin bundle 与 Hook lifecycle](ROUND_9_2_AGENT_PLUGIN_BUNDLE_AND_HOOK_LIFECYCLE_IMPLEMENTATION_SPEC.zh.md)、[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)

本文重新冻结 Pulsara 中 `capability` 的唯一产品含义，并把当前 Built-in tool、MCP tool 与 Skill 投影到同一个**纯语义规划边界**。本轮统一 discovery identity、semantic fact与provider exposure planning，但不统一 permission、physical binding、transport、Skill activation或文件执行方式。

本轮同时把原先寄放在 Round 5B 中、但与compaction无关的 MCP direct/meta 混合能力独立落地：cold epoch 建立时可靠且完整可容纳的MCP cohort进入provider native `tools[]`；epoch中后到的MCP工具只通过固定`inspect_new_mcp_tool`与`use_new_mcp_tool`使用。Round 9.1随后只需把Skill正文作为指导数据接入同一registry/catalog，不需要建立Skill→Tool dependency graph。

本轮不建立统一抽象基类，不恢复hard-cut前的durable capability graph，也不让“统一capability”成为新的execution authority。

Round 7.1是本轮编码硬前置，而不只是引用文档：`list_mcp_servers`、`inspect_new_mcp_tool`、`use_new_mcp_tool`与所有direct MCP结果都必须进入已经激活的normal ToolResult projection。Round 9不得临时复制40,000-byte logical FULL、HEAD_TAIL/COMPACT/REF_ONLY、artifact、FULL-delivery requirement或provider envelope逻辑；若Round 7.1尚未ACTIVATED，本轮不得开始production slice。

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

Plugin不属于本轮的capability leaf。后续Round 9.2把Plugin定义为`CapabilityBundle/CapabilitySource`：启用后贡献Skill roots、MCP server definitions与process-local Hook definitions，并保留一个dormant Subagent-spec inventory；Skill/MCP仍分别交还既有owner，Hook不进入capability leaf，Plugin自身不拥有通用`invoke()`。

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
  -> create one exact-scope composition-attempt token
  -> obtain Builtin / MCP-config / Skill-root owner-issued prepared inventories
  -> validate all three current owner seals and derive the complete registered source set
  -> freeze execution-backed builtin zero-I/O source snapshot
  -> freeze one COMPLETE or UNAVAILABLE snapshot for every registered scope-visible MCP source
  -> freeze one scope-bound snapshot for every registered local Skill source
  -> pure registry admission and FrozenCapabilityRegistrySnapshot
  -> pure KernelCapabilityPlanner
  -> select fixed direct tool cohort
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
  -> publish complete successor Skill source snapshots at safe point
  -> freeze a successor registry snapshot; do not mutate the installed one
  -> append SKILL_CATALOG successor or invalidation
  -> never mutate provider tools
~~~

---

## 1. 范围、非目标与迁移纪律

### 1.1 本轮实施

- capability领域词汇与closed contracts；
- Built-in catalog、MCP server config与Skill root的统一source-registration adapter；
- `IMMUTABLE | SAFE_POINT_REFRESHABLE` source policy、complete source snapshot与pure registry admission；
- Built-in/MCP tool到统一`FrozenToolCapabilityFact`的无损adapter；
- current Skill projection到最小`FrozenSkillCapabilityFact`的adapter；
- `CapabilityIdentity`、version reference与MCP resolved route；
- process-local `FrozenCapabilityRegistrySnapshot`、`FrozenCapabilityPlanningCut`与pure `KernelCapabilityPlanner`；
- cold MCP direct cohort的deterministic all-or-none selection；
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
- 不实现Plugin manifest、install、enable、disable或bundle namespace；
- 不让Plugin动态加载Python代码；
- 不实施compaction summary、snapshot adoption、rebase或MCP promotion；
- 不把late MCP在same epoch热提升为native direct tool；
- 不增加provider-side tool search、BM25、dense retrieval或virtual descriptor filesystem；
- 不新增generic `search_capabilities`/`use_capability`；
- 不把MCP resources/prompts/elicitation重分类为新的capability leaf；它们继续由Round 6现有fixed tools访问；
- 不建立durable capability table、event、job、generation、receipt、checkpoint、projection或repair graph；
- 不承诺跨Host保持同一个capability exposure epoch；replacement Host仍cold build；
- 不修改canonical transcript schema。

### 1.3 Clean-v0纪律

当前仓库允许clean-v0 reset。本轮可以删除或重命名尚未形成对外durable wire contract的旧Python类型，不建立双读或alias图。但必须保留真正被生产路径依赖的descriptor、permission、result、long-horizon与binding语义。

本轮尤其不得同时保留：

- 旧`CapabilityExposurePlan`与新`FrozenCapabilityExposurePlan`两套planner；
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
- `model_input/contracts.py::FrozenToolSpec`与`FrozenModelToolSurface`，它们是provider-visible typed tool schema的唯一frozen truth；
- `conversation_kernel/tool_surface.py::PreparedToolExecutionBinding`与`ProcessLocalToolSurfaceAccess`，它们把semantic descriptor exact join到Host authority与executor；
- `conversation_kernel/mcp/contracts.py::McpToolSemanticFact`、discovery snapshot与catalog snapshot；
- MCP supervisor作为唯一connection、slot、dirty generation与physical close owner；
- Round 6 `MCP_CATALOG`与`list_mcp_servers`；
- local Skill discovery、catalog projection、`ACTIVE_SKILL`和Round 3.1 append-only source head；
- structured compiler的64 tool / 1 MiB aggregate tool-schema bound；
- continuity owner的same-epoch SYSTEM/tools/messages proof；
- Round 7 provider-wire hygiene，不把internal fingerprint/generation写入模型正文。

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

Built-in不是“没有discovery”的例外，而是`IMMUTABLE` source，其discovery退化为对Host-open时**execution-backed descriptor inventory**的纯snapshot。Compiled catalog仍可以保存尚未接入production composition的descriptor，但这些dead/dormant entries不是capability source leaf。MCP server与Skill root是`SAFE_POINT_REFRESHABLE` source；它们可以反复发现新leaf，但每次都必须先形成完整、immutable source snapshot，再进入与Built-in相同的leaf admission。不得让MCP supervisor或filesystem scanner绕过registry直接修改planner集合。

---

## 3. Canonical词汇与authority边界

### 3.1 七个词的唯一含义

| 词 | 定义 | 是否拥有execution |
|---|---|---|
| `CapabilitySourceRegistration` | Host当前承认哪些bounded source及其refresh policy | 否 |
| `CapabilitySourceSnapshot` | 一个registered source在一次complete freeze/discovery后的immutable leaf集合 | 否 |
| `CapabilityFact` | 某项能力是什么的immutable semantic fact | 否 |
| `CapabilityRegistrySnapshot` | 对同一safe point下全部complete source snapshots的pure、closed合并 | 否 |
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
| Skill root source registration | Host skill-root composition policy |
| Skill filesystem manifest | Skill source snapshot |
| Skill active body | existing `ACTIVE_SKILL` projection |
| Capability registry snapshot | pure central factory；没有long-lived mutable owner |
| Provider direct tool surface | `FrozenModelToolSurface` + continuity epoch |
| Capability exposure selection | pure planner result；不是authority |
| Tool attempt/result | existing canonical repository transaction |

`KernelCapabilityPlanner`只能消费上述owner已经冻结的事实。它不能自行connect MCP、读Skill文件、检查permission、创建attempt或关闭slot。

### 3.3 Trust

- Built-in descriptor是Pulsara-owned schema，但是否可调用仍由permission与binding决定；
- MCP descriptor来自远端server，schema经过bounded normalization但不成为本地policy；
- Skill是untrusted instructional data；
- provider exposure只表示“模型可以看到或引用”，不表示“物理操作已授权”；
- Runtime permission、Plan、memory opt-out、MCP dirty fence与effect confirmation始终优先。

### 3.4 注册/发现与执行是两个正交维度

| 类型 | 固定注册 | snapshot/discovery | refresh | execution |
|---|---|---|---|---|
| Built-in tool | Host-open execution-backed builtin source | 对已安装binding与catalog exact join后的inventory做零I/O pure snapshot | `IMMUTABLE` | Builtin binding |
| MCP tool | resolved MCP server source | bounded initialize/list tools/catalog snapshot | `SAFE_POINT_REFRESHABLE` | MCP slot/binding |
| Skill | registered Agent Skills root | bounded filesystem scan/parse snapshot | `SAFE_POINT_REFRESHABLE` | 无 |

这里的“固定注册”不是为MCP复制一份静态remote schema，也不是为Skill发明inline Python manifest：

- MCP固定注册的是server source；remote tool schema仍只由negotiated discovery拥有；
- Skill固定注册的是logical root；skill leaf仍只由该root中的标准`SKILL.md`拥有；
- Built-in descriptor catalog与executor inventory不是同一事实；source adapter必须从Host-open时已安装、scope-visible且能与catalog exact join的binding inventory构造registration/snapshot，catalog-only entry不能进入registry；
- bundled Skill必须先物化到某个registered root再被普通discovery发现；不得走第二条“builtin Skill”leaf通道；
- future Plugin向本文只贡献MCP server registration与Skill root registration；其Hook由独立process-local owner执行，Subagent spec暂时dormant，均不得注册第三种tool executor。

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
    LOCAL_SKILL_ROOT = "LOCAL_SKILL_ROOT"


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

身份规则：

- Built-in tool：`stable_source_id = pulsara-builtin-tools`，`stable_name = exact tool name`；
- MCP tool：`stable_source_id = exact server_id`，`stable_name = complete remote_tool_name`；
- Skill：`stable_source_id = logical skill root identity`，`stable_name = current skill name`；
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
class FrozenCapabilitySourceRegistrationInventory:
    source_kind: CapabilitySourceKind
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    registrations: tuple[FrozenCapabilitySourceRegistration, ...]
    inventory_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenCapabilitySourceRegistrationSet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    registrations: tuple[FrozenCapabilitySourceRegistration, ...]
    source_inventory_fingerprints: tuple[str, str, str]
    registration_set_fingerprint: str
~~~

closed matrix固定为：

| `source.kind` | `refresh_mode` | source-specific registration truth |
|---|---|---|
| `BUILTIN_REGISTRY` | `IMMUTABLE` | Host-open execution-backed builtin inventory + joined catalog contract |
| `MCP_SERVER` | `SAFE_POINT_REFRESHABLE` | one resolved server config identity |
| `LOCAL_SKILL_ROOT` | `SAFE_POINT_REFRESHABLE` | one logical root、scope与precedence policy |

`source_contract_fingerprint`只引用source owner已经冻结的非秘密semantic registration identity。Builtin branch覆盖由完整binding input选出的ordered descriptor/catalog contract identities，但不覆盖executor object/identity或binding generation。Generic contract不保存MCP headers/auth/request state/transport object，也不保存Skill absolute private path、directory handle或watcher。Owner-specific config/root binding仍由MCP supervisor或Skill provider持有；generic registration只证明“这个source被Host composition承认”。

`FrozenCapabilitySourceRegistrationInventory`只是可fingerprint的semantic inventory value；它**不能仅凭自身字段证明现实中的owner inventory完整**。任何调用方都能重算一份内部一致但漏项的tuple，因此不得把`inventory_fingerprint`称为owner signature。完整性由三个既有composition owner在同一次planning attempt签发的process-local admission carrier证明：

| required inventory | 唯一producer | 完整性的含义 |
|---|---|---|
| `BUILTIN_REGISTRY` | Host tool composition + builtin adapter | exact scope下恰有一个immutable builtin source registration；该source的leaf completeness另由完整execution-backed binding input证明 |
| `MCP_SERVER` | resolved MCP config composition | exact scope下当前全部enabled/registered server configs；允许合法空tuple |
| `LOCAL_SKILL_ROOT` | Host Skill-root composition policy | exact scope下当前全部registered logical roots；允许合法空tuple |

Host先创建exact-scope `CapabilityCompositionAttemptToken`，并把同一个opaque token分别交给三个现有owner。它们各自返回private-constructor carrier：

~~~python
@dataclass(frozen=True, slots=True)
class PreparedBuiltinCapabilitySourceInventory:
    inventory: FrozenCapabilitySourceRegistrationInventory
    attempt_token: object = field(repr=False, compare=False)
    builtin_composition_seal: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedMcpCapabilitySourceInventory:
    inventory: FrozenCapabilitySourceRegistrationInventory
    attempt_token: object = field(repr=False, compare=False)
    resolved_config_composition_seal: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedSkillRootCapabilitySourceInventory:
    inventory: FrozenCapabilitySourceRegistrationInventory
    attempt_token: object = field(repr=False, compare=False)
    root_composition_seal: object = field(repr=False, compare=False)
~~~

这些carrier分别exact引用现有sealed Builtin composition、MCP current resolved-config composition与六root composition；seal/token都不序列化、不进入semantic fingerprint，也不成为新的long-lived owner。Production central API固定为：

~~~python
def prepare_capability_source_registration_set(
    *,
    builtin_inventory: PreparedBuiltinCapabilitySourceInventory,
    mcp_inventory: PreparedMcpCapabilitySourceInventory,
    skill_root_inventory: PreparedSkillRootCapabilitySourceInventory,
) -> FrozenCapabilitySourceRegistrationSet: ...
~~~

Central seam必须先以object identity验证三个carrier的attempt token相同且scope相同，再分别要求真实owner确认seal仍是current/exact；然后才把其中三个frozen inventory交给capability package内部pure `_freeze_capability_source_registration_set(...)`。Production其他模块不得直接调用该pure helper或提交任意`registrations` tuple。这样“semantic tuple内部一致”“真实owner inventory没有漏掉”与“每个expected registration有snapshot”成为三个独立证明；同时漏掉registration与snapshot无法靠重算fingerprint伪装完整。

Inventory本身不包含secret、transport、filesystem handle或executor object。Prepared carrier只借用existing owner seal并在planning完成/取消时释放引用；它不是新的generic registry、lease、generation或durable composition owner。MCP/Skill source变化后旧seal validation失败，调用方必须从三个owner重新准备同一attempt，而不能将旧MCP inventory拼到当前Skill inventory。

每个inventory fingerprint覆盖exact scope、source kind与ordered registration fingerprints；其中每个registration的`source.kind`必须与inventory kind一致。Builtin inventory恰有一个registration；MCP与Skill-root inventory允许bounded empty。三个prepared carrier必须来自同一次Host composition/safe-point planning attempt；不能缓存旧MCP inventory再拼当前Skill inventory。

注册规则：

- exact同一`source + registration_fingerprint`重复输入是idempotent；
- 同一`source`在一个registration set中出现两个不同fingerprint是conflict；
- Built-in registration及其execution-backed leaf inventory在Host open后不得替换、删除或新增；compiled catalog中没有production binding的descriptor允许继续作为dormant catalog entry存在，但不得进入source snapshot；
- MCP server/Skill root registration set只能在safe point采纳current complete composition；
- source registration的新增/删除不直接改provider输入，必须经过successor registry与exposure planner；
- 每个source inventory与最终registration set都exact绑定`conversation_scope_kind + scope_subagent_task_id`；ROOT inventory不能复用于child，反之亦然；
- registration set按`(source.kind, stable_source_id)`排序且unique，并且exact等于三个已通过current-seal admission的semantic inventory之scope-filtered并集；
- `source_inventory_fingerprints`固定按`BUILTIN_REGISTRY, MCP_SERVER, LOCAL_SKILL_ROOT`顺序保存三个已验证semantic inventory fingerprint；opaque admission seal不进入结果；
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
    provider_spec: FrozenToolSpec
    semantic_fingerprint: str
~~~

Central factory规则：

- Built-in adapter从Host tool-surface owner在同一surface lock下冻结的exact-scope executor binding inventory出发，逐项exact join现有catalog entry后转换`FrozenToolSpec`；禁止从catalog keys正向枚举leaf；
- MCP直接复用`McpToolSemanticFact.provider_spec()`；
- `semantic_fingerprint`覆盖identity、provider-visible name/description/schema和descriptor fingerprint；
- 不复制effect、permission、availability、slot lease或executor；
- 同一identity/semantic fingerprint必须产生byte-identical `provider_spec`；
- 同一provider name不得映射到两个identity。

`FrozenToolCapabilityFact`不是`PreparedToolExecutionBinding`的父类。二者通过`provider_spec.descriptor_fingerprint`与capability identity exact join。

Built-in adapter的双向不变量固定为：每个进入snapshot的builtin fact恰有一个installed binding与一个matching catalog entry；每个该scope可见的installed builtin binding恰好产生一个fact。Catalog-only dormant descriptor不要求有binding，也不产生fact；binding没有catalog entry、descriptor fingerprint不一致或同名多binding均在provider open前fail closed。Round 9新增的`inspect_new_mcp_tool/use_new_mcp_tool`若要成为fixed tools，必须同时实现真实local executor binding，不能只在catalog加schema。

这条不变量由现有tool-surface owner签发的窄process-local carrier实现，而不是让pure registry读取executor：

~~~python
@dataclass(frozen=True, slots=True)
class PreparedBuiltinCapabilitySourceInput:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    builtin_composition_seal_fingerprint: str
    executor_bindings: tuple[ProductionBuiltinExecutorBinding, ...]
    source_input_fingerprint: str
~~~

`DirectKernelToolPort`必须在同一surface lock下、复用当前ROOT/child过滤规则冻结这份完整binding tuple；adapter逐项读取matching catalog entry并构造generic facts。Seal fingerprint只覆盖full sealed base的ordered binding fingerprints；`source_input_fingerprint`再覆盖seal、scope与ordered projected binding fingerprints。ROOT/child input必须持有同一个seal fingerprint。该carrier可包含process-local executor identity用于preflight join，但这些字段不进入`FrozenToolCapabilityFact`、registry fingerprint或provider body；generic registry package也不得import它。

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
- ROOT/child `PreparedBuiltinCapabilitySourceInput`都只能从同一个sealed base tuple做closed scope projection，不能重新读取mutable ports；
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
    catalog_semantic_fingerprint: str
    activation_semantic_fingerprint: str
~~~

其中：

- `catalog_semantic_fingerprint`只覆盖provider catalog可见字段；
- `activation_semantic_fingerprint`可额外覆盖当前body/version，但body本身不进入catalog；
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

`freeze_capability_registry_snapshot(...)`是唯一leaf admission factory。它是pure、一次性、bounded的builder，不是Host-lived registry owner，也没有自增generation、callback、`register/unregister` side effect或跨Host identity。

`source_snapshot_fingerprint`使用closed domain覆盖registration fingerprint、exact scope、disposition与ordered fact semantic fingerprints；`registry_fingerprint`覆盖derived registration-set fingerprint与ordered source-snapshot fingerprints。任何owner-specific physical identity都不进入这两个semantic fingerprints。

Source snapshot规则：

- 每个snapshot必须exact绑定registration set的scope；snapshot fingerprint覆盖scope，ROOT snapshot不能在child registry复用；
- Built-in snapshot必须`COMPLETE`，只从Host-open时冻结的scope-visible、execution-backed且catalog-joined inventory确定性产生；这就是零I/O退化discovery；
- MCP每个registered server只在**全部远端分页/枚举成功读取**、aggregate/bounds验证完成且既有include/exclude/invalid-tool policy被完整应用后发布`COMPLETE`。`SKIP_TOOL`丢弃单个invalid schema、或include/exclude过滤tool，属于对完整raw listing的确定性normalization，所得集合仍是`COMPLETE`，不是partial；只有连接失败、分页未完成、frame/parse/aggregate/bounds失败或无法证明完整枚举时才发布`UNAVAILABLE + facts=()`；
- Skill root由一次跨全部registered roots的bounded scan统一解析precedence，输出再按winning root拆回source snapshots；invalid/duplicate loser不注册leaf；若任一会影响全局precedence/completeness的枚举或aggregate步骤失败，则该planning cut中**全部registered Skill roots**都发布`UNAVAILABLE + facts=()`，不得保留其他root的partial facts伪装完整catalog；
- `COMPLETE + facts=()`表示合法空source，和`UNAVAILABLE`不同；
- registry factory必须为central composition seam产生的registration set中每一项接收exact one snapshot，且不得接受unregistered snapshot；expected source完整性来自此前的owner-seal admission，snapshot exact-one只证明每个expected source都有明确`COMPLETE | UNAVAILABLE`结果；
- 每个fact的`identity.source`必须exact equal其snapshot registration的`source`；
- source-kind、leaf-kind与origin使用以下closed matrix，任何其他组合均拒绝：

  | snapshot source kind | legal leaf | required identity/origin |
  |---|---|---|
  | `BUILTIN_REGISTRY` | `FrozenToolCapabilityFact` | `identity.kind=TOOL`且`origin=BUILTIN` |
  | `MCP_SERVER` | `FrozenToolCapabilityFact` | `identity.kind=TOOL`且`origin=MCP` |
  | `LOCAL_SKILL_ROOT` | `FrozenSkillCapabilityFact` | `identity.kind=SKILL`，且不存在execution origin/binding |

- visibility不由generic registry按名字或当前配置重算：Builtin adapter复用现有ROOT/child binding过滤；MCP adapter按owner fact中的`root_visible/subagent_visible`为exact scope筛选并与owner projection exact join；Skill root inventory/scan同样先按scope冻结。Generic fact无需再复制visibility字段，因为scope-bound snapshot就是其唯一admission envelope；脱离该snapshot不得复用leaf。
- 一个registry内source identity、capability identity均unique；Tool provider name全局unique；Skill同名winner必须在进入generic factory前由closed root precedence确定；
- facts按`(kind, identity_fingerprint, semantic_fingerprint)`确定性排序；同输入得到byte-identical snapshot/fingerprint；
- registry的Tool/Skill flattened view由`source_snapshots`纯派生，禁止再保存一份caller可独立传值的`builtin_tools/mcp_tools/skill_facts`。

Safe-point refresh不修改旧registry：owner冻结新的complete/unavailable source snapshot，central factory重建新的`FrozenCapabilityRegistrySnapshot`，planner再结合installed epoch决定append-only successor。Watcher/listChanged只负责唤醒；它们不是registry truth。

### 4.6 Capability version 与 MCP tool reference

~~~python
class CapabilityRouteReasonCode(StrEnum):
    DIRECT_NATIVE_SURFACE = "DIRECT_NATIVE_SURFACE"
    NEW_NOT_IN_NATIVE_SURFACE = "NEW_NOT_IN_NATIVE_SURFACE"
    NEW_COLD_COHORT_META_FALLBACK = "NEW_COLD_COHORT_META_FALLBACK"
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


@dataclass(frozen=True, slots=True)
class CapabilityVersionRef:
    identity_fingerprint: str
    semantic_fingerprint: str
    provider_name: str
    version_fingerprint: str


~~~

`CapabilityVersionRef`是Tool/Skill当前semantic version的唯一小型引用：Tool使用fact `semantic_fingerprint + provider_spec.name`；Skill使用`catalog_semantic_fingerprint + public_name`。`version_fingerprint`只覆盖上述三个独立字段，不覆盖scope、status、policy、registry/catalog fingerprint、executor或physical generation。

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
    version: CapabilityVersionRef
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
    source_snapshot_fingerprints: tuple[str, ...]
    snapshot_fingerprint: str
~~~

Round 9 adapter保持当前visible body/catalog behavior，不借机实现Agent Skills parser。`source_snapshot_fingerprints`只能引用同一registry中的`LOCAL_SKILL_ROOT` snapshots；Skill facts不在projection与registry各保存一份。`LocalSkillDiscovery`保留renderer/activation需要的source-specific parsed carrier，central factory必须证明其中winning manifests与registry Skill view exact join。Round 9.1随后以portable Agent Skills manifest替换legacy parser，并删除旧metadata，不扩展dependency surface。

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
    direct_tool_versions: tuple[CapabilityVersionRef, ...]
    native_surface_compatibility_fingerprint: str


CapabilityEpochPredecessor = (
    EmptyCapabilityEpochPredecessor
    | InstalledCapabilityEpochPredecessor
)
~~~

`InstalledCapabilityEpochPredecessor.tool_surface`必须是continuity owner已有epoch view中的同一frozen值或fingerprint-exact value，不允许caller重建第二份surface。`native_surface_compatibility_fingerprint`只覆盖exact scope、ordered native tool specs、direct capability versions与相关fixed tool/lowering contract；它是Capability唯一进入continuity epoch compatibility的fingerprint。

`CapabilityVersionRef`只保存identity fingerprint、semantic fingerprint与provider name；不保存executor binding。

### 4.11 Planning cut

~~~python
@dataclass(frozen=True, slots=True)
class FrozenCapabilityPlanningCut:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    predecessor: CapabilityEpochPredecessor
    registry: FrozenCapabilityRegistrySnapshot
    mcp: FrozenMcpCapabilityProjectionInput
    skills: FrozenSkillProjectionInput
    planning_cut_fingerprint: str
~~~

它是一次provider dispatch planning的immutable semantic input，不是durable snapshot。它不查询数据库，不读取filesystem，不打开MCP连接。

Central factory必须证明：

- exact scope一致；
- registry exact scope与planning scope一致；
- identity unique；
- provider tool names不冲突；
- Built-in/MCP/Skill facts都只来自registry的current complete source snapshots；
- registry必须包含exact one immutable Built-in source snapshot；
- MCP/Skill projection refs分别是registry source snapshot的closed子集，并与owner-specific catalog/discovery carrier exact join；
- predecessor revision/nonce与continuity owner一致；
- source projection均在同一个safe point冻结；
- fingerprint覆盖全部独立输入。

### 4.12 Exposure plan

~~~python
@dataclass(frozen=True, slots=True)
class FrozenCapabilityExposurePlan:
    planning_cut_fingerprint: str
    direct_tool_surface: FrozenModelToolSurface
    direct_tool_versions: tuple[CapabilityVersionRef, ...]
    mcp_catalog_route_projection: FrozenMcpRouteProjection
    native_surface_compatibility_fingerprint: str
    catalog_lineage_fingerprint: str
    exposure_plan_fingerprint: str
~~~

三个fingerprint职责必须分离：

- `native_surface_compatibility_fingerprint`由`direct_tool_surface + direct_tool_versions + fixed contract`确定；cold install后成为epoch-stable compatibility fact，installed planning必须byte-equal复用；
- `catalog_lineage_fingerprint`覆盖current registry、MCP DIRECT/NEW/UNAVAILABLE routes、catalog route projection与`cut.skills.snapshot_fingerprint`；它允许在same epoch随complete successor snapshot变化，只用于本次append-only source/renderer/execution-ref的exact join；
- `exposure_plan_fingerprint`组合planning cut与前两者，证明一次planning result完整，但**不得**整体写成continuity compatibility key。

因此late MCP、Skill refresh、server status或catalog presentation变化可以改变`catalog_lineage_fingerprint/exposure_plan_fingerprint`并追加message suffix，却不能改变`native_surface_compatibility_fingerprint`、触发cold reset或重写provider `tools[]`。

`FrozenMcpRouteProjection.routes`是MCP tool-specific route的唯一tuple；Exposure plan不得再保存第二份`mcp_routes`。Projection只额外保存所join的existing catalog semantic fingerprint；完整server instructions/resources/prompts/status仍由MCP catalog owner/renderer拥有。该plan是pure value，不持有authority。它的字段不包含：

- `PreparedToolExecutionBinding`；
- `McpSlotLease`；
- provider transport；
- permission snapshot；
- filesystem handle；
- continuity install permit；
- canonical repository connection。

Exposure plan不再复制`skill_versions`：registry与`FrozenSkillProjectionInput`已经拥有同一完整Skill semantic cut，central planning-cut factory负责exact-equivalence validation，catalog lineage直接引用`cut.skills.snapshot_fingerprint`。Planner不解析Skill dependency、不渲染final Skill catalog，也不根据当前user text选择active Skill。`KernelSkillProjectionComposer`继续以同一`FrozenSkillProjectionInput`与closed activation subject形成`SKILL_CATALOG`/`ACTIVE_SKILL`；这样generic planner不获得Skill renderer、CLI health或user-message interpretation authority。

### 4.13 KernelCapabilityPlanner

~~~python
class KernelCapabilityPlanner:
    def plan(
        self,
        cut: FrozenCapabilityPlanningCut,
    ) -> FrozenCapabilityExposurePlan: ...
~~~

Planner必须pure、deterministic且bounded。相同cut fingerprint必须产生byte-identical plan。Planner不得接受自由`dict`、callback或resolver object；所有输入均为frozen DTO。

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
- opaque cursor内部exact绑定current catalog semantic fingerprint、current epoch native-surface compatibility fingerprint、ROOT/child exact scope、server filter、page kind、limit与next offset；这些内部字段不得进入provider正文；
- catalog/surface/scope/filter变化使旧cursor返回`STALE_CURSOR`，unknown/invisible server返回`NOT_FOUND`，非法limit/cursor shape返回`INVALID_ARGUMENTS`；
- 它只读取一次已经安装的local catalog/route snapshot，不连接server、不refresh、不触发discovery、不取得MCP physical operation lane；
- `limit`是maximum row count，不是必须返回的数量；page factory复用Round 7.1唯一logical ToolResult renderer/quote，按ordered rows选择不超过limit且在最大合法call-local augmentation下必有FULL variant的最长prefix，并据此准备exact `next_cursor`；它不反向调用compiler，也不依赖未来actual citation碰巧更短；
- successful page标记Round 7.1 `FULL_REQUIRED/MCP_DIRECTORY_PAGE`。Response明确给出returned count、总数与`next_cursor`；不得靠HEAD_TAIL/COMPACT截断一个directory page后仍称其为成功分页。单个已bounded row连同actual logical envelope都无法FULL容纳时返回typed `MCP_DIRECTORY_ROW_OVERBOUND`且不推进cursor；单条page FULL合法但aggregate input不fit时由通用compiler返回resource boundary、provider open=0，Runtime不得把模型未见的`next_cursor`声称为已交付；
- resources/templates/prompts/status/instructions保留总目录诊断语义，但任何secret、raw exception或private URL必须继续服从Round 6 redaction。

### 6.2 Cold direct cohort selection

在`EMPTY` predecessor上：

1. 冻结fixed builtin surface；
2. 由MCP config的既有`exposure_policy.include/exclude`、scope policy与schema validity先得到完整可暴露集合；被配置排除的tool既不direct也不meta；
3. 取得其中当前READY_CLEAN的完整MCP tool cohort；
4. 将完整cohort与builtins合并、按provider name排序；
5. 若总数不超过64且canonical tool bytes不超过1 MiB，完整cohort全部`DIRECT`；
6. 若任一bound超出，全部MCP为`NEW_MCP_META_ONLY`，仅builtins进入native surface；
7. 若builtins自身超限，provider open=0，返回typed tool-surface resource boundary。

禁止按发现时序、前N个、词法排名或embedding挑选部分MCP。用户若需要缩小集合，只能使用Round 6既有per-server exposure include/exclude；这会缩小整个可暴露集合，而不是把明确排除的工具偷偷保留在meta gateway。

该all-or-none fallback保证同一server早1毫秒READY或晚1毫秒READY不会分别造成Host失败与成功：过大的MCP集合无论cold或late都可经meta使用。

### 6.3 Installed epoch

在`INSTALLED` predecessor上：

- 直接复用predecessor `FrozenModelToolSurface`；
- 不重新选择direct cohort；
- current MCP fact与predecessor direct versions exact相同：`DIRECT`；
- current新增identity：`NEW_MCP_META_ONLY`；
- predecessor direct identity消失或连接不可用：provider descriptor仍为DIRECT，local execution state为typed unavailable；
- predecessor direct identity发生schema replacement：旧descriptor继续保留但禁止physical dispatch，新版本不能通过meta绕过；下个cold epoch才可采用新schema；
- same-schema reconnect只换physical binding，不改semantic route。

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
start one provider-dispatch planning attempt deadline and composition-attempt token
-> freeze exact scope and EMPTY continuity predecessor
-> require Builtin composition SEALED and freeze scope projection from sealed base
-> obtain exact Builtin/MCP-config/Skill-root owner-issued prepared inventories
-> validate same attempt/current seals and derive exact current source registration set
-> freeze immutable execution-backed Built-in zero-I/O source snapshot
-> freeze complete/unavailable MCP server source snapshots at safe point
-> freeze complete/unavailable Skill root source snapshots at safe point
-> pure freeze_capability_registry_snapshot(...)
-> freeze MCP/Skill owner-specific projections referencing that registry
-> construct FrozenCapabilityPlanningCut
-> pure planner selects direct/meta and catalog projections
-> prepare exact physical tool-surface access for direct surface
-> collect remaining runtime sources from same dispatch planning attempt
-> compile semantic provider input
-> build provider-wire plan / DirectModel preflight
-> continuity candidate exact joins native-surface compatibility + current catalog lineage/compiled suffix
-> continuity CAS install
-> provider open_once
~~~

若physical exact join失败，candidate必须discard；不得安装一个没有可验证direct surface的epoch。

### 8.2 Installed epoch

~~~text
freeze exact installed continuity epoch view and one composition-attempt token
-> require the same Builtin composition seal fingerprint
-> obtain all three exact-scope owner-issued prepared inventories at safe point
-> validate same attempt/current seals and derive current registration set
-> reuse identical immutable Built-in source snapshot
-> freeze current complete/unavailable MCP/Skill successor source snapshots
-> build a new frozen registry snapshot; never mutate predecessor registry
-> exact join MCP/Skill owner-specific projections to that registry
-> planner reuses predecessor native surface byte-for-byte
-> prove native-surface compatibility fingerprint unchanged
-> derive current NEW MCP routes and catalog successor
-> derive current Skill catalog successor
-> compiler compatible append
-> preflight / continuity CAS / provider open
~~~

Planner若生成不同native surface，属于internal contract conflict，provider open=0。

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
| Builtin composition seal后再次bind fixed/support port | installed surface不变 | none | typed local composition rejection；不增generation |
| 三项owner-issued prepared inventory缺失、错scope、attempt token不一致、owner seal stale或registration union不完整 | no provider open | none | planning conflict |
| duplicate identical source registration | 按唯一项处理 | none | idempotent |
| same source在同一cut出现不同registration | no provider open | none | planning conflict |
| refreshable source完成相同snapshot | 不变 | no-op | current route保持 |
| MCP/Skill无法证明complete snapshot | installed surface不变 | catalog UNAVAILABLE/invalidation | 不发布partial leaf truth |
| no MCP config | fixed builtins | empty/cleared MCP catalog | list返回空 |
| cold READY MCP cohort fits | builtins + all selected MCP | catalog标DIRECT | direct invoke |
| cold cohort overbound | builtins only | catalog标NEW/meta | inspect/use |
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
- compiler working set：64 MiB；
- configured MCP servers：64；
- generic source registrations：1个Builtin + 最多64个MCP server + current registered Skill roots；Round 9.1激活后Skill roots上限为6；
- discovered tools per MCP server：512；
- MCP discovery page/item/body/schema与Host aggregate：继续服从Round 6；
- MCP catalog FULL/COMPACT/REF：32/8/2 KiB；
- single inspected schema必须在既有schema working-set bound内形成exact JSON，并且完整closed inspect DTO必须通过Round 7.1 provider-neutral logical FULL 40,000-byte quote；
- process-local issued new-MCP refs：每scope每epoch最多1,024个unique live token；不做LRU eviction，达到上限时inspect typed capacity failure；
- capability planner canonical fingerprint input：不得复制schema/body；引用既有frozen facts后的额外framing最多4 MiB；
- registry snapshot只组合既有bounded source facts，不把MCP catalog或Skill manifest body复制为第二份generic payload；
- Skill discovery/body bounds保持现状，Round 9.1另行收紧。

所有count/byte检查在provider open或MCP physical attempt前完成。禁止静默截断schema、arguments、identity或ref。

---

## 12. 实施修改面

### 12.1 `capability/contracts.py`

新增纯DTO：

- `CapabilityKind`；
- `CapabilitySourceKind/Ref`；
- `CapabilitySourceRefreshMode`、`FrozenCapabilitySourceRegistration`、semantic inventory与derived registration set；process-local prepared admission carrier不放入pure contracts模块；
- `CapabilityIdentity`；
- `FrozenToolCapabilityFact`；
- `FrozenSkillCapabilityFact`；
- `CapabilitySourceSnapshotDisposition`、`FrozenCapabilitySourceSnapshot`与`FrozenCapabilityRegistrySnapshot`；
- `CapabilityVersionRef`、`McpToolCapabilityRef`、`FrozenMcpToolExposure`、`FrozenMcpRouteProjection`、planning cut与拆分native compatibility/catalog lineage的exposure plan。

该模块只能依赖primitives与`model_input` frozen contracts；禁止importconversation repository、`conversation_kernel.mcp`（包括其pure-looking DTO）、MCP transport、Host、tool runtime或compaction。MCP adapter只能向内构造这里的neutral generic facts。

### 12.2 `capability/registry.py`

现有mutable tool-descriptor registry执行clean replacement：

- 不保留Host-lived自增generation或原地`register/unregister` authority；
- 提供module-private pure `_freeze_capability_source_registration_set(...)`，只处理已经由Host central composition验证过的三个semantic inventory；production不得直接调用；
- 提供pure `freeze_capability_registry_snapshot(...)` central factory；
- 输入只接受complete/unavailable frozen source snapshots；
- exact验证source inventory/set完整性、每项registration恰有一个scope-bound snapshot、source-kind/leaf-kind/origin matrix、source/fact join、identity/provider-name uniqueness与deterministic ordering；
- flattened Tool/Skill view由source snapshots纯派生；
- Built-in、MCP与Skill均必须经过该factory，禁止planner接受旁路leaf tuple；
- 不读取builtin module、MCP supervisor、filesystem、Host或repository。

### 12.3 `capability/planner.py`

- 实现pure `KernelCapabilityPlanner`；
- cold direct cohort all-or-none；
- installed surface exact reuse；
- 以`cut.skills.snapshot_fingerprint`exact joinSkill catalog lineage，不复制ordered Skill version tuple；
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
- discovery candidate按server发布complete/unavailable source snapshot；只有完整wire enumeration经过既有include/exclude与`FAIL_SERVER | SKIP_TOOL` normalization后才能发布COMPLETE，未完成分页/parse/aggregate/bounds验证不得进入registry；
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
- Host tool-surface owner通过`PreparedBuiltinCapabilitySourceInput`在同一surface lock下冻结exact-scope installed builtin binding inventory；adapter逐项join catalog并从binding侧穷尽构造facts，catalog-only dormant entries不进入snapshot；
- `DirectKernelToolPort`增加`PREPARING | SEALED | CLOSED` composition state与`seal_builtin_composition()`；seal前完成所有fixed/support port binding，seal后surface-changing bind typed拒绝，scope snapshot只投影sealed base tuple；
- 永久加入`inspect_new_mcp_tool`与`use_new_mcp_tool`；
- `list_mcp_servers`保留；
- 新fixed descriptor进入其closed scope允许矩阵并必须同时存在真实local executor binding；
- availability即使无MCP配置也保持descriptor存在；实际调用返回empty/unavailable；
- build generic tool facts的central adapter。

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
- global lowering、BASE_SYSTEM、tool-surface与source contract/domain version执行一次cold bump；activation只支持clean-v0/新Host epoch，不迁移或热改已安装epoch；
- 禁止internal identity/fingerprint进入provider body。

### 12.11 Continuity/runner/Host

- Host composition不直接手写registration tuple；
- Host创建一个exact-scope `CapabilityCompositionAttemptToken`，分别从tool-surface owner、resolved MCP config composition与Skill-root composition取得三项private-constructor prepared inventory carrier；
- 新增窄Host-facing `capability_composition.py` seam（或同等现有Host composition模块），负责以object identity验证同一attempt token、Builtin composition seal、current MCP resolved-config seal与exact six-root composition seal；这些opaque值不进入`capability/contracts.py`或fingerprint；
- 只有该seam能把验证后的三项semantic inventory交给registry internal pure factory并派生union；其他production调用点直接使用raw frozen inventory或caller tuple必须由architecture gate拒绝；
- Host必须在interaction/subagent/memory/MCP support与fixed meta binding完成后、任何capability/source/tool snapshot前seal Builtin composition；动态MCP safe-point refresh不属于Builtin unseal；
- capability registry snapshot与planning cut加入provider dispatch planning；
- Built-in退化discovery、MCP discovery与Skill scan在同一planning attempt下先各自freeze，再由pure registry factory合并；
- cold continuity candidate exact joinexposure plan、native-surface compatibility与current catalog lineage；
- installed epoch只以native-surface compatibility判定工具前缀复用；registry/route/catalog lineage变化只能驱动append-only source successor；
- safe-point current catalog变化只成为append-only source；
- cancellation/discard释放ref preparation/physical borrow；
- ROOT/child各有独立scope exposure。

### 12.12 Inspector/CLI

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
- source-kind/leaf-kind/origin closed matrix：Builtin snapshot只能接纳`TOOL/BUILTIN`，MCP只能`TOOL/MCP`，Skill root只能`SKILL`；
- exact duplicate source registration idempotent，同source不同registration fingerprint conflict；
- Host composition seam缺少三项named prepared inventory中的任一项、attempt token不同、owner seal stale/foreign、inventory scope不一致、source kind错位或遗漏真实owner registration均拒绝；重算一份内部一致但漏项的semantic inventory不能通过owner seal validation；
- internal pure registry factory继续拒绝inventory中的registration遗漏、同source两个snapshot或unregistered snapshot；只有Host composition seam可作为production caller；
- ROOT source snapshot不能复用于child registry；MCP scope visibility与current owner fact exact join；
- Built-in zero-I/O snapshot与scope-visible installed binding inventory byte-identical；catalog-only dormant descriptor不进入snapshot；binding缺catalog、descriptor mismatch或duplicate binding均拒绝；
- Builtin composition在PREPARING完成全部bind后seal；seal前snapshot拒绝，seal后late subagent/memory/MCP-support/fixed-tool bind拒绝，重复seal幂等，ROOT/child均投影同一sealed base；dynamic MCP refresh仍可运行且不改seal；
- complete empty与unavailable source snapshot严格区分；
- unavailable snapshot禁止携带facts；MCP分页/聚合未完成不得发布，但完整listing经include/exclude或`SKIP_TOOL`确定性过滤后仍发布COMPLETE并保留exact invalid count；
- source snapshot中的每个fact exact join同一source registration；
- pure registry snapshot跨三类source确定性排序，flattened view无第二份caller输入；
- Tool/Skill closed union穷尽；
- Plugin不是leaf kind；
- no public `Capability(ABC)`；
- identity与provider-mangled name分离；
- same MCP remote identity在same-schema reconnect后semantic fingerprint不变；
- meta ref exact绑定MCP execution policy fingerprint；policy变化使旧ref stale，same-schema/same-policy物理reconnect仍可rebind；
- unrelated MCP server status/instructions/count变化只改变scope-wide catalog projection，不改变未变tool的route fingerprint或重复inspect token；
- legacy Skill private tool/binary/service fields不进入generic fact、planner或MCP identity；
- mutabledict不能进入facts；
- duplicate identity/provider name拒绝；
- fingerprints fixed-point。

### 13.2 Planner tests

- cold builtin-only；
- planner只能消费registry snapshot，拒绝旁路builtin/MCP/Skill leaf tuples；
- 相同source snapshots无论producer构造顺序如何都得到同一registry/plan fingerprint；
- safe-point refresh构造successor registry但不修改旧snapshot；
- cold MCP cohort fit -> all direct；
- count overbound -> all MCP meta；
- byte overbound -> all MCP meta；
- existing exposure include/exclude缩小完整visible cohort后fit，excluded tool不进入meta；
- installed epoch无论current catalog变化都复用exact native surface；
- registry/route/Skill catalog变化只改变catalog lineage/exposure-plan fingerprint，不改变native-surface compatibility或触发cold reset；
- `CapabilityVersionRef/FrozenMcpToolExposure/FrozenMcpRouteProjection`字段与fingerprint golden固定，禁止status/policy/registry进入version或native compatibility；
- deterministic order与plan fingerprint；
- pure planner禁止I/O/import physical packages。

### 13.3 MCP lifecycle

- optional server在initial freeze前READY与freeze后READY都可用；overbound时均走meta，不出现时序性Host failure；
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

- current fixed roots先适配为source registrations，再由scan snapshot进入同一registry；
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
- provider actual-wire plan/continuity proof包含固定tool surface；
- foreign physical binding在provider open前拒绝。

### 13.7 Scope

- ROOT与child只看到各自允许的MCP facts；
- child不继承ROOT-only direct MCP；
- ref不能跨scope；
- Skill snapshots按exact scope注册/投影；
-一个scope新增MCP/Skill不改变另一scope native surface或catalog head。

### 13.8 Bounds与security

- 64 tool/1 MiB schema bound；
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
- Round 6 MCP全部retained；
- Round 7 ToolResult observation；
- Round 8 memory/permission；
- full PostgreSQL suite；
- Go tests/vet/module verify；
- clean-v0 fresh/repeat/deep verify。

### 13.10 Real provider dogfood

至少覆盖：

1. 一个cold direct stdio MCP tool，模型直接调用一次；
2. 一个late-ready或forced-meta MCP tool，模型读取observation后依次inspect/use；
3. provider actual tools数组在late-ready前后byte-identical；
4. meta invocation canonical attempt/result count精确为1；
5. 断连direct tool得到typed unavailable而非Host整体失败。

Dogfood不得记录API key、DSN、完整prompt、MCP arguments/body、headers、tool ref或secret。

---

## 14. Architecture gates

本轮activation必须证明：

- capability package不importMCP physical transport、repository、Host或compaction；
- Built-in/MCP/Skill均只通过一个pure registry factory进入planner；
- registration set只能由exact-scope Builtin/MCP-config/Skill-root三个owner-issued prepared inventory carrier在同一attempt下验证后派生；semantic inventory fingerprint不能替代owner completeness seal；
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
- no arbitrary provider tool gateway；
- no permission-based tool-surface mutation；
- New-MCP ref只绑定tool-specific semantic/policy/route proof，不绑定scope-wide catalog lineage；
- continuity compatibility只覆盖native surface；registry/route/catalog lineage变化不能触发same-epoch rebase；
- no compaction import；
- oracle保持`31 Committed / 23 Live / 13 subjects / 2 guards / 25 product relations / 1 durable job`。

---

## 15. 分片实施顺序

### Slice C0：机器基线

- 验证Round 7.1已ACTIVATED，冻结其public DTO/constant manifest、activation hash与retained node IDs；
- 保存pytest node-ID集合、architecture oracle、source enum、tool descriptors与module/FQCN manifest；
- 保存Chat/Responses fixed-prefix golden；
- 保存Round 6 direct MCP happy path；
-记录旧capability registry/exposure真实调用点，区分production与dormant。

### Slice C1：Pure semantics与命名减法

- 新建contracts/planner；
- 用pure frozen registry factory替换旧mutable descriptor registry；
- 三项owner-issued process-local inventory admission carrier、derived semantic registration set、execution-backed zero-I/O Built-in snapshot与scope/leaf closed admission；
- Builtin composition seal与四个closed version/route/projection DTO；
- tool/skill facts与adapters；
- 删除开放行为基类可能性；
- skill-only/tool-only旧名收窄；
- `SKILL_CATALOG` clean rename；
- pure unit tests通过。

### Slice C2：MCP exposure planning

- MCP server registrations、per-server source snapshots与projection refs；
- 完整raw discovery与既有normalization policy的COMPLETE/UNAVAILABLE分界；
- cold all-or-none cohort；
- installed surface reuse；
- catalog DIRECT/NEW；
- physical exact join；
- Round 6 retained通过。

### Slice C3：Meta gateway

- fixed descriptors；
- inspect/tool-specific route ref/use；
- policy-bound ref与policy-change stale semantics；
- permission/effect/attempt/result；
- direct unavailable/schema replacement；
- ACK/cancel/close tests。

### Slice C4：Skill relationship

- current Skill root registrations、complete scan snapshots与Skill adapter；
- minimal Skill versions与catalog/active projection join；
- legacy private metadata不进入generic capability语义；
- honest Skill names；
- catalog/active placement与prefix；
- 不实施Round 9.1 parser。

### Slice C5：Activation证据

- targeted、full、PostgreSQL、architecture、Go；
- real provider direct/meta dogfood；
- README/Gap Index更新；
- activation evidence hashes与oracle；
- 标记Round 9 ACTIVATED后，Round 9.1才可编码。

---

## 16. Definition of Done

以下全部成立才可激活：

1. `Capability`拥有本文唯一语义定义，代码中不存在公开行为型generic base class。
2. Built-in、MCP与Skill共享source registration、complete source snapshot、leaf admission与frozen registry流程；registration set由三个既有owner的exact-scope complete inventory派生，不能由调用方手写不完整集合。
3. Built-in以IMMUTABLE零I/O、execution-backed退化discovery接入，且在Host open显式seal composition；catalog-only dead descriptor与seal后的late binding不进入provider surface。MCP/Skill以SAFE_POINT_REFRESHABLE discovery接入，三者均不能旁路registry进入planner。
4. Built-in与MCP共享`FrozenToolCapabilityFact`，但各自execution authority不变。
5. Skill是独立`FrozenSkillCapabilityFact`，没有executor。
6. Plugin未进入leaf union、runtime或provider surface。
7. 当前legacy Skill仅通过adapter接入；其private tool/binary/service metadata不进入generic fact/planner，Agent Skills standard与彻底删除这些字段明确留给Round 9.1。
8. Cold MCP cohort在完整fit时all direct，overbound时all meta；builtins不受影响。
9. Late-ready MCP只追加catalog并经inspect/use执行。
10. Direct MCP断连/dirty/schema replacement不修改native tools且不能用meta绕过。
11. `NewMcpToolRef` exact绑定inspect时展示的真实MCP policy与tool-specific NEW route，但不绑定scope-wide catalog；无关server变化保持token稳定，真实tool/policy/route变化要求重新inspect。`use_new_mcp_tool`只有一次canonical attempt/result。
12. `MCP_CATALOG`使用唯一closed、untrusted renderer：小catalog完整列名，overbound只按完整row确定性截断并给出exact omitted counts与分页指引；不使用ranking，不泄露内部identity。
13. `list_mcp_servers`以closed server/tool page union完整表达DIRECT/NEW/UNAVAILABLE、status/resources/prompts、exact counts与pagination；cursor exact绑定catalog/native surface/scope/filter/page/limit/offset且只读local snapshot。Requested limit是maximum，successful page标记Round 7.1 `FULL_REQUIRED/MCP_DIRECTORY_PAGE`，必须完整安装后才算交付，不得用HEAD_TAIL/COMPACT伪装成功分页。
14. `inspect_new_mcp_tool`只在完整closed DTO满足Round 7.1普通ToolResult logical 40,000-byte边界后准备route/policy-bound dormant ref；successful result标记`FULL_REQUIRED/MCP_INSPECT_SCHEMA`，只有exact FULL continuity install后ref才callable。Schema不截断、不artifact化，所有拒绝均不进入remote MCP lane。
15. 已进入DIRECT的MCP在known-down时返回固定typed说明；same-schema reconnect恢复，schema replacement只等待cold adoption且不能meta绕过。
16. `CAPABILITY_CATALOG`已彻底clean rename为`SKILL_CATALOG`。
17. 当前generic Skill类和tool-only类名实相符。
18. Planner pure、bounded、deterministic、无physical/durable authority。
19. Provider preflight exact joinplan surface与current physical access。
20. ROOT/child scope、foreign ref、foreign borrow均fail closed。
21. same epoch SYSTEM/tools不变、messages只追加suffix；catalog lineage变化不得被误作native-surface compatibility变化。
22. 没有新增schema、event、job、guard、subject、receipt、checkpoint、projection或repair。
23. Oracle保持`31/23/13/2/25/1`。
24. Round 7.1已ACTIVATED且其public contract manifest/activation hash exact匹配；Round 3/3.1/5A/5A.1/6/7/7.1/8 retained与全量tests通过。
25. Round 9.1不再需要从Round 5B反向导入MCP meta DTO。

---

## 17. 下游边界

### 17.1 Round 9.1

Round 9.1只负责：

- Agent Skills canonical manifest；
- standard resources/progressive disclosure；
- 六个default Skill root的owner-specific bindings与dynamic scan；
- ordinary `read_file` progressive disclosure、2,000-line default window与无content-suppressing dedup；
- Agent Skills标准filesystem与activation；
- 删除legacy Pulsara metadata且不引入namespaced替代；
- append-only Skill catalog/active body。

Round 9.1必须让每个default root先适配为本文的`SAFE_POINT_REFRESHABLE` source registration，再把complete scan结果作为source snapshots进入同一个registry。它必须复用本文的identity、fact、registry、Skill versions与planner，不能创建Skill-private registry、MCP identity、dependency graph或第二个meta gateway。

### 17.2 Round 9.2 Plugin

后续[Round 9.2](ROUND_9_2_AGENT_PLUGIN_BUNDLE_AND_HOOK_LIFECYCLE_IMPLEMENTATION_SPEC.zh.md)固定Plugin是bundle/source：

~~~text
Enabled Plugin
  -> Skill root source registrations
  -> MCP server source registrations
  -> process-local Hook definitions
  -> dormant Subagent-spec inventory
  -> normalize into existing Round 9 source-registration set
  -> existing source owners discover and publish ordinary snapshots
~~~

Plugin不成为第三种tool binding，也不扫描任意private cache。Hook不进入本文capability leaf union；它由Round 9.2独立的process-local lifecycle owner执行。Subagent spec在PHC-10完成层次化/批量编排前只允许dormant discovery。具体portable manifest、Codex/Claude compatibility、namespace、enablement、trust与dynamic invalidation由Round 9.2冻结。

### 17.3 Round 5B

未来compaction/rebase只能消费本文已经存在的：

- current `FrozenCapabilityRegistrySnapshot`、其registration/source snapshots及owner-specific MCP/Skill projections；
- current `FrozenCapabilityPlanningCut`与exposure result；
- installed direct tool exposure；
- current MCP catalog；
- current Skill projection/source heads。

Compaction可以在cold successor boundary重新选择MCP direct cohort，但本文不实施promotion/adoption。Round 5B不得复制成`CompactionCapabilityGraph`或durable exposure receipt。

---

## 18. 产品示例

### 18.1 Built-in与cold MCP

~~~text
Host cold open
-> obtain and validate exact-scope builtin/MCP-config/Skill-root owner-issued prepared inventories
-> derive immutable builtin、docs MCP server与current Skill root registrations
-> builtin zero-I/O snapshot emits read_file / terminal / memory_search facts
-> docs MCP discovery snapshot emits 4 tool facts
-> Skill scan snapshots emit current Skill facts
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
  <- derived only after three exact-scope owner-issued prepared inventories pass current-seal admission
  -> scope-bound complete CapabilitySourceSnapshot
  -> pure FrozenCapabilityRegistrySnapshot
  -> FrozenCapabilityFact views
  -> pure ExposurePlan
  -> provider channel
  -> TOOL only: existing local gate/binding/execution owner
  -> SKILL only: catalog/activation
~~~

Built-in与MCP统一为Tool capability，Skill作为Instructional capability引用Tool，但不拥有Tool。三者的注册、snapshot与leaf admission逻辑完全共用：Builtin只把discovery退化为对execution-backed inventory的immutable pure snapshot，MCP/Skill则允许safe-point refresh；三项expected source集合分别来自现有composition owner签发的complete inventory，而不是generic caller自报。Plugin未来只作为Bundle/Source registration contributor加入。这个形状既能为Round 9.1提供稳定依赖，也能让Round 5B在真正需要rebase时消费当前事实，而无需恢复任何durable capability/recovery graph。

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
- [Round 6](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md)：MCP supervisor、bounded discovery、dirty fence、effect、scope与direct execution；
- [Round 5B draft](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)：其中§2的non-compaction direct/meta设计被本轮提取；Round 5B后续应删除重复实现，只保留rebase promotion消费；
- [Round 9.1](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)：本轮激活后的直接下游。
