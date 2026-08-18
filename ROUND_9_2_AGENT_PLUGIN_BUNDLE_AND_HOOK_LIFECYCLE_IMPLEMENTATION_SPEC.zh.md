# Round 9.2：Agent Plugins 1.0 Bundle 与 Codex-compatible Hook Lifecycle 实施规格

> 状态：**DRAFT — NOT ACTIVATED**
>
> 记录日期：2026-08-18
>
> 当前代码基线：`e97f1b11ff31aa2b029d78677e41fa90fd62a585`
>
> Codex 本地参考基线：`6138909d6ec58b2fbe635ef973e02caecad5a5aa`
>
> Claude Code 本地参考基线：`5a774a2b62d7949c1d94e0b726281554d7893cfd`
>
> 公开包规范：[Agent Plugins Specification 1.0.0](https://agent-plugins.org/specification)（Working Draft）
>
> 上位契约：[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 provider-input prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 6 MCP](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md)、[Round 7 model-visible observation](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 provider-visible ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Round 9 unified capability semantics](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 9.1 Agent Skills Standard](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)、[Gap Index](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 下游但不属于本轮：PHC-10 Hierarchical / batch subagent task graph、[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)

本文把 Plugin 冻结为 Pulsara 的**可安装组合包与 capability source contributor**，不是第四种 capability leaf、第三种 tool executor，也不是动态 Python 扩展基类。

本轮支持四类包内组件：

1. MCP server definitions；
2. Agent Skills；
3. lifecycle Hooks；
4. Subagent specifications 的包级发现位。

其中 MCP 与 Skill 分别进入 Round 9 / 9.1 已有 source-registration 与 execution/context authority；Hook 使用本文新增的 process-local lifecycle engine；Subagent 只完成受界、可诊断、不可执行的组件发现，具体 schema、provider exposure、调度与执行必须等待 PHC-10。Plugin 自身不拥有通用 `invoke()`。

本文以三层证据冻结兼容边界：

- **portable core**：严格遵循 Agent Plugins 1.0.0；
- **primary host profile**：优先兼容 Codex `.codex-plugin/plugin.json`、`.mcp.json`、`hooks/hooks.json` 与 Codex 11 个 Hook event；
- **secondary host profile**：读取 Claude Code `.claude-plugin/plugin.json` 中与上述四类组件重叠的固定目录与路径声明，不复制 Claude 的 LSP、monitor、PATH injection、settings、theme、output-style 或 marketplace authority。

Agent Plugins 1.0.0 只标准化 `skills/` 与 `mcp.json`。Hooks 与 Subagent 是明确的 client extension；本文绝不把它们伪称为 portable 1.0 core。

---

## 0. 执行结论

### 0.1 Plugin 的唯一产品含义

~~~text
PluginPackage
  -> contributes Skill root registration(s)
  -> contributes MCP server registration(s)
  -> contributes Hook handler definition(s)
  -> may expose a dormant Subagent-spec inventory

Skill           -> Round 9/9.1 instructional capability owner
MCP tool        -> Round 6/9 MCP supervisor and execution owner
Hook            -> Round 9.2 process-local hook dispatcher
Subagent spec   -> deferred to PHC-10; no current execution owner
~~~

Plugin 只回答：

> 哪些彼此相关的能力定义应作为一个可安装、可启用、可诊断的包被组合进 Host？

它不回答：

- Tool 是否获准执行；
- MCP slot 是否 READY；
- Skill 是否应被模型采用；
- Hook 是否可以绕过现有 canonical owner；
- Subagent 应如何拆图、调度、重试或汇总；
- Plugin 是否可加载宿主进程内 Python 代码。

### 0.2 四类组件及其 authority

| Plugin component | Portable 1.0 | Pulsara V1 | Provider 暴露 | 最终 authority |
|---|---:|---:|---|---|
| Agent Skill | 是 | 完整支持 | `SKILL_CATALOG` + ordinary `read_file` | Round 9.1 Skill source / filesystem truth |
| MCP server | 是 | stdio + Streamable HTTP；legacy SSE typed skip | direct tool 或 new-MCP meta route | Round 6/9 supervisor、slot、policy、attempt |
| Hook set | 否，client extension | 支持 Codex 11 events 的 command handler | 仅成功输出可形成 append-only `UNTRUSTED_OBSERVATION` | 现有 User/Tool/Permission/Compaction/Subagent owner；Hook 无 DB authority |
| Subagent spec set | 否，client extension | 只发现与报告 `DEFERRED_PENDING_PHC10` | 无 | future PHC-10 |

### 0.3 11 个 Hook event 使用独立 vocabulary

Hook lifecycle 不复用 `CommittedEventType` 或 `LiveEventType`。新增 closed process-local vocabulary：

~~~text
HookEventType
  SESSION_START_EVENT       = "SessionStartEvent"
  SESSION_END_EVENT         = "SessionEndEvent"
  USER_PROMPT_SUBMIT_EVENT  = "UserPromptSubmitEvent"
  PRE_TOOL_USE_EVENT        = "PreToolUseEvent"
  PERMISSION_REQUEST_EVENT  = "PermissionRequestEvent"
  POST_TOOL_USE_EVENT       = "PostToolUseEvent"
  PRE_COMPACT_EVENT         = "PreCompactEvent"
  POST_COMPACT_EVENT        = "PostCompactEvent"
  SUBAGENT_START_EVENT      = "SubagentStartEvent"
  SUBAGENT_STOP_EVENT       = "SubagentStopEvent"
  STOP_EVENT                = "StopEvent"
~~~

外部 Codex-compatible JSON 仍使用没有 `Event` 后缀的标准名称：

~~~text
SessionStart <-> SessionStartEvent
SessionEnd <-> SessionEndEvent
...
Stop <-> StopEvent
~~~

原因不是词形偏好，而是 authority 隔离：

- `CommittedEventType` 表示已经由 repository 接受的 durable occurrence；
- `LiveEventType` 表示 process-local UI stream；
- `HookEventType` 表示 Runtime 正在某个生命周期 seam 询问外部命令。

三者可能描述相近时刻，但不能因此共享 durable append、replay、ordering 或 subject 语义。

### 0.4 Prefix 与 authority 不变量

同一 Host、同一 exact ROOT/child scope、同一 continuity epoch继续满足：

~~~text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
~~~

因此：

- Plugin enable/disable 不直接改写 `BASE_SYSTEM`；
- Plugin 不直接增删 provider `tools[]`；
- new MCP 继续走 Round 9 meta route；
- Skill catalog变化继续走 Round 9.1 append-only successor observation；
- Hook 模型可见输出只进入 `PLUGIN_HOOK_ACTIVATION_CONTEXT`或`PLUGIN_HOOK_EVENT_CONTEXT`，`trust=UNTRUSTED_OBSERVATION`；
- Hook 的 `systemMessage` 只进入 UI/operational diagnostic，绝不成为 provider SYSTEM；
- Plugin 关闭后，已冻结 direct MCP descriptor继续保留并由 unavailable gate 拒绝，不热删 tool；
- Hook definition refresh 只影响未来 lifecycle event，不回写历史消息。

### 0.5 Durable 减法

本轮不新增：

- PostgreSQL relation、column或migration；
- `CommittedEventType`、`LiveEventType`、subject slot、append guard；
- durable job、receipt、checkpoint、projection、repair或event replay；
- cross-Host Hook recovery；
- durable “loaded plugin capability graph”；
- Hook 执行历史表。

激活后 architecture oracle 保持：

~~~text
Committed events       31
Live events            23
Subject slots          13
Append guards           2
Product relations      25
Durable jobs            1
Hook lifecycle types   11   # 独立process-local vocabulary，不计入前述oracle
~~~

---

## 1. 范围与非目标

### 1.1 本轮实现

1. Agent Plugins 1.0.0 root `plugin.json` parser 与 conformance bounds；
2. Codex `.codex-plugin/plugin.json` primary compatibility adapter；
3. Claude `.claude-plugin/plugin.json` secondary compatibility adapter；
4. local package add/remove/enable/disable/list/doctor；
5. user/workspace Plugin composition、workspace trust 与 safe-point refresh；
6. `${PLUGIN_ROOT}` / `${PLUGIN_DATA}` 及 Claude compatibility aliases；
7. Skill/MCP contribution normalization；
8. Codex-style `hooks/hooks.json`、command handlers、matchers、trust hash与11 events；
9. Subagent component的dormant inventory与明确诊断；
10. prefix、physical close、failure isolation与architecture gates。

### 1.2 明确不实现

- remote marketplace、Git clone、auto-update、publisher signing或dependency resolver；
- Codex Apps / `.app.json`、UI、assets rendering；
- Claude LSP、monitors、`bin/` PATH injection、settings、themes、output styles、commands；
- Hook `prompt` 或 `agent` handler；
- Hook `updatedInput` argument rewrite；
- Hook `updatedMCPToolOutput`、`updatedPermissions` 或 `suppressOutput`；
- Plugin Python entry point、dynamic import、shared-library injection；
- Plugin-defined permission preset或effect taxonomy；
- Plugin-defined durable event、job或repository mutation callback；
- standalone user/project/managed Hook config；本文engine只接收Plugin-bundled non-managed Hooks；
- Subagent spec schema、catalog、automatic selection、task graph、batch execution或result synthesis；
- Round 5B compaction 本身。

### 1.3 关于“不支持 argument rewrite”的精确边界

Codex `PreToolUse` 已支持部分 `updatedInput`。Pulsara V1有意不实现这一项，因为 rewrite 会改变：

- provider tool-call arguments identity；
- local authorize与human confirmation看到的对象；
- ToolExecutionAttempt candidate；
- MCP remote identity；
- memory citation与Round 3.1 exact binding。

Runtime不得静默忽略 `updatedInput` 后继续执行原调用。若一个 trusted Hook返回该字段：

~~~text
before attempt
  -> typed HOOK_UPDATED_INPUT_UNSUPPORTED rejection
  -> physical tool invoke count = 0
  -> existing tool owner产生普通、可见、可重试的local rejection
~~~

这不是通用 Hook failure 的 fail-open 分支，而是一个Runtime无法忠实执行的明确控制请求；执行原参数会比拒绝更危险。

---

## 2. Prior art 与取舍

### 2.1 Agent Plugins 1.0.0：portable truth

公开规范冻结：

- Plugin 是带manifest与可选组件的单一目录；
- root `plugin.json` 必需；
- `$schema` 必须为 `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`；
- portable top-level fields是closed set；unknown field需报告、忽略且不得赋义；
- `skills/` 与 `mcp.json` 是V1仅有的两个portable component；
- 每个 Skill只来自`skills/` immediate child的`SKILL.md`；
- MCP config使用独立 `mcp.json` 与exact schema版本；
- missing component不是错误，component failure应使用最窄边界；
- package path、symlink、cwd与plugin-relative command必须保持real-root containment；
- `${PLUGIN_ROOT}`与`${PLUGIN_DATA}`只在规范允许的字段作单次非递归展开；
- client-specific行为必须位于reverse-domain extension namespace或由client明确拥有。

本文完整接受这些portable语义，不把 Codex/Claude manifest field反向写入公开core。

### 2.2 Codex：primary host contract

Codex 提供本文最重要的宿主组合形状：

~~~text
.codex-plugin/plugin.json
skills/
.mcp.json
hooks/hooks.json
~~~

其关键优点是：

- Plugin仍是package，不成为执行器；
- Skill、MCP、Hook由各自owner加载；
- 安装/启用Plugin不自动trust其command hooks；
- trust绑定exact current hook definition；
- 同一event的matching handlers并发启动，单个Hook不能阻止其他Hook开始；
- event输入、matcher与输出采用可审计JSON；
- Hook拥有覆盖真实agent loop的11个lifecycle seam。

本文以Codex字段名、默认路径、external event name与command JSON shape为primary compatibility目标。

### 2.3 Claude Code：secondary host contract

Claude Code Plugin 更宽：除Skill/MCP/Hook/Agent外，还可包含LSP、monitor、bin、settings、themes与更多host features。本文只吸收重叠的四项：

- `.claude-plugin/plugin.json` identity；
- root `skills/`；
- root `.mcp.json`；
- root `hooks/hooks.json`；
- root `agents/` 的dormant package inventory。

同时提供：

~~~text
CLAUDE_PLUGIN_ROOT = PLUGIN_ROOT
CLAUDE_PLUGIN_DATA = PLUGIN_DATA
~~~

以支持大量只依赖路径变量的existing Hook脚本。Claude专属frontmatter、permission、model、LSP与PATH semantics不进入Pulsara。

### 2.4 不照搬的部分

| Prior art | 不照搬 | 原因 |
|---|---|---|
| Agent Plugins | legacy `sse` transport执行 | Round 6未支持；typed skip即可保持conformance |
| Codex | Hook output成为developer/system context | 会提高第三方data authority并破坏Pulsara trust模型 |
| Codex | PreToolUse argument rewrite | 当前attempt/authorization/identity contract无法诚实承载 |
| Codex | PostToolUse替换原ToolResult | effect已经发生；canonical result不得被Hook遮蔽或改写 |
| Claude | inline MCP/Hook/Agent全面开放语义 | 会复制另一个宿主的authority与大量optional字段 |
| Claude | `bin/`自动加入PATH | 等价于未审计的全局command injection |
| 两者 | private cache扫描 | cache layout不是package标准，也不能证明enablement/trust |

---

## 3. Plugin package profiles

### 3.1 Closed profile union

~~~text
PluginManifestProfile
  AGENT_PLUGINS_1_0
  CODEX_PLUGIN
  CLAUDE_PLUGIN
~~~

AUTO discovery precedence固定为：

1. root `plugin.json`；
2. `.codex-plugin/plugin.json`；
3. `.claude-plugin/plugin.json`。

第一个存在的manifest决定profile。若该manifest无效，整个Plugin拒绝；不得回退到下一profile，也不得把多个manifest的identity/component声明merge。较低优先级manifest只产生`shadowed_manifest` diagnostic。

CLI可用`--profile`显式选择；显式profile要求对应manifest存在并合法。

### 3.2 Portable manifest

`AGENT_PLUGINS_1_0` exact fields：

~~~text
$schema       required exact canonical URI
name          required, 1..64 chars, standard charset/rules
version       optional bounded string
description   optional bounded string
author        optional closed object(name,email,url)
homepage      optional bounded string
repository    optional bounded string
license       optional bounded string
keywords      optional bounded string array
extensions    optional object keyed by reverse-domain namespace
~~~

Unknown top-level field：report + ignore。`extensions`不是object时按公开规范report + ignore该字段并继续；其他schema/type violation：reject Plugin。

Pulsara实现namespace：

~~~text
io.github.plumliu.pulsara
~~~

V1只允许其中声明：

~~~json
{
  "hooks": "./hooks/hooks.json",
  "agents": "./agents/"
}
~~~

省略时仍探测Codex/Claude共同默认位置`hooks/hooks.json`与`agents/`；它们是Pulsara client-extension behavior，不改变Plugin对Agent Plugins 1.0的core conformance判断。

### 3.3 Codex profile adapter

Codex adapter读取：

~~~text
name, version, description, author, homepage, repository, license, keywords
skills
mcpServers
hooks
~~~

`interface`、`apps`与assets只报告`unsupported_presentation_component`，不影响四类受支持组件。

路径字段必须：

- 以`./`开始；
- 相对Plugin root解析；
- realpath后仍位于Plugin root；
- 不指向device、socket或非预期kind。

默认路径：

~~~text
skills      -> ./skills/
mcpServers  -> ./.mcp.json
hooks       -> ./hooks/hooks.json
agents      -> ./agents/   # Pulsara client-extension slot；current Codex不赋予语义；当前dormant
~~~

`hooks`可接受Codex的single path、path array、inline object或inline object array。所有条目按manifest order编号；同一physical file不得重复加载。

### 3.4 Claude profile adapter

Claude adapter只读取与V1 overlap的identity/path字段。默认位置与Claude兼容：

~~~text
skills/
.mcp.json
hooks/hooks.json
agents/
~~~

Claude custom path若不是plugin-contained `./` path则typed reject对应component。commands、LSP、monitors、output styles、settings、themes与`bin/`不加载，也不让Plugin整体失败。

### 3.5 Bounds

~~~text
maximum enabled plugins per Host                  = 64
maximum manifest UTF-8 bytes                      = 256 KiB
maximum manifest JSON nodes                       = 16,384
maximum manifest JSON depth                       = 64
maximum manifest string UTF-8 bytes               = 64 KiB
maximum keyword count                             = 256
maximum component files per Plugin                = 1,024
maximum aggregate inspected package metadata      = 4 MiB
maximum filesystem path UTF-8 bytes                = 4,096
~~~

JSON必须在`json.loads()`前使用Round 6已证明的complete structural scanner；共享实现应下沉到`primitives/bounded_json.py`，Plugin不得反向依赖MCP package。

这些是Host safety bounds，不是portable schema的伪标准。触发时必须给出Pulsara-specific typed diagnostic。

---

## 4. Install、enablement 与 scope

### 4.1 V1只支持local package source

本轮不定义distribution protocol。CLI只接受已在本机存在的目录：

~~~text
pulsara plugins add <local-directory> [--scope user|workspace] [--profile ...]
pulsara plugins remove <plugin-id> [--scope ...] [--purge-data]
pulsara plugins enable <plugin-id> [--scope ...]
pulsara plugins disable <plugin-id> [--scope ...]
pulsara plugins list [--workspace ...]
pulsara plugins doctor [plugin-id]
pulsara plugins review-hooks <plugin-id>
pulsara plugins trust-hooks <plugin-id> --expected-definition-digest <sha256>
pulsara plugins untrust-hooks <plugin-id>
~~~

`add`使用bounded copy到temporary sibling、fsync、validate、atomic rename；最多复制8,192个regular files与512 MiB aggregate bytes，任何symlink escape/device/socket都拒绝。不得symlink到任意private package cache。更新使用再次`add --replace`，同样先完整validate。

### 4.2 Managed roots

~~~text
USER plugin root
  ${PULSARA_HOME}/plugins/<plugin-id>/

WORKSPACE plugin root
  <workspace>/.pulsara/plugins/<plugin-id>/

USER state
  ${PULSARA_HOME}/plugins.yaml

WORKSPACE state
  <workspace>/.pulsara/plugins.yaml

PLUGIN_DATA
  ${PULSARA_HOME}/plugin-data/<scope-identity>/<plugin-id>/
~~~

`plugin-id`由installer配置key拥有；manifest `name`必须与其相等。不得仅凭扫描private cache推断安装状态。

两个`plugins.yaml`使用同一个closed shape，package path由scope与`plugin-id`隐式确定，不允许manifest或config指向任意外部目录：

~~~yaml
plugins:
  repo-policy:
    enabled: true
    profile: AUTO
~~~

约束：

~~~text
maximum config UTF-8 bytes  = 256 KiB
maximum entries            = 64
allowed entry keys         = enabled | profile
profile                    = AUTO | AGENT_PLUGINS_1_0 | CODEX_PLUGIN | CLAUDE_PLUGIN
~~~

CLI修改使用temp sibling + fsync + atomic replace。删除package默认保留对应`PLUGIN_DATA`；只有用户显式`--purge-data`才在exact resolved target下删除。

Workspace state与package内容属于repository-controlled data。Host默认只inspect，不执行其中MCP或Hook；必须由exact Host open显式`trust_workspace_plugins=True`，CLI对应`--trust-workspace-plugins`。仅列出Skill仍遵循Round 9.1的低authority filesystem规则，但启用整个Plugin不会借此绕过MCP/Hook trust。

### 4.3 Enablement 与 component trust分离

~~~text
INSTALLED != ENABLED
ENABLED   != HOOK_TRUSTED
ENABLED   != MCP_AUTHORIZED
~~~

- enablement使Plugin贡献component definitions；
- MCP server仍经过Round 6 config、scope、secret、network与effect policy；
- command Hook只有exact definition digest被用户trust后才运行；
- Skill body始终是untrusted data；
- Subagent spec仍dormant。

### 4.4 Identity

~~~text
PluginInstanceIdentity
  plugin_id
  install_scope: USER | WORKSPACE
  workspace_id?: exact canonical workspace identity
  manifest_profile
  filesystem_resolved_root
~~~

`version`只是presentation/update hint，不是integrity identity。每次composition cut冻结：

~~~text
manifest_content_digest
component_inventory_digest
hook_definition_digest
mcp_definition_digest
skill_root_binding_digest
subagent_inventory_digest
package_snapshot_fingerprint
~~~

fingerprint只证明当前process-local package cut，不持久化为receipt或generation graph。

---

## 5. Package snapshot 与 component failure isolation

### 5.1 Single cut

Plugin manager在safe point执行：

~~~text
read enabled config inventory
  -> resolve exact real roots
  -> select one manifest profile
  -> bounded read manifest/component descriptors
  -> verify every path containment and file kind
  -> stat-before / read / stat-after
  -> freeze one FrozenPluginPackageSnapshot
~~~

若任一被读取文件在cut期间变化，保持已安装的prior snapshot并报告`PACKAGE_CHANGED_DURING_FREEZE`；不得发布混合old/new component set。

### 5.2 Component dispositions

~~~text
PluginComponentDisposition
  COMPLETE
  ABSENT
  INVALID
  UNSUPPORTED
  UNTRUSTED
  DEFERRED_PENDING_PHC10
~~~

每个enabled Plugin的snapshot必须恰好包含四个component disposition：Skill、MCP、Hook、Subagent。缺省不是“忘了加载”，而是显式`ABSENT`。

### 5.3 Narrow failure matrix

| Failure | Plugin | Skill | MCP | Hook | Subagent |
|---|---|---|---|---|---|
| selected manifest invalid | reject | none | none | none | none |
| `skills/` wrong kind | keep | INVALID | unaffected | unaffected | unaffected |
| one Skill invalid | keep | skip exact Skill | unaffected | unaffected | unaffected |
| MCP top-level invalid | keep | unaffected | INVALID | unaffected | unaffected |
| one MCP server invalid/unsupported | keep | unaffected | skip exact server | unaffected | unaffected |
| Hook file invalid | keep | unaffected | unaffected | INVALID file | unaffected |
| one Hook handler invalid | keep | unaffected | unaffected | skip exact handler | unaffected |
| agents dir present | keep | unaffected | unaffected | unaffected | DEFERRED |
| symlink escape | keep/reject narrow target | narrow skip | narrow skip | narrow skip | narrow skip |

### 5.4 Contribution plan

~~~text
FrozenPluginContributionPlan
  plugin_instance_identity
  package_snapshot_fingerprint
  skill_root_registrations
  mcp_server_registrations
  hook_definition_set
  deferred_subagent_inventory
  diagnostics
  contribution_plan_fingerprint
~~~

Plugin composition owner签发该plan后：

- Skill root registration交给Round 9.1 `LocalSkillProvider` owner；
- MCP server registration交给Round 6/9 config/supervisor owner；
- Hook definition进入本文process-local registry；
- Subagent inventory不进入当前 capability registry。

Round 9 central registration-set factory必须消费包含Plugin contributions的owner-issued exact inventory，而不是允许generic caller自报。Plugin manager只贡献registrations，不取代Builtin/MCP/Skill owner签名。

### 5.5 Namespacing

为避免多个包污染同一名字空间：

~~~text
Plugin Skill provider name = <plugin-id>:<skill-name>
Plugin MCP server id       = <plugin-id>.<server-id>
Plugin Hook identity       = <plugin-id>:<hook-file-ordinal>:<group-ordinal>:<handler-ordinal>
Future Subagent name       = reserved <plugin-id>:<agent-name>
~~~

Skill的portable manifest `name`仍与directory basename一致；namespace只属于Host exposure identity，不改写Plugin文件。MCP remote server/tool identity继续由Round 6拥有，prefix只用于Host config registration避免collision。

---

## 6. Skill 与 MCP 组合

### 6.1 Skill contribution

Plugin `skills/`成为一个普通`SAFE_POINT_REFRESHABLE` Round 9 Skill root registration：

- immediate-child `SKILL.md` discovery；
- Agent Skills标准parse；
- 64 KiB file safety bound与Round 9.1 catalog/full-description contract；
- plugin root containment；
- ordinary `read_file` progressive disclosure；
- no `allowed-tools` authority；
- no Plugin-private loaded-state。

Plugin enable/refresh/disable通过同一append-only `SKILL_CATALOG` source表达；不改写BASE_SYSTEM。

### 6.2 MCP contribution

Portable `mcp.json`按Agent Plugins 1.0 normalize：

- stdio -> Round 6 `StdioTransportConfig`；
- streamable-http -> Round 6 `StreamableHttpTransportConfig`；
- sse -> `UNSUPPORTED_TRANSPORT`，只跳过server；
- `${PLUGIN_ROOT}` / `${PLUGIN_DATA}`只在args/env/cwd单次展开；
- command不展开placeholder且必须是single executable token；
- non-loopback HTTP、userinfo、fragment、duplicate header casing按公开规范拒绝；
- manifest env/header不得成为secret mechanism。

Codex/Claude `.mcp.json` adapter接受各自常见direct map、`mcp_servers`或`mcpServers` wrapper，随后必须normalize成同一个Round 6 config union。Adapter不得把unknown host field塞入generic metadata后继续执行。

### 6.3 Dynamic effect

Plugin在epoch中启用：

- new MCP进入Round 9 `NEW_MCP_META_ONLY`；
- new Skill追加catalog successor；
- new trusted Hook从下一个matching lifecycle event起生效；
- direct native tools不热增。

Plugin禁用：

- future Hooks停止admission，active Hook command drain/kill遵循physical owner；
- MCP不再接受新dispatch，supervisor在leases drain后close；
- epoch中已有direct descriptor保持但返回typed unavailable；
- Skill catalog追加removed/unavailable successor；
- provider SYSTEM/tools不变。

---

## 7. Hook config contract

### 7.1 Codex-compatible root shape

~~~json
{
  "description": "Optional plugin hooks",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^terminal$|^mcp__.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${PLUGIN_ROOT}/hooks/pre_tool.py",
            "timeout": 30,
            "statusMessage": "Checking tool use",
            "additionalContextLimit": 2500,
            "async": false
          }
        ]
      }
    ]
  }
}
~~~

