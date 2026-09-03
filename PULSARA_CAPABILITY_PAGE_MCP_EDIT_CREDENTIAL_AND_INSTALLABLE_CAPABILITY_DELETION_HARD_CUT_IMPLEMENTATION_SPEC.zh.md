# Pulsara 能力页、模型协作式 MCP/Plugin 管理与可安装能力删除 Hard-cut 实施规范

> 状态：**IMPLEMENTATION-READY / NOT ACTIVATED**
>
> 定稿日期：2026-09-03。
>
> 本文是能力管理产品面的前向实施权威。代码激活前，现有 production 行为仍以
> [`PULSARA_FRONTEND_APPLICATION_SPEC.zh.md`](PULSARA_FRONTEND_APPLICATION_SPEC.zh.md)、
> [`PULSARA_LOCAL_SKILL_INSTALLATION_PRODUCT_COMPLETION_SPEC.zh.md`](PULSARA_LOCAL_SKILL_INSTALLATION_PRODUCT_COMPLETION_SPEC.zh.md)、
> [`ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md`](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md) 与
> [`ROUND_9_3_AGENT_PLUGIN_BUNDLE_AND_HOOK_ADAPTER_IMPLEMENTATION_SPEC.zh.md`](ROUND_9_3_AGENT_PLUGIN_BUNDLE_AND_HOOK_ADAPTER_IMPLEMENTATION_SPEC.zh.md)
> 为准；实施完成后必须同步这些 active specs，只保留本文规定的单一路径。
>
> Fingerprint subtraction 继续服从
> [`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。

---

## 0. 一句话产品结论

Pulsara 的“能力”页成为用户拥有的可安装能力控制面：local MCP使用同一份结构化表单完成添加与直接编辑，Plugin-provided MCP保持definition只读但允许随时管理instance credential，所有凭据只由用户在该页面提供；主模型通过唯一的typed `manage_capability`工具参与MCP与Plugin管理，runtime依据当前permission与候选完整性自动选择直接执行或复用同一表单请用户完成；任何能由Pulsara安装到某个作用域的loose Skill、local MCP或capability bundle/Plugin，都必须在同一作用域提供真实删除。

这是一条产品承诺，而不是建立抽象的 `GenericCapability`：

```text
统一用户体验
  installable => inspectable => disableable（适用时）=> deletable

仍由三个现有 owner 分别实现
  Loose Skill filesystem owner
  Native MCP config/supervisor owner
  Managed Plugin package/state owner
```

不得新增 capability 数据库、统一 registry、万能删除器、durable operation job、receipt、tombstone、repair graph 或后台 reconciliation。

---

## 1. 已冻结的产品决策

### 1.1 能力页是用户拥有的控制面；模型可以协作但不持有秘密

1. 用户可以在能力页添加、查看、启停、编辑local MCP，为Plugin-provided MCP管理凭据，并删除可安装能力。
2. Local MCP 与 Plugin-provided MCP 的 secret 均由用户直接交给本地能力管理控制面；不得经过主模型、subagent、terminal command、conversation message、canonical transcript、tool arguments/result、provider input、memory、Hook stdin/context 或普通诊断。
3. 模型只可知道认证种类、需要哪个非秘密 binding 名称，以及“未配置 / 已配置 / 需要更换”；模型永远拿不到 secret value。
4. MCP installer Skill可以完成公开endpoint、transport、scope与普通非秘密配置；遇到需要用户凭据或其他用户独占选择时，必须让runtime唤起能力页同款本地表单，而不是要求用户自行找入口或在对话中粘贴key。
5. “用户拥有”不等于“模型只能给教程”。主模型可以通过一个ROOT-only typed tool提交MCP/Plugin安装、编辑、启停或删除意图；当信息完整且当前permission允许时，operation可以直接完成。
6. 当需要secret、缺少用户独占选择、existing product trust review或当前permission要求确认时，runtime必须自动打开与能力页相同的表单；模型不能通过参数强制跳过，也不能把“是否简单”作为自报字段。
7. 用户在表单中修改并提交后的exact值才是最终mutation request。模型的草稿只是prefill，不是authority；secret从browser-local form直接进入local controller/credential owner，不回流给模型。

### 1.2 MCP 可以直接编辑

MCP config 是用户配置，不是不可变软件包。添加与编辑共用一套 structured editor；编辑保存是同一 `server_id`、同一配置来源内的 whole-entry replacement，不是字段级 merge。

- `server_id` 是 native routing identity，不可原地修改；变更 ID 等价于删除后重新添加。
- `USER / WORKSPACE` 配置来源不可在编辑时偷偷迁移；移动作用域等价于在目标作用域新建，再由用户单独删除原项。
- endpoint、stdio command/args/cwd、认证、启用状态、subagent 可见性、exposure、effect 与物理运行参数均可编辑。
- Skill 不新增网页代码编辑器；用户仍可直接编辑其文件目录。
- Plugin package 仍是 immutable managed bundle；不允许在能力页修改包内 Skill/MCP/Hook 定义，更新继续走 exact replace/install。
- Plugin-provided MCP 的 credential binding不是package definition。用户可以在能力页为exact Plugin instance/MCP component配置、更换或清除Bearer、秘密Header或stdio secret env；endpoint、command、args、cwd、public headers与其他包内字段保持只读。

### 1.3 所有“可安装对象”必须可删除

本文中的安装对象只有：

```text
LOOSE_SKILL
  USER      ${PULSARA_HOME}/skills/<name>
  WORKSPACE <workspace>/.pulsara/skills/<name>

LOCAL_MCP_SERVER
  USER      ~/.pulsara/mcp.yaml 中一个 native entry
  WORKSPACE <workspace>/.pulsara/mcp.yaml 中一个 native entry

CAPABILITY_BUNDLE_PLUGIN
  USER / WORKSPACE 中一个 managed Plugin instance state
```

以下不是独立可删除安装对象：

- Pulsara 随发行版提供的 bundled Skill 或 builtin tool；
- Plugin 内部贡献的某个 Skill、MCP server 或 Hook；删除它们的产品单位是整个 Plugin。Plugin MCP 的instance credential可以单独配置或清除，但这不等于删除component；
- 从其他作用域继承而来、当前页面不拥有的能力；
- process-local Host override；
- 已经进入 canonical transcript、provider input 或历史 ToolResult 的内容。

这些行不渲染删除按钮，不能显示一个无后端动作的“不可用”按钮。

### 1.4 删除不是关闭，也不是历史抹除

```text
关闭 / disable
  保留安装与配置，只阻止未来采用或执行

删除 / remove / uninstall
  从该作用域的 canonical local source 移除安装对象
  清理只属于该对象的附属管理状态
  阻止 future admission
  允许已经取得 authority 的旧 physical consumer 安全 drain
```

删除不会：

- 修改或抹除已有会话记录、provider replay、ToolResult、memory 或 compaction summary；
- 撤销远端已经发生的 MCP tool effect；
- 删除远端账户、吊销供应商 API key 或修改供应商账单；
- 删除安装来源目录；
- 因为 UI 需要而重建同一 epoch 的 provider input prefix。

### 1.5 Plugin data 不与 capability 删除混为一谈

删除 Plugin instance 必须移除其 future Skill/MCP/Hook contribution，删除只属于该instance的managed MCP credential bindings，并使 immutable package root 在旧 consumer drain 后可由现有 GC 回收。现有 `PLUGIN_DATA` 可能包含用户工作数据，默认继续保留；本轮不增加“同时删除数据”复选框，也不把普通 Plugin 删除冒充数据擦除。

Plugin MCP credential是可重新取得的连接认证附属状态，不是Plugin工作数据：disable时保留，remove时删除。Environment reference由用户/launcher拥有，remove只删除引用，不修改环境变量本身。

如果未来需要删除 `PLUGIN_DATA`，必须作为单独、明确、不可误触的数据删除产品动作规范；不能暗中级联进本轮统一 capability 删除。

### 1.6 一个模型工具，两种合法完成路径

本轮新增且只新增一个模型侧管理入口：`manage_capability`。它是现有MCP config、Plugin management、credential与live-adoption owners的窄facade，不是`GenericCapability`、第二套mutation service或万能文件写入工具。

同一tool call只有两种合法完成路径：

```text
DIRECT
  closed candidate完整
  不需要secret或其他用户独占输入
  existing product review已满足
  当前FrozenRunPermissionSnapshot允许本次exact effect
  => typed owner mutation => runtime automatic adoption => sanitized ToolResult

USER_FORM
  需要secret / 缺少用户选择 / 需要existing product review
  或当前permission要求用户确认
  => 打开shared capability editor/review
  => 用户可检查、补齐或修改
  => 用户提交exact request
  => typed owner mutation => runtime automatic adoption => sanitized ToolResult
```

二者必须汇入同一个typed operation与同一个live adoption owner。不得让DIRECT shell out到CLI、让USER_FORM拼YAML，或为模型、Web页面各实现一条写入路径。

`READ_ONLY`下模型调用不得直接修改能力；runtime可以打开一个由用户拥有的prefilled editor。该表单提交是新的显式用户控制面动作，模型tool call只等待并观察其non-secret settlement，不能把它伪装成模型获得了write permission。`ASK_PERMISSIONS`、`ACCEPT_EDITS`与`BYPASS_PERMISSIONS`继续完全服从现有permission preset；本文不发明第五种mode或capability-specific bypass。

Plugin仍保持install-disabled、optional credential overlay、enable review三个独立settle。`manage_capability`可以编排连续UI，但不能把三种authority折叠为一个“安装并信任”布尔值。一个公开配置完整的Plugin可以在permission允许时直接安装为disabled；`SET_PLUGIN_ENABLED(enabled=true)`始终进入USER_FORM，由用户review current exact package后产生call-local acceptance。Disable仍按实际effect与permission正常分流。

---

## 2. 当前代码真源与具体缺口

### 2.1 用户能力页

当前 [`frontend/components/capability-view.tsx`](frontend/components/capability-view.tsx)：

- 有 Plugin、MCP、Skill 三个 tab；
- MCP 只可填写基本 HTTP/stdio 字段并添加，不能编辑，也不能删除；
- Skill 可安装、启停，不能从 UI 删除；
- Plugin 已有启停与移除确认。

当前 [`src/pulsara_agent/web_app/http_server.py`](src/pulsara_agent/web_app/http_server.py) 与
[`src/pulsara_agent/web_app/session_controller.py`](src/pulsara_agent/web_app/session_controller.py)：

- 有 user Skill install/enable；无 typed removal；
- 有 user MCP create/enable；无 update/remove；
- 有 user Plugin install/enable/remove。

项目能力面已有 workspace MCP removal，但 workspace Skill 仍只有安装/启停；因此“能安装就能删除”尚未成立。

### 2.2 Loose Skill owner

当前 `LocalSkillManagementService` 只有：

```text
validate_local_skill_source
install_loose_local_skill
inspect_effective_skill_catalog
```

直接从文件系统 copy/edit/delete/rename 本来就是合法真相，但 Web/CLI 尚无 descriptor-safe typed delete operation。能力页不能自行调用 `rm` 或实现第二套路径校验。

### 2.3 MCP auth 与管理面

当前 native MCP config 已支持：

```text
NoAuth
BearerEnvironmentRef
StaticHeaderEnvironmentRefs
StdioTransportConfig.secret_environment_refs
```

HTTP transport 会在 physical open/request 时解析 secret reference；stdio transport 会把引用解析为 child environment。Raw value 不进入 config repr 或普通 fingerprint。

但 `pulsara mcp add`、用户能力页、项目能力面都没有表达 auth/header/secret-env 的字段，因此当前状态是：**Kernel transport 能力存在，正式安装产品链路不完整。**

`UnsupportedOAuth` 仍是明确的 unsupported branch。本文不恢复 OAuth，不渲染 OAuth 控件，也不实现登录回调、token refresh 或 OAuth credential lifecycle。

### 2.4 Plugin removal

现有 `remove_local_plugin` 已以 instance state unlink 作为 future contribution authority cut；旧 package root 由 physical lifetime anchor 保持到 consumer drain，再由现有 GC 回收。本文复用并补齐 UI stale guard，不新建第二套 Plugin uninstaller。

`PluginStoreLayout`把USER与WORKSPACE两种namespace的package/state/lock都放在`PULSARA_HOME/plugins/...`；WORKSPACE只决定identity与可见范围，不表示物理写入落在workspace目录。Permission classification必须按这个physical truth判定为outside-workspace write。

现有`SetLocalPluginEnabledRequest`要求`enabled=true`与`ExternalProcessAcceptance.ACCEPTED`exact同行；CLI只在展示current package summary并收到用户确认后构造该call-local value。模型参数不能成为这项acceptance，因此model-assisted enable必须落到用户review form，而不是由permission的DIRECT verdict单独替代。

当前 Agent Plugins 1.0 `mcp.json` 只表达literal `env`/`headers`，没有credential slot schema；现有 `PluginMcpAdapter`也把所有Plugin MCP归一化为`NoAuth`。因此带API key的Plugin MCP目前没有正式用户配置路径。实现不得谎称package manifest已经声明secret slot，也不得把用户secret回写immutable package；本轮增加的唯一新truth是Plugin instance state中的non-secret credential overlay与local credential store中的value。

### 2.5 当前 permission、interaction 与 reload 真源

当前代码已经有且只应保留一套run permission authority：

```text
FrozenRunPermissionSnapshot
  -> BuiltinToolCallClassifier
  -> PolicyPermissionGate
  -> ToolDispatchAuthorizationPolicy
  -> ALLOW | DENY | REQUIRE_CONFIRMATION
```

[`src/pulsara_agent/primitives/permission.py`](src/pulsara_agent/primitives/permission.py)定义四种preset，普通run默认`BYPASS_PERMISSIONS`；`READ_ONLY`禁止write/terminal，`ASK_PERMISSIONS`逐次询问，`ACCEPT_EDITS`允许workspace write但对outside-workspace write与terminal询问，`BYPASS_PERMISSIONS`允许普通host-local write/terminal。本文只把capability action的exact effect接入这张现有表，不改变preset语义。

当前`DefaultBuiltinToolCallClassifier`只读取raw model arguments与static action override，`PolicyPermissionGate._filesystem_write_access()`只从`arguments["path"]`推导inside/outside；READ_ONLY的non-read-only call会在`DirectKernelToolPort.authorize()`成为终局DENY，而只有REQUIRE_CONFIRMATION才进入interaction。由于`manage_capability`禁止模型提供mutation destination path，实施必须先由trusted preparation owner解析target/effect，再窄扩展这条现有链；不能靠descriptor标成read-only或把DENY粗暴改成ALLOW来弹表单。

当前[`src/pulsara_agent/conversation_kernel/interaction.py`](src/pulsara_agent/conversation_kernel/interaction.py)与live-control/protocol只拥有process-local单一current tool confirmation，resolution仍是通用allow/deny；它尚不能承载editable capability form或secret-safe submission。这是需要扩展的真实缺口，但不构成新增durable interaction subsystem的理由。

当前`Host.reload_capabilities`已经明确same-epoch no-rebase，Web的若干user mutation也会通知live sessions；`reload_capabilities` builtin则是ROOT/bypass的显式process-control adapter。缺口不是再造refresh，而是让所有first-party typed mutation统一自动进入已有refresh owner，并把model tool保留为out-of-band/partial recovery。

现有workspace路径只由`LocalSessionController._mark_workspace_capability_refresh()`登记请求，并在`_workspace_adoption_payload()`公开为`next_user_turn`；`KernelHostSession._adopt_project_capabilities_if_requested()`也只在prompt delivery取得下一条root prompt后执行。这个时点不足以支持模型在同一turn完成“安装 → 验证”，因此本规范明确把它hard-cut为“下一次provider dispatch前的合法safe point”，同时保留old surface borrow drain与same-epoch prefix continuity。

---

## 3. 范围与非目标

### 3.1 本轮必须完成

1. 用户能力页 MCP add/edit 共用完整 structured form。
2. 当前 native MCP 能力中适合用户配置的绝大多数选项进入常用或高级区。
3. 用户独占的 managed MCP secret 输入与环境变量引用两条路径。
4. Plugin-provided MCP在MCP列表与Plugin详情中共用同一credential editor；package definition保持只读。
5. User 与已有 project management surface 的 Skill/MCP 删除闭合。
6. User Plugin 删除补齐 exact stale guard 与统一文案；workspace Plugin CLI 继续使用现有 remove owner。
7. 新增唯一ROOT-only `manage_capability` builtin tool，让主模型以closed actions参与local MCP与Plugin管理；它不得接收secret或任意YAML/shell payload。
8. `manage_capability`接入现有frozen run permission、call classifier、permission gate与same-Host interaction owner，自动决定DIRECT或USER_FORM，不新增permission mode或旁路。
9. 能力页与模型唤起的表单复用同一component、schema projection、typed controller operation与stale guard；用户提交可覆盖模型prefill。
10. First-party Web/tool mutation成功后由runtime直接调用existing live capability refresh/safe-point adoption owner；不得要求模型例行补调`reload_capabilities`。
11. MCP/Plugin installer Skill明确direct/form分流、用户凭据边界与automatic adoption；`reload_capabilities`仅作为out-of-band变更或明确adoption attention的补救措施。

### 3.2 本轮明确不做

- raw YAML editor；
- MCP OAuth；
- MCP elicitation、sampling、roots、Apps/Tasks 或 server logging 新产品面；
- 在能力页直接执行 MCP tool/resource/prompt；
- Skill 文本/资源在线编辑器；
- Plugin 包内 component definition editor；
- Plugin data deletion；
- remote secret acquisition、API key 创建、轮换或供应商账号管理；
- capability history、undo log、trash timeline、soft-delete rows；
- PostgreSQL schema/event/subject/guard/relation/job 增长；
- provider、compaction、memory、task、Hook execution或terminal语义变化；
- 新permission mode、修改现有四种permission preset含义、capability专用permission gate或模型自报“simple/trusted”绕过；
- 通过解析terminal command、watch filesystem或轮询来猜测能力配置是否变化；
- 任意 provider-specific MCP auth branch。

---

## 4. 能力页信息架构

### 4.1 保持轻量列表

三个 tab 继续使用当前产品名称：

```text
插件 | MCP | 技能
```

列表只显示用户决策所需信息。高级 config、完整 tool schema、internal provider name、tool ref、fingerprint、generation、package path 和 secret reference carrier 不常驻列表。

### 4.2 MCP add/edit 共用 editor

点击“添加 MCP”打开空 editor；点击一个 owned MCP 的“编辑”打开同一 editor，并填入完整非秘密配置。

`manage_capability`需要用户参与时也打开这个editor，而不是另做一张简化确认卡：

- model给出的公开配置只作为prefill；字段仍然可编辑；
- 因permission而需要确认、但candidate已经完整时，editor以compact review状态打开，仍允许用户展开完整配置；
- 需要secret或缺少用户独占选择时，直接聚焦相应字段并解释缺少什么，不要求用户重新填写模型已经可靠提供的公开字段；
- 用户提交走与能力页手动添加/编辑完全相同的controller operation；取消则产生typed `CANCELLED` ToolResult，让模型继续自然收尾；
- 表单不得包含“允许模型以后都这样做”“将此MCP视为安全”或其他新的持久授权控件。

Editor 分为两层：

#### 常用配置

- 显示名称；
- 连接方式：Streamable HTTP / 本地命令；
- HTTP endpoint，或 stdio command 与 ordered args；
- 认证：无需认证 / Bearer Token / 自定义秘密 Header；
- 是否启用；
- 是否向 subagent 提供。

#### 高级配置

- 使用 Pulsara 本地凭据或引用环境变量；
- stdio working directory；
- stdio ordinary environment 与 secret environment bindings；
- tool include/exclude；
- invalid tool policy；
- server default effect 与 per-tool effect overrides；
- server timeout 与 per-tool timeout；
- catalog refresh interval；
- private network / localhost HTTP 明确允许；
- `required`；
- parallel tool calls 与 proved-stateless HTTP declaration；
- stateless maximum in-flight。

高级区默认折叠。所有数值范围、组合限制继续由唯一 native parser 校验；前端可提供即时提示，但不得成为第二套 authority。

### 4.3 Plugin-provided MCP credential editor

Plugin-provided MCP与local MCP出现在同一MCP列表，并明确标注来源Plugin。它不进入local MCP完整编辑模式：

```text
只读
  display name / Plugin name
  endpoint 或 command/args/cwd
  package public headers / ordinary environment
  exposure/effect/timeout/concurrency等package-derived配置

可编辑
  HTTP：无需认证 / Bearer / custom secret headers
  stdio：secret environment bindings
```

Agent Plugins 1.0没有credential-slot declaration，Pulsara不得根据变量名、Header名或错误字符串猜测secret。用户依据Plugin文档或连接要求，显式选择closed auth kind并填写non-secret Header/env target name；secret value仍只进入local credential store。

当overlay target与package public Header/ordinary env同名时，credential overlay只在effective runtime覆盖该值；editor必须展示“将覆盖插件默认值：<field name>”。Package原文、inspection summary与managed package bytes保持不变。

该配置是以`scope + plugin_id + local_server_id`定位的Plugin instance credential overlay，不修改`mcp.json`、managed package root或portable component summary。Plugin详情中的“配置连接”和MCP列表中的“配置凭据”必须打开同一个editor、调用同一个operation，不能形成两份truth。

这些可管理行来自installed Plugin inspection，不以当前live MCP catalog为唯一来源：Plugin默认disabled或暂时连接失败时仍能先配置credential，并标注“插件已关闭”或真实连接状态；它不会因此被发布给模型。Invalid/skipped component没有可执行definition，不渲染伪credential editor，只在所属Plugin诊断中说明。

Plugin UI可以把下列过程呈现为一个连续向导，但Kernel/API必须保持三个有序且独立settle的operation：

```text
1. validate/copy/install Plugin，创建exact disabled instance
   - install request schema不接受任何secret
2. 配置Plugin MCP连接（可跳过）
   - 对已存在的instance调用credential-overlay operation
3. review并enable exact Plugin instance
   - 显示endpoint/command与non-secret credential presence，不显示value
```

因此用户可以在视觉上的“安装过程”中紧接着填写key，但secret永远不是package validation/copy/install transaction的输入，也不是安装成功的前提。Install未成功或instance尚不存在时，UI不得提前保存Plugin credential。用户跳过credential步骤后仍可选择enable；缺少认证只使exact MCP显示“需要凭据”或真实connection failure，Plugin的其他Skill/Hook不因此被伪装成未安装。用户也可以暂不enable，之后随时从能力页继续配置。

这条分阶段边界只适用于Plugin。Standalone local MCP创建的就是其canonical config entry，“添加MCP”可以在同一create form中接收credential mutation，但仍遵守secret不进入YAML/model/log的边界。

### 4.4 不向用户展示内核字段

以下字段只可作为 opaque request guard 或内部诊断使用，不显示为表单项：

```text
semantic_config_fingerprint
runtime_config_fingerprint
resolved_config_identity
workspace_approval_identity
physical_lifetime_anchor
provider-mangled tool name
opaque tool_ref
connection generation / slot identity
```

### 4.5 认证状态文案

列表只使用产品文案：

```text
无需认证
需要凭据
已配置凭据
环境变量不可用
正在连接
已连接
连接失败
已关闭
```

不得显示 secret slot、环境变量值、Header 值、Keychain account、内部异常 class 或 request dump。

### 4.6 删除入口与确认

每个 owned installable row 在展开详情或 overflow menu 中提供一个明确动作：

```text
Skill   删除技能
MCP     删除连接
Plugin  卸载插件
```

第一次点击进入同一行/面板内确认；确认期间明确列出影响，不使用浏览器原生 `confirm()`。

- Skill：说明将删除 Pulsara 管理根中的完整 Skill 目录，不影响最初的安装来源；若位于 `~/.agents`，额外说明其他兼容 agent 也可能不再看到它。
- MCP：说明当前作用域中的连接配置及由 Pulsara 保存的对应凭据将被删除，远端 key 不会被供应商吊销。
- Plugin：说明该 Plugin 提供的 Skill、MCP 与 Hook 会一并从 future capability view 消失；Plugin data 默认保留。

禁用、loading、stale 或失败状态不能悄悄把删除变成无动作按钮。可重试的失败必须显示原因并保留用户重新确认/刷新路径。

---

## 5. MCP secret 产品边界

### 5.1 Closed secret reference

Native local MCP config与Plugin instance credential overlay都只持有引用，不持有可打印 secret。实现 hard-cut 到以下 closed source union；命名可以按现有模块风格微调，但语义不得改变：

```text
McpSecretReference
  EnvironmentVariableReference(name)
  ManagedLocalCredentialReference(binding)
```

HTTP auth 使用：

```text
NoAuth
BearerSecret(reference)
StaticHeaderSecretReferences(
  case-insensitive unique header name -> reference
)
```

stdio transport 使用：

```text
ordinary_environment:
  child env name -> non-secret literal

secret_environment:
  child env name -> McpSecretReference
```

不得支持 raw secret string 进入 `McpServerConfig`、YAML、fingerprint payload 或 public DTO。

### 5.2 Managed local credential

默认“Bearer Token / 自定义秘密 Header / stdio secret env”的输入由本地 Web controller 直接写入 supported OS credential store。当前 macOS distribution 使用 Keychain；不得把 secret 改存 PostgreSQL、`mcp.yaml`、Plugin state、Skill 文件或普通 `.env`。

Credential binding 由以下非秘密 identity 唯一确定：

```text
credential owner
  LocalMcpOwner(configuration scope identity, server_id)
  PluginMcpOwner(plugin scope identity, plugin_id, local_server_id)
binding kind (bearer | header:<normalized-name> | env:<target-name>)
```

- Local `server_id`、Plugin `plugin_id + local_server_id`与scope均不可直接编辑，因此binding不随显示名称变化；
- Plugin binding不包含`package_install_id`：同一Plugin component的合法package replace可以保留凭据，但replacement必须在采用前重新验证transport kind与reserved names；
- workspace binding 使用现有 stable workspace identity，而不是把任意绝对路径当 secret key；
- 不增加 credential registry 或数据库映射表；
- credential store 只返回 present/missing 与 call-local secret value；
- Python/runtime 不虚构内存零化保证，但必须缩短值的可达 lifetime，禁止进入 repr/log/event/trace。

在不支持 managed credential store 的平台，UI 不渲染“由 Pulsara 保存”选项，只渲染真实可工作的 environment reference；不得显示不可用按钮。

### 5.3 Environment reference

高级用户可选择“引用环境变量”，只填写变量名。能力页可以显示当前 Host 观察到“可用 / 不可用”，但不得返回或显示值。

- `reload_capabilities` 只能重新解析当前 Host 已拥有的环境；child terminal 中的 `export` 不能修改 parent Host。
- 如果变量是通过新的 env file 或外部 launcher 配置，用户需要重启 Host；UI 必须诚实说明，不能承诺 reload 会摄取另一个进程的新环境。
- `PULSARA_API_KEY` 不得被引用为 MCP credential；模型 provider credential 与 MCP credential 必须保持不同 authority。

### 5.4 Secret mutation

现有 secret 永不回显。Editor 只呈现：

```text
未配置
已配置
更换
清除
```

- 未触碰 secret field 的普通编辑必须保留原 binding/value；
- 输入新值并保存才替换；空白 placeholder 不代表清除；
- 清除需要明确动作；enabled config 在 required secret 被清除后成为“需要凭据”，不得匿名 fallback；
- custom Header name 大小写归一后必须唯一；禁止用户覆盖 MCP protocol-owned `Host`、`Content-Type`、`Accept`、`Mcp-Session-Id` 等 transport headers；
- Plugin MCP credential overlay可以显式覆盖package已经声明的同名public Header或ordinary stdio env，这正是为immutable package补充instance secret的必要运行时语义。优先级固定为`package public/ordinary value < user credential overlay < MCP protocol-owned fields`；UI必须明确显示被覆盖的non-secret field name，不能静默覆盖。Protocol-owned Header仍不可配置；
- secret 不得放入 endpoint path/query。Pulsara 不按字符串形状猜测 key，但 UI、Skill 与文档必须只引导 Header/secret-env 方案。

### 5.5 Credential failure 与删除

Credential lookup missing/denied/unavailable 必须在 physical connect 前形成 typed, non-secret failure；不得发起匿名请求或把 value 填入错误文本。

删除 local MCP 的 canonical authority cut 是先原子移除 exact config entry，再停止 future admission；删除Plugin的canonical authority cut仍是先移除exact instance state。随后由同一个request owner删除对应managed credential bindings。只有authority cut与credential cleanup都settlement后才显示普通“已删除/已卸载”。Credential cleanup失败不能恢复已删除的MCP/Plugin，也不能宣称全部清理完成：operation返回“能力已删除，凭据清理需要重试”的typed partial outcome，并在当前结果面板提供一次明确重试。重试只携带删除前已经冻结的非秘密binding identities；不创建repair job、credential registry或后台reconciliation。若进程在authority cut后崩溃，能力已没有future execution authority，但local credential store可能留下inert credential；产品不能伪称它已经清理，用户可从OS credential manager删除这一namespaced item。本文不为这一极窄crash window增加durability。

Environment variable 本身由用户/launcher 拥有，删除 MCP 只删除引用，不修改 process environment 或 `.env` 文件。

### 5.6 Runtime secret egress boundary

Secret-safe不能只检查request side。每个physical MCP client/process在解析其credential refs时必须同时冻结一个process-local exact-value scrubber/lease；它只覆盖该client实际注入的每个non-empty credential value，并满足：

- HTTP value只进入exact auth/custom Header；stdio value只进入exact child environment target；
- server返回的tool result、resource、prompt、catalog metadata、protocol error、stderr/log、exception与UI/trace diagnostic在离开MCP transport owner前均检查该scrub set；
- exact value命中时不把原文交给模型、subagent、Hook、transcript或普通diagnostic，使用固定non-secret marker并产生typed credential-egress diagnostic；
- 不按`key/token/password`字符串形状广泛猜测或清洗普通内容；authority仅来自本次实际解析的exact values；
- old client在rotation/reconfigure后继续持有old scrubber直到old request/stream drain，new client只持new values；close后释放value references；
- scrubber/lease使用`repr=False, compare=False`或等价private carrier，不进入config identity、event、database、provider replay或durable registry。

不得把现有只服务`PULSARA_API_KEY`的process boundary扩展成全局secret service locator。MCP credential lease由native MCP physical owner局部持有，并与client/process lifetime一起关闭。

---

## 6. MCP typed management operations

Web controller 与 CLI-independent service 使用四个 typed operations；不得让 React 拼 YAML，也不得让 Web service shell out 到 CLI：

```text
create_local_mcp_server(
  scope,
  server_id,
  complete_candidate,
  optional_secret_mutations
)

update_local_mcp_server(
  scope,
  server_id,
  expected_current_identity,
  complete_replacement,
  optional_secret_mutations
)

remove_local_mcp_server(
  scope,
  server_id,
  expected_current_identity
)

test_local_mcp_server(
  complete_candidate,
  call_local_secret_inputs_or_references
)
```

### 6.1 Whole-entry update

Update 必须：

1. 从 canonical scope 重新读取 current entry；
2. exact compare browser inspection 携带的 existing config identity；
3. 用唯一 parser 解析完整 replacement；
4. 在现有 capability mutation lane 内 atomic replace YAML；
5. 触发现有 live capability refresh；
6. 返回新 inspection 与逐 session adoption/attention。

不得 partial merge auth、transport 或 policy；不得在 stale 时覆盖用户/其他进程刚保存的新值。

- USER mutation 使用 existing `resolved_config_identity` 作为同一运行内 opaque stale guard。
- WORKSPACE mutation继续使用 existing `workspace_approval_identity`，并在变更后只批准用户刚保存的完整新值。
- 不为 update/delete 新增 quote fingerprint、revision table 或 config registry。

### 6.2 Save 与 test 的关系

“测试连接”使用 disposable candidate/client，只提供 connectivity/catalog 证据：

- 不写 config；
- 不发布 provider tool surface；
- 不取得 future dispatch authority；
- 不要求成功后才能保存；
- 使用已有 connect/read/write/settlement physical watchdog，不增加 total UI operation lifetime cap；
- test 完成后关闭所有 client/process/secret lease。

用户仍可保存当前暂时不可达的服务；保存后列表显示真实失败并允许再次编辑/重连。不得因为 test 失败偷偷保留旧 config，也不得把 test 成功伪装成已经安装。

### 6.3 SDK capability boundary

能力页尽量投影 current native MCP config 与 SDK 已被 Pulsara 正式采用的能力，不直接暴露 SDK 的开放对象：

- 只支持 Streamable HTTP 与 stdio；
- 支持 tools/resources/resource templates/prompts 的发现计数与非秘密说明；
- 不因 SDK 新增 experimental field 自动生成 UI；
- OAuth、elicitation/sampling/roots 等未进入 native closed contract 的能力保持不可配置。

“发挥 SDK 绝大部分能力”指完整利用 Pulsara 已拥有并验证的 native transport/auth/exposure/effect/timeout/concurrency contract，不指把 SDK 配置对象或任意 JSON 原样交给用户。

### 6.4 Plugin MCP credential operation

Plugin MCP不调用local config update，因为package definition不是local entry。Plugin management owner增加一个窄operation：

```text
replace_plugin_mcp_credential_overlay(
  scope,
  plugin_id,
  local_server_id,
  expected_current_package_install_id,
  complete_non_secret_overlay,
  optional_secret_mutations
)
```

`complete_non_secret_overlay`只允许closed auth/secret-env refs；空overlay表示清除instance override。Operation必须在exact instance lock内完成：

1. 重读current instance并exact compare package install id；
2. exact join current portable MCP component，拒绝不存在、invalid或transport kind不匹配；
3. 使用native auth/secret-env validator验证完整overlay，并按固定precedence形成effective runtime fields；
4. settlement secret mutation并保留本次call-local rollback material；
5. atomic replace existing Plugin instance state中的该component overlay；
6. 通过existing Plugin composition/native MCP supervisor refresh发布new physical config。

Non-secret overlay是current Plugin instance state的一部分，不新增sidecar YAML、credential registry、数据库row或第二个Plugin state owner。Managed secret value只在credential store中。UI取消、stale或secret write失败不得留下“页面显示已配置但overlay没有引用”的split truth；跨filesystem/credential-store不具备原子事务时，implementation必须以secret先成功写入、instance overlay后发布为顺序，overlay publish失败则由同一个request owner立即删除/恢复本次写入的exact bindings并报告cleanup attention。不得为此增加durable transaction log。

Package replace对同一`plugin_id + local_server_id`保留overlay，但在new instance state publish前重新校验；component消失、transport kind变化或credential target不再合法时，该overlay不得进入new runtime，replace inspection必须明确列出“凭据需要重新配置”，并由同一个replace owner清理不再引用的managed bindings。New package增加同名public Header/ordinary env只改变UI所示的被覆盖base value，不绕过既定overlay precedence。

Replace继续遵守existing“new package默认disabled”：保留的credential在用户重新enable前只是suspended local binding，不能连接或spawn。Enable review必须exact join new package MCP endpoint/command、credential auth kind、non-secret target names与configured/present状态并显示给用户；用户接受new exact package后才允许把旧value用于new physical destination。Review不显示secret，也不新增consent receipt。Ordinary disable保留overlay；remove删除overlay及managed values。

### 6.5 唯一模型入口：`manage_capability`

Builtin catalog新增一个ROOT-only tool，名称固定为`manage_capability`。它在四种permission mode中均可被模型调用，不得照搬`reload_capabilities`当前“仅ROOT bypass可调用”的特殊授权。第一版closed action union只覆盖本轮确有共享typed owner的MCP与Plugin动作：

```text
ADD_LOCAL_MCP
UPDATE_LOCAL_MCP
REMOVE_LOCAL_MCP

INSTALL_PLUGIN
SET_PLUGIN_ENABLED
REMOVE_PLUGIN
CONFIGURE_PLUGIN_MCP_CREDENTIAL
```

`UPDATE_LOCAL_MCP`包含启停与完整whole-entry replacement；Plugin definition不可编辑，因此没有`UPDATE_PLUGIN`。Plugin credential replace/clear共用`CONFIGURE_PLUGIN_MCP_CREDENTIAL`的closed mutation intent。Loose Skill继续由既有installer/filesystem与本轮Web delete owner管理；不要因为tool名较宽就在本轮塞入任意Skill文件写入或generic capability action。

Tool input只能包含：

- closed action；
- `USER | WORKSPACE`与当前workspace context可exact resolve的scope；
- server/plugin/component identity与相应expected-current guard；
- typed public source/candidate、非秘密credential kind/target names，以及模型已知的用户意图。

Tool input不得包含：

- raw YAML/JSON config blob、shell command wrapper或作为mutation destination的任意filesystem path；Plugin可使用existing typed local/remote source locator，但目标root必须由scope owner导出；
- Bearer value、Header value、secret env value、OAuth token或“请用户把key贴到这里”的字段；
- `simple`、`trusted`、`skip_confirmation`、`force`、`interactive=false`等由模型自报的授权结论；
- 另一个tool call、CLI invocation或UI submit token。

`INSTALL_PLUGIN`只接收existing Plugin installer已经支持的typed source identity与验证参数，并继续落为disabled exact instance。`SET_PLUGIN_ENABLED(enabled=true)`必须复用existing Plugin review/enable owner且恒走USER_FORM：表单exact join current package install id，展示normalized Skill/MCP/Hook与credential-presence摘要，只有用户SUBMIT时才生成call-local `ExternalProcessAcceptance.ACCEPTED`并交给existing owner。Acceptance不出现在模型参数，也不持久化为receipt。`enabled=false`按实际write/process effect与permission正常分流。`manage_capability`不执行包内代码来判断Plugin是否“简单”，也不根据名称、README或模型描述放宽permission。

配置完整的environment reference是非秘密candidate，可按permission进入DIRECT；新增或更换managed credential value必然进入USER_FORM。清除existing managed credential不需要读取value，可按exact destructive effect与当前permission分流，不能因为“曾经含secret”而永远强制表单。

Tool最终只返回sanitized closed outcome：

```text
APPLIED | CANCELLED | REJECTED | CONFLICT | PARTIAL
object kind / action / scope / stable public identity
current non-secret inspection
automatic adoption: RELOADED | PENDING_SAFE_POINT | PARTIAL | NOT_APPLICABLE
reloaded / pending / attention session counts（如有）
```

USER_FORM是tool执行期间的process-local interaction状态，不是提前返回的伪成功ToolResult。用户取消、拒绝或关闭表单后必须让原tool call以`CANCELLED`/`REJECTED`settle，模型取得下一次推理机会并自然收尾；不得把一轮永远停在未完成tool card。Secret、opaque form carrier与private rollback material均不得进入最终ToolResult。

### 6.6 Permission与表单分流算法

当前generic authorization先按raw tool arguments分类，READ_ONLY的non-read-only call会直接得到终局DENY，filesystem policy也只认识模型参数中的`path`。`manage_capability`不得假设只加descriptor/action override就能得到USER_FORM；它必须是一个窄的prepare-first binding。

Preparation owner先形成一个process-local、不可由模型伪造的typed value：

```text
PreparedCapabilityManagementInvocation
  normalized closed action and public candidate
  exact resolved target/current inspection/expected guard
  user-only input requirements
  existing product review requirements
  ResolvedCapabilityEffectProjection
```

该对象不持久化、不加fingerprint，也不进入provider input。其唯一用途是把已经由canonical owner解析的事实交给现有permission与execution owner。“是否直接执行”按以下顺序机械计算：

1. resolve exact target owner与current inspection，拒绝foreign/inherited/bundled/component-only target；
2. 用对应唯一parser/preflight解析public candidate，列出缺失或必须由用户提供的字段；
3. 根据真实physical destination与adoption行为导出effect projection，而不是把逻辑USER/WORKSPACE scope直接当成filesystem边界；
4. 将prepared projection与当前`FrozenRunPermissionSnapshot`交给existing `BuiltinToolCallClassifier -> PolicyPermissionGate -> ToolDispatchAuthorizationPolicy`的窄扩展接口；permission gate消费resolved facts，不从模型参数读取mutation target path；
5. `ALLOW`只有在无user-only input且无pending product review时才进入DIRECT；`REQUIRE_CONFIRMATION`、user-only input或product review进入process-local `CAPABILITY_FORM_REQUIRED`；
6. READ_ONLY的write `DENY`绝不转换为模型执行permit。Tool execution adapter只允许展示process-local form；用户SUBMIT以独立user-control-plane actor调用同一mutation owner。其他policy DENY仍立即settle为no-attempt ToolResult；
7. `CAPABILITY_FORM_REQUIRED`必须在generic“DENY立即返回”和普通boolean confirmation execution之前由exact prepared owner消费；用户取消或controller缺失则不执行mutation，但仍给原tool call一个最终ToolResult；
8. 表单提交后重新读取current target、重新parse完整submitted candidate并exact compare stale guard；不得执行最初草稿、把用户submit伪装成model permission，或last-write-wins；
9. 调用第6节已有typed owner，等待mutation与automatic adoption settlement，再生成一个ToolResult。

`CAPABILITY_FORM_REQUIRED`只是existing `KernelToolAuthorization`/same-Host interaction flow新增的process-local closed disposition，不是permission mode、durable event或通用表单框架。普通tools的DENY/REQUIRE_CONFIRMATION控制流保持不变；同一`PreparedCapabilityManagementInvocation`只能被DIRECT execution或一次form submission线性消费。

Physical effect mapping固定为：

| Exact operation part | 交给existing permission gate的事实 |
|---|---|
| USER local MCP YAML create/update/remove | `outside_workspace_write` |
| WORKSPACE local MCP YAML create/update/remove | `workspace_write` |
| 任意scope Plugin package/state install/enable/disable/remove | physical target位于`PULSARA_HOME/plugins/...`，一律`outside_workspace_write` |
| 任意scope Plugin MCP non-secret overlay | Plugin instance state位于`PULSARA_HOME`，一律`outside_workspace_write` |
| 任意managed credential create/replace/clear/cleanup | local credential store不在workspace，一律追加`outside_workspace_write` |
| Environment reference only | 不产生credential-store write；仍保留其config/state physical write |
| Adoption会start/stop stdio process或activate/deactivate local Hook/executable | 同时追加existing terminal/process effect |
| HTTP reconnect/catalog refresh | 继续服从existing `network_isolated=false`，不凭“网络”新增确认 |

一个operation可同时携带多个effect，使用现有规则中更严格的实际verdict。`BuiltinToolCallClassifier`可以按closed action与prepared projection生成不同effective category/metadata，但不得信任模型提供的path或风险标签；不要把所有action永久写死为bypass-only，也不要为此新建capability permission service。

四种mode的产品矩阵固定为：

| 当前 effective permission | `manage_capability`可否调用 | 完整、无secret、无需额外product review | 缺secret/用户选择或需existing review |
|---|---|---|---|
| READ_ONLY | 可以 | 不允许模型DIRECT；打开prefilled USER_FORM，由用户控制面提交独立显式mutation | 打开USER_FORM；模型只观察脱敏settlement |
| ASK_PERMISSIONS | 可以 | 进入existing permission confirmation；Hook未替用户合法settle时打开shared review/editor | 打开同一表单补齐并确认；Hook不能代填secret或product review |
| ACCEPT_EDITS | 可以 | 只有pure WORKSPACE local-MCP YAML write可DIRECT；USER MCP、所有Plugin state、managed credential、stdio/local process或Hook effect均按现有outside-write/terminal规则打开表单 | 打开同一表单补齐并确认 |
| BYPASS_PERMISSIONS | 可以 | DIRECT；但Plugin `enabled=true`仍需existing product review | 打开表单；bypass不能替用户发明secret、缺失值或existing product trust决定 |

HTTP/network本身继续服从现有`network_isolated=false`preset，不因“网络”二字发明新approval；stdio process与Plugin executable/Hook activation映射到existing terminal/executable effect。一个operation若同时含多种effect，采用现有规则中更严格的实际边界，但不能把“未来也许有副作用”泛化成所有MCP/Plugin都询问。

表单不是permission旁路：

- permission为ASK且existing PermissionRequest Hook没有合法settle时，它承载本次exact确认；Hook decision只能处理permission，不能代替secret input或Plugin product review；
- READ_ONLY时，表单提交的actor是用户控制面，而非把模型run提升到write mode；
- secret/缺失选择触发表单时，即使permission为BYPASS，最终authority也只覆盖用户实际提交的exact candidate；
- current controller不存在、interaction被替换、用户取消或stale时不执行mutation；
- PreToolUse/PermissionRequest Hook最多看到sanitized public candidate与effect projection，永远看不到form secret。

现有same-Host interaction owner只需增加一种process-local typed capability form payload与对应submission resolution。它复用现有单一current interaction、controller、replacement与settlement纪律；不得增加数据库interaction row、committed event kind、durable form job、receipt或跨重启恢复。前端复用第4.2节editor component，不让模型操纵DOM。

### 6.7 Automatic adoption 与 `reload_capabilities`补救边界

First-party能力变更——无论来自能力页、`manage_capability` DIRECT还是其USER_FORM submit——在typed mutation成功后，都由当前request owner直接调用existing Host/live-session refresh与safe-point adoption。Runtime调用的是`reload_capabilities`背后的同一个内部owner，不是伪造一次模型tool call：

- 不新增assistant tool request、ToolResult、provider roundtrip或canonical transcript项；
- USER mutation继续传播给所有live sessions，WORKSPACE mutation只传播给matching sessions；每个target取得一个requested adoption revision；
- healthy provider stream/tool attempt不被中断。每个session在第一个合法safe point采用：若mutation来自`manage_capability`，发起它的当前session必须在同一turn的下一次provider call前settle该revision；其他busy/idle matching sessions最迟在各自下一次provider dispatch前采用，不要求等到“下一条user message”；
- old tool-surface borrow可继续drain，new surface只供后续dispatch。Adoption不得修改same-epoch provider SYSTEM/tools/messages prefix；新/变化MCP继续走existing DIRECT/META exposure规则；
- mutation settlement与adoption settlement明确分层，adoption失败不回滚已经保存的canonical capability truth；
- `manage_capability`的ToolResult等待origin-session requested revision：成功才返回`APPLIED + RELOADED`，失败返回`APPLIED + PARTIAL`并禁止假装可立即验证。普通Web API可对尚未到safe point的session返回`PENDING_SAFE_POINT`；正常pending不是attention；
- response列出reloaded/pending/attention session counts。模型不得在`RELOADED`后再例行reload。

保留现有ROOT-only `reload_capabilities` builtin tool，但把它定位为显式补救入口，而非正常安装步骤。它只用于：

1. terminal/CLI、手工编辑YAML/Skill目录、git checkout或其他进程installer造成的out-of-band source change；
2. first-party mutation已经返回明确`PARTIAL`/adoption attention，需要用户或模型在问题解除后重试；
3. 诊断时需要重新读取canonical sources以确认外部修改是否可采用。

`reload_capabilities`继续服从它已有的ROOT/bypass授权与same-epoch no-rebase契约；其他mode下的用户可从能力页发起同一底层刷新补救，不因此扩大模型run permission。Reload不能安装、启用、信任、删除或修复invalid config，不能摄取另一个进程后来新增的environment value，也不能代替MCP reconnect/credential editor。

不得通过解析terminal command来猜测“刚才可能改了能力”并自动reload，也不新增filesystem watcher。Out-of-band caller负责在确知source settled后显式调用补救入口。

---

## 7. 删除代数

### 7.1 通用规则

任何删除操作都必须满足：

```text
删除对象 = 当前页面拥有的 exact source item
不是按 display name 搜索
不是删除所有同名候选
不是删除 inherited/bundled/Plugin child contribution
```

删除通过原类型 owner 完成。UI 共享的只是下列产品状态，并不要求三个 subsystem 发明同一组底层 outcome：

```text
REMOVED
NOT_FOUND
STALE
CLEANUP_ATTENTION（canonical source 已切除，但附属物理清理未完成）
```

Plugin owner 继续保留其已有 `UNAVAILABLE / CANCELLED / TIMED_OUT / ACK_UNKNOWN` physical settlement；Skill 或 MCP 不得仅为视觉对称新增这些状态。不得为了统一 UI 强迫三个 subsystem 使用一个 DTO；UI adapter exhaustive 映射各自既有或本轮确有必要的 closed outcome 到共同产品文案。

### 7.2 Loose Skill 删除

新增第四个 local Skill management operation：

```text
remove_loose_local_skill(
  exact installed SKILL.md/path,
  scope,
  workspace_root when WORKSPACE
) -> LocalSkillRemovalOutcome
```

行为：

1. 目标必须 exact 位于受支持 loose root 的 immediate child：
   - `${PULSARA_HOME}/skills/<name>`；
   - `~/.agents/skills/<name>`（只对用户能力页已枚举的 exact row）；
   - `<workspace>/.pulsara/skills/<name>`。
2. Root、target 与 descendants 使用 descriptor-relative no-follow 观察/删除；symlink 作为 leaf unlink，绝不沿链接删除 root 外内容。
3. 先在 held root 下把 exact visible child 原子 unbind 到 scanner 忽略的 private hidden delete stage，再递归清理 stage；不得逐文件删除 visible tree 后才让 catalog 消失。
4. 同时从 `skills.yaml` 中移除该 exact path 的 enable/disable override，使同路径未来重新安装时回到默认 enabled，而不是继承幽灵关闭状态。
5. 删除的是安装副本；原始 `source_path` 不受影响。
6. 若同名较低优先级 candidate 存在，它按现有 resolver 自然成为 winner；UI 不得声称整个 Skill name 已消失。
7. Plugin/bundled Skill 不进入该 operation；Plugin Skill 必须删除 parent Plugin。

Hidden delete stage 只是 descriptor-safe physical cleanup，不是 tombstone、receipt 或恢复 authority。Ordinary caught failure必须尝试清理；进程 crash 后残留仍由 scanner 忽略。本轮不增加后台 janitor；doctor 可以报告 exact Pulsara-owned residue，但不按 mtime 猜测删除未知目录。

### 7.3 Local MCP 删除

USER 与 WORKSPACE local entry 使用同一 native config removal owner：

1. exact source kind + `server_id` 查找；
2. optimistic compare current config identity；
3. atomic whole-entry removal；
4. workspace entry 同时撤销 existing local approval；
5. future supervisor publication 不再包含该 source；
6. already-borrowed old request/client/process按 existing owner drain/close；
7. 清理该 entry 的 managed credentials；environment refs只删除引用；
8. 刷新页面与所有相关 live sessions。

删除 user entry 后，某个 workspace 或 Plugin 中同名/相关 server 可能在其合法作用域继续存在；UI 必须重新 inspection，不得把“删除这一项”翻译成“所有会话均不存在同名 MCP”。

Plugin-produced MCP row不可单独删除；其详情提供credential管理与“管理所属插件”，删除入口只存在于parent Plugin。

### 7.4 Capability bundle/Plugin 删除

复用 `remove_local_plugin`，并收紧 stale input：

```text
scope
plugin_id
expected_current_package_install_id
workspace_root when WORKSPACE
```

在 instance lock 内重读 current state；package install id 不一致返回 `STALE`，不得删除 replacement 后的新包。

State unlink FULL 后：

- future enabled Plugin view 不再包含该 instance；
- 该 bundle 的 Skill、MCP、Hook 一起从 future capability composition 消失；
- instance state中的Plugin MCP credential overlays失去authority，同一个remove owner继续删除其managed credential bindings；environment refs只随state消失，不修改Host environment；
- already-retained Skill body、running Hook、MCP slot/request/process继续持有原 physical anchor直到 settlement；
- package root只在 unreferenced 且取得 existing exclusive package lock时由 existing GC删除；
- GC未立即回收不影响“Plugin 已卸载”的产品成功，因为 instance state已是唯一 current installation authority；
- `PLUGIN_DATA` 保留并在确认文案中明示。

不得逐个删除 bundle 内 component、修改 package root、绕过 state owner，或在 UI 成功前强制杀死 healthy old consumer。

### 7.5 Delete effect matrix

| 用户删除 | Canonical source cut | 附属清理 | 不受影响 |
|---|---|---|---|
| User loose Skill | exact user-root Skill child | exact `skills.yaml` path override、private delete stage | 原始安装来源、同名其他 root candidate、历史 transcript |
| Workspace loose Skill | exact workspace-root Skill child | exact workspace enablement override、private delete stage | user/inherited/Plugin/bundled candidate、其他目录 |
| User MCP | user `mcp.yaml` exact entry | managed credential bindings、old slot drain | workspace/Plugin server、environment variable、远端账户 |
| Workspace MCP | workspace `mcp.yaml` exact entry | workspace approval、managed credential bindings、old slot drain | user/Plugin server、其他 workspace |
| User Plugin | exact USER instance state | Plugin MCP managed credentials、future Skill/MCP/Hook contributions；package later GC | Plugin data、history、workspace instance、Host env values |
| Workspace Plugin | exact WORKSPACE instance state | Plugin MCP managed credentials、future Skill/MCP/Hook contributions；package later GC | Plugin data、history、USER instance、其他 workspace、Host env values |

---

## 8. Live adoption、continuity 与并发

### 8.1 Provider-input prefix continuity

Capability edit/delete不是新的 provider rebase boundary。

- same epoch 中 SYSTEM 与 provider tools byte-identical，messages append-only；
- Skill 删除只改变下一次 legal Skill observation；已经提供给模型的 Skill body不回写历史；
- MCP/Plugin edit/delete通过既有 supervisor/Plugin safe-point publication改变future availability；
- Plugin MCP credential变化只改变该native config的runtime/resolved physical identity并触发必要reconnect，不改变portable package bytes或stable semantic tool identity；
- 已安装的旧 DIRECT descriptor如果仍存在于 immutable tools prefix，availability gate必须返回 typed unavailable，不能调用已删除 binding；
- 新增或变化后的工具继续服从现有 DIRECT/META 与 cold epoch/compaction successor规则；
- 只有 new cold epoch 与 adopted compaction successor可重建 provider input roots。

### 8.2 Linear ownership

- 用户点击保存/删除只产生一个 capability mutation owner；double click在前端和controller lane均不得形成重复 physical mutation。
- Old MCP client/process、Plugin package anchor、Hook attempt与Skill retained body继续由其既有唯一 owner持有。
- Delete不夺取已借出的 handle，不 double-close，也不通过路径重新打开以假装原 object。
- UI request cancellation后，已经进入 filesystem/keychain/config cut 的worker必须 shield+join 并返回 exact settlement；不得留下 detached mutation。

### 8.3 Live sessions

User capability mutation继续并行通知所有已打开sessions；一个session adoption失败不回滚已保存的用户真相，也不阻塞其他session。Workspace mutation继续只影响同一resolved workspace的sessions，但采用边界统一为“该session下一次provider dispatch之前的第一个合法safe point”，不再限定到下一次root user turn。这个传播由Web与`manage_capability`共用的mutation owner自动触发，而不是由发起变更的模型session单独调用`reload_capabilities`。

若mutation由当前root turn中的`manage_capability`发起，当前session是origin target：tool owner必须在允许旧execution borrow安全存活的前提下等待requested revision adoption，然后才完成ToolResult；这样模型能在同一turn的后续provider call中使用list/inspect/meta route验证。若adoption失败，ToolResult明确为PARTIAL，模型可以自然说明，但不能调用尚未证明available的新能力。其他session可以保持`PENDING_SAFE_POINT`直到自身下一次dispatch；这不是失败，也不需要固定轮询或唤醒一个空turn。

页面展示：

```text
已保存 / 已删除
已更新 N 个会话
等待安全采用 P 个会话
M 个会话需要重试
```

不得用固定轮询制造执行恢复；用户刷新、后续明确 mutation 或既有连接生命周期可以再次观察。

`manage_capability`的USER_FORM仍占用当前session唯一same-Host interaction slot；它可以按existing arbiter被更新或替换，但不能与同一tool call的另一份表单alias。表单关闭后，无论mutation成功、取消、拒绝、stale或partial，原tool execution owner都必须settle并把一个最终ToolResult送回模型。

---

## 9. MCP 与 Plugin installer Skill 契约

### 9.1 MCP installer

Bundled `pulsara-mcp-installer`必须以`manage_capability`作为模型参与正式安装/编辑的首选路径，同时保留production CLI作为用户明确要求或tool不可用时的out-of-band路径，并新增以下指导：

```text
Pulsara 的能力页支持 Streamable HTTP、stdio、Bearer、自定义秘密 Header、
stdio secret environment、tool exposure/effect/timeout 与其他高级 MCP 配置。

不要要求用户在对话、terminal command 或可见配置文本中粘贴 secret。
1. 查明公开 endpoint、transport、auth shape 与所需非秘密 Header/env 名；
2. 用 manage_capability 提交typed public candidate；不要先自行判断要不要弹窗；
3. runtime会在信息完整且permission允许时直接保存，否则自动打开“能力 → MCP”同款表单；
4. 用户若需要输入secret，只在该表单中输入；model与Skill均不得索取或复述；
5. manage_capability返回APPLIED + RELOADED后，直接使用list_mcp_servers /
   inspect_new_mcp_tool / use_new_mcp_tool或doctor验证exact server与representative
   safe operation，不再例行调用reload_capabilities；
6. 只有走CLI/手工文件等out-of-band路径，或结果明确为PARTIAL/adoption attention时，
   才使用reload_capabilities补救；
7. 只报告connector state、tool count、使用的工具与non-secret result。
```

推荐用户文案：

> 该 MCP 需要凭据。我会打开本地配置表单；请只在那里填写密钥，不要发送到对话里。保存后我会继续检查连接并实际验证工具。

Skill不应把所有高级配置都推给用户。对于有权且可由official management surface表达的非秘密配置，模型应形成尽可能完整的prefill；是否DIRECT由runtime和当前permission决定。只有secret、缺失的用户独占选择、existing trust/review或当前surface不支持的设置需要用户补充。

如果 authoritative server 只支持 OAuth，Skill 必须诚实报告当前 Pulsara 不支持，不能伪装成 Bearer、自行抓 token 或把网页登录 cookie写入 config。

### 9.2 Plugin installer

Bundled `pulsara-plugin-installer`不得要求用户把Plugin MCP API key发进对话，不得把secret写进source package、转换candidate、`mcp.json`、terminal command或Plugin data。它必须遵循：

1. 用`manage_capability(INSTALL_PLUGIN)`进入existing exact validate/install workflow；secret不是package合法性或安装成功的条件，成功后仍先得到disabled instance。
2. 从inspection列出Plugin提供的MCP component及其公开endpoint/transport，不猜测credential slot。
3. 若Plugin文档或连接结果表明需要认证，用`CONFIGURE_PLUGIN_MCP_CREDENTIAL`提交non-secret binding shape，由runtime自动打开shared credential form；不让用户把secret发给模型。
4. `SET_PLUGIN_ENABLED(enabled=true)`始终复用existing exact-package review UI；只有用户SUBMIT产生call-local external-process acceptance，不能把install、credential、enable压成一次隐式信任。
5. 每个first-party mutation返回`RELOADED`后直接通过existing list/inspect/use路径验证；不要求重装Plugin，也不例行调用`reload_capabilities`。
6. 只有CLI/手工source等out-of-band变更或明确`PARTIAL`/adoption attention才调用reload补救。
7. 只报告configured/present、connection状态、tool catalog与non-secret call结果。

推荐用户文案：

> 插件已经安装，但其中的这个 MCP 还需要凭据。我会打开它的本地配置表单；请只在那里填写密钥，不要发送到对话里。保存后我会继续验证。

---

## 10. Web/API projection

具体 URL 可按现有 server routing风格调整，但必须只有一组 user operations 与一组 workspace operations。推荐形状：

```text
POST   /api/capabilities/mcp
PUT    /api/capabilities/mcp/{server_id}
DELETE /api/capabilities/mcp/{server_id}
POST   /api/capabilities/mcp/test

POST   /api/capabilities/skills/remove
DELETE /api/capabilities/plugins/{plugin_id}
PUT    /api/capabilities/plugins/{plugin_id}/mcp/{local_server_id}/credentials

PUT    /api/sessions/{session_id}/capabilities/mcp/{server_id}
DELETE /api/sessions/{session_id}/capabilities/mcp/{server_id}
POST   /api/sessions/{session_id}/capabilities/skills/remove
PUT    /api/sessions/{session_id}/capabilities/plugins/{plugin_id}/mcp/{local_server_id}/credentials
```

要求：

- create/update request 可一次性携带 secret mutation，但 request body不得被普通 logger/trace保存；
- inspection response只返回 editable non-secret config、credential presence与opaque current identity；
- update/delete携带 expected current identity；stale返回 conflict并附fresh inspection，不接受last-write-wins；
- Plugin delete增加 expected package install id；
- Plugin MCP credential update同样携带expected package install id，只能whole-replace该component的credential overlay；
- Skill delete只接受当前 inspection中exact owned path/row，不接受任意绝对路径作为通用recursive delete API；
- UI adapter不解析 CLI JSON，不操作 YAML，不接触 Keychain API；
- server/controller不把 secret复制到 capability operation details。
- `manage_capability`不得绕过这些controller/service contracts直接写source；DIRECT与USER_FORM submit只是在actor/admission上不同。

### 10.1 MCP editable projection

非秘密 inspection至少包括：

```text
server_id
display_name
source scope
enabled / required
transport kind and complete non-secret fields
auth kind
for each auth/secret binding:
  binding name
  source kind (managed | environment)
  configured/present boolean
exposure/scope/effect policy
parallel/stateless/refresh/timeout settings
opaque current config identity
current connector/catalog projection（exact-join后）
```

Secret value字段在任何 response schema中都不存在，而不是返回 `***` 伪值。

Plugin-provided MCP使用同一credential presence projection，但另外携带只读`source_plugin`、`local_server_id`与opaque current package identity；它不返回local MCP update identity，也不接受对package-derived definition fields的写入。

### 10.2 Model-triggered capability form projection

Current same-Host interaction projection增加一个closed `CAPABILITY_FORM` variant，至少携带：

```text
interaction identity / revision / expiry
requested closed action
object kind / USER-or-WORKSPACE scope / public target identity
shared editor mode (create | edit | review | credential)
sanitized prefill
editable field schema/profile
public effect summary
permission reason（产品文案）
opaque expected-current guard
```

Projection不得携带existing secret value、credential-store binding internals、model/provider payload、raw Plugin package bytes或让frontend选择backend operation的自由字符串。Frontend按variant加载与能力页相同的form component；submission返回完整public candidate、closed secret mutation与opaque guard，直接交给唯一pending interaction owner。

Secret-bearing submission只可通过本地controller transport进入credential owner；request/response middleware、WebSocket/protobuf debug logging、error serialization与browser persistence均须显式排除其value。Live snapshot/reconnect最多重新投影non-secret form与`configured/present`，不得重放用户已经输入但尚未成功settle的secret。Controller断开后保留现有process-local interaction行为，不增加durable草稿或跨重启恢复。

同一interaction resolution必须原子选择`SUBMIT | CANCEL`之一并exact compare interaction identity/revision。普通tool confirmation的`ALLOW | DENY`继续原样存在；不得把capability form的完整candidate塞进通用allow boolean，也不得用先调用Web API、再单独点击ALLOW的两阶段split-brain流程。

---

## 11. 文件与 owner 变更上限

实施优先修改现有 owner；以下是上限，不是要求创建空壳模块：

```text
src/pulsara_agent/mcp_config.py
  closed secret refs、完整 local entry create/update/remove

src/pulsara_agent/conversation_kernel/mcp/sdk_facade.py
  call-local credential resolution、HTTP/stdio injection

src/pulsara_agent/capability/local_skill_management.py
src/pulsara_agent/capability/local_skill_publisher.py (或窄 removal owner)
  typed loose Skill remove、descriptor-safe unbind/cleanup

src/pulsara_agent/plugins/contracts.py
src/pulsara_agent/plugins/management.py
src/pulsara_agent/plugins/package_store.py
src/pulsara_agent/plugins/mcp_adapter.py
  instance credential overlay、native composition、remove stale guard；不复制 package GC

src/pulsara_agent/capability/builtin_catalog.py
src/pulsara_agent/capability/call_classifier.py
src/pulsara_agent/tool_permission.py
  注册唯一manage_capability descriptor；按closed action投影exact scope/effect并复用现有permission gate

src/pulsara_agent/conversation_kernel/tool_contracts.py
src/pulsara_agent/conversation_kernel/tool_runtime.py
src/pulsara_agent/conversation_kernel/tool_execution.py
src/pulsara_agent/conversation_kernel/host.py
  manage_capability preparation/authorization/execution、typed owner dispatch与automatic adoption；不复制mutation逻辑

src/pulsara_agent/conversation_kernel/interaction.py
src/pulsara_agent/conversation_kernel/live_control.py
src/pulsara_agent/ports/live_agent_event.py
src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto
src/pulsara_agent/terminal_protocol/v3_gateway.py
  process-local CAPABILITY_FORM projection/submission；复用唯一current interaction，不增加durable row/event

src/pulsara_agent/web_app/http_server.py
src/pulsara_agent/web_app/session_controller.py
  user/workspace structured operations、secret request boundary、live adoption

frontend/components/capability-view.tsx
frontend/components/workbench-view.tsx
frontend/components/inspector-panel.tsx
frontend/lib/pulsara-types.ts
frontend/lib/runtime-adapter.ts
  shared add/edit/review form、conversation interaction、credential UX、三类删除与typed outcomes

src/pulsara_agent/bundled_skills/pulsara-mcp-installer/SKILL.md
src/pulsara_agent/bundled_skills/pulsara-plugin-installer/SKILL.md
  user-owned credential handoff与Plugin MCP配置指引
```

Managed credential store或capability mutation facade若需要新模块，各只允许一个窄local port/adapter；`manage_capability`只能调用facade，不能让Keychain adapter本身成为模型tool。不得把任一模块扩展成通用账号系统、OAuth vault、remote sync、database service、generic capability registry或第二套permission owner。

---

## 12. Hard-cut 实施顺序

1. 冻结本文与 active spec同步点；确认数据库/event oracle不变。
2. 建立 MCP secret reference与local credential boundary，先完成纯本地 unit tests。
3. 将 local MCP mutation 收敛为 complete typed create/update/remove/test；删除 Web/CLI 中手工拼接缩减 entry 的重复路径。
4. 实现 descriptor-safe loose Skill removal，并同步清理 exact enablement override。
5. 在existing Plugin instance state中加入per-component non-secret credential overlay，并在`PluginMcpAdapter`中与immutable package definition exact join；不给portable schema增加伪slot。
6. 给 Plugin removal增加 expected package install id stale guard与managed credential cleanup，继续复用existing state/anchor/GC。
7. 扩展Web controller与non-secret inspection projection；让所有first-party mutation在settle后调用同一个automatic live-adoption owner，并把workspace adoption推进到每个session下一次provider dispatch前的safe point；secret request全链路禁止日志/回显。
8. 在builtin catalog注册唯一ROOT-only `manage_capability` closed descriptor；实现prepare-first invocation与resolved physical effect projection，再接入existing call classifier/permission gate。先证明四种mode与Plugin/credential physical-location矩阵，禁止临时bypass-only special case。
9. 实现DIRECT路径，只dispatch到第3、5、6步的typed owners；model-origin mutation必须在同一turn后续provider call前settleorigin adoption revision，mutation/adoption分层，一个tool call只产生一个最终ToolResult。
10. 扩展existing same-Host current interaction为typed `CAPABILITY_FORM`与process-local authorization disposition，明确READ_ONLY form-only actor边界，实现exact submit/cancel与controller-loss settlement；不改数据库/event oracle。
11. 重做能力页MCP add/edit shared editor与progressive advanced fields；Plugin MCP复用credential部分但definition保持只读，并让workbench interaction复用同一component/schema。
12. 给user Skill/MCP/Plugin与已有workspace Skill/MCP management surface补齐删除入口和确认。
13. 更新`pulsara-mcp-installer`与`pulsara-plugin-installer`，删除旧“auth surface不能表达就只能blocker”和“成功后例行reload”的过时描述；保留OAuth等真实blocker，并把`reload_capabilities`写成out-of-band/partial补救。
14. 运行focused tests、全量Python/frontend、PostgreSQL clean-v0 verify与real MCP/Plugin model-assisted dogfood。
15. 同步active specs，删除旧no-edit/no-delete/no-Plugin-credential、model只能口头指导、manual reload happy path契约，以及compat aliases、旧DTO fields与重复UI/API路径。

不得保留 v1/v2 form、old/new auth双读写、临时 feature flag、raw YAML fallback或“编辑其实是删除再添加”的前端伪实现。

---

## 13. Required semantic golden matrix

### 13.1 MCP add/edit

| 场景 | 必须结果 |
|---|---|
| 添加 keyless HTTP | 保存完整 native entry，连接状态来自 exact live config |
| 编辑 display name | server identity不变，whole-entry atomic replace |
| 编辑 endpoint/command | new physical config发布；old in-flight consumer安全drain |
| 尝试编辑 server_id/scope | UI不提供；API拒绝 |
| stale browser编辑 | conflict + fresh inspection；不得覆盖 current |
| test成功后取消 | 无 config、secret、client/process残留 |
| test失败后保存 | 允许保存，显示真实连接失败 |
| same-epoch tools变化 | 不重写 provider prefix；按existing DIRECT/META规则采用 |

### 13.2 Credentials

| 场景 | 必须结果 |
|---|---|
| managed Bearer | exact `Authorization: Bearer <value>`只进入HTTP physical request |
| managed custom Header | exact configured header注入；值不进response/log/trace |
| environment Bearer | Host env有值则连接；缺失则typed needs-credential |
| stdio managed secret env | 只进入exact child env；不进入command/args/output |
| ordinary edit未触碰secret | old binding/value保持 |
| replace secret | runtime physical identity变化并重连；不改semantic tool identity |
| clear required secret | config保留但needs-credential；不匿名fallback |
| 引用 `PULSARA_API_KEY` | admission拒绝 |
| OAuth-only server | 不渲染伪支持；Skill报告真实 blocker |
| capability inspection | 只有presence/source kind，没有secret value字段 |
| malicious/buggy server回显credential | exact value在transport owner内拦截/替换，不进入ToolResult、模型、UI或trace |
| credential rotation时old request仍运行 | old/new physical client各持对应scrubber直到各自drain，无lease alias或提前释放 |

### 13.3 Plugin MCP credentials

| 场景 | 必须结果 |
|---|---|
| 安装含authenticated MCP的Plugin | package可先成功安装；缺凭据只影响exact MCP连接 |
| 连续安装向导填写key | 后台严格settle为install-disabled → credential overlay → enable；install request/validator从未接收secret |
| install失败或instance不存在 | 不创建credential binding，不进入后续配置/enable operation |
| Plugin尚未enable | MCP列表仍从installed inspection显示可配置行，但不发布runtime/model capability |
| 从Plugin详情配置 | 与MCP列表入口读写同一个instance overlay |
| 修改endpoint/command等definition | UI只读、API拒绝；managed package bytes不变 |
| 配置Bearer/custom Header/secret env | 只写non-secret overlay与local credential store |
| package没有slot声明 | UI不伪称已声明；用户显式选择auth kind和target name |
| USER与WORKSPACE同一Plugin | credential bindings隔离，不互相继承 |
| stale package install id | 不向replacement写入overlay，返回fresh inspection |
| same component合法replace | overlay通过new definition重验后保留 |
| replace改变physical destination | new package保持disabled；enable review显示new destination与non-secret credential摘要，接受前不使用旧value |
| component消失/transport变化/target非法 | new runtime不采用old overlay，明确要求重新配置并清理obsolete managed value |
| overlay与package public Header/env同名 | UI明确提示覆盖；runtime只采用secret overlay value，package bytes不变 |
| disable/re-enable | overlay与managed value保留 |
| clear credential | package definition仍在，exact MCP回到package-derived无overlay状态；若服务仍需认证则显示需要凭据/连接失败 |
| model/installer验证 | 模型只能看到presence与连接结果，不能看到value |

### 13.4 Skill deletion

| 场景 | 必须结果 |
|---|---|
| 删除 `${PULSARA_HOME}` Skill | exact visible child unbind并清理；source目录不变 |
| 删除 `~/.agents` Skill | 明确共享影响后exact删除；不触碰其他child |
| 删除 workspace Skill | 只影响同目录；user/inherited保持 |
| 删除disabled Skill | exact `skills.yaml` override一并清理 |
| 高优先级同名被删 | lower candidate按resolver自然出现 |
| target被换成symlink | no-follow拒绝/只unlink leaf，绝不越界 |
| cleanup中断 | visible capability已切除时报告partial cleanup，不伪造rollback |
| bundled/Plugin Skill | 不显示独立删除；指向parent source |

### 13.5 MCP deletion

| 场景 | 必须结果 |
|---|---|
| 删除user keyless MCP | exact user entry消失；所有live sessions收到refresh |
| 删除managed-secret MCP | config authority先切除，credential随后删除 |
| credential cleanup失败 | MCP仍已删除；明确partial outcome，不恢复连接 |
| 删除env-ref MCP | 不修改process env或env file |
| 删除workspace MCP | exact approval一并撤销，只影响同workspace |
| stale config identity | 不删除新replacement，返回conflict |
| running MCP call | old request按existing outcome owner settle；future admission unavailable |
| Plugin MCP row | 无独立删除；管理parent Plugin |

### 13.6 Plugin deletion

| 场景 | 必须结果 |
|---|---|
| 删除disabled Plugin | exact instance state删除 |
| 删除enabled Plugin | future Skill/MCP/Hook三类贡献同时消失 |
| Plugin含managed MCP credentials | instance authority先切除，managed values随后清理；Plugin data仍保留 |
| package刚被replace | old expected install id返回STALE，不删new state |
| old MCP/Hook仍运行 | package lock保持；GC报告IN_USE而非强删 |
| consumers drain后GC | unreferenced immutable package可回收 |
| USER instance删除 | 同名WORKSPACE instance不受影响，反之亦然 |
| Plugin data存在 | 默认保留且UI明确说明 |

### 13.7 UI

- Add 与 edit 必须共用同一 form component/schema projection。
- Secret input 不进入 React debug output、toast、error boundary、test fixture snapshot或browser persistence。
- Existing secret不回填；“已配置”与 replace/clear交互可键盘访问。
- Plugin MCP definition字段只读，credential字段可编辑；Plugin详情与MCP列表状态一致。
- Owned rows有真实删除；inherited/bundled/component rows没有假按钮。
- Delete confirmation清楚区分 Skill directory、MCP credential与Plugin data。
- Mutation settle前关闭/backdrop/重复提交入口按现有规则锁定。
- Backend stale/partial/attention必须有可理解文案，不能只显示异常名。

### 13.8 Model participation、permission与reload

| 场景 | 必须结果 |
|---|---|
| ROOT + READ_ONLY调用manage_capability | tool可调用但无模型DIRECT write；自动打开prefilled form，用户submit作为独立user-control-plane authority |
| ROOT + ASK_PERMISSIONS完整keyless candidate | 进入existing permission confirmation；Hook没有合法ALLOW/DENY时自动打开shared review/editor，用户确认后原tool call继续settle |
| ROOT + ACCEPT_EDITS创建workspace keyless HTTP MCP | candidate完整且无需existing review时DIRECT |
| ROOT + ACCEPT_EDITS创建user MCP | 按outside-workspace write语义打开form，不偷换成workspace write |
| ROOT + ACCEPT_EDITS安装/启停/删除任意scope Plugin | package/state实际位于PULSARA_HOME，按outside-workspace write打开form；WORKSPACE namespace不冒充workspace filesystem |
| ROOT + ACCEPT_EDITS操作WORKSPACE MCP managed credential | YAML可在workspace，但credential store effect为outside-workspace，create/replace/clear/cleanup均打开form |
| ROOT + ACCEPT_EDITS创建/启用stdio或Hook-bearing capability | 按existing terminal/executable effect打开form |
| ROOT + BYPASS_PERMISSIONS完整keyless MCP | DIRECT；不因MCP类别本身强制弹窗 |
| ROOT + BYPASS_PERMISSIONS缺Bearer value | 自动打开credential form；不得匿名安装、猜值或要求在chat粘贴 |
| SUBAGENT调用manage_capability | 明确拒绝；不能转发secret请求或借ROOT authority |
| 模型传simple/skip_confirmation/raw YAML/path/secret | schema或admission拒绝，不进入mutation |
| 模型prefill后用户修改公开字段 | 重新parse并执行用户实际提交的完整candidate，不执行旧草稿 |
| form target在等待时变化 | stale/conflict + fresh non-secret inspection；不覆盖current |
| 用户取消/拒绝/关闭form | 原tool call得到CANCELLED/REJECTED ToolResult，模型可继续自然语言收尾 |
| controller缺失或interaction被替换 | 无mutation；pending owner关闭且tool call确定settle，不永久显示“操作未完成” |
| model安装完整Plugin | permission允许时可DIRECT安装为disabled；不隐式enable |
| 任意mode执行SET_PLUGIN_ENABLED(true) | 恒进入exact package review form；只有user SUBMIT生成call-local ExternalProcessAcceptance，模型参数/数据库没有acceptance |
| Plugin credential动作需要value | shared credential form；value不进入tool args/result/Hook/transcript |
| model tool origin mutation成功且origin adoption成功 | 同一turn下一次provider call前完成requested revision；ToolResult为APPLIED + RELOADED，可直接list/inspect/use |
| model tool origin adoption失败 | capability truth保留；ToolResult为APPLIED + PARTIAL，不伪称新能力已可用 |
| Web mutation影响busy matching session | API可返回PENDING_SAFE_POINT；该session下一次provider dispatch前采用，pending不算失败 |
| automatic adoption为RELOADED | 模型不再调用reload_capabilities，直接list/inspect/use验证 |
| CLI/手工YAML/外部installer修改 | 不自动猜测；明确调用reload_capabilities后再验证 |
| automatic adoption为PARTIAL | capability truth不回滚；问题解除后允许显式reload补救 |
| same-epoch automatic/manual reload | SYSTEM/tools既有prefix不重写，messages只追加suffix |
| permission Hook观察manage_capability | 只见public candidate/effect；form secret永不进入Hook input |

---

## 14. 验证与 dogfood

### 14.1 Focused tests

至少覆盖：

```text
tests/test_local_skill_management.py
tests/test_local_skill_management_concurrency.py（若现有拆分适用）
tests/test_project_capability_management.py
tests/test_round6_mcp_production.py
tests/test_round9_3_plugin_physical_semantics.py
tests/test_round9_unified_capability_semantics.py
tests/test_round4_plan_workflow.py
tests/test_stage2_conversation_runner.py
tests/test_stage2_protocol_v3.py
tests/test_local_web_http_surface.py
frontend/app/pulsara-app.test.tsx
frontend/lib/runtime-adapter.test.ts
```

新增/扩展测试必须至少分层证明：

- `manage_capability`只有一个descriptor与一个execution owner，closed actions逐项schema/classifier覆盖；
- preparation先形成不可伪造的resolved effect projection；permission不从模型path/risk标签推导；
- ROOT四种permission mode矩阵、SUBAGENT拒绝；READ_ONLY只允许展示用户表单而不能把DENY升级成model permit，其他DENY仍走no-attempt；
- physical effect mapping覆盖USER/WORKSPACE local MCP YAML、两种scope的Plugin PULSARA_HOME state、managed credential store与stdio/Hook process；
- ACCEPT_EDITS下Workspace Plugin与Workspace MCP managed-credential create/clear/cleanup仍按outside write询问；
- DIRECT与USER_FORM汇入同一个MCP/Plugin typed mutation fake，既不shell out也不double apply；
- shared form prefill/edit/submit/cancel/stale/controller-loss与最终ToolResult settlement；
- Plugin enabled=true恒由exact review SUBMIT产生call-local ExternalProcessAcceptance；disable不携带acceptance；
- secret不出现在tool args/result、live projection、Hook input、protocol diagnostics或frontend state snapshot；
- model-origin first-party mutation在同一turn下一次provider call前settle requested adoption revision；old surface borrow可drain且无deadlock/double-close；
- Web-origin busy session返回PENDING_SAFE_POINT并在下一次provider dispatch前采用；pending与PARTIAL区分；
- first-party mutation只调用一次底层refresh；`RELOADED`后没有第二次model reload；
- CLI/manual source变更保持不可见直到explicit reload，reload仍不rebase same-epoch provider prefix；
- Plugin install-disabled、credential、enable三个settle保持独立。

不得弱化现有 assertion、增加 skip/xfail或通过隐藏真实错误换绿。

### 14.2 Full verification

使用 repository-root `uv` 管理的 `.venv`：

```text
.venv/bin/pytest -q
cd frontend && npm test
cd frontend && npm run lint
cd frontend && npm run build:local
git diff --check
```

按当前 active database spec 对已核实的 loopback disposable PostgreSQL执行 clean-v0 migrate/deep verify。本文不应改变 schema；oracle任何增长都视为 blocker。

### 14.3 Real MCP dogfood

至少完成：

1. Keyless Firecrawl：模型调用`manage_capability` → bypass下DIRECT add → automatic reload → list → inspect → safe tool call；不得出现例行第二次reload。
2. 同一keyless candidate分别在ASK与ACCEPT_EDITS user/workspace scope运行，证明表单/直达分流符合13.8而非bypass-only；另以Workspace Plugin与Workspace MCP managed credential证明physical outside-write不会被逻辑scope放宽。
3. Authenticated HTTP MCP：模型发起typed public candidate，runtime自动打开shared form；用户只在表单输入Bearer，页面不回显，连接发现完整catalog并完成一个safe call。
4. Edit：从能力页与模型各变更一个non-secret setting，证明共用typed owner、old/new physical cut与prefix continuity。
5. Delete：删除连接后future call unavailable，managed credential不再存在。
6. stdio fixture：managed secret env只到child process；fixture主动回显secret时transport boundary拦截，trace/terminal/provider均不含值。
7. Skill user/workspace delete与duplicate fallback。
8. Authenticated Plugin MCP：模型在bypass下可先直接安装disabled Plugin，再由自动表单配置credential并完成exact-package enable review；只由user SUBMIT产生call-local acceptance，不改package bytes，automatic reload后完成一个safe call；disable/re-enable保持binding。
9. Replace Plugin：stable component保留并重验credential；new package在enable acceptance前不使用old value；removed/incompatible component不采用old binding。
10. Enabled Plugin delete：future contributions消失、managed MCP credentials清理、Plugin data保留，old physical consumer drain后GC。
11. Out-of-band CLI修改：变更本身不被runtime猜测；一次explicit`reload_capabilities`补救后采用，且不改provider prefix。

Real provider dogfood保留实际 prompt、provider-visible messages/tools、model reply、non-secret MCP result与失败详情；不得记录任何 MCP credential value，也不得输出 `PULSARA_API_KEY`。

若外部 API credential不可用，authenticated remote dogfood可精确报告environment blocker，但 synthetic authenticated HTTP/stdio fixture、全部本地测试与keyless路径必须完成；不得声称真实认证已通过。

---

## 15. Definition of Done

1. 用户能力页可以用同一 structured editor添加和直接编辑 MCP。
2. Current native HTTP/stdio/auth/exposure/effect/timeout/concurrency配置有完整但渐进披露的产品入口。
3. Plugin-provided MCP definition保持immutable/read-only，但其instance-scoped credential可从MCP列表或Plugin详情随时配置、更换、清除。
4. 唯一ROOT-only `manage_capability` tool以closed actions覆盖本轮MCP/Plugin管理；它先从canonical owner形成不可伪造的resolved physical effect projection，不接收secret、raw config/path、shell wrapper或模型自报授权标签。
5. 该tool在READ_ONLY、ASK_PERMISSIONS、ACCEPT_EDITS、BYPASS_PERMISSIONS均可调用；现有permission按MCP YAML、PULSARA_HOME Plugin state、credential store与process的真实边界决定DIRECT或USER_FORM，不是bypass-only或逻辑scope猜测。
6. 能力页与model-triggered interaction复用同一form/schema/controller/typed owner；READ_ONLY只允许展示user-control-plane form而不产生model write permit，用户提交的exact candidate覆盖model prefill，cancel/stale/controller-loss均确定settle并让模型继续。
7. Plugin安装不以secret为输入或成功条件；连续UI在exact disabled instance创建后才调用credential overlay，再进行enable review。`enabled=true`恒由用户对current exact package提交review并生成call-local external-process acceptance；模型不能自报，系统不新增receipt。缺少凭据只使exact MCP需要配置，不使整个Plugin伪失败。
8. Managed secret由用户直接交给local credential store；模型、transcript、terminal、config、Hook、live projection和响应均看不到值。
9. HTTP/stdio server即使回显已注入credential，exact value也在native MCP transport owner内被拦截，old/new client各自保持正确scrubber lifetime。
10. Environment reference仍是一等高级路径；reload与restart语义诚实。
11. First-party Web/tool mutation自动调用existing live adoption owner并返回独立mutation/adoption settlement；model-origin requested revision在同一turn下一次provider call前settle，其他session使用`PENDING_SAFE_POINT`而不是伪失败。`reload_capabilities`只保留为out-of-band或PARTIAL补救，不再是happy-path步骤。
12. OAuth没有伪UI或fallback。
13. User 与现有 workspace install surfaces 对 loose Skill、local MCP、Plugin instance满足“可安装即有删除”。
14. Skill删除只删除exact managed-root row并清理enablement override；同名候选自然重算。
15. MCP删除切除exact config/approval、清理managed credential、允许old physical request drain。
16. Plugin删除复用existing state/anchor/GC并以expected package install id防止stale delete；bundle contributions与instance MCP credentials一起消失，Plugin data保留。
17. Builtin、bundled、inherited与Plugin child contribution没有假删除按钮。
18. Same-epoch provider prefix不重写；canonical transcript/history不被删除操作回溯修改。
19. 无generic capability registry、第二套permission owner、database growth、event/job/receipt/tombstone/repair machinery、compat alias或双路径。
20. Focused/full/frontend/PostgreSQL/dogfood全部通过或对真实外部阻塞精确报告。
21. Active specs与bundled MCP/Plugin installer Skills只描述新单一路径。

---

## 16. 最终产品文案基线

能力页说明：

> 管理这台设备上的插件、MCP 与技能。需要密钥的 MCP 请在这里完成认证，不要把密钥发送到对话里。

MCP 保存成功：

> 连接设置已保存。Pulsara 正在为已打开的会话刷新能力。

MCP 需要凭据：

> 连接已保存，但还需要凭据。完成认证后即可测试连接。

模型已准备完整配置、当前permission要求确认：

> Pulsara 已准备好这项能力设置。请检查后确认；你也可以在保存前修改。

模型需要用户补齐配置：

> Pulsara 已填写可以确认的公开设置。请补充标出的内容后保存；密钥只会交给这台设备上的凭据存储，不会发送给模型。

用户取消模型发起的表单后，tool结果对应的自然语言不得带责备或反复索取：

> 已取消，本次没有更改能力设置。

Automatic adoption partial：

> 设置已保存，但部分已打开的会话还没有刷新成功。你可以稍后重试刷新。

Plugin MCP 需要凭据：

> 这个连接由插件提供，连接设置不可修改，但你可以在这里配置或更换它使用的凭据。

Skill 删除确认：

> 将删除这个 Skill 的安装副本及其中的文件。最初用于安装的来源目录不会受到影响。

MCP 删除确认：

> 将删除这个连接及 Pulsara 为它保存的本地凭据。远端 API key 不会被吊销，已有对话记录也不会被删除。

Plugin 删除确认：

> 将卸载这个插件；它提供的技能、MCP 与 Hook 将不再用于后续工作，Pulsara 为这些连接保存的凭据也会删除。插件数据默认保留。

任何内部 enum、reason code、fingerprint、package install id、Keychain binding、source priority 或 supervisor state都必须在前端翻译为上述用户语义，不得直接暴露实现术语。
