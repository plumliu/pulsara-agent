# Round 9.3：Local Agent Plugin Package、第三 Skill Producer 与 MCP/Hook Adapter 实施规格

> 状态：**ACTIVATED**
>
> 激活日期：2026-08-26
>
> 本文已按2026-08-26 working-tree production truth完成Agent Plugins Published 1.0.0 hard cut；production code、tests、PostgreSQL、packaging、local dogfood、real-provider dogfood、active-spec synchronization与§15 Definition of Done已全部闭合。它不是旧的Plugin Skill materialization方案，也没有dormant/compatibility path。
>
> 修订日期：2026-08-26
>
> 当前代码真源：`src/pulsara_agent/capability/`、`conversation_kernel/capability.py`、`mcp_config.py`、`conversation_kernel/mcp/`、`hooks/`、`conversation_kernel/host.py`、`conversation_kernel/tool_runtime.py`、`conversation_kernel/subagent.py`、`conversation_kernel/cold_epoch.py`。本文中的 owner、transaction、carrier 和调用顺序如与旧文字冲突，以实施时 working-tree production topology 为准；不得为旧稿恢复已删除的 DTO、owner、dual path 或 compatibility wrapper。
>
> 强制上位约束：[`AGENTS.md`](AGENTS.md)、[`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、[`PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、[`ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md`](ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md)、[`ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md`](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[`ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md`](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md)、[`ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md`](ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md)、[`ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md`](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)。`contracts/`与archived gap index不是本轮authority；本轮不更新它们，也不更新README。
>
> 公开格式基线：[Agent Plugins Specification 1.0.0](https://agent-plugins.org/specification)（Published）。实施者还必须窄读官方[manifest](https://agent-plugins.org/plugin-authors/manifest)、[Skills](https://agent-plugins.org/plugin-authors/skills)、[MCP servers](https://agent-plugins.org/plugin-authors/mcp-servers)、[client extensions](https://agent-plugins.org/plugin-authors/client-extensions)、[loading/discovery](https://agent-plugins.org/client-implementers/loading-and-discovery)、[MCP runtime](https://agent-plugins.org/client-implementers/mcp-runtime)、[conformance](https://agent-plugins.org/client-implementers/conformance)与[schemas](https://agent-plugins.org/schemas)。Published prose与schema若有边界差异，以1.0 normative prose为准，并按本文冻结的diagnostic/failure isolation投影；不得从1.1 working draft补语义。Pulsara V1的package identity与portable component只来自该标准；Codex只提供本文明确列出的Hook/authoring文件语义adapter，不构成第二套package identity或whole-package profile。

本文把 Plugin 定义为一个**本地安装、显式启用、可诊断的immutable component package**。Plugin不是第四种capability leaf，不拥有generic `invoke()`，也不拥有第二套Skill catalog、MCP supervisor、Hook engine、subagent runtime、permission owner或provider-prefix owner。

本轮支持三类package contribution：

1. Agent Plugins 1.0 portable Skills；
2. Agent Plugins 1.0 portable MCP server definitions；
3. `dev.pulsara/hooks/hooks.json`中的Codex-compatible command Hooks。

Portable core只有前两项；第三项是Pulsara client extension，不冒充Agent Plugins 1.0 portable component。

---

## 0. 最终产品形状

### 0.1 一个package lifecycle，三个既有consumer

```text
local Plugin source directory
  -> one Agent Plugins 1.0 parser
  -> one narrow immutable package publisher
  -> one enabled-package observation owner
  -> FrozenEnabledPluginView
       ├─ FrozenPluginSkillDefinitions
       │    -> existing parse_skill_document + placement validator
       │    -> existing central SkillCatalogResolver
       │    -> the same SKILL_CATALOG / ACTIVE_SKILL / read_file path
       ├─ normalized McpServerConfig values
       │    -> existing MCP supervisor / DIRECT-META / permission-effect owners
       ├─ FrozenHookSourceSnapshot values
       │    -> existing config_parser / HookTrustStore / KernelHookDispatcher
```

Plugin package owner只回答：

- 哪些local package versions完整安装；
- 哪个USER或exact WORKSPACE instance是current；
- 该instance是否enabled；
- current immutable root、persistent data root和component observations是什么；
- 哪些old immutable roots可由显式GC安全回收。

它不回答：

- Skill winner或是否activation；
- MCP tool是否READY、DIRECT、META、permitted或safe；
- Hook command是否trusted、match、block或追加context；
- worker是否可启动、能用什么工具、用哪个模型；
- canonical conversation、ToolResult或compaction是否提交。

### 0.2 Current baseline 与本轮真实hard cut

当前production已经是：

```text
BundledSkillDefinitionProducer ┐
                               ├─ SkillCatalogResolver -> one effective catalog
LooseSkillDefinitionProducer   ┘

Local USER/WORKSPACE Hook sources -> one KernelHookDispatcher
local/Host MCP configs             -> one MCP supervisor
Round 10 task graph                 -> one child runtime/cold assembler
```

本轮必须一次性变成：

```text
BundledSkillDefinitionProducer ┐
LooseSkillDefinitionProducer   ├─ SkillCatalogResolver -> one effective catalog
PluginSkillDefinitionProducer  ┘
```

这必须是显式third input和closed union hard cut，不能改成：

- `list[SkillProvider]`；
- `register_producer()`；
- entry-point discovery；
- service locator；
- generic mutable registry；
- generation-indexed view map；
- digest-to-object registry。

Four loose Skill roots及用户直接copy/edit/delete的Round 9.1/Unified product semantics完全保留；Plugin package不会接管或监听它们。与loose Skill不同，Plugin含process-bearing MCP/Hook等components，只有本文typed install + instance state才是Plugin enablement truth；手工把目录复制进managed `packages/`不会安装或启用Plugin，也不会被Runtime猜测采用。

### 0.3 旧9.3设计必须完整删除

以下旧稿结构不保留compatibility wrapper或dormant branch：

- 把Plugin Skills复制到`${PULSARA_HOME}/skills`或`<workspace>/.pulsara/skills`；
- `.pulsara-skill-source.json` provenance sidecar；
- `PluginSkillMaterializer`；
- pre-scan reconciler、projection drift、orphan Skill cleanup；
- Plugin Skill tree/content ownership digest；
- `compute_skill_dir_hash()`复活或任何替代recursive hash；
- Plugin作为第五个loose Skill root；
- Plugin-private Skill scanner/parser/resolver/activation/read tool；
- root `hooks/hooks.json`或vendor manifest的隐式profile猜测；
- `.codex-plugin/plugin.json`的identity/priority fallback；
- multi-manifest name precedence；
- `hooks/parser.py`旧模块名；唯一production parser是`hooks/config_parser.py`；
- Plugin provenance注入generic Hook stdin或provider-visible `HOOK_CONTEXT`；
- Hook output head/tail preview或Plugin-private artifact path；
- `PluginHookDispatcher`、`PluginHookTrustStore`、`PLUGIN_HOOK_CONTEXT`；
- dormant `agents/` inventory而没有Round 10 consumer；
- package snapshot/contribution-plan/root-policy fingerprints。

仓库中尚无production `src/pulsara_agent/plugins/`，因此本轮不存在外部deployed Plugin state需要兼容。新实现只形成一条路径。

### 0.4 Prefix continuity

同一exact ROOT/child scope、同一installed continuity epoch：

```text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
```

所以：

- Plugin install/replace/enable/disable/remove不直接重写running Host prefix；
- Plugin Skill变化只经existing Skill safe-point source追加suffix；
- late Plugin MCP只经existing `MCP_CATALOG`和META route追加suffix；
- 已安装DIRECT descriptor不从same epoch `tools[]`热删；不可用时由existing gate拒绝；
- Hook output只经source-neutral `HOOK_CONTEXT`追加user-role untrusted suffix；
- ordinary cold open与explicitly adopted compaction successor仍是唯一可重建SYSTEM/tools的boundaries；
- Plugin reload、Hook trust变化、MCP reconnect或package GC不得创造第三种rebase boundary。

### 0.5 Small durability 与oracle

本轮允许的新增durable local product state只有：

- immutable managed Plugin package version directories；
- USER/exact WORKSPACE current instance state；
- per-instance writable Plugin data directory；
- package/state/GC所需local OS lock files。

它们是local installation/configuration truth，不是conversation execution recovery。Hook trust继续由Round 9.2现有generic `HookTrustStore`拥有。

本轮不新增：

- PostgreSQL relation、column或migration；
- CommittedEventType、LiveEventType、subject、guard或product relation；
- durable job、receipt、history、checkpoint、replay、repair queue或recovery graph；
- watcher、registry generation、version→consumer map或lease relation；
- Plugin execution history；
- cross-Host Hook/MCP/worker execution recovery。

七维oracle保持：

```text
29 / 24 / 11 / 1 / 25 / 0 / 11
```

最后一项仍是Round 9.2唯一`HookEventType`的11项vocabulary。

---

## 1. Scope 与明确非目标

### 1.1 本轮实现

1. Agent Plugins 1.0.0 root `plugin.json` offline parser与validator；
2. fixed portable `skills/`与`mcp.json` discovery；
3. fixed Pulsara extension directory `dev.pulsara/`；
4. local directory validate/add/replace/enable/disable/remove/list/doctor/gc；
5. USER与exact WORKSPACE instance scope；
6. immutable managed version roots与persistent data roots；
7. complete enabled-package observation与process-local current view；
8. explicit Plugin Skill third producer与seven-tier resolver；
9. portable MCP到existing `McpServerConfig`的adapter；
10. command Hook source adapter，复用Round 9.2的11 lifecycle events；
11. fixed ROOT-only `reload_capabilities` Builtin；
12. CLI/in-process service、diagnostics、packaging、tests与real-provider dogfood；
13. 一个package内只读`pulsara-plugin-installer` bundled Skill：strict validation成功时不转换；只有deterministic Codex package-format差异才指导模型读取实际Plugin内容与对应Codex source reference，生成Agent Plugins 1.0临时候选并重新交给同一production validator。Hook target必须exact join §9的event/matcher/stdin/environment/output/control/lifecycle；Skill不是第二parser、install authority或Runtime compatibility profile。

### 1.2 本轮明确不实现

- remote marketplace、Git/npm download、archive extraction、publisher signing、auto-update；
- dependency installation或package lifecycle scripts；
- OpenAI hosted Plugins、Apps UI或universal directory submission；
- production parser/Runtime中的Codex whole-package manifest profile；
- `.codex-plugin/plugin.json`、`.app.json`、marketplace manifest；
- Codex commands、LSP、monitors、themes、output styles、settings或PATH injection；
- Agent Plugins future component version guessing；
- MCP OAuth、credential acquisition或secret store；
- Hook `http`、`mcp_tool`、`prompt`或`agent` handler；
- Hook argument rewrite、ToolResult rewrite或output suppression；
- Plugin Python import/entry point/shared-library injection；
- Plugin-defined permission/effect/model/provider/worker profile；
- named persistent agent session、recursive worker或Plugin-private task graph；
- Plugin uninstall rollback、package downgrade history或auto-GC；
- provider-visible install/update/remove management tool；
- provider prompt中的完整Plugin inventory。

### 1.3 Vendor compatibility的精确定义

Pulsara本轮的安装单元是Agent Plugins 1.0 package，不声称直接安装任意Codex package。

兼容性仅指`dev.pulsara/hooks/hooks.json`使用Round 9.2已经冻结的Codex-compatible command Hook grammar；portable core仍严格遵守Agent Plugins 1.0。

作者若要迁移Codex package，应创建root `plugin.json`并把Pulsara-specific Hook file放入`dev.pulsara/`。Runtime不得从root basename、vendor manifest或alternate path猜identity/component。Package内只读`pulsara-plugin-installer`只提供模型侧authoring workflow：首次strict validation失败且failure是deterministic format/schema difference时，模型才按需读取Codex source reference与实际source内容，在attempt-local目录生成new standard candidate，再交给同一个production parser。Hook conversion不能只按同名event猜测，必须exact join §9的matcher/stdin/environment/output/control/owner timing。Filesystem/symlink/special-file/secret/race/unavailable/cancel/deadline失败不得触发转换；任何required active component没有exact Pulsara representation时必须报告`NOT_CONVERTIBLE`且不安装。该Skill不增加foreign manifest parser、alternate package identity、partial Plugin、Runtime fallback、receipt或durable conversion state。

---

## 2. Agent Plugins 1.0 与Pulsara extension

### 2.1 唯一package identity

合法source root必须有exact regular file：

```text
plugin.json
```

它必须满足Published 1.0.0：

- `$schema == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"`；
- `name`满足标准的1–64字符、lowercase alphanumeric/`-`/`.`约束；
- optional metadata只按标准JSON type约束验证；`version`不是SemVer、`homepage`/`repository`/`author.url`不是可识别URL、`author.email`不是可识别email或`license`不是SPDX identifier，都不能仅因此拒绝manifest；`author`出现unknown field或任一field type错误仍按closed schema fatal；
- unknown top-level fields逐项diagnostic-and-ignore，不赋予语义；
- non-object `extensions` diagnostic-and-ignore；
- 其他schema violation拒绝package；
- unsupported schema拒绝package；
- Runtime绝不联网下载schema。

Pulsara V1只recognize上述Published 1.0.0 canonical identifiers。官方仓库中的1.1.0仍是working draft，不是本轮兼容目标；不得预读draft schema、猜测future version或把unknown `$schema`映射到1.0 parser。`version`及其他metadata只用于inspection/display；本轮没有auto-update/cache-freshness owner，它们不参与package identity、precedence、install id或enablement。

官方1.0.0 `plugin.schema.json`与`mcp.schema.json`作为read-only package resources随wheel发布。Git与wheel拥有这些资源的内容identity；不得保存schema SHA、下载etag或compatibility fingerprint。

Parser只接受UTF-8 JSON并拒绝duplicate object keys；绝不fallback到YAML。由于Published text规定unknown manifest top-level fields和whole-field non-object `extensions`有窄的report-and-ignore边界，implementation必须先按规范机械分类这些fields，再用embedded schema验证remaining normative object；不能直接让`additionalProperties`错误把本应继续加载的package整包拒绝。若`extensions`是object，unimplemented namespace的value必须整块ignore而不深入validate。Embedded `$ref` resolver必须local-only，任何remote resolution attempt都是test failure。

Production schema owner固定使用one Draft 2020-12 validator path（`jsonschema.Draft202012Validator` + embedded-only registry）；`jsonschema`必须成为`pyproject.toml` direct runtime dependency，不能因当前lock中偶然transitive存在而省略。不得同时保留手写partial schema validator或Pydantic fallback；规范文字要求的unknown-field/non-object-extension preprocessing和cross-document version rule是schema前后的显式semantic layer，不是第二parser。

### 2.2 Portable fixed components

Agent Plugins 1.0 portable core只有：

```text
skills/    # immediate child directories only
mcp.json   # exact root file
```

规则：

- location missing不是错误；
- present但filesystem kind错误使该component确定性`INVALID`，不是physical `UNAVAILABLE`，且不使其他component失效；
- `skills/`不递归寻找更深的Skill；
- invalid Skill逐项skip并进入完整diagnostic；
- invalid top-level `mcp.json`禁用该package的MCP component；
- invalid/unsupported individual MCP server只skip该server；
- package parser不把unknown root files猜成component。

`mcp.json`同样拒绝duplicate keys；top-level envelope整体验证后，individual entries使用embedded schema的server definition逐项验证，从而保留standard per-server failure isolation。

Portable loader的path failure boundary必须保持Published 1.0的最窄层级：root `plugin.json`逃逸才reject package；fixed `skills/`或`mcp.json` kind/escape只invalid exact component；one `SKILL.md` escape只skip exact Skill；one MCP `command`/`cwd` escape只invalid exact server；其他escaping resource只deny exact access。`INVALID`表示完整观察到的deterministic defect，`UNAVAILABLE`只用于I/O、deadline前无法完成的physical observation或identity/membership race；两者不能互换。

### 2.3 Pulsara client extension

Pulsara拥有固定reverse-domain namespace：

```text
dev.pulsara/
  hooks/hooks.json
```

V1只实现file extension directory，不实现`plugin.json.extensions["dev.pulsara"]` manifest-data arm。只要whole `extensions`本身是object，该namespace value与所有unimplemented namespace value一样整块ignore而不validate；它不携带配置、不会重定向目录，也不会使file extension失效。若whole `extensions`不是object，才走Published 1.0明确的report-and-ignore whole-field边界。

Pulsara extension locations固定，不能被manifest path重定向：

- `dev.pulsara/hooks/hooks.json`是单一Hook config source；
- root `hooks/`、inline manifest objects不被读取；
- extension path missing不是错误；kind错误使对应extension component确定性`INVALID`，只有I/O、identity/membership race或无法完成physical observation才是`UNAVAILABLE`。

### 2.4 Filesystem admission subset

Agent Plugins允许client选择如何处理in-root symlink。Pulsara V1的**managed local acquisition/installability policy**采用更窄、机械的admission：

- source root本身不能是symlink；
- package tree内symlink、junction、reparse point、socket、device、FIFO全部拒绝；
- 只复制regular files与directories；
- package-relative traversal不能包含empty/`.`/`..`，不能escape held root；
- descriptor-relative read与final revalidation证明membership和identity；
- `/tmp`、`/var`等host system aliases先经shared source-binding seam规范化，再执行no-follow traversal；
- 不放松source tree内部no-follow规则。

因此`validate_local_plugin_source`回答“能否按Pulsara managed-store policy安装”，不是单独签发一份抽象的portable-conformance certificate。一个otherwise portable、仅含in-root symlink的package可以被Pulsara installation policy拒绝，但实现不得把该client acquisition policy反向写成Agent Plugins portable parser的整包failure rule。已经admit的managed root以及same-UID干扰后的Runtime读取仍按§2.2最窄component/entry boundary分类。

这不是sandbox声明。Plugin MCP/Hook process仍以Host OS user identity运行。

### 2.5 Per-document与existing physical bounds

本轮不增加total package bytes、total resources、total installed versions、total enabled Plugins或Plugin lifetime cap。

只保留有真实consumer的per-operation bounds：

- `plugin.json`与`mcp.json`各自最多1 MiB；
- 两个Agent Plugins JSON documents复用一个purpose-neutral bounded JSON structural loader：maximum 16,384 nodes、depth 64、one scalar/key 64 KiB；duplicate keys仍由各自closed parser拒绝；
- 每个internal `PluginInstanceState` JSON最多1 MiB；这是单分片corruption/RSS boundary，不是aggregate state或Plugin count cap；
- `SKILL.md`复用current 64 KiB document bound；
- Hook config复用Round 9.2的1 MiB bound；
- MCP configured-server aggregate复用current 64-server bound；
- final effective Skill winners复用current 64-winner、catalog 384 KiB、active 16/512 KiB bounds；
- tool result/page复用existing ToolResult physical bound。

Package regular-file byte copy、secret scan与paired compare必须constant-memory streaming；directory membership/identity evidence与one immutable inspection result允许诚实的`O(observed member count)`metadata，不得宣称whole operation constant RAM。`MemoryError`或OS resource failure进入typed `UNAVAILABLE`/cleanup，而不是偷偷增加total member/package cap。若实现采用external sort/scratch来降低RSS，它只能是attempt-local、立即unlink且最终close的non-authoritative physical scratch，受同一deadline/cancel/secret cleanup owner约束，绝不能成为receipt、generation或recovery input。

---

## 3. Typed management boundary

### 3.1 六个底层operations

CLI、Desktop GUI与future local Host API只能调用同一组in-process typed operations：

```text
validate_local_plugin_source(request) -> PluginValidationOutcome
install_local_plugin(request)         -> PluginInstallOutcome
set_local_plugin_enabled(request)     -> PluginEnablementOutcome
remove_local_plugin(request)          -> PluginRemovalOutcome
inspect_local_plugins(request)        -> PluginInspectionResult
gc_local_plugin_packages(request)     -> PluginGcOutcome
```

其中：

- `install_local_plugin(replace=false)`是add；
- `install_local_plugin(replace=true)`安装new immutable version、切换current并强制`enabled=false`；
- enable/disable只改current instance state；
- remove只删除current instance state，不删除persistent data；
- gc只回收unreferenced且未被physical consumer持有的version/stage，以及§4.3 exact grammar、同instance lock下可证明安全的state-temp residue；
- list与doctor共享exact一次`inspect_local_plugins`结果，只是不同projection。

Service不依赖argparse、stdout、JSON schema、terminal cwd或UI state。CLI `--json`只是typed outcome projection，不是GUI IPC/wire authority。

Every operation request携带one caller-frozen absolute monotonic deadline与cooperative cancellation port；nested scan/copy/schema/state/cleanup不得刷新deadline或另取完整handler timeout。已经开始的filesystem/physical close按§5.5 shield+join，但logical disposition仍按唯一cut分类。本轮不新增Plugin/session/lifecycle total timeout。

Enable request必须额外携带：

```text
expected_current_package_install_id
external_process_acceptance = ACCEPTED
```

Management service在exact instance lock内重读state并比较install id；不匹配返回typed `STALE/plugin_state_raced`，不能授权未审阅的replacement。Disable不需要`external_process_acceptance`，但仍比较caller观察的expected install id。这个字段有直接consumer：它确认用户已看到同一package的normalized component summary，并接受future Host composition可能启动stdio MCP process或连接HTTP endpoint；它不是receipt、durable consent log或remote-tool permission bypass。

### 3.2 Scope与路径解析

```text
PluginScopeKind = USER | WORKSPACE
```

USER request：

- 完全不依赖cwd；
- 使用shared absolute-only `PULSARA_HOME` resolver；
- unset/empty使用default home；
- `expanduser()`后仍relative则typed fail；
- 绝不相对cwd调用`resolve()`。

WORKSPACE request：

- explicit `--workspace`优先；
- 未提供时以caller cwd作为workspace root；
- 规范化为exact absolute project root；
- 使用existing workspace state-key derivation；
- transient/non-project workspace不允许创建WORKSPACE Plugin instance。

Plugin store统一位于`${PULSARA_HOME}`，因此invalid Plugin home配置会阻止USER与WORKSPACE Plugin lifecycle；这不应反向阻止unrelated loose workspace Skill validation/install。

Standalone `validate_local_plugin_source`只需要explicit source path、current API-key scrub input与call deadline/cancel port，不读取Plugin store或workspace，因此也不能被unrelated invalid `PULSARA_HOME`或cwd阻断。

### 3.3 Closed dispositions

```text
PluginValidationDisposition =
  VALID | INVALID | UNAVAILABLE | CANCELLED | TIMED_OUT

PluginInstallDisposition =
  INSTALLED | REPLACED | ALREADY_PRESENT | INVALID | UNAVAILABLE |
  CANCELLED | TIMED_OUT | ACK_UNKNOWN | CLEANUP_UNAVAILABLE

PluginEnablementDisposition =
  ENABLED | DISABLED | ALREADY_ENABLED | ALREADY_DISABLED |
  NOT_FOUND | STALE | UNAVAILABLE | CANCELLED | TIMED_OUT | ACK_UNKNOWN

PluginRemovalDisposition =
  REMOVED | NOT_FOUND | UNAVAILABLE | CANCELLED | TIMED_OUT | ACK_UNKNOWN

PluginInspectionDisposition = COMPLETE | UNAVAILABLE

PluginInspectionAbortReason = CANCELLED | TIMED_OUT

PluginGcDisposition =
  COMPLETE | UNAVAILABLE | CANCELLED | TIMED_OUT
```

Inspection的semantic value与outer abort使用closed sum，而不是把两类truth塞进同一disposition：

```text
PluginInspectionResult = PluginInspectionOutcome | PluginInspectionAbort

PluginInspectionAbort
  reason: PluginInspectionAbortReason
```

`PluginInspectionAbort`没有instances、versions、component facts或partial issues；CLI/GUI必须exhaustive projection这两个arms，不能靠caught exception或message string识别abort。

Expected user input、OS I/O、race、deadline与cancellation不能靠exception string分类。Programmer invariant violation仍可raise。

上述enum不是裸status string；六个operations的production result必须是closed sum，payload matrix冻结为：

```text
PluginValidationOutcome
  VALID       -> exact PluginValidationSummary + ordered component issues
  INVALID     -> no summary/component facts; ordered deterministic diagnostics
  UNAVAILABLE -> no summary/component facts; ordered physical diagnostics
  CANCELLED | TIMED_OUT -> outer-abort reason only

PluginInstallOutcome
  INSTALLED | REPLACED
    -> scope/workspace key/plugin id/new package install id/enabled=false/
       exact normalized summary/ordered diagnostics
  ALREADY_PRESENT
    -> exact current package install id/current enabled bit/current summary
  INVALID
    -> exact PluginValidationOutcome.INVALID
  UNAVAILABLE | CANCELLED | TIMED_OUT
    -> attempted scope/plugin id when already known + no claimed state mutation
  ACK_UNKNOWN
    -> attempted package install id + intended post-state + last known cut;
       never a fabricated success or retry instruction
  CLEANUP_UNAVAILABLE
    -> one non-recursive prior non-cleanup PluginInstallOutcome +
       attempted path + closed location status

PluginEnablementOutcome
  ENABLED | DISABLED | ALREADY_ENABLED | ALREADY_DISABLED
    -> scope/plugin id/exact current package install id/resulting enabled bit
  NOT_FOUND -> exact requested instance identity
  STALE     -> expected install id + observed current install id
  UNAVAILABLE | CANCELLED | TIMED_OUT
    -> requested identity + desired bit; no claimed state cut
  ACK_UNKNOWN
    -> exact install id + desired bit + last known state cut

PluginRemovalOutcome
  REMOVED   -> exact removed instance identity + prior package install id
  NOT_FOUND -> exact requested instance identity
  UNAVAILABLE | CANCELLED | TIMED_OUT
    -> requested identity; no claimed unlink
  ACK_UNKNOWN
    -> requested identity + prior package install id + last known unlink cut

PluginInspectionResult
  -> §3.3既有PluginInspectionOutcome | PluginInspectionAbort closed union

PluginGcProgress
  ordered_removed: exact VERSION/STAGE/state-temp refs
  ordered_in_use: exact VERSION/STAGE refs
  current_attempted_ref?
  current_location_status?: ABSENT | STAGE_ONLY | UNREFERENCED_VERSION |
                            STATE_TEMP | UNKNOWN
  unvisited_suffix: bool

PluginGcOutcome
  COMPLETE    -> progress.unvisited_suffix=false且无current attempted failure
  UNAVAILABLE -> progress（允许此前已有FULL delete cuts）+ exact diagnostic
  CANCELLED | TIMED_OUT
              -> progress（允许此前已有FULL delete cuts）+ outer-abort reason
```

任何GC disposition都不能靠名称暗示“零mutation”；第一个unlink/rmdir FULL后，所有arms都强制携带同一个`PluginGcProgress`。GC没有`ACK_UNKNOWN`：每个local unlink/rmdir syscall及其join有明确FULL/not-FULL cut；reply cancellation不能把已知progress变unknown。GC也没有`CLEANUP_UNAVAILABLE` wrapper：删除本身就是primary operation，物理失败直接是保留progress的`UNAVAILABLE`。CLI/GUI/JSON必须exhaustive match arm与required payload，不能读取optional-field组合猜语义。

Validation语义固定：

- `VALID`表示portable core identity与physical package tree可安全安装；它可以携带invalid Skill、skipped MCP server或unavailable Hook等narrow component issues，因为Agent Plugins要求component failure isolation；
- `INVALID`表示fatal manifest/schema、tree kind/escape/symlink、reserved secret或其他确定性source defect；
- `UNAVAILABLE`表示source I/O、identity/membership race或validator physical observation无法完成；
- `CANCELLED`/`TIMED_OUT`只表示对应outer abort在validation outcome install前胜出。

Install只接受`VALID`package，但不能把component issues升级成整包拒绝，也不能静默丢弃这些issues。

Install的`CLEANUP_UNAVAILABLE`只携带：

- attempted path；
- closed location status：`ABSENT | STAGE_ONLY | UNREFERENCED_VERSION | UNKNOWN`；
- prior semantic disposition。

它不携带tree listing、bytes、digest、repair token或recovery plan。

### 3.4 Settlement cuts 与priority

每个mutation owner使用同一caller deadline/cancel port，并按以下总规则结算；后续章节的physical cause priority必须嵌入本表，不能另起一套：

1. irreversible state/unlink cut若已FULL，胜过其后的cancel/deadline；owner返回已知success/progress，只有需要确认且确认本身不可用时才使用对应`ACK_UNKNOWN`；
2. state cut前已经完整观察到的source race，优先于source I/O、stage I/O、publish conflict与outer abort；其后依次是source I/O → staging → publish conflict → state/data-root unavailable；
3. 若尚无上述settled physical cause，cancel signal先于absolute deadline则`CANCELLED`，deadline已经到达则`TIMED_OUT`；不得刷新deadline来等待更喜欢的outcome；
4. worker一旦开始必须shield+join；join后才知道的physical cause按1–3归类，不能让caller cancellation制造detached work；
5. install ordinary cleanup在prior semantic outcome之后运行；cleanup失败只形成one non-recursive `CLEANUP_UNAVAILABLE(prior=...)`，不能覆盖prior package/state identity；
6. reply formatting、logging或CLI projection失败不追溯改变已经安装的typed outcome。

### 3.5 Closed diagnostics

新增一个closed `PluginDiagnosticCode`，exact 37 members冻结如下；增删任何member都必须先修订本文并同步exact-count guard。Production mapping必须exhaustive，不能把raw exception message当code：

```text
plugin_home_configuration_invalid
plugin_workspace_required
plugin_source_not_directory
plugin_source_final_symlink
plugin_source_tree_symlink
plugin_source_special_file
plugin_source_escape
plugin_source_unavailable
plugin_source_raced
plugin_source_contains_active_api_key
plugin_manifest_missing
plugin_manifest_overbound
plugin_manifest_invalid_utf8
plugin_manifest_invalid_json
plugin_manifest_schema_unsupported
plugin_manifest_invalid
plugin_manifest_unknown_field_ignored
plugin_extensions_field_ignored
plugin_package_not_found
plugin_package_root_unavailable
plugin_package_root_raced
plugin_staging_unavailable
plugin_publish_conflict
plugin_state_unavailable
plugin_state_raced
plugin_cleanup_unavailable
plugin_package_in_use
plugin_data_root_unavailable
plugin_data_root_raced
plugin_component_kind_invalid
plugin_mcp_component_invalid
plugin_mcp_server_invalid
plugin_mcp_transport_unsupported
plugin_mcp_server_id_collision
plugin_mcp_configured_bound_exceeded
plugin_view_unavailable
plugin_reload_partial
```

`PluginDiagnosticSeverity = INFO | WARNING | ERROR`，code→severity唯一mapping冻结为：

```text
INFO
  plugin_manifest_unknown_field_ignored
  plugin_extensions_field_ignored
  plugin_package_in_use
  plugin_mcp_transport_unsupported

WARNING
  plugin_package_not_found
  plugin_cleanup_unavailable
  plugin_component_kind_invalid
  plugin_mcp_component_invalid
  plugin_mcp_server_invalid
  plugin_mcp_server_id_collision
  plugin_mcp_configured_bound_exceeded
  plugin_reload_partial

ERROR
  plugin_home_configuration_invalid
  plugin_workspace_required
  plugin_source_not_directory
  plugin_source_final_symlink
  plugin_source_tree_symlink
  plugin_source_special_file
  plugin_source_escape
  plugin_source_unavailable
  plugin_source_raced
  plugin_source_contains_active_api_key
  plugin_manifest_missing
  plugin_manifest_overbound
  plugin_manifest_invalid_utf8
  plugin_manifest_invalid_json
  plugin_manifest_schema_unsupported
  plugin_manifest_invalid
  plugin_package_root_unavailable
  plugin_package_root_raced
  plugin_staging_unavailable
  plugin_publish_conflict
  plugin_state_unavailable
  plugin_state_raced
  plugin_data_root_unavailable
  plugin_data_root_raced
  plugin_view_unavailable
```

Exact-count test必须证明三组disjoint union等于全部37 codes。`sse`是Agent Plugins optional transport，本轮skip exact server并报告INFO known-unsupported；不得把lack of optional transport support升级成package/server config invalid。

Skill document/placement issues继续使用existing closed `SkillDiagnosticCode`；Hook config/source/trust/runtime issues继续使用Round 9.2 `HookDiagnostic` codes与trust disposition；不得同时生成一个generic Plugin duplicate diagnostic作为第二truth。Plugin owner只可为package path/state/view/publication这些自身causes生成Plugin diagnostics。

### 3.6 CLI projection

新增：

```text
pulsara plugins validate <path>
pulsara plugins add --scope user|workspace [--workspace <path>] [--replace] <path>
pulsara plugins enable --scope user|workspace [--workspace <path>] [--yes] <plugin-id>
pulsara plugins disable --scope user|workspace [--workspace <path>] <plugin-id>
pulsara plugins remove --scope user|workspace [--workspace <path>] <plugin-id>
pulsara plugins list [--workspace <path>]
pulsara plugins doctor [--workspace <path>]
pulsara plugins gc [--workspace <path>]
```

每项支持human与`--json`projection。`list`只显示current USER/WORKSPACE instances与effective component summary；`doctor`显示invalid、disabled、shadowed/conflicting、component unavailable、unreferenced/in-use roots与trust/reload notices。两者不能各自scan。

`enable`在mutation前必须投影exact current install的normalized summary：Skill names/descriptions、每个MCP stdio command/args/cwd/env key/value、HTTP endpoint/public header key/value与完整Hook definitions。Interactive CLI显示summary并要求明确确认；non-interactive `--yes`只省略prompt，仍把刚观察的exact install id和`ACCEPTED`传给typed operation。Desktop/未来Web UI直接展示同一typed summary并调用service，不解析CLI输出；管理界面可以把exact install id作为mutation输入，但不得向用户展示managed package root、随机package install id或内部存储层级。Enable Plugin MCP授权future composition以Host OS user启动/connect server；具体remote tool invocation仍完整经过existing scope、permission、effect、dirty和attempt owners。Enable不等于Hook trust，Hook command继续单独exact trust。

Global `pulsara` launcher继续由`[project.scripts]`发行，必须从任意non-source cwd运行，不依赖repository cwd、repository `.venv`、`PYTHONPATH`、source symlink或`uv run`。

Agent若通过terminal调用CLI，继续服从existing terminal/permission owner；不新增provider-visible Plugin install tool。

---

## 4. Managed store、state与physical lifetime

### 4.1 Store layout

```text
${PULSARA_HOME}/plugins/
  packages/
    user/<plugin-id>/<package-install-id>/
    user/<plugin-id>/.pulsara-stage-<package-install-id>-<attempt-nonce>/
    workspace/<workspace-state-key>/<plugin-id>/<package-install-id>/
    workspace/<workspace-state-key>/<plugin-id>/.pulsara-stage-<package-install-id>-<attempt-nonce>/
  state/
    user/<plugin-id>.json
    user/.pulsara-state-<plugin-id>-<attempt-nonce>.tmp
    workspace/<workspace-state-key>/<plugin-id>.json
    workspace/<workspace-state-key>/.pulsara-state-<plugin-id>-<attempt-nonce>.tmp
  data/
    user/<plugin-id>/
    workspace/<workspace-state-key>/<plugin-id>/
  locks/
    instances/<scope-key>/<plugin-id>.lock
    packages/<scope-key>/<plugin-id>/<package-install-id>.lock
```

所有components通过shared `PULSARA_HOME` resolver取得同一absolute store。目录逐component no-follow创建，不跟随已有symlink，不用broad `mkdir(parents=True)`跨越未经验证的ancestor。

`package-install-id`语法固定为`pkg_`加32个lowercase hex；`attempt-nonce`固定为32个lowercase hex。两者都是random opaque physical identity，不是content hash。只有exact上述hidden grammar属于publisher/GC；其他dot entry和malformed lookalike不是Plugin-owned，scanner/GC必须ignore且永不按mtime/age猜测删除。

### 4.2 Instance state

每个instance state只保存：

```text
PluginInstanceState
  contract_id = "pulsara.plugin-instance-state.v1"
  plugin_id
  scope
  workspace_state_key?   # iff WORKSPACE
  current_package_install_id
  enabled
```

不保存：

- source path；
- manifest/component copy；
- package tree digest；
- history或previous install list；
- install receipt；
- last scan generation；
- contribution fingerprint；
- repair status。

`package_install_id`是上述随机、opaque、stable version-root identity，不是content hash。它必须能在state、path、Hook trust input和GC中exact join；不得用manifest version或tree digest代替。

### 4.3 Lifecycle linearization

- add：publish immutable root后atomic create state，initial `enabled=false`；
- add existing instance且`replace=false`：`ALREADY_PRESENT`，不改state；state path/content identity不一致则按state unavailable处理，不另造无消费者的identity-conflict disposition；
- replace：publish new immutable root后atomic replace state并强制`enabled=false`；old package的enable acceptance不能授权new package；
- enable/disable：per-instance lock内重读exact predecessor并atomic replace；
- remove：per-instance lock内重读后atomic unlink state；
- gc：state complete observation后，只删除unreferenced且取得package exclusive lock的roots。

每个operation只锁exact instance。没有global Plugin mutation mutex；inspection则必须完整观察其目标USER/exact WORKSPACE aggregate，不能返回mixed partial truth。

Publisher必须在hidden stage创建前取得exact package-install-id lock，并从stage lifetime开始一直持有到package publish、state atomic cut、same-process confirmation与ordinary cleanup全部settle。否则GC可能在package publish与state create/replace之间把尚未referenced的新root删掉。GC取得不了该exclusive lock时只报告`IN_USE`并继续其他roots；不得等待publisher或推断其最终state。

State create/replace只使用same-parent exact hidden state-temp grammar：exclusive create → bounded write/close → atomic no-follow rename/replace cut。State enumerator忽略这些exact temp names；GC只有在nonblocking取得对应instance lock后才可删除crash residue。Unknown/malformed hidden state entry不属于产品，不扫描内容、不删除。SIGKILL residue不会令ordinary state enumeration永久`UNAVAILABLE`，但visible non-hidden malformed/missing state仍按typed unavailable处理。

### 4.4 Immutable package root

Publisher在publish前把stage mode规范化为：

- directories `0500`；
- regular non-executable files `0400`；
- source任一execute bit为true的regular file `0500`；
- clear setuid/setgid/sticky；
- 不复制owner、ACL、xattr或platform metadata。

Source observation冻结的是regular-file bytes、membership、identity与executable class，不要求destination mode byte-equal source mode。

Managed root对Pulsara是immutable input。Host OS同UID仍可chmod、move或replace它；这是明确out-of-contract namespace interference，不得虚构sandbox、tamper-proof或reply-time exact-path liveness。

### 4.5 Consumer lifetime 与GC

每个opened package root通过adjacent package lock取得shared OS lock。以下physical consumers通过ordinary object sharing持有exact lock handle：

- current Host Plugin composition view；
- old Hook attempt直到settle；
- old MCP slot/process直到retire/close/drain；
- call-local inspection/CLI projection。

Darwin/Linux统一使用per-open-description `flock(LOCK_SH)`与GC `flock(LOCK_EX | LOCK_NB)`；publisher持有与GC exclusive互斥的package lock。Anchor复制使用`os.dup`保持同一locked open-file description，直到最后一个duplicate close才释放；不得换成“closing任意fd可能释放process全部record locks”的process-scoped POSIX record-lock语义，也不得只用Python mutex冒充cross-process exclusion。

“ordinary object sharing”必须落到一个generic process-local `PhysicalLifetimeAnchor`，不能只写成注释：

- package binding为每个需要跨view生存的leaf复制一份独立shared-lock file descriptor；没有manual refcount、view lease DTO或registry；
- Plugin Hook adapter把anchor附着在generic `FrozenHookSourceProvenance`/definition可达对象，old sync/background attempt与one-shot context只要仍持exact definition就保持锁；
- Plugin MCP adapter把anchor附着在generic native `McpServerConfig`/client可达对象，retiring candidate/slot/process drain前保持锁；
- fields必须`repr=False, compare=False`，不进入trust/config/fact fingerprint；package root、data root、resolved command/cwd/env等真实concrete values仍进入对应Hook trust或MCP runtime identity；
- anchor拥有idempotent close/finalizer；Host close在Hook attempts与MCP slots完整drain并清除owner references后关闭current anchors，unreachable old leaf的独立descriptor自然关闭；GC等待的是OS lock，不查询Python refcount；
- local Hook/MCP值携带`None`，generic cores不得import `plugins/*`或回读Plugin state。

这不是manual refcount或durable lease。延迟finalization最多让GC保守报告`IN_USE`，绝不能让仍在执行的consumer失去锁。

GC仅在state aggregate证明root unreferenced且能nonblocking取得exclusive package lock时删除。它不查询manual refcount、generation、registry或database lease。

GC deletion从held packages parent/root descriptor开始，逐项no-follow、iterative地unlink files/symlinks并移除empty directories；即使managed root遭same-UID interference也绝不能跟随symlink/junction逃出exact version root。Root/path binding或membership在delete cut前变化时exact root settle unavailable；已经FULL unlink的prior members不回滚，按§5.7报告progress。

Skill historical messages中的absolute path与loose filesystem path具有相同弱liveness：current view/root borrow退休后，历史路径不保证永远可读。Activated Skill body与compaction retained body已经由existing frozen/canonical proof拥有，不依赖GC重读package文件。

### 4.6 Data directory

`PLUGIN_DATA`按instance而非version定位，replace后保留。第一次successful enable在state enabled cut前由package store逐component no-follow安全创建为Host-user private、可写/可进入的`0700` directory，并机械验证subprocess user可写；创建/permission验证失败则enable `UNAVAILABLE`且state保持disabled。Cold/reload composition在产生process-bearing MCP/Hook values前只调用同一package-store `ensure_instance_data_root()`窄口重验/必要时重建，不让MCP/Hook core写Plugin store；失败只使该instance的process-bearing components unavailable，Skill仍按自身事实处理。Remove默认保留，避免把remove伪装成data deletion authority。

本轮没有`--purge-data`。未来若需要必须独立规格化destructive ownership与confirmation。

---

## 5. Atomic local installation

### 5.1 Shared source-binding preparation

validate与install必须共用一个source-binding seam：

1. lexical absolute preparation；
2. host-known `/tmp -> /private/tmp`、`/var -> /private/var`等system alias normalization；
3. final source directory no-follow open；
4. descriptor-relative package traversal；
5. frozen logical source basename仅用于diagnostic，不用于Plugin identity；identity只来自`plugin.json.name`。

Final source symlink拒绝；system alias normalization不能放松source tree内部symlink禁令。

Current implementation已在`capability/local_skill_source_binding.py`拥有经过dogfood的Darwin alias与absolute descriptor walk。实施时应把该physical policy hard-cut抽到一个purpose-neutral narrow module（例如`pulsara_agent/local_source_binding.py`），由loose Skill validation/publisher、bundled binding和Plugin package source共同调用；删除Skill-named旧module/path，不留re-export alias或两份alias table。Plugin package core不得复制`_DARWIN_SYSTEM_ALIASES`。

### 5.2 One frozen observation

第一次validation、copy、source final revalidation和stage verification必须属于同一个descriptor-relative frozen source observation：

```text
held source root
  -> validate plugin.json and component envelopes
  -> deterministic iterative traversal
  -> streaming copy into hidden stage
  -> final source membership/identity revalidation
  -> paired source/stage streaming byte + executable-class verification
  -> exclusive immutable-root publish
```

不得在validation后关闭source再按string path重开；不得只比较root mtime；不得保存aggregate tree digest充当observation。

Directory evidence至少冻结每个relative member的name、file type、device、inode与executable class；regular-file before/after/path evidence至少比较device、inode、size、`mtime_ns`、`ctime_ns`。Every directory membership在copy后descriptor-relative重枚举并与initial evidence exact比较；root/path binding也要重验。任何差异都settle source raced，不能返回从未真实存在过的mixed package tree。该membership evidence是诚实的`O(member count)`call-local metadata；不得把它伪称constant memory，也不得保存为durable inventory。

Recursive traversal必须iterative，不依赖Python recursion limit，不把所有resource bytes收集到内存。File copy、API-key detection、paired compare均使用constant-memory byte chunks；只有上述identity/membership metadata随member count线性增长。`MemoryError`按§3.4/§5.5进入typed settlement与cleanup，不能用新total cap解决。

Standalone validation使用相同held traversal、component validation、special-file policy、API-key detection与final source revalidation，只省略stage/copy/publish/state mutation。Validate与install不能对同一source给出两套合法性truth。

### 5.3 Hidden stage 与publish

Stage是final package-install-id root的exact hidden sibling：

```text
.pulsara-stage-<package-install-id>-<attempt-nonce>
```

Publisher先取得`<package-install-id>.lock`的exclusive/shared-publish owner，再exclusive创建该stage；stage与final root通过same held parent descriptor操作。Stage validation使用source `plugin.json` identity与frozen final install root，不用stage basename推导package语义。

Final immutable root publish使用exclusive no-replace primitive：

- Darwin：`renameatx_np(..., RENAME_EXCL)`；
- Linux：`renameat2(..., RENAME_NOREPLACE)`；
- unsupported platform返回typed `UNAVAILABLE`；
- 禁止普通`rename`/`os.replace`fallback。

Darwin `RENAME_EXCL`对source directory为`0500`时会返回`EACCES`。Production在stage已经完整normalize/verify为`0500`后，只在同一held stage descriptor与`ProcessApiKeyBoundary` guard跨越irreversible cut期间临时`fchmod(stage_fd, 0700)`，无论publish成功/失败都在离开cut前恢复`0500`。该narrow syscall prerequisite不改变最终immutable mode、不重开path，也不扩大同UID tamper-proof承诺。

Exclusive primitive只保证held destination parent中的final-name no-replace。Final cut后同UID移动parent或替换private stage属于out-of-contract namespace interference。

Package-install-id lock在stage创建前已经由同一publisher持有，并覆盖final publish到instance-state cut/confirmation；publish后不能先release再写state。Lock是physical exclusion，不是durable receipt、lease relation或consumer registry。

GC枚举exact stage grammar后解析同一个package install id，nonblocking取得同一package exclusive lock并重新descriptor-bind exact stage，成功才可删除；锁忙则加入`PluginGcProgress.ordered_in_use`。Malformed/unknown hidden sibling按§4.1永远ignore；GC不能按age猜“旧stage”，也不能删一个无法join lock identity的path。SIGKILL释放OS lock后，后续GC即可回收exact合法stage。

### 5.4 Copy failure classification

优先级固定为：

1. source identity/membership变化 → source raced；
2. source read/metadata I/O → source unavailable；
3. stage create/write/readback/mode/verification → staging unavailable；
4. final-name collision → publish conflict；
5. cleanup failure在prior disposition外层形成`CLEANUP_UNAVAILABLE`。

如果同一attempt同时观察多个failure，必须按上述physical owner priority settle，不能依赖last exception。

### 5.5 Cancellation、worker与cleanup

所有thread/filesystem workers必须：

- 接收absolute deadline与cooperative cancel port；
- caller cancellation时shield；
- 在返回前join exact worker；
- worker确认不再访问source/stage后才cleanup；
- 不留下detached worker。

Stage一旦创建，`MemoryError`、source/stage I/O、deadline、cancellation、validation或publish failure都必须进入同一个typed settlement/finally cleanup owner。不能让`MemoryError`逃逸并遗留stage，也不能用total package cap解决。

### 5.6 PULSARA_API_KEY

Non-empty current exact `PULSARA_API_KEY` value是唯一secret。Install copy必须对每个frozen package-relative member name/path的filesystem bytes以及每个regular-file byte stream做跨chunk exact检测；命中则拒绝package并cleanup，diagnostic path本身也必须经下述scrub。不能把secret藏进resource filename后复制到managed store。

Raw `os.getenv()` snapshot不足以闭合rotation。本轮必须建立一个显式注入、process-local且purpose-specific的`ProcessApiKeyBoundary`：

- application bootstrap为one Pulsara process构造exact one boundary object，并显式注入Plugin package store、generic Hook executor、native MCP SDK facade与provider dispatch sink；不得用module singleton lookup、service locator或每子系统各建一把互不相干的mutex；
- owner内部只有one mutex与current raw-environment snapshot；不是service locator、generation、secret store或durable registry；
- 所有Pulsara-supported key rotation在该mutex内更新`os.environ`与boundary snapshot；所有install/Hook/MCP/provider sinks使用同一boundary；
- 大文件scan可在mutex外完成，但final admission必须重新取得mutex，读取raw environment并与scan snapshot exact比较；相同则保持mutex跨越irreversible `renameatx_np`/`renameat2`、process spawn或HTTP/provider request admission cut，随后立即释放；变化则释放后重扫，不设retry cap，absolute deadline/cancel仍可胜出；
- command、final env/stdin/header/provider model payload的最终secret check也在该guard内完成；不得检查后释放guard、再调用sink；
- 非合作方绕过该port直接并发写`os.environ`属于same-process namespace interference，产品不虚构可序列化它；但每次guard acquisition仍必须从raw environment重读，不能只信cached value。

Provider SDK必须把该key作为authentication credential送给configured provider，因此唯一允许携带exact value的HTTP field是adapter构造时冻结的client-owned credential header（当前为`Authorization`）。它不是model-visible payload、Plugin/public header或diagnostic/evidence内容；final request admission仍检查URL、完整request body、所有header names及除此closed credential-name外的header values。MCP/Plugin headers没有该豁免。该窄carrier是provider authentication本身，不是第二secret authority或任意header allowlist。

该boundary不能用互不相干的`asyncio.Lock`与`threading.Lock`实现。本轮冻结one underlying `threading.Lock` linearization gate与两个narrow acquisition ports：

```text
sync_guard()   # 只供filesystem worker/非event-loop同步sink
async_guard()  # event-loop caller；等待同一underlying gate时offload acquisition
```

- event-loop thread禁止直接blocking acquire；`async_guard()`把同一lock acquisition交给joined worker，caller cancellation时shield该worker直到它取得lock并立即release，或把owned token交还caller，绝不留下detached waiter；
- Python lock token可以由event-loop owner在finally release；两种port共享同一互斥序，不是两个阶段或两个authority；
- guard不是reentrant；持有期间不能调用supported rotation、nested boundary acquisition或任何会反向等待该gate的worker；
- sync guard只跨streaming scan后的final compare + synchronous rename/request-enqueue cut；async guard只跨final compare + exact subprocess/HTTP/provider admission operation。若该operation本身必须`await`才能知道cut是否FULL，则在existing absolute deadline内shield/join这一个admission await并保持gate，知道FULL/not-FULL后立即release；不持gate等待response body、Hook/MCP process lifetime、retry、retire或drain；
- supported rotation从对应sync/async port取得同一gate后一次更新raw `os.environ`与boundary snapshot；因此event loop继续调度不会造成mutex deadlock，而rotation与每个sink cut仍有单一线性化顺序。

Copy开始时的snapshot不能替代publish sink。Stage byte/mode verification完成后，publisher必须在同一absolute deadline内执行：snapshot current non-empty key → streaming scan entire staged membership names/relative paths与regular-file bytes → 进入上述guard并确认raw current key仍等于该snapshot → 保持guard完成exclusive publish admission。Scan期间轮换则用new exact value重新scan；不设retry cap，deadline/cancel胜出时typed settle并cleanup。未来才轮换成一个package原本就含有的byte sequence不追溯撤销已安装package；current provider/process sink仍负责当次exact secret。

Attempt-local scrub set必须保留本次观察过的每个non-empty exact key value，覆盖source/managed paths、exception、diagnostic、human/JSON projection与trace；例如source path本身含key时不能因“diagnostic只显示path”而泄漏。Raw package bytes/definitions使用`repr=False`或等价private carrier。除这些exact values外不广泛redact prompt、Hook/MCP payload、model reply、DSN或普通path。

该检查不递归hash或分析其他secrets，不扫描PATH/dependencies的semantic meaning。Runtime Hook/MCP spawn/request仍必须在最终sink重新snapshot current key并验证command、args、environment、stdin/headers；Plugin Skill/context/ToolResult进入模型时复用existing provider sink final check。Install-time检查不能替代任一runtime postcondition。

### 5.7 Crash 与ACK语义

- ordinary exception/cancel：join worker并尽力cleanup后返回typed outcome；
- state atomic cut前cancel：semantic mutation未发生；已publish version可能成为unreferenced root；
- state atomic cut后reply前cancel：mutation已发生；owner先用known install id/current state作同process exact confirmation，不重跑copy/effect；确认成功返回真实success，确认本身不可用才返回closed `ACK_UNKNOWN`，绝不能谎报`CANCELLED`；
- SIGKILL：可能留下hidden stage或unreferenced complete root；
- power loss：因为本轮不承诺fsync durability，重启后可能看到old/new/missing/malformed state、hidden state temp、hidden package stage、unreferenced root，或new state引用missing package root；这些都按state/package observation typed `UNAVAILABLE`或orphan inventory处理，绝不自动repair；
- `gc`可清理可证明unreferenced且unlocked的物理残留，但不是repair queue/recovery job；
- GC的每个root/stage/state-temp unlink/rmdir是独立FULL cut；若deadline/cancellation/IO在若干cuts后胜出，已经删除的items不回滚。Outcome严格使用§3.3 `PluginGcProgress`，不能用裸status暗示零mutation，也不能声称未访问suffix已分类；
- 不创建receipt、operation nonce relation、rollback log或startup recovery loop。

Final destination在ordinary settled failure下必须完整存在或完全不存在；existing immutable root绝不覆盖。

---

## 6. Enabled-package observation 与Host composition

### 6.1 Closed aggregate

```text
EnabledPluginViewDisposition = COMPLETE | UNAVAILABLE

FrozenEnabledPluginView
  disposition
  ordered USER instances
  ordered exact WORKSPACE instances
  closed component observations
  diagnostics
  held package bindings
```

`COMPLETE`要求instances按`(scope, workspace_state_key, plugin_id)` deterministic且unique，并且每个enabled instance exact join one held current package binding。`UNAVAILABLE`只携带closed aggregate diagnostics，instances/component facts/bindings全空；不能把已经读到的prefix当partial truth。

State observer必须：

- descriptor-held、no-follow枚举目标state roots；
- 冻结direct-child membership和file identity；
- descriptor-relative读取每个state；
- bind referenced immutable package root与manifest identity；
- final revalidate state membership、state files与package root identities；
- 任何replace/delete/mixed observation返回whole `UNAVAILABLE`，不发布partial winners；
- missing state roots表示COMPLETE empty；
- disabled instance仍进入inspection，但不进入enabled component values。

不重试到“看起来一致”，不增加generation或watcher。

Runtime enabled view只观察instance state及其referenced current roots；它不枚举全部historical/unreferenced package directories，因此一个无关orphan root的physical race不能阻止Host composition。`inspect_local_plugins`与`gc_local_plugin_packages`在同一store policy上额外做complete version inventory；该inventory失败只使对应inspection/GC unavailable，不反向伪造Runtime enabled state。

每个enabled package的fixed component observation使用同一closed disposition：

```text
PluginComponentObservationDisposition = MISSING | COMPLETE | INVALID | UNAVAILABLE

FrozenPluginSkillComponentObservation
  MISSING
  COMPLETE(valid definitions, ordered Skill issues)
  INVALID(ordered deterministic issues; no definitions)
  UNAVAILABLE(ordered physical diagnostics; no partial facts)

FrozenPluginMcpComponentObservation
  MISSING
  COMPLETE(ordered declared server keys,
           ordered valid normalized server candidates,
           ordered invalid/unsupported entry issues)
  INVALID(ordered top-level/kind diagnostics; no declared keys/configs)
  UNAVAILABLE(ordered physical diagnostics; no declared keys/configs)

FrozenPluginHookComponentObservation
  MISSING | COMPLETE(generic source snapshot) |
  INVALID(no definitions) | UNAVAILABLE(no definitions)

```

`MISSING`只表示fixed path在complete membership observation中不存在。`INVALID`只表示kind/schema/content等deterministic defect；`UNAVAILABLE`只表示I/O、identity/membership race或无法完成physical observation。Parser、cross-scope selection、inspection和reload必须共享这些exact objects，不能各自重新扫描或从diagnostic string反推presence/declared keys。

### 6.2 Scope-neutral package truth

一个call-local inspection同时观察：

- all USER current instances；
- exact workspace的current instances；
- referenced/unreferenced managed versions；
- enabled/disabled状态；
- per-component COMPLETE/UNAVAILABLE/invalid issues；
- roots被current process或其他process持有时的in-use状态。

`PluginInspectionOutcome.COMPLETE`是list、doctor、GUI与future local Host endpoint的唯一inspection truth。Result只在本次call内immutable；不做跨调用cache、generation或“同一时刻”宣称。

Inspection只拥有package/instance/component observation aggregate，不复制leaf semantics。为形成effective summary，它在同一次call-local cut中：把Plugin Skill batch连同current loose/bundled batches交给existing `SkillCatalogResolver`；把Plugin MCP candidates连同production local/Host config交给同一个native normalization/collision function；读取generic Hook trust assessment。Nested component outcome可以独立`UNAVAILABLE`而package inspection仍`COMPLETE`并完整报告原因。`list`与`doctor`投影同一个已经组成的result，不能再次scan、再次resolve或拿running Host pointer冒充filesystem current truth。

Inspection只能说明“current local state若被new cold/reload合法采用将产生什么”；running Host是否仍持predecessor由refresh notice表达，不能通过CLI跨进程探测或伪造adoption confirmation。

Caller deadline/cancellation在inspection install前胜出时走outer operation abort，不构造`PluginInspectionOutcome`、不把`CANCELLED`伪装成第三种semantic inspection disposition。已经在deadline前FULL install的COMPLETE/UNAVAILABLE observation不因reply阶段cancellation被追溯撤销。

### 6.3 Host current view

Host cold open从一个COMPLETE enabled-package observation构造`FrozenEnabledPluginView`。Package aggregate UNAVAILABLE时：

- Plugin components接收explicit unavailable而不是伪造COMPLETE empty；
- Host本身继续可用；
- Plugin Hook/MCP按各自fail-open边界不阻止local Hook/MCP或ordinary Host；
- 因Plugin是effective Skill catalog的required producer，current `SKILL_CATALOG`按§7.5 whole `UNAVAILABLE`，不得同时声称loose/bundled winners仍可投影；
- diagnostics诚实说明Plugin view unavailable。

Running Host仅在以下seam替换current Plugin view：

- explicit ROOT-only `reload_capabilities`；
- approved compaction successor preparation；
- new Host cold open。

没有watcher。CLI mutation必须提示running Host需要reload、compaction或restart。

Semantic observation `UNAVAILABLE`与owner outer abort必须分开：

- state/package observation在deadline前完整settle为`UNAVAILABLE`时，Host尝试发布一个closed unavailable Plugin view：Skill third batch `UNAVAILABLE`且无facts；future Plugin Hook sources为空并发出Host-level diagnostic；Plugin MCP candidate tuple为空并由native owner retire old future configs；old already-dispatched physical consumers仍drain；
- deadline/cancellation/stale Host owner在candidate observation install前胜出时，不构造Plugin view、不发布任何component，predecessor保持current；
- 不得把semantic unavailable当outer cancel，也不得在semantic unavailable时从predecessor Plugin values恢复stale future contribution。

### 6.4 Component failure isolation

Package state aggregate完整后，各component按自己的标准failure boundary处理：

- invalid Plugin Skill逐项issue，其他Skills继续；
- fixed-location wrong kind进入exact component `INVALID`，不是aggregate/component `UNAVAILABLE`；
- invalid `mcp.json` top-level进入MCP `INVALID`并只禁用该package MCP；`COMPLETE`中的invalid server只skip exact server且其declared key仍被保留；
- invalid Hook config进入exact Hook component `INVALID`；physical read/race才是`UNAVAILABLE`；
- one component failure不回滚package enablement或其他components。

No component adapter能改写package state。

### 6.5 Cross-scope component selection

USER与exact WORKSPACE同`plugin_id`可以同时enabled。Selection按component明确：

- Skills：两者都进入central resolver，WORKSPACE Plugin tier高于USER Plugin tier；
- MCP：同`plugin_id + local_server_id`时WORKSPACE contribution覆盖USER contribution；
- Hooks：同`plugin_id`时WORKSPACE Hook source覆盖USER Hook source；
不同Plugin之间不按manifest version、install time、directory order或lexicographic accident赋予semantic precedence。需要tie outcome的Skill见§7.4；MCP identity本身包含plugin id。

同plugin id的cross-scope fallback必须区分missing与broken override：

- WORKSPACE fixed component path missing表示没有override claim，USER同Plugin component/leaf可继续；
- Skills继续遵守central resolver既有truth：invalid higher candidate不成为valid shadow marker，lower valid tier可胜出；
- WORKSPACE `mcp.json` COMPLETE时，每个declared server key即使individual invalid也claim exact `(plugin_id, local_server_id)`并压住USER counterpart，其他USER-only server继续；top-level MCP component present但UNAVAILABLE/invalid且无法形成完整keys时，压住该Plugin整个USER MCP component而不是执行lower unknown set；
- WORKSPACE Hook fixed path present即claim whole Hook component；invalid、unavailable、disabled generic Hook trust或UNTRUSTED都不能让USER Plugin Hook偷偷fallback执行；
- doctor必须显示“broken higher override suppressed lower”而不是把lower标成普通winner。

这些shadow markers只存在于本次complete component observation，不是durable tombstone、registry或generic precedence framework。

---

## 7. Plugin Skill：显式第三definition producer

### 7.1 Dependency direction

Plugin Skill adapter只把enabled package中的portable `skills/`变成current Skill subsystem能消费的typed batch：

```text
plugins/skill_producer.py
  -> capability.parse_skill_document
  -> capability.validate_skill_candidate_placement
  -> capability.PluginSkillOrigin
  -> FrozenPluginSkillDefinitions

capability/*  -X-> plugins/*
```

`SkillCatalogResolver`只消费三个explicit inputs；它不读Plugin state/filesystem。

### 7.2 Closed types hard cut

在`capability/types.py`与resolver contracts中一次性增加：

```text
SkillProducerKind.PLUGIN
SkillSource.PLUGIN

PluginSkillVisibilityScope = USER | WORKSPACE

PluginSkillOrigin
  visibility_scope
  workspace_state_key?     # iff WORKSPACE
  plugin_id
  package_install_id
  package_relative_skill_directory  # skills/<name>

SkillDefinitionOrigin =
  LooseSkillOrigin | BundledSkillOrigin | PluginSkillOrigin

PluginSkillDefinitionsDisposition = COMPLETE | UNAVAILABLE

FrozenPluginSkillDefinitions
  disposition
  candidates
  invalid_issues
  unavailable_cause?

SkillProducerUnavailableReason additions
  PLUGIN_VIEW_UNAVAILABLE
  PLUGIN_RESOURCE_UNAVAILABLE
  PLUGIN_DISCOVERY_RACED

SkillDiagnosticCode additions
  PLUGIN_DEFINITIONS_UNAVAILABLE
    = "skill_plugin_definitions_unavailable"
  PLUGIN_SAME_TIER_NAME_CONFLICT
    = "skill_plugin_same_tier_name_conflict"
```

Origin携带完整exact values，不新增stored/caller-supplied origin/provenance fingerprint。`PluginSkillOrigin`定义在capability contracts中，避免capability反向import Plugin runtime。Current `SkillDiagnosticCode` exact guard随上述two additions由33 hard-cut为35；不存在old/new enum dual path。

同时机械扩展current closed invariants：

- `SkillManifest.__post_init__`接受exact three-origin union；
- `CompleteEffectiveSkillCatalogInspection`允许`InvalidSkillCandidateIssue.origin`为Loose或Plugin，仍禁止Bundled invalid issue；
- `SkillCandidateIssueKind`增加`CONFLICTING`，union增加一个group-level `ConflictingSkillCandidateIssue` arm：`name + exact tier + ordered(path, origin) candidates + exact diagnostic code`；每个group至少two members且每个candidate只出现一次；members仅为deterministic projection按`(plugin_id, package_install_id, path)`排序，不形成winner precedence；
- every complete valid candidate必须exact分配为winner、shadowed或conflicting之一；
- `skill_candidate_issue_sort_key`与producer-unavailable order显式包含Plugin tier/kind；
- `skill_source_for_origin`、`skill_origin_label`、catalog/active/doctor projection对Plugin origin exhaustive；
- unknown origin仍typed/raise closed-union error，不用default branch猜source。

### 7.3 Single parser/placement path

Plugin producer对每个portable `skills/` immediate child：

1. 从held immutable package root descriptor观察exact child；
2. exact regular `SKILL.md`才是candidate；
3. 调用current root-neutral `parse_skill_document(raw)`；
4. 调用current placement validator，以immediate child basename验证name；
5. 形成普通`SkillManifest`与`PluginSkillOrigin`；
6. invalid document/placement形成普通`InvalidSkillCandidateIssue`；
7. supporting resources不递归scan/hash。

不得出现Plugin name regex、YAML loader、fallback parser、document bound或host-extension semantics副本。

### 7.4 Seven-tier precedence 与same-tier collision

Final precedence：

```text
1. workspace <workspace>/.pulsara/skills
2. workspace <workspace>/.agents/skills
3. user ${PULSARA_HOME}/skills
4. user ~/.agents/skills
5. exact WORKSPACE Plugin Skills
6. USER Plugin Skills
7. Pulsara bundled Skills
```

同一Plugin tier内，两个不同enabled Plugins可以提供同名Skill。Resolver不能按plugin id、install time或path任意选winner。

对每个name按tier从高到低：

1. first non-empty tier只有一个candidate：它是winner，所有lower valid candidates是SHADOWED；
2. first non-empty tier有多个candidates：形成one group-level `ConflictingSkillCandidateIssue`与one causal `skill_plugin_same_tier_name_conflict`，该tier不产生winner，继续检查下一tier；
3. lower tier若有unique candidate可成为fallback winner；
4. 所有tiers都无unique candidate时该name不进入effective catalog；
5. 已有更高unique winner时，lower Plugin candidates只记SHADOWED，不重复制造无消费者conflict truth。

因此一个Plugin collision不会让整个Skill aggregate unavailable，也不会随机授权某个Plugin；其他Skill names继续可用。

本轮必须扩展closed issue union与diagnostic enum，而不是把conflict伪装成INVALID或UNAVAILABLE。

`SkillDiagnosticCode.PLUGIN_SAME_TIER_NAME_CONFLICT`只有一个message/severity owner；Plugin inspection不得再产生一个同义generic diagnostic。

Resolver allocation proof把group issue中的ordered candidate refs展开后与input valid candidates exact比较；不能为便于assertion又建立一份conflict map。Group carrier避免N个conflicting candidates各自复制N个peers造成O(n²) metadata；projection仍完整显示全部paths/origins且不截断。

### 7.5 Required producer availability

Plugin package view complete且零enabled package时，`FrozenPluginSkillDefinitions.COMPLETE(empty)`。

只有以下physical causes使Plugin Skill producer `UNAVAILABLE`：

- enabled-package aggregate本身`UNAVAILABLE`；
- 任一enabled instance的referenced immutable package root无法bind、被replace或无法完成identity revalidation；
- 任一enabled instance的`skills/` physical observation为`UNAVAILABLE`（包括descriptor-relative membership race或I/O failure）。

`skills/`为`MISSING`或deterministic `INVALID`（例如fixed path wrong-kind）都属于`COMPLETE` producer batch中的empty exact package slice + complete issues；它们不把Plugin producer升级成`UNAVAILABLE`，也不抹去其他Plugin、loose或bundled Skill winners。Invalid individual Plugin Skill同样只进入existing Skill issue algebra。换言之，只有无法证明完整physical batch的cause才触发whole-catalog fail-closed，已经完整观察到的deterministic component defect不冒充availability failure。

Owner deadline/cancellation在Skill source snapshot install前胜出时走existing outer abort，不构造`FrozenPluginSkillDefinitions.UNAVAILABLE`、不追加source observation/CAS；只有deadline前settled的non-deadline Plugin failure才可成为semantic producer cause。

与current bundled/loose contract一致，任何required producer `UNAVAILABLE`时effective Skill catalog whole `UNAVAILABLE`、零winners、零facts、active fail-closed。

Unavailable cause order固定：

```text
LOOSE, PLUGIN, BUNDLED
```

不因Plugin failure从predecessor/current hidden map恢复stale winners。

### 7.6 Composer与source contract

`KernelSkillProjectionComposer`显式接收：

```text
BundledSkillDefinitionProducer
LooseSkillDefinitionProducer
exact FrozenPluginSkillDefinitions from the captured FrozenEnabledPluginView
SkillCatalogResolver
```

每个safe point冻结一个absolute deadline，按current owner topology取得三项完整batch，再resolve/install一次。`freeze_owner_snapshot(...)`必须显式接收本次Host capture的exact `FrozenPluginSkillDefinitions` object，或接收一个只返回该exact object的narrow owner-held callable；不得让composer自行scanPlugin state、查询service locator或接受caller手写package paths。Plugin batch必须与同一`FrozenEnabledPluginView`逐对象join。

Capability source继续exact：

```text
CapabilitySourceKind.LOCAL_SKILL_CATALOG
stable_source_id = "pulsara-local-skill-catalog"
```

Source contract hard-cut为新单一路径，例如：

```text
domain = "skill-source-contract:v4-bundled-loose-plugin-skills"
payload = {
  parser_contract,
  placement_contract,
  producer_kinds: ("LOOSE", "PLUGIN", "BUNDLED"),
  precedence: seven exact tiers,
  bundled_names: exact current inventory,
}
```

删除v3 production path，不做dual contract/compatibility flag。既有loose/bundled candidate/fact identity在其origin fields未变时必须保持；Plugin fact origin framing直接编码完整`PluginSkillOrigin`，不保存另一个DTO fingerprint。

### 7.7 Runtime、read与compaction

Plugin winner与loose/bundled winner走完全相同的：

- `ResolvedSkillCatalogEntry`；
- `SKILL_CATALOG`；
- textual/explicit activation；
- `ACTIVE_SKILL`；
- ordinary `read_file` absolute path；
- ToolResult projection；
- retained Skill proof；
- compaction successor composition；
- same-epoch continuity。

Plugin root只读，不等于provider authority；Skill body仍是untrusted guidance。Supporting resource read使用ordinary filesystem tool和existing permission/path policy，不建立Plugin read tool。

Package replace/disable后，old active/retained body按existing frozen/canonical facts完成；current catalog在next Plugin view publication + Skill safe point使用new truth。历史absolute path不承诺跨GC永久存活。

---

## 8. Plugin MCP：只进入existing native config/supervisor

### 8.1 Portable parser

只读取root `mcp.json`，必须满足Agent Plugins 1.0：

- exact `$schema == https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`；
- schema version与`plugin.json`匹配；
- top-level只有`$schema`与`mcpServers`；
- 每个server独立validate；
- supported transports：`stdio`与`streamable-http`；
- `sse`本轮typed unsupported，不fallback；
- Runtime不联网fetch schema。

### 8.2 Native normalization

每个valid server归一化为existing `McpServerConfig`：

```text
server_id = "plugin:<decimal UTF-8 byte length of plugin-id>:<plugin-id>:<full local-server-id>"
display_name = "<plugin-id>:<local-server-id>"
enabled = true
required = false
scope_policy = ROOT_AND_SUBAGENTS
effect_policy = AUTO
exposure_policy = ALL
```

Plugin不能声明：

- required startup gate；
- permission bypass；
- effect override；
- ROOT-only/child-only hidden authority；
- concurrency/timeout beyond native accepted values；
- existing configured-server bound override。

为使native supervisor在model surface不变、physical package version变化时仍能retire旧client，generic `McpServerConfig`增加一个有直接consumer的closed source carrier：

```text
McpRuntimeSourceIdentity =
  LocalConfiguredMcpRuntimeSource |
  ManagedPackageMcpRuntimeSource(
    store_scope_key,
    package_owner_key,
    package_install_id,
  )
```

Existing local/Host config使用first arm；Plugin adapter用scope/workspace-key + plugin id机械形成前两个safe exact keys，并携带current package install id。Native MCP core只比较/编码closed values，不解析它们回读Plugin state。该carrier不是durable provenance、tree fingerprint或display identity；唯一consumer是runtime replacement/reconnect与old lifetime anchor retirement。

`plugin:`是Plugin-generated server namespace。Length framing按UTF-8 bytes解析出exact plugin id，local server id是剩余完整suffix；因此`("a.b", "c")`与`("a", "b.c")`等组合不能alias。禁止用`.`拼接、escaping约定、truncate或hash来弥补非injective identity。Native 128-byte server-id bound在完成上述framing后应用；overbound只使exact server invalid。

Local/Host config若已有同一exact server id，它作为existing explicit authority保留，冲突的exact Plugin server形成`plugin_mcp_server_id_collision`并被skip；两个Plugin candidate若仍形成同一exact framed id，也全部skip而无任意winner。其他local/Plugin servers继续进入candidate tuple。不能让merge order静默覆盖，也不能因一个collision保留整份stale predecessor config。

Provider name collision不是Plugin diagnostic owner。Existing native naming/install path必须从raise-only hard-cut为closed call-local collision truth：

```text
McpProviderNameCollisionFact
  provider_name
  ordered exact members: (server_id, remote_tool_name, discovered tool identity)
```

Native owner先对本次完整discovery candidate set运行`mangle_mcp_tool_names`，再按provider name形成groups。Group size大于一时不选winner，全部ambiguous members同时从DIRECT projection和`MCP_CATALOG`/META directory省略；同一server内部truncate/normalize collision也走同一algebra。其他tools、servers、resources、templates、prompts及execution-policy facts继续可用。不得用hash改写model-visible name，也不得让一个raw `ValueError`/`RuntimeError`中止whole MCP install。Cold/compaction cohort只安装collision-free descriptor；same epoch已经安装的DIRECT descriptor仍遵循existing stale-schema/availability gate，late discovery不能改写`tools[]`。

Per-server `McpDiscoverySnapshot`继续拥有remote discovery truth；cross-server分组只能由收齐全部server snapshots的native aggregate projection owner完成。`FrozenMcpCapabilityProjectionInput`因此增加ordered `provider_name_collision_facts` nested field，并把这些groups加入其existing catalog/projection fingerprint framing；各collision member不进入source tool facts/inspectability routes。它不是Plugin diagnostic、Committed/Live event、durable receipt或新的fingerprint registry。Directory/doctor projection可以解释被省略成员，但dispatch只能解析collision-free exact `(server_id, remote_tool_name)`。

### 8.3 stdio

- `command`保持一个token，不shell split；
- bare executable按本次native config composition冻结的Host `PATH`规则解析；该exact configured `PATH`进入runtime/resolved config identity并随changed config正常retire/restart，spawn不能重新读取一个不同PATH偷换reviewed lookup；Plugin作者不能假定任意Host具有某个bare executable；
- `./...`command descriptor-relative resolve到immutable package root内，再形成exact absolute executable path；
- command若含path separator则只接受上述contained `./...` form；absolute path、`../...`或其他relative path form使exact server invalid；
- omitted cwd使用package root；
- `cwd`只接受standard `./...`、`${PLUGIN_ROOT}`或`${PLUGIN_DATA}` forms并保持contained；
- `args`、`env` values与`cwd`对原始string中的每一个exact `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` occurrence执行一次、left-to-right、non-recursive expansion；replacement产生的文本不再scan；
- `command`和environment key永不做placeholder expansion；unknown placeholder-like text保持literal；
- `args`/`env`中的unrecognized placeholder-like text按standard保持literal；replacement引入的text不再scan，因此不存在cycle evaluator；
- 只有`cwd`因不满足上述closed forms或post-expansion containment才使exact server invalid；`args`/`env` values按standard是opaque strings，不做path containment猜测；
- Plugin env中exact `PLUGIN_ROOT`、`PLUGIN_DATA`或`PULSARA_API_KEY` key使exact server invalid；不得先接受再覆盖或删除；
- final subprocess environment由native MCP transport owner按existing public base allowlist + frozen Plugin overlay构造，最后强制设置exact `PLUGIN_ROOT`/`PLUGIN_DATA`；bare command查找使用的就是这份final frozen `PATH`；
- `PULSARA_API_KEY`从base/overlay删除并在spawn sink重新验证command、args、cwd、env。

Current `StdioTransportConfig.cwd: str | None`与SDK“永远workspace-relative resolve”不能承载上述语义。本轮在native `mcp_config.py` hard-cut为closed generic carrier：

```text
McpStdioCwdBinding =
  WorkspaceRelativeMcpCwd(relative_path) |
  ExactAbsoluteMcpCwd(absolute_path, authority = PACKAGE_ROOT | INSTANCE_DATA)
```

Existing local/Host config的omitted cwd仍由native parser形成`WorkspaceRelativeMcpCwd(".")`，relative cwd继续由SDK在exact Host workspace内resolve；Plugin server无论cwd omitted、`./...`、`${PLUGIN_ROOT}...`或`${PLUGIN_DATA}...`，adapter都先通过held package/data binding形成`ExactAbsoluteMcpCwd`。SDK收到absolute arm后必须原样作为cwd，不再拼接/re-resolve workspace。Binding arm、exact normalized path和frozen lookup `PATH`进入runtime config payload；§4.5 `PhysicalLifetimeAnchor`附在generic config/client可达对象，`repr=False, compare=False`且不进入任何fingerprint。

Package root/data binding由Plugin composition传给adapter；MCP supervisor不读Plugin state或manifest。`${PLUGIN_DATA}` cwd在composition时还必须通过package-store narrow port证明exact instance data root存在、是held no-follow directory且Host user可写/进入；失败只使exact process-bearing server unavailable。

### 8.4 Streamable HTTP

Agent Plugins fixed `headers`是visible public data，不是secret refs，也不是authentication mechanism。Current native config hard-cut在generic `McpServerConfig`增加独立字段：

```text
public_headers: tuple[(original_name, literal_value), ...] = ()
```

Existing local/Host configs默认empty。不得给`McpAuthConfig`增加`StaticPublicHeaders` arm，不得把public headers塞进`StaticHeaderEnvironmentRefs`、伪造environment variable names或建立Plugin-private HTTP client/optional config wrapper。`McpServerConfig.__post_init__`、`resolved_headers()`、runtime/resolved config fingerprint builders与HTTP request owner必须共同消费这一generic field；exact literal values进入runtime payload，并由resolved identity间接覆盖，但不进入model-surface semantic payload；`PhysicalLifetimeAnchor`不进入任何identity。

规则：

- absolute HTTP/HTTPS，无userinfo/fragment；
- non-loopback必须HTTPS；HTTP只接受case-insensitive exact hostname `localhost`，或由`ipaddress.ip_address(parsed.hostname).is_loopback`判定为true的IPv4/IPv6 literal；不做DNS resolution，也不把`localhost.example`、private/link-local地址当loopback；
- no redirect/cross-origin header forwarding；
- header name必须是RFC 9110 ASCII `tchar`且按ASCII casefold唯一；literal value必须为空或由HTAB/SP/visible ASCII组成，禁止NUL、CR、LF、其他control/non-ASCII及leading/trailing OWS；owner不得trim、case-normalize或重写用户审阅值；
- url/header不做placeholder/env expansion；
- current exact `PULSARA_API_KEY`值出现在url/header时拒绝；
- no OAuth、credential ref或headers helper；
- final header merge按case-insensitive key执行：package `public_headers`最低，native HTTP/MCP protocol headers与resolved existing auth headers覆盖同名public value；client-owned layers之间继续使用existing native precedence。最终request headers在§5.6同一`ProcessApiKeyBoundary` guard内重验并进入HTTP admission cut；
- missing/invalid secret environment ref、authorization acquisition失败或server返回authentication failure属于native connection/runtime failure，不回写Plugin config为INVALID。

### 8.5 One MCP inventory

Host composition显式合并：

```text
existing local/Host McpServerConfig tuple
+ current Plugin McpServerConfig tuple
-> one deterministic tuple
-> existing MCP supervisor install_config_epoch
```

MCP supervisor、discovery、naming、slot、catalog、DIRECT/META route、permission、effect、attempt、reconnect、close全部保持唯一owner。`conversation_kernel/mcp/*`不得import`plugins/*`。

Existing `MAXIMUM_CONFIGURED_MCP_SERVERS = 64`对合并后tuple生效。Overbound时new MCP config epoch不发布，old exact supervisor config继续；Plugin Skill/Hook component不因无关MCP bound被回滚。该outcome必须进入`reload_capabilities`component result与doctor。

### 8.6 Runtime behavior

- cold/compaction决定完整cohort的DIRECT/META exposure；
- same epoch late READY走existing `MCP_CATALOG` successor；
- meta wrapper只对resolved underlying remote tool产生一组Pre/Permission/Post Hook；
- same identity schema replacement不能通过META绕过installed DIRECT descriptor；
- disable/remove后future META route消失；installed DIRECT descriptor仍在`tools[]`，native availability gate拒绝；
- config unchanged时reload应复用existing physical slot/process；
- old changed/removed process持有old package lock直到terminate/kill/drain；
- one server start/connect/handshake failure不阻止其他server或Plugin components。

每个Plugin-produced native config都携带一份独立§4.5 `PhysicalLifetimeAnchor`；supervisor candidate、installed slot、stdio process或HTTP client只要仍可能start/reconnect/request/retire，就必须通过generic config/client object持有它。Config comparison忽略anchor，因此semantic/runtime/resolved identity不变时可复用slot；被拒candidate在确认无client/process引用后close自身anchor，changed/removed old slot则在native terminate/kill/drain与所有request settlement完成后close。不能只让`FrozenEnabledPluginView`持lock，因为view replace早于old physical consumer drain。

Native identity matrix固定为：

```text
semantic_config_fingerprint
  = model-visible server identity/display + enabled/required/scope/exposure/effect

runtime_config_fingerprint
  = transport/endpoint/command/args/native cwd binding/frozen PATH/env/auth/
    public_headers/timeouts/concurrency/refresh + McpRuntimeSourceIdentity

resolved_config_identity
  = exact server_id + semantic_config_fingerprint + runtime_config_fingerprint

excluded from all three
  = PhysicalLifetimeAnchor object/descriptor
```

Package replace即使HTTP URL/public headers完全相同，`ManagedPackageMcpRuntimeSource.package_install_id`仍变化，因此是runtime-only config change：native supervisor保留same-epoch semantic descriptor，立即fence old slot的新dispatch，连接new config/client，并按existing safe point切换；old slot/request/process完成terminate/kill/drain后释放old anchor。New config anchor不能通过“resolved identity unchanged”被丢弃，old anchor也不能滞留到Host close。只有上述全部runtime values（含package source identity）都exact相同才可复用physical slot。

---

## 9. Plugin Hook：Round 9.2第三definition source

### 9.1 One adapter, not a Hook subsystem

Plugin adapter只读取：

```text
dev.pulsara/hooks/hooks.json
```

然后构造Plugin provenance并调用唯一production：

```text
hooks/config_parser.py
HookTrustStore
KernelHookDispatcher
HookCommandExecutor
Hook output parser/aggregation
HookContextOwner
ContextSourceKind.HOOK_CONTEXT
```

`hooks/*`不得import`plugins/*`；dependency只能是`plugins/hook_adapter.py -> hooks/contracts.py + hooks/config_parser.py`。

### 9.2 Closed source identity

Round 9.2 source contract一次性hard-cut为closed union：

```text
LocalFileHookSourceIdentity
  USER_FILE | WORKSPACE_FILE
  canonical path
  visibility/workspace key

PluginHookSourceIdentity
  kind = PLUGIN
  visibility scope = USER | WORKSPACE
  workspace_state_key?
  plugin_id
  package_install_id
  config_relative_path = "dev.pulsara/hooks/hooks.json"
  canonical managed path
```

不要把Plugin fields加成一组optional fields塞进local identity。

Trust subject稳定定位instance：

```text
plugin:user:<plugin-id>
plugin:workspace:<workspace-state-key>:<plugin-id>
```

Subject carrier同样是closed union，而不是在current `HookTrustSubject(source_kind, stable_locator)`上继续堆string parsing：

```text
LocalFileHookTrustSubject
  source kind = USER_FILE | WORKSPACE_FILE
  workspace_state_key?  # iff WORKSPACE_FILE

PluginHookTrustSubject
  source kind = PLUGIN
  visibility scope = USER | WORKSPACE
  workspace_state_key?  # iff WORKSPACE
  plugin_id

HookTrustSubject = LocalFileHookTrustSubject | PluginHookTrustSubject
```

`stable_locator`若仍用于display/diagnostic，只能由typed subject pure projection产生，不能反向解析为execution/path authority。

Current package install id、exact normalized definitions与declaration environment进入Round 9.2 existing normalized-definition trust digest，不进入subject key。Replace即使command文本相同也因new install id成为MODIFIED/UNTRUSTED；enable不等于trust。

Digest builder以closed identity union分支framing：existing USER_FILE/WORKSPACE_FILE inputs的canonical bytes与digest必须保持byte-identical；Plugin arm额外编码visibility/workspace key/plugin id/package install id/config relative path/canonical managed path。这里不需要v1/v2 dual evaluator；同一builder对local old arms保持原framing，对new arm使用新closed payload。

Existing `HookTrustStore._state_path/_lock_path`当前只覆盖one USER file和one exact WORKSPACE file，不能用`else -> workspace`承载多个Plugin subjects。本轮在同一个generic store内把closed path matrix hard-cut为：

```text
trust/user.json
trust/workspace/<workspace-state-key>.json
trust/plugin/user/<plugin-id>.json
trust/plugin/workspace/<workspace-state-key>/<plugin-id>.json

locks/user.lock
locks/workspace/<workspace-state-key>.lock
locks/plugin/user/<plugin-id>.lock
locks/plugin/workspace/<workspace-state-key>/<plugin-id>.lock
```

Logical subject仍用closed typed fields而不是解析`stable_locator`字符串决定scope/path；plugin id与workspace key使用各自already-validated filesystem-safe exact component。不得把所有Plugin subjects塞进一个不断增长的global JSON，不新增Plugin-private store或legacy dual path。Orphan Plugin trust可由generic Hook doctor/revoke看到，但没有current exact source时永远不能执行。

Generic trust store必须接收shared resolver产出的absolute `PULSARA_HOME`，并对上述新增directory components逐级no-follow准备；不得在store内部对relative home调用cwd-relative `resolve()`。

### 9.3 Source order 与scope

Future view source order固定：

```text
1. USER hooks.json
2. WORKSPACE hooks.json (project workspace only)
3. effective USER Plugin Hook sources, plugin_id sorted
4. effective WORKSPACE Plugin Hook sources, plugin_id sorted
```

同plugin id的WORKSPACE source在adapter层覆盖USER source，因此不会重复运行；不同Plugin sources与local sources是append关系，不做semantic dedup。

Order只决定existing aggregation中的stable definition order，不授予某个source绕过explicit control/fail-open rules。

### 9.4 Environment与placeholder

Plugin source declaration environment只可包含：

```text
PLUGIN_ROOT=<exact managed immutable root>
PLUGIN_DATA=<exact persistent data root>
```

Plugin adapter不得对Hook `command`/`commandWindows`做`${PLUGIN_ROOT}`/`${PLUGIN_DATA}`字符串替换、shell quoting或path containment猜测。Command保持config parser产出的exact normalized string；generic Hook executor把上述两个public values作为declaration environment交给现有shell execution，作者可按shell语义显式写`"${PLUGIN_ROOT}"`/`"${PLUGIN_DATA}"`。`inspect`必须同时显示exact command与exact declaration environment，existing trust digest覆盖二者；package root/install id变化会通过source identity/environment使trust变为MODIFIED。Executor最终收到的command必须与用户审阅值byte-identical，不得在trust后重写。

这不是Hook subprocess sandbox：reviewed command仍可有ordinary shell expansion、bare PATH executable或访问package root之外的路径。Plugin package containment只约束安装与fixed component读取；Hook trust授权的是exact command declaration，不是递归依赖attestation。

Host-derived workspace/project variables继续由Round 9.2 attempt-time executor拥有，不进入Plugin stable overlay。Plugin不能声明或覆盖`PULSARA_API_KEY`、Hook source/project owner fields。

### 9.5 Generic stdin/context不含Plugin provenance

Hook stdin仍严格使用Round 9.2的11 public variants。不得增加：

- plugin id；
- package install id；
- config path；
- trust subject；
- package scope。

这些值只属于inspection、trust与diagnostic。Provider-visible `HOOK_CONTEXT`只显示source-neutral event/context；不出现`PLUGIN_HOOK_CONTEXT`或package provenance。

### 9.6 Trust与CLI

Round 9.2 generic Hook management扩展Plugin source selection，但不新增Plugin trust CLI/owner：

```text
pulsara hooks inspect --source plugin:<plugin-id> --scope ...
pulsara hooks trust --source plugin:<plugin-id> --scope ... --expected-definition-digest ...
pulsara hooks revoke --source plugin:<plugin-id> --scope ...
```

Exact CLI syntax应按current Hook CLI parser窄改并由tests冻结。Trust state仍位于generic Hook trust store；orphan trust没有current exact source时永远不能执行。

Trust UI必须明确：Plugin enable授权package contribution和MCP spawn，Hook command执行仍需separate exact trust；Hook process以Host OS user运行，不是假装sandboxed Tool。

### 9.7 One future-view publication lane

`KernelHookDispatcher`现有`_publication_lock`是唯一future Hook view publication mutex，但current `reload()`在mutex内执行filesystem discovery会长时间占锁。本轮把scan/build与publication hard-cut分开，并冻结全局锁序：

```text
slow scan / parse / trust assessment / candidate build  # no publication/Host lock
-> Hook _publication_lock
-> short Host _lock
-> exact revalidation + pointer cuts only
-> release Host _lock
-> release publication lock
```

任何路径都不得按`Host _lock -> Hook _publication_lock`反向取得；持有两把锁时不得等待filesystem worker、Hook execution、MCP install/start/retire、provider call或physical drain。

Narrow merge seam必须做到：

- `reload_hooks`先capture one complete Hook predecessor view，在两把锁之外完整重读USER/WORKSPACE files，并对该captured view中的current immutable Plugin source declarations重评generic trust；进入publication mutex后必须验证`dispatcher.current_view is captured_predecessor`，而不是只比较Plugin slice。任一local或Plugin slice、trust/diagnostic view已经被其他writer发布，都立即release mutex并在同一absolute deadline下从fresh complete capture重做slow assessment；identity仍exact相同才用candidate local slice + assessed Plugin slice merge。该optimistic loop无retry-count cap且cancel/deadline可胜出；它不scan Plugin state/package，也不在mutex内读trust filesystem；
- `reload_capabilities`先在两把锁之外观察package并构造new Plugin Hook slice；进入publication mutex后重新捕获**当前** Hook commit predecessor并保留其current local slice，再merge candidate Plugin slice；它不重读local Hook files；
- 两者随后只短暂进入Host lock，revalidate Host仍open、exact Plugin composition predecessor/candidate仍current、Hook commit predecessor object identity未变，再执行各自授权的pointer cut；失败则不发布该owner，返回typed stale/partial outcome；
- concurrent reloads因此总是以进入publication mutex时的current opposite slice合并，不能lost-update或分别发布相互覆盖的views；
- 不暴露generic mutable event bus、view lease、manual refcount、generation或registry map。

这里有两个不能混淆的predecessor：`reload_capabilities`自己的PreTool/Permission/PostTool使用invoke前捕获的**lifecycle predecessor view**，确保同一physical attempt不被new Hooks反向包围；publication merge/revalidation使用进mutex后捕获的**commit predecessor view**，确保并发`reload_hooks`更新不丢失。PostTool仍使用lifecycle predecessor，即使commit已经FULL。New definitions只从下一event生效。

### 9.8 Runtime semantics

- 11 events、11 public stdin variants、12 internal causal arms不变；
- unsupported event/handler在最窄entry typed skip；
- engine/transport/parser failure fail-open；
- only successfully parsed explicit control能block/continue；
- old in-flight attempt持有old immutable definition/package lock直到settle；
- disable/remove/replace/revoke只影响future events；
- Stop/SubagentStop continuation仍最多一次；
- SessionEnd physical order不变；
- Plugin adapter不直接dispatch任何lifecycle event。

Plugin definition的generic `FrozenHookSourceProvenance`必须携带§4.5 one independent `PhysicalLifetimeAnchor`（local source为`None`），并由每个normalized definition/execution request可达；field `repr=False, compare=False`且不进入trust digest、matcher、stdin、context或diagnostic。Dispatch capture、sync/background execution与one-shot context settlement只要仍持old definition就持old shared package lock；settle并释放最后ordinary object后anchor idempotent close/finalize，GC才可能取得exclusive lock。不得用source path重开package lock，也不得增加manual refcount/view lease/registry。

---

---

## 10. Reload、compaction与publication semantics

### 10.1 `reload_capabilities` descriptor

新增fixed ROOT-only Builtin `reload_capabilities`：

- 从所有cold epochs一开始就存在于Builtin surface；
- ROOT BYPASS-permissions only，其他scope/mode typed denied；
- 不安装package，不修改state，不grant Hook trust；
- 只观察已提交local state并尝试为future consumers发布new component values；
- 返回per-component closed outcomes与diagnostics。

它不能在Plugin首次出现时热加自身descriptor。

### 10.2 Publication不是虚假跨owner transaction

Plugin lifecycle state、Skill source、MCP config epoch与Hook future view各有真实owner。本轮不虚构一个跨这些owners的atomic transaction。

`PluginRuntimeCompositionOwner`可以保存latest FULL-observed `FrozenEnabledPluginView`以及各consumer当前安装的exact projection object references，但不能用一个`all_components_current=true`、generation或aggregate fingerprint掩盖partial publication。每个consumer的current truth仍由其真实owner pointer/config epoch决定；inspection则报告durable package state与running-Host refresh notice，不伪造跨process Runtime状态。

`reload_capabilities`顺序固定：

1. 在short Host capture中冻结predecessor Plugin composition view、invoke-time lifecycle Hook view、MCP config owner value与current tool/planning owner已经给出的absolute deadline，然后释放Host lock；
2. ordinary PreTool、以及仅在existing permission owner已经决定ASK时的PermissionRequest，使用invoke-time lifecycle Hook view；
3. 在Host/Hook publication locks之外complete observe candidate package view；
4. 在locks之外build all component candidate values，无side effect；
5. 取得Hook `_publication_lock`，重新capture current Hook **commit predecessor**，用其current local slice + candidate Plugin slice构造exact merged view；
6. 在仍持publication lock时短暂取得Host `_lock`，revalidate Host open、step 1 exact Plugin predecessor仍current、candidate package bindings仍owned、Hook commit predecessor object identity仍current；全部成立才以non-awaiting pointer cuts发布future Plugin Skill view与merged Hook view；随后先release Host lock，再release publication lock；
7. 不持上述两把锁，向existing MCP owner请求在其native safe point install candidate merged config epoch；
8. ordinary PostTool Hook仍使用step 1同一invoke-time lifecycle view；
9. return exact per-component result。

Step 5–6只做call-local merge、revalidation和pointer assignment；不得scan、重评filesystem trust、等待Hook attempt或调用MCP。`reload_hooks`遵守§9.7同一`publication -> Host`顺序。任何Host code持`_lock`时都不能进入publication mutex，从而没有ABBA edge。

Step 1同时冻结current tool/planning owner已经给出的absolute deadline并贯穿2–9；package observation、Skill snapshot、Hook publication和MCP install不得各自重新获得完整timeout。Existing physical MCP terminate/kill/drain可以在logical deadline后按native no-detach close contract完成，但不能把logical result改回success。

如果step 3在owner deadline前settle为semantic whole `UNAVAILABLE`，按§6.3构造无partial facts的closed unavailable component values并继续step 5–7；如果step 3被outer deadline/cancel/stale owner打断，则不构造view、不发布任何component。Step 6 Host/Hook combined short publication没有partial arm：revalidation失败时二者都保持predecessor；step 6 FULL后step 7 MCP owner仍可能保留其predecessor并返回`plugin_reload_partial`。不回滚已经FULL的其他owner，不建立compensation/repair queue；再次reload从各owner current exact values收敛。

Cancellation必须shield/join已经开始的publication/physical MCP close，不能留下detached worker或half-owned package lock。

### 10.3 Compaction

Compaction只能复用current Round 5B/9/9.2 order：

- PreCompact使用predecessor Hook view；
- Plugin state refresh发生在approved successor preparation safe point；
- Plugin Skill与MCP加入同一个cold successor capability cut；
- PostCompact与ROOT `SessionStart(compact)`只在adoption FULL后运行；
- new Plugin Hook slice只能通过single future-view publication lane生效；
- no-Hook/Hook cold siblings、single final continuity CAS/install/open规则不变；
- 不增加second CAS、installed-base augmentation、runner back-reference或service locator。

若Plugin refresh unavailable，compaction本身仍可按existing fail-open/typed source rules继续；不得永久占住compaction lane/fence。

### 10.4 Old consumers

Replace/disable/remove后的old consumers：

- already-dispatched Hook attempt完成；
- existing MCP process/slot按native retirement close/terminate/kill/drain；
- prepared provider attempt按exact installed view完成；
- GC等到exclusive lock成功。

Old Hook definitions与old native MCP configs分别通过§9.8/§8.6 generic `PhysicalLifetimeAnchor` leaf持有独立duplicated shared-lock descriptor，而不是依赖current Plugin view继续存在。Host close严格先停止新admission，再drain Hook attempts与MCP requests/processes，清除owner references并close current anchors，最后package GC才可能成功；任何仍在使用的old leaf都会令GC保守返回`IN_USE`。

这不是instant cross-process revocation。CLI/doctor必须诚实显示running Hosts需要reload/restart和package可能`IN_USE`。

---

## 11. Owners与forbidden dependencies

### 11.1 Owner matrix

| Owner | 拥有 | 明确不拥有 |
|---|---|---|
| `PluginPackageParser` | Agent Plugins manifest/schema/component envelope pure validation | filesystem copy、state、Runtime |
| `PluginPackageStore` | source observation、hidden stage、exclusive publish、instance state、data root、GC | Skill winner、MCP slot、Hook trust |
| `PluginInspectionService` | one call-local installed/effective package truth | CLI formatting、cross-call cache |
| `PluginRuntimeCompositionOwner` | current Host package view、held package bindings、closed component observations | provider prefix、cross-owner rollback |
| generic `ProcessApiKeyBoundary` | supported key rotation与install/process/HTTP/provider irreversible sink admission的one mutex guard | arbitrary secret store、durability、noncooperating raw environment writers |
| `PluginSkillDefinitionProducer` | portable Skill candidates/invalid issues from exact package values | parser semantics、precedence、materialization |
| existing `SkillCatalogResolver` | seven-tier winner/conflict/shadow/final bounds | filesystem、package lifecycle |
| `PluginMcpAdapter` | portable config→native cwd/header/lifetime-bearing `McpServerConfig` normalization | connection、tool permission/effect/attempt |
| existing MCP supervisor | config epoch、slot、discovery、provider-name collision groups、DIRECT/META、physical close | Plugin state/parser |
| `PluginHookContributionAdapter` | fixed config location、Plugin source identity、declaration environment、generic lifetime anchor | trust、matcher、executor、context |
| existing `HookTrustStore` / `KernelHookDispatcher` | trust、single future-view publication lane、dispatch、execution/context | Plugin lifecycle |
| existing compiler/continuity | source placement、wire/CAS/prefix | package install/GC |

### 11.2 Forbidden dependency directions

```text
capability/*                -X-> plugins/*
hooks/*                     -X-> plugins/*
conversation_kernel/mcp/*   -X-> plugins/*
Round 10 coordinator        -X-> Plugin filesystem/manifest parser
Plugin package core         -X-> provider/repository/compaction
Plugin adapters             -X-> canonical conversation writes
CLI                         -X-> private scanner/copy implementation
```

Host composition layer是唯一接线点。Adapters输出existing owners消费的typed values，不把owner藏在service locator。

### 11.3 Fingerprint subtraction

本轮允许继续/新增的摘要只有真实边界：

- existing Skill raw-document/semantic/fact fingerprints；
- existing Skill fact builder内由typed origin即时计算的nested origin framing；Plugin arm直接覆盖scope/workspace key/plugin id/package install id/relative Skill directory，并只进入现有`winning_root_provenance_fingerprint`canonical payload slot；
- existing MCP config/discovery/fact fingerprints；
- existing Hook normalized-definition trust digest；
- existing capability source contract fingerprint。

禁止：

- package tree/content digest；
- per-file hash inventory；
- enabled view fingerprint字段；
- contribution plan fingerprint；
- root-policy digest；
- stored origin/provenance fingerprint field或caller-supplied provenance digest；
- activation evidence SHA；
- fingerprint→object map；
- recursive script/dependency hashing。

Copy verification直接stream compare；package install id使用random opaque identity。

Current `_skill_origin_provenance_fingerprint(origin)`是唯一capability-fact builder内部的pure framing helper，不是DTO、registry或package identity，必须保留并增加closed Plugin arm。Loose/Bundled输入的既有framing与fact identitybyte保持不变；不得为了字面删除“fingerprint”函数名而改写已经成立的canonical fact boundary。

---

## 12. Implementation slices

### R9.3-0：Contracts、schema与package core

- closed scopes/dispositions/diagnostics；
- vendored official Agent Plugins 1.0 schemas；
- pure manifest/component parser；
- shared source binding；
- immutable package store/state/data/locks；
- streaming copy/verification、exclusive publish、cancel/cleanup/crash；
- inspection service与CLI lifecycle。

### R9.3-A：Enabled view 与third Skill producer

- complete USER/exact WORKSPACE state observation；
- Host current Plugin composition owner；
- `PluginSkillOrigin`与`FrozenPluginSkillDefinitions`；
- explicit third composer input；
- seven-tier conflict algebra；
- source contract hard cut；
- Runtime/list/doctor/compaction retained tests；
- delete every materialization/provenance/reconcile artifact from spec/tests if any remains。

### R9.3-B：MCP adapter

- portable MCP schema parser；
- independent generic literal `public_headers` field与native merge；
- native cwd closed union、Plugin root/data expansion与frozen PATH；
- one merged config inventory；
- injective server identity、provider collision groups、bound/failure semantics；
- old slot/process package-lock lifetime。

### R9.3-C：Hook adapter与single publication lane

- Plugin Hook identity/trust subject closed arm；
- fixed extension config discovery；
- reuse `hooks/config_parser.py`；
- generic Hook CLI source selection；
- `reload_hooks`/`reload_capabilities` exact predecessor merge；
- predecessor-view lifecycle for reload tool；
- no Plugin provenance in stdin/context；
- old attempts drain。

### R9.3-D：Activation

- full retained/PostgreSQL/static/packaging gates；
- local service+CLI dogfood；
- real provider Plugin Skill/MCP/Hook/compaction trajectory；
- active spec synchronization；
- only after all DoD update status/date/evidence。

所有slices最终必须形成single production path；不得以feature flag、old/new package profile、dormant adapter或compatibility mode分批激活。

---

## 13. Production modification map

实施时按actual code truth至少核对：

```text
src/pulsara_agent/plugins/
  __init__.py
  contracts.py
  schemas/1.0.0/plugin.schema.json
  schemas/1.0.0/mcp.schema.json
  package_parser.py
  package_store.py
  inspection.py
  composition.py
  skill_producer.py
  mcp_adapter.py
  hook_adapter.py

src/pulsara_agent/local_source_binding.py  # extracted one physical policy
src/pulsara_agent/process_api_key_boundary.py  # one injected process sink/rotation guard

src/pulsara_agent/capability/
  types.py
  resolver.py
  contracts.py
  __init__.py
  local_skill_source_binding.py            # delete; no compatibility wrapper

src/pulsara_agent/conversation_kernel/
  host.py
  capability.py
  capability_composition.py
  context_sources.py
  provider_dispatch.py
  tool_runtime.py
  cold_epoch.py
  subagent.py
  mcp/* only where native config/header/lifetime seam truly belongs

src/pulsara_agent/hooks/
  contracts.py
  source.py
  config_parser.py
  trust.py
  dispatcher.py

src/pulsara_agent/model_input/contracts.py
src/pulsara_agent/mcp_config.py
src/pulsara_agent/capability/builtin_catalog.py
src/pulsara_agent/cli.py
pyproject.toml / uv.lock                    # direct jsonschema dependency
tests/
tools/Plugin dogfood script and machine trace path
```

Module split是上限提示，不是创建decorative DTO/module的命令。实现若能用更少narrow modules且保持owners清晰，应删除空壳。

Active spec synchronization至少覆盖unified Skill、Round 9.2 Hook、Round 6/9 MCP、Round 5B compaction truth。不要修改`contracts/`、archived docs或README。

---

## 14. Test plan

### 14.1 Package parser与physical source

- official Agent Plugins 1.0 manifest/MCP fixtures；
- offline embedded schema，runtime network call=0；
- one Draft 2020-12 validator path，`jsonschema` direct dependency而非transitive accident；
- missing/unsupported/invalid schema；
- unknown manifest field和non-object extensions的standard failure boundary；
- type-correct但semantically nonsensical `version`/homepage/repository/author URL/email/license仍被接受并仅display；`author` unknown field与field type错误仍fatal；
- object `extensions`中的unimplemented namespace value不深入validate；
- portable fixed locations only；
- fixed path MISSING/regular valid/wrong-kind INVALID/physical race UNAVAILABLE four-way matrix，不把deterministic kind defect升级成availability；
- root vendor manifests/alternate paths不被猜测；
- `/tmp`、`/var` source happy path；final source symlink拒绝；
- internal symlink/junction/special file拒绝；
- root/membership/file identity replacement typed raced；
- source validation/copy/final revalidation/stage verification同一observation；
- streaming large resource constant memory；
- wide directory tree只允许membership metadata按observed member count增长；RSS probe证明不随largest regular-file bytes增长且没有hidden member cap；
- injected `MemoryError`不逃逸、不留stage；
- current API key跨chunk detection且diagnostic不含value；
- API-key在stage scan期间rotation会以new exact value重验，未重验不得publish；
- one underlying gate的sync worker与async event-loop ports互斥；async等待不blocking loop，cancelled acquisition被shield/join且无detached waiter，sink/rotation交错无deadlock；
- no total package/resource/version cap。

### 14.2 Atomic lifecycle

- fresh no-follow root creation；
- Darwin `RENAME_EXCL`、Linux `RENAME_NOREPLACE`；unsupported platform typed unavailable；
- existing final root永不覆盖；
- add/replace均disabled、enable/disable/remove exact state；
- enable summary/confirmation exact-binds current install；concurrent replace不能让stale acceptance授权new package；
- data root no-follow create before enable cut；later unavailable只隔离process-bearing MCP/Hook components；
- per-instance concurrent CAS/lock races；
- state enumeration replace/delete whole unavailable；
- cancellation before/after package publish/state cut；
- publisher从stage create跨publish/state confirmation持有package lock，concurrent GC不能删除pre-state new root；
- shield+join，无detached worker；
- cleanup unavailable carrier exact；
- SIGKILL/ACK-unknown/power-loss语义golden；
- unreferenced version与hidden stage explicit gc；
- hidden stage name可parse回exact package install id，publisher与GC竞争同一package lock；无法parse的lookalike永不删除；
- exact state-temp只在同一instance lock下回收；enumeration忽略它，malformed dot entry不删除；
- GC在部分root cuts后cancel/timeout/unavailable时报告completed roots、attempted location与unvisited suffix，不谎报零mutation；
- shared/exclusive package lock阻止in-use deletion；
- same-UID namespace interference不被错误宣称为guarantee。

### 14.3 Inspection、CLI与packaging

- list/doctor consume same exact inspection object；
- list current/effective only，doctor all issues；
- call-local immutable，无cross-call cache/generation；
- USER不依赖cwd；WORKSPACE explicit/default cwd；
- GUI adapter direct typed call，不spawn CLI/parse JSON；
- arbitrary non-source cwd global launcher；
- wheel包含official schemas与any required Plugin resources；
- `--json` exhaustive dispositions/codes；
- no raw exception-string classification。

### 14.4 Skill third producer

- Plugin/loose/bundled identical Skill bytes走same parser/placement；
- portable immediate child only，no recursive Skill discovery；
- no four-root materialization、sidecar、reconcile、orphan cleanup；
- seven exact tiers；
- workspace Plugin > user Plugin > bundled；
- high loose winner shadows Plugin；
- same-tier two-Plugin conflict skips exact tier and allows lower fallback；
- conflict without fallback omits name but preserves complete issues；
- invalid Plugin candidate allows other candidate/lower winner；
- every valid candidate exactly winner/shadowed/conflicting；
- required Plugin producer unavailable makes whole catalog unavailable；
- Plugin observation deadline/cancel before install走outer abort、无fake unavailable source/CAS；
- zero Plugins COMPLETE empty；
- source contract v4 only，no dual path；
- existing loose/bundled fact identities retained；
- ordinary `read_file` reads Plugin SKILL.md/supporting resource；
- activation/retained/compaction use same existing path；
- complete issues不受旧128 cap截断。

### 14.5 MCP

- stdio/streamable-http standard fixtures；
- SSE typed unsupported/no fallback；
- per-server invalid skip；top-level invalid disables only package MCP；
- one frozen MCP component observation完整覆盖`MISSING | COMPLETE(declared keys, valid configs, issues) | INVALID(no partial) | UNAVAILABLE(no partial)`，selection/inspection/reload按object identity复用而不重新parse；
- broken WORKSPACE MCP override suppresses only the frozen same-plugin identity/component boundary，不意外fallback执行USER counterpart；
- length-framed server id对含`.`/`:`等合法local id组合保持injective；128-byte bound在framing后应用，无truncate/hash；
- exact namespaced server IDs与local/plugin collisions；collision members all-skip、其他configs保留；
- same-server与cross-server provider-name normalize/truncate collision形成complete typed groups，全部ambiguous tools同时从DIRECT/META省略，其他tools/resources/servers保留且无raw exception；
- root/data expansion覆盖每个exact occurrence且single/non-recursive；command/env keys不expand，unknown placeholder remains literal；
- reserved env keys exact invalid；frozen configured PATH参与bare command lookup与runtime/resolved identity；
- bare command vs contained `./` command；
- native cwd closed union证明local config仍workspace-relative、Plugin omitted/root/data cwd均是exact absolute且SDK不二次拼workspace；DATA cwd writable/no-follow failure只隔离exact server；
- public literal headers是independent generic field而非auth arm；RFC name/value rejection、case-insensitive duplicate与case-insensitive overwrite precedence全覆盖；runtime/resolved identity随literal header变化而semantic identity保持；
- native identity matrix golden：cwd/PATH/public headers与managed package source只改变runtime/resolved，不改变semantic；same HTTP config的new package install id仍触发runtime reconnect、old slot drain并释放old anchor；
- exact `localhost`与`ipaddress.is_loopback` IPv4/IPv6允许HTTP；private/link-local、localhost suffix与non-loopback HTTP拒绝；无DNS猜测；
- auth ref/acquisition/server authentication failure是connection failure而非config INVALID；no OAuth/no redirect forwarding；
- local/Host exact server-id collision保留local config并只skip冲突Plugin server；
- final API-key spawn sink rotation probe；
- combined 64-server bound retains old config on new overbound；
- same server config reuses process；changed server retires/drains old lock；
- late READY META、cold/compaction DIRECT；
- meta resolved tool only one Hook lifecycle set；
- existing permission/effect/bypass/scope gates unchanged。

### 14.6 Hook adapter

- fixed extension path only；
- all 11 Hook events through one config parser；
- unsupported handler narrow skip；
- local USER/WORKSPACE + Plugin deterministic source order；
- workspace Plugin same id covers user Plugin Hook；
- missing WORKSPACE Hook component permits USER fallback；present invalid/untrusted WORKSPACE Hook suppresses USER counterpart；
- enable without trust cannot execute；
- generic trust store四臂path matrix无USER/workspace/Plugin subject collision；
- trust digest changes on install id/definition/environment；
- existing USER/WORKSPACE file Hook normalized digests byte-identical after union hard cut；
- Hook command byte-identical through inspect/trust/executor；root/data只作为reviewed declaration environment，不做adapter string expansion；
- generic Hook stdin/context contains no Plugin provenance；
- `reload_hooks` does not scan package；`reload_capabilities` does not reread local files；
- concurrent reload在scan期间互不占publication mutex，并按`publication -> Host`固定锁序完成；stress probe无ABBA、无lost local/Plugin slice update；
- two concurrent `reload_hooks`从同一V0 scan、先后发布时，later writer必须因complete predecessor identity变化而rescan，不能把先发布的new local slice覆回stale值；
- reload tool Pre/Permission/Post uses invoke-time lifecycle predecessor，commit merge使用lock内current predecessor；两者不同的并发轨迹被冻结；
- old sync/background attempt与one-shot context completes with old generic definition/root anchor；current view replace后GC仍`IN_USE`，settle后才可exclusive；
- API key通过supported boundary在Hook spawn final cut前rotation时旧scan不能穿透；guard持有跨spawn admission；
- engine/parser/transport fail-open；explicit control only；
- Round 9.2 all lifecycle/Plan/compaction/subagent/Stop retained tests pass。

### 14.7 Prefix、architecture与oracle

- same epoch SYSTEM/tools byte-identical；
- messages suffix-only；
- reload/install/trust/GC no rebase；
- only cold/compaction rebuild；
- no second Skill catalog/MCP supervisor/Hook engine/subagent runtime；
- no Plugin import from capability/hooks/MCP owners；
- no generic registry/service locator/event bus；
- no receipt/watcher/generation/replay/repair/durable job；
- no total Plugin/package/resource/history/lifetime cap；
- no package/contribution/stored provenance fingerprints；
- existing Skill fact builder call-local typed-origin framing保留，no stored/caller-supplied provenance digest；
- PostgreSQL/oracle exact `29 / 24 / 11 / 1 / 25 / 0 / 11`；
- new skip/xfail = 0。

### 14.8 Final verification与dogfood

Activation前统一执行并记录：

- retained full pytest；
- PostgreSQL full suite、clean-v0 migration/deep verification；
- Ruff；
- compileall；
- protocol generator check；
- `uv lock --check`；
- `git diff --check`；
- non-editable sdist→wheel build；
- isolated tool install与arbitrary non-source cwd launcher smoke；
- pure local service+CLI dogfood；
- real provider Plugin Skill/MCP/Hook trajectory；
- new skip/xfail count。

Pure local dogfood至少覆盖：

```text
validate -> add(disabled) -> inspect -> enable -> inspect
-> replace -> disable -> remove -> gc
```

Real provider dogfood至少覆盖一个single package：

1. Plugin Skill进入seven-tier catalog并由ordinary `read_file`读取；
2. local stdio Plugin MCP经META或DIRECT实际调用；
3. exact trusted Plugin `UserPromptSubmit`/`PreToolUse` Hook运行并产生context/control；
4. `reload_capabilities`不改same-epoch SYSTEM/tools；
5. compaction successor重新冻结current Plugin Skill/MCP并运行Pre/PostCompact、SessionStart(compact)；
6. replace自动disabled；对new exact install再次enable后Hook trust仍为MODIFIED，old MCP/Hook consumer drain；
7. disable/remove后future contributions消失，old DIRECT descriptor typed unavailable；
8. 保留actual prompt、provider-visible messages/tools、Hook stdin/stdout/stderr、MCP/ToolResults与model reply；只排除exact non-empty `PULSARA_API_KEY`。

---

## 15. Definition of Done

只有全部满足，本文才能标记`ACTIVATED`：

1. Agent Plugins 1.0 root manifest是唯一package identity；one embedded Draft 2020-12 validator path且runtime schema lookup不联网；`jsonschema`是direct dependency。
2. Portable core只从fixed `skills/`与`mcp.json`读取；Pulsara extension只从fixed `dev.pulsara/`读取。
3. Vendor manifest/profile猜测与multi-manifest priority完全不存在。
4. Six typed management operations成为CLI/GUI共同boundary；list/doctor共享one inspection。
5. Source validate/copy/revalidate/stage verify来自one held observation；regular-file bytes streaming constant-memory，membership evidence诚实O(member count)。
6. Fresh roots no-follow，Darwin/Linux exclusive publish，existing root永不覆盖。
7. Publisher package lock覆盖stage create→state cut/confirmation；concurrent GC不能删除pre-state new root。
8. Cancellation shield+join，MemoryError/IO进入typed cleanup；file bytes streaming constant-memory、membership metadata诚实O(member count)且无hidden cap；ordinary failure无stage leak；partial GC mutation有exact progress truth。
9. SIGKILL、ACK-unknown与power-loss弱保证诚实，无fsync/receipt/recovery。
10. Store/state/data/locks是only new durable local state；PostgreSQL无增长。
11. Add/replace均initial disabled；Enable exact-binds reviewed current install与external-process acceptance；它不替代remote tool permission或Hook trust。
12. COMPLETE enabled view不能mixed；三类component observation由parse/selection/inspection/reload共享；wrong-kind是INVALID、physical race才是UNAVAILABLE；UNAVAILABLE不发布partial contribution；inspection outer abort不是第三semantic disposition。
13. Plugin Skill是explicit third producer，使用same parser/placement/resolver/catalog/activation/read/retained path。
14. No Plugin Skill materialization、sidecar、reconciler、orphan cleanup或tree digest。
15. Seven-tier precedence与same-tier conflict fallback全函数实现。
16. Stable Skill capability source kind/id保留；v4 source contract single path；no dual compatibility。
17. Plugin MCP只生成generic native `McpServerConfig`并进入one supervisor；server id length-framed injective，native cwd closed union承载exact package/data path，literal public headers是独立generic field；model surface/runtime/resolved identity matrix exact，package replace即使HTTP config同值也runtime-reconnect并释放old anchor。
18. Plugin不能grant MCP required/permission/effect/scope authority；existing 64-server bound是only aggregate bound。
19. Plugin Hook只增加closed source identity/trust subject arm；parser/trust/dispatcher/executor/context owner仍唯一。
20. Hook command在inspect/trust/execute间byte-identical；root/data只通过trust-covered declaration environment提供。
21. Generic Hook stdin/`HOOK_CONTEXT`没有Plugin provenance；11 events/11 public variants/12 causal arms不变。
22. `reload_hooks`与`reload_capabilities`scan/build不占publication/Host locks，固定`Hook publication -> Host`锁序；`reload_hooks`提交校验complete captured predecessor，任一slice变化即重做，`reload_capabilities`以lock内current opposite slice merge；无ABBA、stale overwrite、lost update、generation或registry。
23. Reload tool lifecycle使用invoke-time predecessor、commit使用lock内current predecessor；old Hook/MCP leaves各自通过generic lifetime anchor持package lock直到physical drain。
24. Same-epoch SYSTEM/tools exact、messages suffix-only；无第三rebase boundary。
25. Long-horizon availability无new total cap；all scans/copy/list use streaming/pagination/deadline。
26. One injected process-local `ProcessApiKeyBoundary`以one underlying gate和nonblocking-event-loop sync/async ports串行化supported rotation与install publish、Hook/MCP spawn/HTTP、provider final sink admission；guard持有跨irreversible cut，cancel acquisition shield+join、无deadlock/detached waiter，rotation probe不能穿透，diagnostics/dogfood不泄漏exact value。
27. No generic mutable registry/service locator/event bus、watcher、receipt、history、replay、repair或durable job。
28. Fingerprint只保留§11.3真实边界；无package tree/view/contribution/stored provenance hash。
29. Architecture oracle保持`29 / 24 / 11 / 1 / 25 / 0 / 11`。
30. Active specs、tests、CLI help与dogfood只描述新单一路径；不修改`contracts/`、archived docs或README。
31. Full retained/PostgreSQL/static/wheel/launcher/local+real-provider dogfood全绿，new skip/xfail为0。

---

## 16. 最终冻结

Round 9.3激活后的产品语义应当是：

> Pulsara从本地目录安装一个Agent Plugins 1.0 package为immutable managed version，以USER或exact WORKSPACE instance显式enable。Portable Plugin Skills不复制进four loose roots，而作为第三typed definition producer，与用户/工作空间loose Skills和Pulsara package bundled Skills共同进入唯一central catalog；优先级为four loose roots、WORKSPACE Plugin、USER Plugin、bundled。Portable MCP definitions只归一化为existing native MCP configs；Pulsara extension command Hooks只进入existing Round 9.2 dispatcher/trust。Plugin package本身不成为capability、executor、permission owner、canonical authority或durable recovery system，也不提供agent-definition或subagent runtime。Running Host refresh不改写installed prefix，只有existing cold open与adopted compaction successor能重建SYSTEM/tools。

本文已经按§17 evidence闭合全部DoD并标记`ACTIVATED`。

---

## 17. Activation evidence（2026-08-26）

Machine-readable gate/results authority：

- [`round9_3_agent_plugin_product_activation.json`](benchmarks/suites/core/v1/round9_3_agent_plugin_product_activation.json)
- [`round9_3_agent_plugin_product_trace.json`](benchmarks/suites/core/v1/round9_3_agent_plugin_product_trace.json)
- [`round9_3_plugin_installer_discovery_trace.json`](benchmarks/suites/core/v1/round9_3_plugin_installer_discovery_trace.json)

Final working-tree gates：

```text
.venv/bin/python -m pytest -q
  -> 1011 passed in 180.90s

.venv/bin/python -m pytest -q -m postgres
  -> 223 passed, 788 deselected in 127.10s

.venv/bin/python -m pytest -q tests/test_stage5_clean_migration.py
  -> 12 passed in 3.40s

Ruff / compileall / protocol generator / uv lock --check / git diff --check
  -> all exit 0
```

Clean-v0 dogfood创建并删除exact loopback ephemeral PostgreSQL database；first migration applied `(0)`, repeat migrate applied `()`, head remained `0`, deep verification carrier为`PostgresFastVerificationBundle`。PostgreSQL relation/column/migration、Committed/Live event、subject、append guard、product relation与durable job均无增长；oracle保持`29 / 24 / 11 / 1 / 25 / 0 / 11`，new skip/xfail为0。

Non-editable sdist→wheel在`/tmp/pulsara-r9-3-p1-final.CLhwty`成功；isolated `uv tool install`安装51个packages与one global `pulsara` executable。任意non-source cwd下`pulsara --version`为`0.1.0`，八个Plugin subcommands可见，installed-wheel embedded schemas成功把vault `positive/elements-of-style`验证为`VALID`，empty list为`COMPLETE`。

Pure local service与CLI都完成`validate -> add(disabled) -> inspect -> enable -> inspect -> replace(disabled) -> disable -> remove -> gc`。Real-provider trace使用production `openai_chat_completions`与`deepseek-v4-flash-vision-exp`，结果`passed`：13次foreground provider requests、3次compaction summary requests、14条Hook command logs与one real stdio Plugin MCP call。Plugin Skill由ordinary `read_file`读取；trusted `UserPromptSubmit`/`PreToolUse`产生context/control；PreCompact/PostCompact/SessionStart(compact)运行；same-epoch reload保持SYSTEM/tools exact且messages suffix-only；compaction successor重新冻结Plugin Skill/MCP；replace后的new Hook为`MODIFIED`且单独trust前不可运行；old package在view持有期间GC为`IN_USE`，disable/remove后future contributions消失且old direct descriptor返回`TOOL_UNAVAILABLE`。

5,321,370-byte trace保留actual prompts、provider-visible SYSTEM/tools/messages、provider stream、Hook stdin/stdout/stderr、MCP logs、ToolResults、canonical transcript与model replies。`pulsara_api_key_recorded=false`，Hook/MCP child environment也均未包含exact key。Provider与network真实可达；没有external availability blocker或remaining runtime defect。

合入前P1 closure同一working-tree另以deterministic probes与retained tests证明：package shared anchor使用caller absolute deadline/cancel的nonblocking lock acquisition；paired source read/fstat失败保持source physical owner；`reload_capabilities`同一deadline贯穿settlement、Hook/Host publication与native MCP cut；HTTP/provider每个physical request保持one `ProcessApiKeyBoundary` gate直到request body FULL/not-FULL，随后在response headers/body前释放。对应Round 9.3 focused gate为`33 passed in 1.47s`，没有新增task-owner site、durability、retry cap或fingerprint。

Supplemental bundled installer discovery matrix同样使用production `openai_chat_completions`与`deepseek-v4-flash-vision-exp`，以五个隔离HOME/PULSARA_HOME覆盖vault全部positive packages：`elements-of-style@1.0.0`、`jumpcloud-admin@1.1.0`、`fdeops@3.10.3`、`spar@0.5.0`、`one@1.0.3`。每条human user message只自然请求安装本地Plugin与处理不兼容格式，没有出现installer Skill name；模型从provider-visible `SKILL_CATALOG`自行选择exact bundled guidance，以ordinary `read_file`完整读取其`SKILL.md`，且只有在该ToolResult进入later provider call后才生成terminal CLI命令。五条轨迹均通过installed global `pulsara`确认八个commands、strict validate=`VALID`、`add --scope user`=`INSTALLED`、list/doctor检查；independent management inspection均确认exact version、USER scope与`enabled=false`。模型均说明只有exact behavior-preserving candidate且same validator重新返回`VALID`时才可继续，否则honest stop且不安装partial Plugin。

该matrix总计46次provider requests、44,549个provider stream items、5次exact guidance reads、34次terminal calls与67条canonical tool rows。20,163,692-byte trace保留natural user prompts、provider-visible input、Skill read ToolResults、CLI commands/stdout/stderr、managed-state inspections、canonical transcript与model replies；`pulsara_api_key_recorded=false`。隔离non-source sdist→wheel→`uv tool install` launcher为`0.1.0`；ephemeral exact-loopback PostgreSQL仍为clean-v0 first `(0)`、repeat `()`、head `0`与`PostgresFastVerificationBundle` deep verification。