V1 handler type只允许`command`。`prompt`与`agent`可parse为`UNSUPPORTED_HANDLER_TYPE`，不得运行。

### 7.2 Definition bounds

~~~text
maximum hook files per Plugin                    = 16
maximum hook JSON bytes per file                 = 1 MiB
maximum hook JSON nodes                          = 16,384
maximum hook JSON depth                          = 64
maximum matcher groups per file                  = 256
maximum handlers per matcher group               = 16
maximum command UTF-8 bytes                      = 8,192
maximum matcher UTF-8 bytes                      = 1,024
maximum status message UTF-8 bytes               = 4,096
maximum selected handlers per event              = 16
maximum concurrently running hook commands/Host  = 16
maximum background hook commands/Host            = 8
~~~

### 7.3 Matcher

Codex matcher语义保留：空、`*`或缺省表示match all；其他值是regex。Pulsara不得直接用可能灾难回溯的Python `re`执行第三方pattern。

实现必须使用linear-time、Rust-regex/RE2-compatible engine。unsupported lookaround/backreference在load时typed skip exact matcher group。Matcher input最多512 UTF-8 bytes。

| External event | matcher target |
|---|---|
| SessionStart | `startup | resume | clear | compact` source |
| SessionEnd | `other` compatibility reason |
| UserPromptSubmit | matcher ignored |
| PreToolUse | canonical logical tool name + closed aliases |
| PermissionRequest | canonical logical tool name + closed aliases |
| PostToolUse | canonical logical tool name + closed aliases |
| PreCompact | `manual | auto` |
| PostCompact | `manual | auto` |
| SubagentStart | `agent_type` |
| SubagentStop | `agent_type` |
| Stop | matcher ignored |

Tool aliases只保留诚实映射：

- `terminal`可额外匹配Codex compatibility alias `Bash`；
- `apply_patch`可匹配`apply_patch | Edit | Write`；
- MCP使用exact provider-qualified logical name；
- meta-routed MCP在resolve后只触发一次，以resolved MCP logical name匹配，并在extension field保留`provider_tool_name=use_new_mcp_tool`；
- 不按description或模糊相似度匹配。

### 7.4 Trust digest

`hook_definition_digest`覆盖：

~~~text
plugin instance identity
selected profile
ordered hook files / inline objects
event name
matcher
handler type
command / commandWindows
timeout
async
statusMessage
additionalContextLimit
environment contract version
~~~

安装或enable不自动trust。`trust-hooks`必须带调用者刚review到的expected digest；写入前重新freeze package并exact compare。任一covered definition field变化即撤销trust。和Codex一样，这个trust确认的是command definition，不是对shell command随后读取的全部脚本、解释器或依赖做供应链签名；用户仍需只信任自己愿意执行的local package。

Trust record只存用户侧`${PULSARA_HOME}/plugin-hook-trust.json`，即使目标Plugin是WORKSPACE scope也不得把trust写进repository：

~~~json
{
  "trusted_plugin_hooks": {
    "<domain-separated-plugin-instance-key>": {
      "definition_digest": "sha256:..."
    }
  }
}
~~~

文件上限256 KiB、最多256项，使用atomic replace。Instance key覆盖install scope与canonical workspace identity；Trust不进入PostgreSQL，不跨plugin instance复用，也不按manifest version泛化。

---

## 8. Hook physical execution

### 8.1 Exact command attempt owner

~~~text
HookCommandAttempt
  definition identity/fingerprint
  HookEventType
  immutable JSON stdin bytes
  plugin root/data paths
  admitted_at_monotonic
  absolute deadline
  process identity
  stdout/stderr bounded accumulators
  state: PREPARING | PROCESS_INSTALLED | RESULT_READY | ABORT_REQUESTED | SETTLED
~~~

该owner纯process-local。Hook dispatcher与worker在registry lock下裁决exact process；timeout/cancel/Host close时kill process group并join。不得detach无owner subprocess。

### 8.2 Timeout

为兼容Codex健康操作空间：

~~~text
ordinary Hook default timeout = 600 seconds
ordinary Hook allowed range   = 1..600 seconds
SessionEnd default timeout    = 1 second
SessionEnd allowed range      = 1..3 seconds
Hook abort kill/join deadline = 60 seconds
Hook registry close deadline  = 120 seconds
~~~

每个command使用独立deadline；不存在turn-wide Hook deadline。Background Hook受同样timeout与close owner约束。两项close deadline属于Round 5A closed watchdog policy的新process-local owner，不复用Host session/job/GC close常量。

### 8.3 Output bounds

~~~text
maximum stdout bytes per command          = 1 MiB
maximum stderr bytes per command          = 1 MiB
maximum parsed JSON nodes                 = 16,384
maximum parsed JSON depth                 = 64
maximum one model-visible context body    = 40,000 UTF-8 bytes
maximum aggregate Hook context per event  = 160,000 UTF-8 bytes
~~~

`additionalContextLimit`保持Codex字段名与approximate-token意图：

- default `2500`；
- allowed `0..10000`；
- process-local budgeter使用deterministic但仅属soft policy的`ceil(utf8_bytes / 4)` quote；它不声称是provider tokenizer或correctness bound；
- `0`表示不施加soft limit，但仍受40,000-byte hard bound；
- 超soft limit形成head/tail preview与omitted-byte count；
- V1不建立durable spill/artifact；完整Hook输出仍可由Plugin自己写入`PLUGIN_DATA`并在preview中返回路径。

### 8.4 Environment

Hook command是Codex-compatible shell command string；Pulsara通过platform command shell启动，因而它是明确的arbitrary-code trust boundary。

在launch前，对command、commandWindows与其他Hook-owned string只执行`${PLUGIN_ROOT}`、`${PLUGIN_DATA}`、`${CLAUDE_PLUGIN_ROOT}`、`${CLAUDE_PLUGIN_DATA}`的单次非递归exact replacement；不执行任意`${ENV}`模板。替换后command仍由definition digest与exact trust覆盖。

工作目录固定为Host workspace root。环境只继承minimal safe baseline：`PATH`、`HOME`、`USER`、`SHELL`、`TMPDIR`与locale；随后Runtime覆盖：

~~~text
PLUGIN_ROOT
PLUGIN_DATA
CLAUDE_PLUGIN_ROOT = PLUGIN_ROOT
CLAUDE_PLUGIN_DATA = PLUGIN_DATA
PULSARA_PLUGIN_ID
~~~

不得传入：

- provider API key、Authorization/header；
- PostgreSQL DSN；
- MCP sealed secret值或requestState；
- embedding/rerank key；
- hidden reasoning或完整provider prompt；
- Runtime私有receipt/fingerprint map。

### 8.5 Concurrent start、ordered settlement

同一event全部matching command handler先在全局并发bound内启动；一个Handler的deny/失败不能阻止其他matching handler开始。结果完成顺序可以不同，但aggregate必须按frozen configured order决定：

~~~text
all matching handlers launched
  -> collect bounded outcomes
  -> sort by configured ordinal
  -> any DENY/STOP wins where event supports it
  -> otherwise ALLOW wins where event supports it
  -> otherwise DEFER/CONTINUE
  -> concatenate accepted additionalContext in configured order
~~~

### 8.6 Background handler

`async=false`是默认且是所有gating Hook的推荐形状。`async=true`使用Codex-compatible advisory background语义：

- lifecycle boundary不等待command；
- deny、allow、continue、stop、rewrite等control field全部typed ignored并形成diagnostic；
- bounded `additionalContext`只有在exact scope仍active时，才在下一个provider safe point追加；
- scope已结束、Host closing或definition已撤销时丢弃尚未交付输出；
- `SessionEnd`无条件synchronous；
- background command同样必须timeout、kill/join，Host close不得留下orphan。

因此安全策略不得把`async=true`当作PreToolUse/PermissionRequest enforcement boundary。

### 8.7 Failure policy

Plugin Hook是non-managed Hook：

- untrusted -> skip + diagnostic；
- spawn error / timeout / signal / malformed JSON / ordinary nonzero -> fail-open + diagnostic；
- explicit supported deny/block -> honor；
- output over hard bound -> fail-open + diagnostic；
- unsupported `updatedInput` before tool -> typed reject exact tool call；
- unsupported post-effect rewrite -> keep exact canonical result + diagnostic；
- caller cancellation只能detach waiter；physical attempt owner继续kill/join或settle。

本文不实现managed policy Hook。Future managed Hook若加入，必须是另一source kind与fail-closed contract，不能通过Plugin manifest伪装。

---

## 9. Hook input/output wire

### 9.1 Common input

每个command在stdin收到一个bounded JSON object：

~~~json
{
  "session_id": "...",
  "transcript_path": null,
  "cwd": "/workspace",
  "hook_event_name": "PreToolUse",
  "model": "provider/model",
  "turn_id": "...",
  "permission_mode": "bypassPermissions"
}
~~~

`transcript_path`保持Codex nullable shape，Pulsara V1固定为`null`；本轮不建立另一份transcript spool或不稳定文件API。Hook只获得对应event显式字段。

Permission mapping：

~~~text
READ_ONLY           -> plan
ASK_PERMISSIONS     -> default
ACCEPT_EDITS        -> acceptEdits
BYPASS_PERMISSIONS  -> bypassPermissions
~~~

Pulsara-specific精确值可放在`pulsara_permission_mode` extension field，不能让external compatibility field出现未知字符串。

### 9.2 Common output

接受Codex JSON envelope：

~~~json
{
  "continue": true,
  "stopReason": "optional",
  "systemMessage": "optional UI warning",
  "suppressOutput": false,
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "optional"
  }
}
~~~

Pulsara normalization：

- `systemMessage` -> UI/operational diagnostic only；
- `additionalContext` -> bounded activation/event Hook context source；
- `suppressOutput=true` -> unsupported diagnostic；
- plain stdout只在Codex允许的event映射为`additionalContext`；
- unknown field保留forward-compatible ignore，但unknown control field不得获得语义。

### 9.3 Provider carrier

不能把session-long context与one-shot反馈塞进同一个source head。固定使用两个低authority source：

~~~text
PLUGIN_HOOK_ACTIVATION_CONTEXT
  producers   SessionStartEvent | SubagentStartEvent
  trust       UNTRUSTED_OBSERVATION
  lifecycle   ACTIVATION
  presence    VALUE | CLEARED | UNAVAILABLE
  body        exact-scope current activation-context snapshot

PLUGIN_HOOK_EVENT_CONTEXT
  producers   UserPromptSubmitEvent | PreToolUseEvent | PermissionRequestEvent
              PostToolUseEvent | PreCompactEvent | PostCompactEvent
              SubagentStopEvent | StopEvent
  trust       UNTRUSTED_OBSERVATION
  lifecycle   TURN | CALL | ONE_SHOT, as frozen by event matrix
  presence    VALUE | NOT_APPLICABLE
  body        ordered bounded event-context entries
~~~

`PluginHookActivationContextOwner`只在process-local exact scope保存当前activation snapshot。Plugin disable、definition replacement或scope close时，下一safe point为activation source追加不含旧entry的successor，必要时`CLEARED`；它不持久化、不跨Host恢复。Event context不成为snapshot，不因普通无输出Hook追加CLEARED。

Provider body不携带Hook definition digest、Plugin filesystem root、process id或internal contract version。每项只含：

~~~json
{
  "plugin": "repo-policy",
  "event": "PreToolUse",
  "message": "The generated files require review."
}
~~~

Hook输出不能成为SYSTEM、developer role、ToolResult replacement或permission truth。

---

## 10. 11 个 Hook event 的 exact seam

### 10.1 Event matrix

| Internal type | External name | Fire point | Model context lifecycle | Control outcome |
|---|---|---|---|---|
| `SessionStartEvent` | `SessionStart` | Host composition完成、首次/resume dispatch前 | `ACTIVATION` | context；`continue=false`可阻止本次provider open |
| `SessionEndEvent` | `SessionEnd` | Host close已停止admission并join业务owner后、Hook engine close前 | none | observe only |
| `UserPromptSubmitEvent` | `UserPromptSubmit` | exact ROOT user command canonical admission前 | `TURN` | block or context |
| `PreToolUseEvent` | `PreToolUse` | logical tool resolve后、authorize/confirmation/attempt前 | `CALL` | deny/allow/context；no rewrite |
| `PermissionRequestEvent` | `PermissionRequest` | existing policy确实需要human confirmation时、interaction公开前 | `CALL` | deny/allow/defer |
| `PostToolUseEvent` | `PostToolUse` | exact ToolResult canonical settlement后 | `CALL` | context or interrupt continuation；不能改写result |
| `PreCompactEvent` | `PreCompact` | Round 5B lane取得且target重验后、summary/fence前 | `ONE_SHOT` | block compaction/context |
| `PostCompactEvent` | `PostCompact` | Round 5B canonical adoption FULL后；active安装分支还需成功安装 | `ONE_SHOT` | context or stop continuation |
| `SubagentStartEvent` | `SubagentStart` | existing child task admission后、child首次provider open前 | child `ACTIVATION` | context；不能取消已接受task |
| `SubagentStopEvent` | `SubagentStop` | natural child terminalization前；forced terminalization后observe-only | `ONE_SHOT` | natural path可请求一次continuation |
| `StopEvent` | `Stop` | natural ROOT turn completion提交前 | `ONE_SHOT` | 可请求一次continuation |

### 10.2 SessionStartEvent

External fields：`source=startup|resume|clear|compact`。

- V1真实发出`startup|resume`；
- `compact`由Round 5B激活后发出；
- `clear`保留compatibility vocabulary，当前无producer；
- Hook context进入首次/恢复后的exact scope；
- 若任一trusted Hook明确`continue=false`，provider open为0，但Host仍可接受下一用户命令。

### 10.3 SessionEndEvent

- 只对ROOT Host session；不替代SubagentStop；
- external `reason`固定为Codex-compatible `other`；Pulsara reason放extension；
- synchronous，default 1s/max 3s；
- output不进入model；
- 失败不得阻塞physical Host close。

### 10.4 UserPromptSubmitEvent

只对真实ROOT `USER_MESSAGE` submission发出；不对steer、automatic continuation、Hook continuation或subagent objective伪造“用户提交”。

顺序：

~~~text
freeze exact user command candidate
  -> run trusted UserPromptSubmit Hooks
  -> BLOCK: no canonical user entry, typed command rejection
  -> CONTINUE: existing start_root_turn admission/confirmation
  -> accepted Hook context joins same dispatch planning cut
~~~

Hook失败不改变用户command。Hook没有直接写entry的authority。

### 10.5 PreToolUseEvent

顺序唯一冻结为：

~~~text
resolve provider tool call to exact logical capability/binding
  -> PreToolUse Hook
  -> ordinary permission/effect authorize
  -> possible PermissionRequest Hook / human confirmation
  -> operation admission permit
  -> canonical ToolExecutionAttempt
  -> physical invoke
~~~

因此Hook deny时：

- no ToolExecutionAttempt；
- no remote/physical effect；
- existing tool owner形成typed local denial ToolResult；
- model可修正调用；
- Hook本身不提交canonical row。

### 10.6 PermissionRequestEvent

只在当前permission policy原本会公开human confirmation时运行。BYPASS/READ_ONLY static allow/deny路径不触发。

Aggregate：

~~~text
any deny -> deny
else any allow -> allow without UI
else -> existing human interaction
~~~

最终Capability/Interaction decision仍由现有repository transaction持有，并记录decision source为process-local policy provenance；Hook没有append authority。

### 10.7 PostToolUseEvent

输入使用Round 7.1 pure renderer从已接受canonical result构造的bounded `HookToolResponseView`，不读取sealed raw secret或任意大canonical body。该view使用当时可用的ordinary public preview与最多40,000-byte logical envelope，但不是尚未发生的下一次compiler variant selection；Hook不得假装知道下一次provider最终选择FULL/COMPACT/REF_ONLY中的哪一种。

Hook不能：

- 撤销已发生effect；
- 删除、替换或hide canonical ToolResult；
- 把known result改为unknown；
- 触发tool自动重跑。

`decision=block`或`continue=false`只可：保留原ToolResult，追加untrusted feedback，并在其后interrupt/continue model loop。Late result只有在current writer实际canonicalize并可继续对应scope时才发PostToolUse；stale writer无跨Host Hook delivery。

### 10.8 PreCompactEvent / PostCompactEvent

两项类型与config现在实现，但在Round 5B ACTIVATED前没有producer。

Round 5B必须消费本文registry，不得自建compaction-only Hook engine。PreCompact block若使当前run无法通过context/resource boundary继续，则由Round 5B返回typed resource boundary；Hook不能强迫Runtime发送超界provider request。

### 10.9 SubagentStartEvent / SubagentStopEvent

这两项可绑定当前flat child task，不依赖future hierarchical graph。

- Start附加context只进入exact child scope；
- `continue=false`不撤销已经原子接受的child task；
- natural Stop可请求至多一次child continuation；
- user stop、Host close、takeover或failure path只observe，不可复活task；
- future PHC-10必须复用这两个event，而不是新增GraphSubagentHook vocabulary。

### 10.10 StopEvent

只在natural ROOT completion前运行。`decision=block`表示“继续当前run”，但Pulsara不伪造user message：

~~~text
Hook reason
  -> PLUGIN_HOOK_EVENT_CONTEXT ONE_SHOT
  -> existing automatic continuation owner
  -> next provider call in same run
~~~

每个natural stop transition最多允许一次Hook continuation；后续`stop_hook_active=true`。显式user stop、Host close、provider failure或resource interruption不运行可continuation Stop Hook。

---

## 11. Dynamic refresh 与 continuity

### 11.1 Safe-point only

Plugin add/remove/enable/disable/package refresh只能在Host safe point安装successor composition：

- 不在provider stream中途替换Hook registry；
- 不在assistant tool batch中途改变MCP/Skill registrations；
- 不让Hook definition在同一次event dispatch中变化；
- old package snapshot borrow drain后释放。

### 11.2 Exact event registry borrow

每个event dispatch取得immutable：

~~~text
FrozenHookRegistryView
  ordered trusted handlers
  package snapshot fingerprints
  definition digests
  event matcher plan
  registry view fingerprint
~~~

refresh只能生成successor view。已开始的event使用old view完整drain；新event使用successor。Plugin disable不能detach已启动command。

### 11.3 Model-visible context exact join

Hook additional context在compiler前形成immutable projection，并加入本次source collection。Continuity candidate必须exact join该projection fingerprint；CAS失败丢弃call-local projection并从current source heads重建，不把old Hook输出错误安装到new epoch head。

### 11.4 No provider-tool mutation

Hooks不是provider tool；enable Hook不改变`tools[]`。Plugin MCP/Skill变化只使用Round 9/9.1既有direct/meta/catalog contract。由此同epoch strict prefix保持可机器证明。

---

## 12. Subagent component deferred contract

### 12.1 为什么现在保留component slot

Codex与Claude Plugin生态都把specialized agent/subagent视为常见package成员。完全忽略`agents/`会让Pulsara installer错误声称一个包已完整支持；现在定义包级slot可以诚实报告：

~~~text
Plugin valid
Skills/MCP/Hooks active as applicable
Subagent component discovered but deferred pending PHC-10
~~~

### 12.2 本轮唯一允许的操作

- 检查`agents/`是否为plugin-contained directory；
- bounded枚举immediate child regular Markdown files；
- 冻结relative path、byte size与content digest；
- 输出`DEFERRED_PENDING_PHC10` diagnostic；
- 不读取正文进provider，不解析host-specific frontmatter，不执行。

~~~text
maximum dormant agent files per Plugin       = 128
maximum one dormant agent file safety bytes  = 256 KiB
maximum aggregate dormant inventory bytes    = 2 MiB
~~~

### 12.3 PHC-10必须决定的内容

- portable还是Codex/Claude profile schema；
- objective、model、tools、permission与scope；
- hierarchical/batch task graph；
- scheduling、join、cancel、failure与result synthesis；
- provider catalog与routing；
- Plugin agent version refresh。

Round 9.2不得提前以脆弱规则把Claude `agents/*.md`转换为当前flat subagent task。

---

## 13. DTO 与 owner 边界

### 13.1 Pure values

~~~text
PluginManifestProfile
PluginInstallScope
PluginComponentKind
PluginComponentDisposition
PluginInstanceIdentity
FrozenPluginManifest
FrozenPluginPackageSnapshot
FrozenPluginContributionPlan
FrozenDeferredSubagentInventory

HookEventType
FrozenHookHandlerDefinition
FrozenHookDefinitionSet
FrozenHookRegistryView
HookEventRequest = closed 11-variant union
HookRunOutcome
HookEventAggregateOutcome
FrozenHookContextProjection
~~~

### 13.2 Owners

| Owner | 唯一职责 | 明确不拥有 |
|---|---|---|
| `PluginInstallConfigOwner` | local package state与atomic config write | capability execution |
| `PluginCompositionOwner` | enabled package cut、refresh、contribution plan | Skill/MCP discovery truth |
| Round 9 Skill/MCP source owners | ordinary complete snapshots | Plugin enablement |
| `PluginHookTrustStore` | exact definition digest trust | Hook execution outcome |
| `PluginHookRegistryOwner` | trusted handler composition与immutable views | canonical rows |
| `PluginHookActivationContextOwner` | exact-scope Session/Subagent Start context snapshot | durable memory或Hook execution |
| `HookCommandAttemptOwner` | exact subprocess、timeout、kill/join | lifecycle product decision |
| Host/Runner/Tool/Subagent/Compaction owner | event seam与最终typed product action | Hook process management |

### 13.3 Forbidden dependency direction

~~~text
plugins/contracts.py        -> primitives only
plugins/manifests.py        -> contracts + bounded JSON/path helpers
plugins/composition.py      -> manifests + Round 9 registration adapters
plugins/hooks/contracts.py  -> primitives + plugin identity
plugins/hooks/dispatcher.py -> hook contracts/process owner

Round 9/9.1 owners may consume Plugin registrations
Plugin code must not import repository implementation
Hook code must not call repository append methods
~~~

---

## 14. Implementation slices

### R9.2-0：portable package core

- bounded JSON primitive；
- Agent Plugins 1.0 root manifest；
- path/symlink containment；
- portable `skills/`与`mcp.json`；
- component disposition/failure matrix；
- conformance fixtures。

### R9.2-A：Codex/Claude adapters 与 local lifecycle

- `.codex-plugin` primary adapter；
- `.claude-plugin` secondary adapter；
- managed user/workspace roots；
- add/remove/enable/disable/list/doctor；
- `PLUGIN_DATA`与workspace trust；
- Skill/MCP contribution integration。

### R9.2-B：Hook definition、trust 与 physical owner

- `HookEventType` 11项；
- Codex hooks JSON；
- linear-time matcher；
- trust digest/review CLI；
- command attempt owner、bounds、concurrency、close；
- common input/output adapter。

### R9.2-C：11 lifecycle seams

- User admission；
- tool resolve/permission/result；
- current flat subagent start/stop；
- Host start/end；
- natural Stop continuation；
- dormant Pre/PostCompact producers until Round 5B；
- append-only activation/event Hook context sources。

### R9.2-D：dormant Subagent component

- package inventory；
- diagnostic；
- no execution/provider exposure；
- PHC-10 handoff documentation。

### R9.2-E：activation evidence

- retained Round 6/7/7.1/9/9.1；
- full pytest/PostgreSQL；
- local real Plugin dogfood；
- oracle/link/secret/architecture checks。

---

## 15. Production modification map

### 15.1 New package

~~~text
src/pulsara_agent/plugins/
  __init__.py
  contracts.py
  manifests.py
  install_config.py
  composition.py
  mcp_adapter.py
  skill_adapter.py
  hooks/
    contracts.py
    config.py
    trust.py
    matcher.py
    process.py
    dispatcher.py
    projection.py
~~~

### 15.2 Existing files

- `src/pulsara_agent/cli.py`：plugins commands与workspace trust flag；
- `src/pulsara_agent/conversation_kernel/host.py`：composition、SessionStart/End、safe-point refresh、close；
- `src/pulsara_agent/conversation_kernel/runner.py`：UserPromptSubmit、Stop、Hook context planning；
- `src/pulsara_agent/conversation_kernel/tool_runtime.py`：logical resolve后的PreToolUse、PermissionRequest与PostToolUse seam；
- `src/pulsara_agent/conversation_kernel/subagent.py`：current flat Start/Stop seam；
- `src/pulsara_agent/conversation_kernel/context_sources.py`：两类Plugin Hook low-authority renderer；
- `src/pulsara_agent/conversation_kernel/mcp/*`：只接收normalized registrations，不新增Plugin MCP executor；
- `src/pulsara_agent/capability/local_skills.py`：接收explicit plugin root registrations，不扫描cache；
- `src/pulsara_agent/conversation_kernel/vocabulary.py`：不得加入HookEventType；新vocabulary位于plugins/hooks/contracts.py；
- Round 5B：未来只安装Pre/PostCompact producer，不复制engine。

### 15.3 Dependency

为第三方matcher提供linear-time regex，允许新增一个pinned RE2-compatible Python dependency。不得以方便为由退回unbounded Python `re`。

---

## 16. Test plan

### 16.1 Agent Plugins 1.0 conformance

- minimal/full official manifest；
- exact `$schema`；
- unknown top-level fields report+ignore；
- unsupported version reject；
- `skills/` immediate child only；
- invalid Skill narrow skip；
- portable MCP stdio/Streamable HTTP；
- sse typed skip；
- path traversal、symlink escape、cwd/data containment；
- single non-recursive placeholder expansion；
- component missing/invalid failure isolation。

### 16.2 Compatibility profiles

- Codex default/explicit Skill、MCP、Hook paths；
- Codex direct map与`mcp_servers` wrapper；
- Claude default Skill/MCP/Hook/agents paths；
- multiple manifests precedence；
- selected invalid manifest不得fallback；
- unsupported Apps/LSP/monitor/bin/settings只诊断；
- `CLAUDE_PLUGIN_ROOT/DATA` aliases exact。

### 16.3 Install/composition

- user/workspace plugin同名隔离；
- workspace config未trust不执行MCP/Hook；
- `add` atomic failure不留下partial package；
- package refresh mixed-stat拒绝；
- owner-issued registration inventory包含所有enabled Plugin contributions；
- component namespace collision deterministic；
- disable/refcount/close drain。

### 16.4 Hook config/trust

- 11 external names映射11 internal `*Event`值；
- unknown event typed skip；
- exact definition hash变化撤销trust；
- enable不自动trust；
- unsupported prompt/agent handlers不运行；
- unsafe regex/backreference拒绝；
- matcher aliases；
- JSON preparse bounds。

### 16.5 Hook execution

- all matching handlers先并发启动；
- outcome按configured order；
- any deny wins；
- timeout kill/join exact process；
- caller cancellation只detach；
- stdout/stderr/node/depth bounds；
- SessionEnd 1..3s；
- Host close无orphan Hook process；
- no API key/DSN/requestState/hidden reasoning in env/stdin。

### 16.6 Event seam

- UserPrompt block -> nocanonical entry；
- UserPrompt context -> append-onlyuntrusted suffix；
- PreToolUse deny -> noattempt、noeffect；
- unsupported updatedInput -> noattempt、typed reject；
- PermissionRequest deny/allow/defer；
- PostToolUse无法hide/replace exact result；
- natural Stop one continuation；explicit stop不复活；
- child context exact-scope隔离；
- SubagentStop forced path observe-only；
- Pre/PostCompact在Round 5B前无producer；
- SessionEnd失败不阻塞close。

### 16.7 Prefix与architecture

Chat Completions与Responses分别证明：

~~~text
Plugin/Hook refresh inside epoch
  SYSTEM equal
  tools equal
  messages equal or append-only suffix
~~~

Architecture guards：

- Plugin不属于`FrozenCapabilityFact` leaf union；
- HookEventType不进入Committed/Live enum与Protocol generator；
- Hook package不可import repository append；
- no Plugin tool executor；
- no private cache scan；
- no DB migration/event/job/guard；
- oracle `31/23/13/2/25/1` + Hook types `11`。

### 16.8 Local dogfood

构造一个临时Plugin：

- 一个standard Skill；
- 一个stdio MCP server；
- `UserPromptSubmit` context Hook；
- `PreToolUse` deny Hook；
- 一个dormant `agents/reviewer.md`。

验证：

1. Plugin package被三种profile之一加载；
2. Skill catalog可见且ordinary read可用；
3. MCP direct/meta按epoch timing工作；
4. Hook在trust前不运行，trust后运行；
5. deny前physical tool invoke为0；
6. model-visible Hook context为untrusted append-only observation；
7. agent spec只报告deferred；
8. disable后所有physical owner drain；
9. 不记录prompt、Hook正文、API key、DSN或环境secret。

---

## 17. Definition of Done

Round 9.2只有同时满足以下条件才可标记`ACTIVATED`：

1. Agent Plugins 1.0 portable manifest、skills与MCP conformance通过；
2. Codex profile是primary兼容层，Claude overlap profile可加载；
3. Plugin固定为bundle/source contributor，不是leaf/executor；
4. Skill/MCP exact进入Round 9/9.1既有owner；
5. 11项`HookEventType`全部存在且带`Event`后缀；
6. external Codex event name与JSON config可用；
7. command Hook exact trust、bounds、concurrency、kill/join闭合；
8. Hook不能改写BASE_SYSTEM、provider tools、canonical ToolResult或repository rows；
9. model-visible Hook output只为append-only `UNTRUSTED_OBSERVATION`；
10. PreToolUse deny在attempt/effect前，PermissionRequest复用existing decision owner；
11. argument rewrite明确typed unsupported，绝不静默执行原参数；
12. Subagent component只dormant发现，不偷跑PHC-10；
13. dynamic enable/disable保持strict prefix；
14. no new durable schema/event/job/receipt/recovery；
15. full test/quality/oracle/evidence gates全部通过。

---

## 18. 与后续轮次的接口

### 18.1 PHC-10 Hierarchical / batch subagent graph

PHC-10可消费`FrozenDeferredSubagentInventory`作为package provenance输入，但必须独立冻结Subagent spec、scope、provider exposure、tool/permission、task graph与execution owner。Round 9.2的`agents/`文件存在不等于它们可执行。

### 18.2 Round 5B compaction

Round 5B只需：

- 在其exact seam发`PreCompactEvent` / `PostCompactEvent`；
- successor cold epoch重新消费current Plugin composition；
- Skill/MCP rebase复用Round 9/9.1；
- Hook additional context仍走普通append-only source；
- 不建立compaction-only Plugin/Hook DTO。

### 18.3 Future managed Hooks

若未来引入enterprise managed Hooks：

- 必须有独立source与trust authority；
- 可定义fail-closed failure policy；
- 不从Plugin manifest获得managed身份；
- 不改变本文Plugin Hook默认non-managed/fail-open物理故障语义。

---

## 19. 参考实现与文档证据

### 19.1 公开规范

- [Agent Plugins Specification 1.0.0](https://agent-plugins.org/specification)：portable manifest、fixed component locations、Skill/MCP、client extensions、path containment、PLUGIN_ROOT/DATA与failure boundary；
- [OpenAI Plugin architecture](https://developers.openai.com/plugins/concepts/plugins)：Codex Plugin的Skill/MCP/Hook组合定位；
- [OpenAI Package your plugin](https://developers.openai.com/plugins/build/plugins)：`.codex-plugin/plugin.json`、`.mcp.json`、`hooks/hooks.json`与Plugin Hook trust；
- [OpenAI Hooks](https://learn.chatgpt.com/docs/hooks)：11 events、matcher、command input/output、concurrency、timeout与event-specific behavior；
- [OpenAI Claude Plugin conversion](https://developers.openai.com/plugins/guides/submit-claude-plugin)：Claude archive到Codex manifest的兼容事实；
- [Claude Code Plugins reference](https://code.claude.com/docs/en/plugins-reference)：Skill、Agent、Hook、MCP与更宽host-specific components；
- [Claude Code Create plugins](https://code.claude.com/docs/en/plugins)：plugin package与component lifecycle。

### 19.2 本地 Codex 窄探针

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/utils/plugins/src/plugin_namespace.rs`：同时识别`.codex-plugin/plugin.json`与`.claude-plugin/plugin.json`；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core-plugins/src/manifest.rs`：Codex manifest的Skill/MCP/Hook路径与inline Hook declarations；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/hooks/src/engine/dispatcher.rs`：matching handler并发启动、configured-order settlement与event scope；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/hooks/src/events`：11 event request/outcome与decision aggregation；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/hooks/src/engine/output_parser.rs`：Codex output wire与unsupported fields。

### 19.3 本地 Claude Code 窄探针

- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/plugins/pluginLoader.ts`：`.claude-plugin/plugin.json`、default Skill/Agent/Hook discovery与component failure isolation；
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/plugins/schemas.ts`：Claude manifest的宽host-specific字段；
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/plugins/loadPluginHooks.ts`：Plugin Hook loading；
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/plugins/loadPluginAgents.ts`：Agent component；
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/hooks/hookEvents.ts`：Claude lifecycle Hook vocabulary。

---

## 20. 最终冻结

Round 9.2的最小、完整形状是：

~~~text
portable Agent Plugin package
  + Codex-first / Claude-overlap adapters
  -> enabled local Plugin composition
  -> Skill registrations -----> Round 9/9.1
  -> MCP registrations -------> Round 6/9
  -> trusted Hook definitions -> 11-event process-local dispatcher
  -> Subagent inventory ------> deferred PHC-10

Hook lifecycle seam
  -> bounded external command
  -> typed process-local outcome
  -> existing product owner decides
  -> optional append-only UNTRUSTED_OBSERVATION

never
  -> new Plugin executor
  -> Hook-owned canonical append
  -> BASE_SYSTEM/tool rewrite
  -> committed-event replay
  -> durable plugin/hook recovery graph
~~~

它既不是把Claude Code的整个生态复制进Pulsara，也不是发明一个只供Pulsara使用的新Plugin格式；它以Agent Plugins 1.0为portable core，以Codex为主要运行时兼容目标，并把Pulsara真正拥有authority的部分交还已有owner。
