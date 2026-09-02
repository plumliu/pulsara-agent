# Pulsara Bundled + Loose + Plugin Skill 统一目录 Hard Cut 实施规格

> 状态：**ACTIVATED**
>
> 首次激活日期：2026-08-25；Round 9.3同步日期：2026-08-26
>
> 机器轨迹：[unified_skill_definition_producers_trace.json](benchmarks/suites/core/v1/unified_skill_definition_producers_trace.json)、[round9_3_agent_plugin_product_trace.json](benchmarks/suites/core/v1/round9_3_agent_plugin_product_trace.json)
>
> 本文冻结当前production的三个显式Skill definition来源：Pulsara package内的只读bundled Skills、四个既有roots中的用户/工作空间loose Skills，以及Round 9.3 enabled local Agent Plugin package中的portable Skills。Round 9.3只新增第三producer与closed origin/outcome/precedence arms，继续复用本文已经激活的parser、resolver、effective catalog、ordinary `read_file`与retained/compaction路径。
>
> 当前production code是实施时第一代码真源。本文是bundled distribution到direct definitions hard cut的实施authority；如Round 9.1、local Skill completion、compaction或其他active specs仍描述bundled copy/sync、local-only catalog truth或current-manifest retained rejoin，必须在同一implementation diff同步删除旧断言。不得保留dual path、compatibility flag、legacy alias或后台reconciliation。
>
> 强制上位约束：`AGENTS.md`与`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`。同一installed epoch内SYSTEM/provider tools必须byte-identical，messages只能追加suffix；Skill refresh不得创造第三种rebase boundary。完整typed origin已经送达消费者时不得再保存DTO provenance fingerprint。

本文把本轮最终产品模型冻结为：

> **Bundled、Loose和Plugin三个显式definition producer提交所有候选；一个两阶段production parser/placement validator、一个seven-tier central precedence resolver和一个effective catalog产生唯一Skill truth。Bundled definitions只存在于installed Pulsara package，loose definitions继续由用户直接管理，Plugin definitions只来自Round 9.3完整enabled-package view。**

---

## 0. 最终产品语义

### 0.1 本轮激活形状

```text
Bundled Skill definitions
  installed Pulsara package内、只读、随Pulsara版本更新
                                                    ┐
Loose Skill definitions                             │
  workspace/.pulsara/skills                         │
  workspace/.agents/skills                          ├─> document parser
  ${PULSARA_HOME}/skills                            │   -> placement validator
  ~/.agents/skills                                  │   -> central precedence resolver
                                                    │   -> one effective Skill catalog
Plugin Skill definitions                            │   -> Runtime / CLI / GUI
  exact WORKSPACE enabled packages                  │
  USER enabled packages                             ┘
```

三个来源中的winner进入完全相同的：

- catalog projection；
- explicit activation与`ACTIVE_SKILL`；
- ordinary `read_file`；
- capability fact/registry sibling view；
- compaction retained-skill proof；
- provider-visible source placement。

“同一种Skill”只指进入catalog以后的消费语义相同，不表示三个来源拥有相同write authority或precedence。Bundled是Pulsara-package-owned read-only default；loose是用户-owned editable override；Plugin由Round 9.3 package lifecycle owner安装、显式enable并冻结immutable view。Skill正文无论来源都只是untrusted guidance，不获得Tool、permission、Hook、MCP、model或canonical authority。

### 0.2 当前与hard cut后

| 维度 | 当前production | 本hard cut后 |
|---|---|---|
| bundled存放 | package原版复制到`${PULSARA_HOME}/skills` | 只从installed package直接读取 |
| Runtime发现 | 复制品被当作user loose Skill | bundled producer直接提交只读definitions |
| loose发现 | four-root scanner内部先选winner | four-root producer提交所有候选，central resolver唯一选winner |
| 用户定制 | 修改受管copy，形成modified/deleted状态 | 创建或安装同名loose override |
| 删除override | 受manifest/opt-out/reset状态影响 | 下一safe point自然回退到next lower unique Plugin或bundled definition |
| Pulsara升级 | sync比较package、manifest与target | 没有同名loose override时，新process读取新package definitions |
| bundled CLI | `sync-bundled/status/reset` | 三项全部删除 |
| bundled local state | manifest、provenance、backup、opt-out | 全部删除且不再读取 |
| inspection | local-only catalog | bundled+loose+Plugin effective catalog |

现有用户目录中的旧bundled copies不会自动消失。它们从hard cut后只是ordinary high-precedence loose Skills，因此可能继续遮挡新版package definition；§9.3冻结诚实的升级与手工删除语义。

### 0.3 Happy path

```text
Pulsara process starts
  -> pin exact filesystem-backed bundled resource root
  -> prepare exact four-root loose policy

At one existing Skill safe point
  -> observe bundled batch exactly once
  -> observe loose batch exactly once
  -> consume one complete Plugin batch exactly once
  -> submit all valid candidates and invalid issues
  -> resolve seven tiers exactly once
  -> publish one complete effective catalog observation

User installs same-name loose Skill
  -> next complete safe point selects loose winner

User deletes loose override
  -> next complete safe point selects next lower unique Plugin or bundled fallback
```

不存在“安装bundled Skill”“同步built-in copy”“reset built-in copy”或“bundled installation receipt”的产品概念。

### 0.4 Activated Round 9.3 relationship

Round 9.3已经通过§12的single-path extension把真实enabled Plugin package Skill definitions接入本文架构。Plugin package owner冻结complete enabled view；第三producer只消费该immutable observation。该hard cut不得：

- 物化Plugin Skills到four roots；
- 建立Plugin-private parser、catalog、activation engine或read tool；
- 让Skill subsystem扫描Plugin cache/state猜安装与enable状态；
- 复用loose installer承担Plugin install/update/remove；
- 把CLI JSON当作Plugin或GUI IPC；
- 建立registry、generation、receipt或fingerprint map。

Plugin Skill view、same-tier conflict、lifetime drain及真实provider/compaction dogfood由Round 9.3 §15与activation evidence闭合。

### 0.5 本轮明确不实现

- remote marketplace、Git/npm acquisition、archive extraction、dependency installation、lifecycle scripts、auto-update或credential acquisition；
- 第五个loose root；
- Skill watcher、installation receipt/history/replay/repair/generation；
- provider-visibleSkill management tool；
- generic mutable event bus、producer registry或service locator；
- overwrite/update/remove/rollback loose Skill operations；
- recursive supporting-resource enumeration或hash；
- 任意总Skill/resource/history/turn/model-call/lifetime cap。

---

## 1. 强制架构不变量

### 1.1 两阶段唯一parser path

Production validation严格拆成两项pure operations：

```text
parse_skill_document(exact_bytes)
  -> ParsedSkillDocument | DocumentDiagnostics

validate_skill_candidate_placement(
  parsed_document,
  expected_immediate_child_name,
)
  -> PlacementValid | PlacementDiagnostics
```

第一阶段只理解portable Agent Skills document contract，不接收root、scope、path、directory basename或source kind。同一exact bytes在standalone validation、source copy、staged verification、loose scan和bundled scan中必须产生相同parsed fields与document diagnostics。

第二阶段唯一拥有`DIRECTORY_NAME_MISMATCH`，机械比较parsed `name`与owner冻结的logical final basename：

- standalone validation：normalized source directory basename；
- install initial observation与source final revalidation：同一次source binding冻结的source directory basename；
- source placement通过后，parsed `name`才成为frozen final destination basename；因为source placement已经证明parsed `name == source basename`，错误命名的source directory绝不能通过install被“纠正”；
- hidden staging与staged/final destination validation：使用frozen final destination basename，不使用random staging basename；
- loose discovery：held root descriptor下的immediate child name；
- bundled discovery：closed official immediate child name。

不得保留接受`expected_directory_name`的旧parser wrapper、第二套name regex/YAML loader、fallback parser或source-specific bounds。

### 1.2 三个显式immutable inputs

Composition owner只接受三个显式、call-local、immutable batch values：

```text
FrozenLooseSkillDefinitions
FrozenPluginSkillDefinitions
FrozenBundledSkillDefinitions
```

不得改为`list[SkillProvider]`、`register_producer()`、entry-point discovery、global singleton、generation-indexed map或digest-to-object registry。第三来源已经通过显式constructor与closed union hard cut接入，不存在old two-input或generic compatibility path。

### 1.3 Four roots只属于loose producer

Four-root policy继续固定为：

```text
1. <workspace>/.pulsara/skills
2. <workspace>/.agents/skills
3. ${PULSARA_HOME}/skills
4. ~/.agents/skills
```

Bundled package root与Plugin package root都不是第五个local root。Loose producer不读取package resources；bundled producer不调用`PULSARA_HOME` resolver、不读取workspace roots并且不写任何user path；Plugin producer不扫描four roots或instance state，只消费Round 9.3 complete view。

四个roots在production中始终全部启用；删除`include_user_skills`、`excluded_root_kinds`及任何production root-subset request。Tests需要隔离单root时使用不进入production contract的narrow fixture。

Prepared policy必须在任何candidate read前证明四个physical roots两两不同：

1. 先比较owner规范化后的absolute lexical path；任意两项exact相等即alias；
2. 再按既有no-follow component规则打开每个已经存在的root；任意两项最终`dev/inode` exact相等也即alias；
3. 尚不存在的root只参与lexical比较；本次observation中创建、出现或rebind仍按ordinary membership/root race处理；
4. alias不按precedence扫描两次、不选择其中一个、不抛裸`ValueError`，而使whole loose batch成为`LOOSE_CONFIGURATION_INVALID`，并携带exact `LOOSE_ROOT_ALIAS` causal diagnostic与可解释的prepared policy；
5. diagnostic机械携带冲突的两个closed root kinds及各自normalized absolute path；已打开时可携带其相同`dev/inode` observation，但这些metadata不进入fingerprint或registry。

该规则覆盖workspace等于OS home、`${PULSARA_HOME}`导致root重合、lexically不同但指向同一directory的symlink/inode alias。它不增加root disable、自动去重、fallback precedence或重试。

### 1.4 One precedence owner

Physical producers只拥有完整观察，不拥有winner selection。它们输出：

- 所有valid normalized candidates；
- 所有standard-invalid candidate issues；
- batch-level COMPLETE或UNAVAILABLE。

只有central `SkillCatalogResolver`拥有seven-tier precedence、winner、shadowed与Plugin same-tier group conflict issue。`LocalSkillProvider`旧`winners_by_name`/`winner_ordinal`路径保持删除，不得保留producer-local winner或两级authority。

### 1.5 Prefix continuity

同一exact ROOT/child scope、同一installed epoch：

```text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
```

Loose edit/delete、bundled observation变化、catalog shadowing或discovery unavailable都不得热改SYSTEM/tools。Skill catalog与active observations继续使用Round 9.1既有stateful source safe-point successor；ordinary cold open和已批准compaction successor仍是唯一provider root rebuild boundary。

### 1.6 Durability与availability

本轮不新增PostgreSQL relation/column/migration、committed/live event、subject、append guard、product relation或durable job。不新增total attempts、history、Skills、resources或process lifetime cap。Process crash后丢失call-local inspection可以在下一safe point重新观察，不增加receipt/recovery。

---

## 2. Typed product operations 与 CLI

底层management operations保持且仅有：

```text
validate_local_skill_source
install_loose_local_skill
inspect_effective_skill_catalog
```

前两项只属于loose local Skill；第三项hard-cut取代`inspect_local_skill_catalog`，成为Runtime、CLI、Desktop GUI与future local Host API的唯一effective truth。

### 2.1 Validate local Skill source

Closed result：

```text
VALID(parsed document, document/placement INFO diagnostics)
INVALID(non-empty document or placement diagnostics)
UNAVAILABLE(closed source observation reason)
```

它只验证一个独立local source。它复用§1.1两阶段path，不读取bundled batch，不依赖`PULSARA_HOME`或workspace。

### 2.2 Install loose local Skill

继续使用当前no-overwrite、hidden staging、exclusive publish、same-observation copy verification与cancel/join contract，只写：

```text
workspace -> <workspace>/.pulsara/skills/<name>
user      -> ${PULSARA_HOME}/skills/<name>
```

它不写package root、不生成provenance、不承担bundled install/reset。Workspace install不因invalid user-home配置失败；user install使用唯一absolute-only `PULSARA_HOME` resolver。

Install的同一次descriptor-relative frozen source observation依次执行：source basename placement -> copy -> source final revalidation。只有source placement成功后，parsed name才冻结为destination basename；hidden stage、staged verification与final destination validation都使用该destination basename。Source basename错误必须与standalone validate一样返回INVALID，不能因destination取parsed name而绕过。

### 2.3 Inspect effective Skill catalog

一次调用冻结一个call-local immutable bundled+loose+Plugin truth。`list`与`doctor`只投影同一个typed inspection：

- `list`只展示effective winners及derived source；
- `doctor`展示winner origin、所有invalid、所有shadowed和aggregate unavailable；
- 两次CLI invocation是两次独立observation，不承诺同一时刻；
- GUI直接消费typed result，不执行CLI subprocess、不解析stdout/JSON。

### 2.4 Final `pulsara skills` namespace

Hard cut后只保留：

```text
pulsara skills validate <path> [--json]
pulsara skills install --scope workspace|user [--workspace PATH] <path> [--json]
pulsara skills list [--workspace PATH] [--json]
pulsara skills doctor [--workspace PATH] [--json]
```

四项命令都保留当前`--json` terminal projection。JSON只序列化同一个typed operation result，不是management service contract、GUI IPC或future Web wire protocol。

完整删除：

```text
pulsara skills sync-bundled
pulsara skills status
pulsara skills reset
```

旧名称成为ordinary usage error；不保留hidden parser、deprecated alias、compatibility flag或环境变量逃生路径。

---

## 3. Source-neutral contracts

### 3.1 Parsed document

```text
ParsedSkillDocument
  name
  description
  license | NONE
  compatibility | NONE
  metadata
  body
  raw_document
  raw_document_digest
  manifest_semantic_fingerprint
  authoring diagnostics
```

`ParsedLocalSkillDocument`必须hard-cut为source-neutral名称。`raw_document_digest`继续证明exact document bytes；`manifest_semantic_fingerprint`继续服务现有Skill semantic/provider boundary。两者不得扩张为directory inventory、installation receipt或producer batch identity。

Optional `license`、`compatibility`、metadata和host-extension inert semantics对三个producer及management validator完全一致。

### 3.2 Closed origin union

Current closed union只有三个arms：

```text
SkillDefinitionOrigin =
  LooseSkillOrigin(root_kind)
    root_kind = WORKSPACE_PULSARA
              | WORKSPACE_AGENTS
              | USER_PULSARA
              | USER_AGENTS

  | PluginSkillOrigin(
      visibility_scope,
      plugin_id,
      package_install_id,
      package_relative_skill_directory,
      workspace_state_key?,
    )

  | BundledSkillOrigin(package_relative_skill_directory)
```

`LocalSkillRootKind`继续只有four loose values，不添加`BUNDLED`伪root。Bundled origin不在每个candidate重复保存constant distribution id/version；当前process的single bundled producer已经绑定exact installed Pulsara distribution。

`PluginSkillOrigin`只接受`USER | WORKSPACE` visibility，WORKSPACE时必须携带exact workspace state key，并携带valid plugin id、`pkg_<32 hex>` install id及POSIX `skills/<name>`relative directory。Held descriptors与generic physical lifetime anchor属于package/view owner，不进入manifest。Origin不带fingerprint；第三arm已经是一次真实hard cut，不存在optional Plugin fields或old/new union。

### 3.3 Normalized candidate

```text
SkillDefinitionCandidate
  parsed_document
  origin
  exact SKILL.md path
  exact base directory
  model-visible location
  document/placement INFO diagnostics
```

不得增加`source-local precedence facts`、open metadata bag、stored coarse source或stored human-readable label。Resolver从closed origin机械派生tier；renderers从origin即时派生source/label。

Current `LocalSkillManifest` hard-cut为source-neutral `SkillManifest`或等价唯一名称；不得保留optional `root_kind=None` DTO、local manifest wrapper或parallel bundled manifest。

### 3.4 Candidate outcomes

当前three-producer valid candidate恰好落入一种状态：

```text
EffectiveSkillWinner(candidate)

ShadowedSkillCandidateIssue
  candidate name/origin/path
  exact winner origin/path
  candidate INFO diagnostic codes

ConflictingSkillCandidateIssue
  exact Plugin tier与Skill name
  at least two ordered(path, PluginSkillOrigin) candidates
  PLUGIN_SAME_TIER_NAME_CONFLICT diagnostic
```

Invalid physical candidate形成：

```text
InvalidSkillCandidateIssue
  physical directory/SKILL.md path
  origin
  declared name | NONE
  non-empty document or placement diagnostics
```

`SkillCandidateIssue = InvalidSkillCandidateIssue | ShadowedSkillCandidateIssue | ConflictingSkillCandidateIssue`。

不保存`winner_ordinal`。Shadowed issue携带同一frozen inspection中winner的exact typed origin/path；constructor必须验证name join且该winner真实存在。同一Plugin tier的同名group conflict没有winner并继续查找lower-tier unique fallback；members只为deterministic projection排序，不产生隐式precedence。每个valid candidate必须在winners、shadowed或conflicting issues中出现且仅出现一次；每个invalid candidate只出现一次。

### 3.5 Closed effective inspection

```text
EffectiveSkillCatalogInspection =
  CompleteEffectiveSkillCatalogInspection
    disposition = COMPLETE
    prepared loose root policy
    winners
    candidate issues
    no unavailable causes

  | UnavailableEffectiveSkillCatalogInspection
    disposition = UNAVAILABLE
    prepared loose root policy
    non-empty SkillCatalogUnavailableCause tuple
    no winners
    no candidate issues
```

```text
ProducerKind = LOOSE | PLUGIN | BUNDLED

SkillProducerUnavailableReason =
  LOOSE_CONFIGURATION_INVALID
  | LOOSE_DISCOVERY_RACED
  | LOOSE_DISCOVERY_OVERBOUND
  | PLUGIN_VIEW_UNAVAILABLE
  | PLUGIN_RESOURCE_UNAVAILABLE
  | PLUGIN_DISCOVERY_RACED
  | BUNDLED_RESOURCE_UNAVAILABLE
  | BUNDLED_INVENTORY_MISMATCH
  | BUNDLED_DEFINITION_INVALID
  | BUNDLED_DISCOVERY_RACED

ProducerUnavailableCause
  producer_kind
  SkillProducerUnavailableReason compatible with producer_kind
  non-empty causal diagnostics

SkillResolutionUnavailableReason =
  EFFECTIVE_WINNER_BOUND_EXCEEDED
  | CATALOG_PROJECTION_OVERBOUND

ResolutionUnavailableCause
  SkillResolutionUnavailableReason
  one causal diagnostic

SkillCatalogUnavailableCause =
  ProducerUnavailableCause
  | ResolutionUnavailableCause
```

Producer causes按`LOOSE`、`PLUGIN`、`BUNDLED`固定排序；每个producer最多一个cause。Diagnostics在cause内按code、path、message稳定排序。即使任一producer先产生non-deadline failure，其余required inputs仍在同一absolute owner deadline内exactly once观察/消费；但owner deadline/cancellation不是producer cause，也不构造inspection，严格按§4.4与§6.2的outer abort path整体中止。

Constructor invariant固定为：producer causes可以有一至三项；resolution cause必须单独存在且只有在三个producer均COMPLETE后才合法。不得把observed candidates/issues塞入UNAVAILABLE carrier，也不得同时声称producer unavailable与resolution overbound。

`COMPLETE`不得截断issues。Standard-invalid与shadowed不使aggregate UNAVAILABLE；只有无法证明required producer的完整物理观察、official bundled inventory或final projection才产生UNAVAILABLE。

### 3.6 Derived public projection

User-facing source不是stored truth，而是origin property：

```text
WORKSPACE <- WORKSPACE_PULSARA | WORKSPACE_AGENTS
USER      <- USER_PULSARA | USER_AGENTS
PLUGIN    <- PluginSkillOrigin
BUNDLED   <- BundledSkillOrigin
```

`SkillSource` closed enum为`WORKSPACE | USER | PLUGIN | BUNDLED`，但`SkillManifest`、`ResolvedSkillCatalogEntry`与`ActiveSkillInjection`都携带exact origin并以property派生source；constructor不接受可与origin冲突的stored source。

CLI origin labels只能机械渲染为以下closed constants：

```text
workspace:.pulsara/skills
workspace:.agents/skills
user:${PULSARA_HOME}/skills
user:~/.agents/skills
workspace-plugin:<plugin-id>@<package-install-id>
user-plugin:<plugin-id>@<package-install-id>
bundled:pulsara-agent
```

Label不进入manifest、capability fact或provider-visible catalog。Provider catalog继续只渲染name/description/location。

---

## 4. Physical producer contracts

### 4.1 Narrow descriptor observer

Bundled与Loose physical producers复用一个narrow descriptor-relative Skill directory observer；Plugin package observer在其held immutable root中复用同一document parser与placement validator。观察层只负责：

- held no-follow root/component binding；
- immediate child enumeration；
- exact regular `SKILL.md` identity/bytes读取；
- §1.1两阶段parse/placement；
- root、candidate、document和membership final revalidation；
- standard diagnostics映射。

它不选择winner、读取Plugin package manager state、管理lifecycle或接受caller自报provenance。Plugin visibility与identity来自Round 9.3 complete view的closed values，不从path反推。Supporting resources继续progressive disclosure，不在catalog scan中递归枚举、预读或hash。

### 4.2 Loose definition producer

`LocalSkillProvider` hard-cut为`LooseSkillDefinitionProducer`或等价唯一owner。它继续唯一拥有scope-neutral four-root policy与complete physical observation，但删除内部winner selection。

语义保持：

- direct copy/edit/delete是一等路径；
- dot-prefixedhidden staging不进入membership；
- ordinary child缺少`SKILL.md`时被忽略；
- standard-invalid形成exact issue；
- root/candidate/document/membership race使whole loose batch UNAVAILABLE；
- invalid `PULSARA_HOME`只影响需要user roots的effective inspection/user install，不影响standalone validation/workspace install；
- 每次effective inspection/Host composition只调用一次typed `resolve_user_home()`，并将同一call-local immutable observation同时交给absolute-only `PULSARA_HOME` resolver、`USER_AGENTS` root与ordinary read-path projection；OS home lookup的`MemoryError`、`OSError`或`RuntimeError`结算为`LOOSE_CONFIGURATION_INVALID + USER_HOME_CONFIGURATION_INVALID`，不得逃出typed inspection或在同一次composition中重新观察；
- all valid candidates按root precedence、directory name、path稳定排序后提交central resolver。

Prepared four-root policy还必须执行§1.3的lexical/inode alias证明。Alias是configuration defect：whole loose batch以`LOOSE_CONFIGURATION_INVALID` + `LOOSE_ROOT_ALIAS`返回，不能让一个physical candidate带两个origins，也不能从较高tier静默吞掉较低tier binding。

Loose batch不读取bundled root，不把同名candidate预先删除或转成winner ordinal。

### 4.3 Bundled definition producer

Bundled producer只从当前installed Pulsara distribution的：

```text
pulsara_agent/bundled_skills/
```

读取definitions。Production construction使用`importlib.resources.files("pulsara_agent").joinpath("bundled_skills")`，但结果必须是real filesystem-backed absolute `Path`。Darwin wheel/tool installation常位于OS-owned顶层`/var` alias，bundled binding与validate/install复用同一窄的`/tmp | /var | /etc -> /private/...` path-preparation seam后再逐组件no-follow；任意其他parent、bundled child与document symlink仍严格禁止。需要`as_file()`临时提取的Traversable、zip resource、temporary/cache copy或cwd-derived fallback一律typed UNAVAILABLE。

Official inventory在代码中closed为：

```text
EXPECTED_BUNDLED_SKILL_NAMES = (
  "pulsara-mcp-installer",
  "pulsara-plugin-installer",
  "pulsara-skill-creator",
  "pulsara-skill-installer",
)
```

它是产品inventory，不是hash、registry或可扩展配置。新增/删除official bundled Skill必须修订tuple、spec和tests。

Build gate与Runtime必须复用同一个pure inventory classification function及同一个closed expected set，不能各自过滤：

1. 枚举`bundled_skills/`下**所有non-dot immediate entries**；dot-prefixed entries在两处都忽略；
2. observed names必须与`EXPECTED_BUNDLED_SKILL_NAMES` exact set equality，且expected tuple自身unique；
3. 每个observed entry必须是no-follow directory，并且恰有一个名为`SKILL.md`的no-follow regular file；
4. 额外non-dot README、regular file、socket或不含`SKILL.md`的directory都属于extra/invalid inventory，build失败、Runtime typed UNAVAILABLE；
5. 两处不得使用“只筛出含SKILL.md的directory”这种较窄集合。

Build missing/extra/duplicate是packaging failure；Runtime对应`BUNDLED_INVENTORY_MISMATCH`，且不发布partial built-ins。

`BundledSkillDistributionBindingOwner`是显式process-local physical owner。Current production `KernelHostCore`是app/kernel composition root：它在任何`KernelHostSession`前恰好构造一个binding owner；它：

1. 获取filesystem-backed absolute path；
2. no-follow打开root descriptor；
3. 冻结initial dev/inode与lexical path binding；
4. 显式注入同一process内的所有Host sessions、Desktop/GUI inspection service与bundled producer；
5. 持有descriptor，直到`KernelHostCore.shutdown()`确认所有Host sessions、management calls与filesystem workers均已quiesce/join后，执行binding owner自身idempotent `aclose()`。

`KernelHostCore`还必须用同一个narrow lifecycle gate线性化session open与shutdown：open attempt在任何binding borrow前登记process-local settlement；shutdown原子关闭admission并join当时全部attempt。已注册winner进入ordinary session close；shutdown先胜出的未注册attempt必须关闭其session/IO、不得注册或返回。只有open attempts与registered sessions都settle后才可关闭binding。该gate不是binding lease、manual refcount、registry或durable state。

Construction本身是closed：owner内部恰好冻结`BOUND(held descriptor, lexical path, dev/inode)`或`UNAVAILABLE(BUNDLED_RESOURCE_UNAVAILABLE causal diagnostics)`。后者仍允许process/Host启动，但该process的每次bundled observation都机械返回同一construction cause；它不在future safe point重开path或adopt新distribution。`aclose()`对BOUND关闭descriptor，对UNAVAILABLE为idempotent no-op。该binding不是capability fact、registry、generation或durable state。

Supported app process只构造一个`KernelHostCore` composition root；Host session只借用binding，不单独构造、替换或关闭它，因此同一process的新Host不能在package namespace变化后adopt另一root。Tests若直接构造narrow owner必须在fixture teardown关闭。不得用module global singleton、service locator、manual refcount、lease、generation或registry实现共享。Standalone CLI的`list/doctor` invocation构造call-local binding owner，并在typed operation及所有workers settle后于`finally`幂等关闭；`validate/install`不需要也不构造bundled binding。

每次observation从held descriptor读取exact official children，并在返回前重新打开absolute root验证仍为initial dev/inode。Absolute component path、empty/`.`/`..` segment、symlinked child、missing/extra official child、invalid official document、identity rebound或read race均使whole bundled batch UNAVAILABLE；不发布partial built-ins，也不回退到旧user copy。

Installed environment在running process期间不可被同UID原地替换是package distribution外部前提。若namespace被替换，future observation必须UNAVAILABLE而不是adopt新bytes；ordinary absolute `read_file`对这种out-of-contract interference不承诺旧path liveness。正常package upgrade由new process读取new distribution。

Bundled candidate `location/path/base_dir`是installed package中的real absolute path，supporting resources通过existing ordinary `read_file`读取。不得发明`bundled://` URI、virtual filesystem、`read_skill` tool或materialized copy。

### 4.3.1 Plugin definition producer

`PluginSkillDefinitionProducer`由Round 9.3 adapter拥有，只消费one `FrozenEnabledPluginView`形成的complete `FrozenPluginSkillDefinitions`。每个enabled package的portable root `skills/`在同一次complete package observation中按immediate child观察；exact regular `SKILL.md`复用§1.1 parser/placement，standard-invalid形成ordinary invalid issue，physical I/O/race或whole view unavailable形成closed Plugin producer cause且不发布partial contribution。

Producer不读取durable instance state、不选择scope winner、不扫描managed package store猜enablement、不复制definitions到four roots，也不拥有package close。View owner持有generic physical lifetime anchors；future view变化只影响后续catalog，old attempt/resource consumer按Round 9.3 lifetime规则drain。

### 4.4 Producer availability

```text
FrozenLooseSkillDefinitions = COMPLETE(candidates, invalid issues)
                           | UNAVAILABLE(cause)

FrozenPluginSkillDefinitions = COMPLETE(candidates, invalid issues)
                            | UNAVAILABLE(cause)

FrozenBundledSkillDefinitions = COMPLETE(exact official candidates)
                             | UNAVAILABLE(cause)
```

Bundled official documents必须全部valid；因此其standard-invalid parser/placement diagnostics属于UNAVAILABLE cause而不是ordinary complete invalid issue。Loose与Plugin standard-invalid属于COMPLETE candidate issue。

在owner deadline尚未胜出时，engine、parser adapter、transport、`MemoryError`或`OSError`必须按closed typed mapping settle为non-deadline producer cause。不得按exception string决定disposition，不得catch `BaseException`吞掉cancellation，不得刷新deadline或无限重试。

Deadline/cancellation有唯一外层owner：

- 每个同步检查和joined filesystem worker都继承同一个absolute owner deadline；不创建Skill subdeadline或settlement headroom；
- deadline到期或caller cancellation一旦在effective source snapshot成功安装前被观察到，胜过尚未安装的configuration/resource/race结果，整个safe-point以`ABORTED_BY_OWNER_DEADLINE`或existing cancellation path中止；
- 此时没有`EffectiveSkillCatalogInspection`、没有Skill source observation、没有successor CAS/install，也没有`*_DEADLINE_EXPIRED` producer/resolution reason；
- 若non-deadline failure已经在deadline前构造并沿existing continuity transaction成功安装，则之后的owner deadline只影响后续work，不能追溯改写已安装observation；
- worker必须shield并join，但shield不是越过deadline安装snapshot的许可。Physical join/drain完成后仍返回外层deadline/cancel outcome。

---

## 5. Central precedence 与 physical bounds

### 5.1 Exact seven-tier precedence

从高到低：

```text
1. workspace/.pulsara/skills
2. workspace/.agents/skills
3. ${PULSARA_HOME}/skills
4. ~/.agents/skills
5. exact WORKSPACE Plugin Skills
6. USER Plugin Skills
7. Pulsara bundled package definitions
```

Invalid candidate不占precedence slot。最高non-conflicting valid candidate成为winner；所有lower valid siblings各形成一个shadowed issue。若一个Plugin tier有多个同名valid candidates，该tier形成group conflict并继续寻找lower-tier unique candidate，不能按plugin id、install time或path选winner。删除任意loose winner后，下一complete safe point自然选择下一loose、Plugin或bundled fallback。

### 5.2 Pure resolver contract

`SkillCatalogResolver`只消费三个COMPLETE batches。它：

- 从closed origin派生tier；
- 按name分组；
- 为每个name选择highest unique valid winner；
- 对Plugin same-tier non-unique group形成one typed conflict issue并继续lower tier；
- 为每个lower valid sibling产生一个typed shadowed issue；
- 合并producer invalid issues；
- 按name、origin tier、path、issue kind稳定排序；
- 在最终winner/projection bounds后返回COMPLETE或closed UNAVAILABLE。

Resolver不访问filesystem、不解析document、不重新读取PULSARA_HOME、不保存cache或第二份winner registry。

### 5.3 Duplicate与official defects

Four roots中同name valid candidates是ordinary precedence/shadowing，不使用`DUPLICATE_NAME`选first winner。一个immediate child只能提供一个`SKILL.md`，因此不存在同directory多个valid candidate。

同一Plugin tier的同名valid candidates不是ordinary shadowing；它们作为一个deterministic conflict group整体失去winner资格，lower tier可成为fallback。该排序只服务projection，不授予plugin id/path precedence。

Bundled expected-name tuple自身必须unique；package inventory duplicate/missing/extra以及official document declared name与directory mismatch均是bundled batch defect。Runtime不得按enumeration order选择official winner。

### 5.4 Existing bounds的唯一owner

本轮不新增或复制bounds。现有边界hard-cut到以下owner：

| Bound | Final owner与计算阶段 |
|---|---|
| 64 KiB document及frontmatter/YAML/field bounds | §1.1 parser，per document，三个producer与management共用 |
| `MAX_SKILL_DIRECT_CHILDREN = 1024` | loose producer，per each of four roots |
| `MAX_DISCOVERY_SKILL_BYTES = 16 MiB` | loose producer，一次four-root observation读取的全部candidate `SKILL.md` bytes |
| exact four official bundled names | bundled product inventory，不是通用resource cap |
| `MAX_SKILL_LOCATION_BYTES = 1024` | candidate placement/enrichment，三个producer共用 |
| `MAX_ADMITTED_SKILLS = 64` | central resolver，最终effective winners总数，不是per producer |
| `MAX_SKILL_CATALOG_UTF8_BYTES = 384 KiB` | sole catalog renderer，最终effective catalog |
| `MAX_ACTIVE_SKILLS = 16` | sole active selector，最终selected winners |
| `MAX_ACTIVE_SKILL_BODY_UTF8_BYTES = 512 KiB` | sole active renderer |
| retained 8 items / 40,000 tokens | existing Round 5B retained context product boundary，保持不变 |

Complete inspection issues不设额外cap；它必须保留每个observed invalid/shadowed/conflicting candidate。Plugin package admission/member bounds由Round 9.3 package owner唯一拥有，不复制成Skill catalog cap。Supporting resource count/bytes不在discovery中扫描，因此不增加bundle/resource总cap。

---

## 6. Runtime safe point 与 source settlement

### 6.1 One composition cut

`KernelSkillProjectionComposer`或其hard-cut successor是唯一Host接线点。一次safe-point cut：

```text
freeze one absolute owner deadline
-> use process-pinned bundled root binding
-> prepare exact loose root policy
-> borrow one complete enabled Plugin package view
-> observe bundled exactly once
-> observe loose exactly once
-> derive Plugin definitions exactly once
-> if all three COMPLETE, central resolve exactly once
-> render/preflight catalog and freeze the winner map
-> issue one effective Skill capability source snapshot
-> publish through existing stateful-source successor path
```

观察顺序固定为bundled、loose、Plugin input，不提供parallel/sequential实现选择，以免deadline边缘产生不同cause truth。Plugin package view本身由Round 9.3 publication lane在调用前冻结为完整immutable observation。若任一physical read委托filesystem worker，worker必须shield并join；cancel不能留下detached worker。同步读取也必须反复检查同一个absolute deadline。不得为后续producer、render或CAS刷新deadline。

Composition在每个producer返回后、resolve/render后以及existing continuity install linearization前检查同一个absolute owner deadline/cancellation。Deadline不得刷新，也不得因已知non-deadline cause而跳过最后检查。Composition不会发布producer-local snapshot。Registry、compiler、CLI和GUI只看到最终effective inspection/source snapshot。

### 6.2 Exact UNAVAILABLE behavior

任一required producer、central winner bound或catalog projection因**non-deadline**原因UNAVAILABLE，并且该snapshot在absolute owner deadline前进入existing continuity install时：

- effective source snapshot为UNAVAILABLE且不含facts；
- Runtime必须沿现有stateful source path追加typed `UNAVAILABLE_MINIMAL` Skill catalog observation；
- 不得把predecessor catalog继续宣称为current effective truth；
- 不得发布另一producer的partial winners；
- 不得fallback到旧bundled user copy；
- 同一planning cut不重试新filesystem view。

“必须追加”只适用于上述已形成且实际进入install的typed UNAVAILABLE snapshot。若deadline/cancellation在安装前胜出，则按§4.4整体abort：不伪造deadline cause、不追加observation、不进行successor CAS/install。若install已经FULL，则deadline不能撤销或重写已安装snapshot；ACK/return由existing continuity transaction语义处理，本轮不增加补偿、第二次CAS或receipt。

Winner map只存在于`CompleteEffectiveSkillCatalogInspection`。任一effective inspection为UNAVAILABLE时——包括`CATALOG_PROJECTION_OVERBOUND`——不得在carrier、composer、registry或active path保留hidden winner map/facts；active固定返回`ACTIVE_SELECTION_UNAVAILABLE`。只有catalog inspection COMPLETE时，prompt-specific active selector才消费该inspection的exact frozen winner map，并可能独立返回`ACTIVE_PROJECTION_OVERBOUND`。Active outcome不反向改写call-local complete inspection；catalog cause与active outcome仍是两个closed causal owners。

`UNAVAILABLE_MINIMAL` domain identity只使用§3.5已排序的closed cause kind/reason tuple；不包含exception message、path timing或另造fingerprint。Public diagnostics仍从exact typed causes投影。

Semantic current truth与physical old attempt必须区分：已经安装的旧provider attempt可以按现有continuity owner完成，但它不使新safe point的catalog重新变成COMPLETE。Bundled root由current process持有，loose supporting-resource后续read按ordinary filesystem outcome完成；Plugin old attempt/resource consumer通过Round 9.3 generic physical lifetime anchor保持旧immutable package root，直到drain后才允许GC。

### 6.3 Catalog与active projection

`SkillCatalogCapabilityProvider`取代local-only provider naming，消费exact `EffectiveSkillCatalogInspection`。Catalog row为：

```text
name
description
location
exact origin
```

Coarse source和CLI label均为derived property，不stored。Provider-visible catalog renderer仍只输出name/description/location，保持一个routing representation。

Explicit `$skill-name`、`skill:name`与Host-command activation从同一个winner map取得body。Bundled、Loose与Plugin使用同一个`ACTIVE_SKILL` role、placement、bounds和diagnostics。

### 6.4 Ordinary resource reads

两种winner都提供real absolute `path/base_dir`与现有model-visible `location`：

- loose继续使用既有stable location prefix；
- bundled使用installed package absolute path。

Existing ordinary `read_file`是唯一supporting-resource tool。本轮不新增virtual URI、resource handle、artifact copy、read proxy或provider-visible Skill tool。

Catalog完成后resource消失、permission变化或user edit发生时，ordinary tool返回现有typed failure/current bytes；它不触发implicit catalog fallback、materialization或another-source lookup。Skill body已经进入installed `ACTIVE_SKILL`时，same-turn follow-up继续使用existing installed head，不从filesystem偷换。

### 6.5 Compaction retained Skill hard cut

`retained_skill.py`必须接受source-neutral manifest与effective source observations，并删除“inherited retained item必须重新join current manifest”的旧路径。

新FULL read proof使用provider历史，而不是current filesystem/catalog，并冻结为以下唯一causal join：

1. 从canonical ToolResult decision定位exact successful `read_file(offset=1)` result及其tool-call identity；要求它以FULL mode进入predecessor installed messages；
2. 由该identity定位发出调用的唯一assistant tool-request canonical item；再通过existing installed message placements（canonical origin entry identity与provider-input item identity）定位包含该request的唯一installed provider message及其message ordinal；缺失或多于一项都拒绝retention；
3. 只考虑message ordinal**严格小于**assistant request ordinal的installed `SKILL_CATALOG` source observations；选择其中ordinal最高者，不能取ToolResult前latest、predecessor current head或compaction-time catalog；
4. 所选observation必须是canonical installed `FULL/VALUE`，且其payload可由sole catalog decoder完整解码；`CLEARED`、`UNAVAILABLE_MINIMAL`、duplicate catalog row、重复name/location或不可解码payload都拒绝retention；
5. request `path`必须exact等于该historical catalog唯一matching row的model-visible `location`；该row必须以`.../<logical-name>/SKILL.md`结束，placement expected basename机械取historical row location中`SKILL.md`的parent basename；
6. ToolResult的public `path`必须exact等于ordinary `read_file`对该historical location的确定性public-path projection：workspace-relative location保持relative；`${PULSARA_HOME}`用Host为该epoch冻结的shared Pulsara-home binding展开；`~/.agents`用同一Host冻结的OS-home binding展开；bundled absolute location保持absolute。该projection只使用historical row与Host-frozen workspace/home bindings，不访问current filesystem；
7. 验证result `status=ok`、line ordinals连续、`offset=1`、`truncated=false`且覆盖`total_lines`；request/result identity、path与canonical settlement必须属于同一tool call；
8. 从actual FULL `N|line` records机械去除line-number framing，以LF连接为`delivered_document_text`；`had_utf8_bom`只证明tool已剥离BOM，不重新注入；
9. 对`delivered_document_text.encode("utf-8")`调用§1.1 document parser，并用步骤5的historical parent basename调用placement validator；
10. 要求parsed name/description与该historical catalog row exact join；retained body定义为该provider-delivered normalized document解析出的body。

该proof不声称恢复filesystem raw bytes、原始line-ending或raw document digest；它精确证明并保留模型实际收到的FULL logical text。不得用compaction时的current effective winner替换、批准或否决历史FULL read。Loose override已删除、bundled/loose winner已经变化时，历史read body仍可以被retained；supporting resource未来是否仍可读由ordinary tool决定。

Canonical ToolResult、installed messages/placements与historical catalog observation是唯一proof inputs；不得增加per-read receipt、manifest history、process-local lookup map或durable evidence row。

Inherited retained items只从predecessor installed `RETAINED_SKILL_CONTEXT` observation解码，并验证existing canonical shape、installed observation fingerprint与retained bounds；不重新join current manifest。Existing duplicate-tail subtraction、8-item/40,000-token选择和evidence source fingerprint保持不变。

Compaction prebound `ACTIVE_SKILL`继续从installed predecessor恢复exact body，不访问filesystem。Cold/compaction successor可以同时包含current effective catalog与historical retained/active body；两者是不同的合法source truths，不用generation、receipt或name lookup连接。

### 6.6 Same-epoch proof

Tests必须覆盖至少：

- bundled default -> loose override -> delete loose -> bundled fallback；
- loose/bundled UNAVAILABLE successor；
- catalog变化后的same-turn ToolResult follow-up；
- compaction successor同时保留historical Skill body并展示current catalog；
- Chat Completions与Responses paths。

每组trace中SYSTEM/tools exact byte equality、messages prefix equality和suffix-only append必须机械断言。

---

## 7. Inspection、diagnostics 与 projections

### 7.1 Closed diagnostic additions

Current `SkillDiagnosticCode`继续是唯一closed vocabulary。本轮只允许增加以下具有独立因果的codes：

```text
LOOSE_ROOT_ALIAS                  = "skill_loose_root_alias"
BUNDLED_DEFINITIONS_UNAVAILABLE   = "skill_bundled_definitions_unavailable"
BUNDLED_INVENTORY_MISMATCH        = "skill_bundled_inventory_mismatch"
```

Bundled/Loose hard cut删除`DISCOVERY_DEADLINE_EXPIRED`与`DUPLICATE_NAME`；Round 9.3再加入`PLUGIN_DEFINITIONS_UNAVAILABLE`与`PLUGIN_SAME_TIER_NAME_CONFLICT`。前者由§4.4 outer deadline abort取代，duplicate winner由typed shadowed/group-conflict algebra取代；不得保留unreachable enum member或CLI/context mapping。Current vocabulary exact为35 members，architecture guard断言`35 <= 64`。

Owner固定为：

- loose root-policy preparer唯一产生`LOOSE_ROOT_ALIAS`，并只映射到`LOOSE_CONFIGURATION_INVALID`；
- bundled adapter唯一产生上述两个aggregate causal codes；
- §1.1 parser/placement validator唯一产生document/placement codes；
- shared descriptor observer唯一产生root/candidate/document physical/race codes，producer adapter只加入typed source cause；
- central resolver唯一产生typed shadowed issues和final winner-bound outcome；
- catalog renderer唯一产生catalog projection overbound；
- active selector/renderer继续拥有active-not-found与active projection overbound。

Official bundled document invalid时，unavailable cause包含exact parser/placement diagnostics，并可附一个`BUNDLED_DEFINITIONS_UNAVAILABLE` aggregate diagnostic；不得把同一parser error再复制成generic invalid issue。Missing/extra official inventory使用`BUNDLED_INVENTORY_MISMATCH`。

§3.5的source-neutral unavailable enums取代当前`SkillCatalogUnavailableReason`同时混合physical discovery、catalog与active outcomes的路径。Prompt-specific active path使用独立closed `ActiveSkillProjectionUnavailableReason = ACTIVE_SELECTION_UNAVAILABLE | ACTIVE_PROJECTION_OVERBOUND`。Public context adapter仍机械映射到existing source observation disposition。所有enum-to-CLI/JSON/context diagnostic mappings必须exhaustive，不使用unknown/default或exception-string classification。

### 7.2 Diagnostic ownership subtraction

- INVALID candidate只有producer observer owner；
- SHADOWED candidate只有central resolver owner；
- winner authoring INFO只有winner carrier owner；
- shadowed candidate INFO只附在其typed shadowed issue；
- aggregate UNAVAILABLE只包含causal producer/projection diagnostics；
- `DISCOVERY_DEADLINE_EXPIRED`与`DUPLICATE_NAME` diagnostic codes及其generic projections删除；deadline属于outer abort，shadowing只有typed issue truth；
- `CATALOG_PROJECTION_BOUND_EXCEEDED`与active `PROJECTION_OVERBOUND`保持不同causal codes。

Inspection/doctor与Runtime context使用两个不同的、单向pure projections，不能共享“逐issue输出”实现：

- `EffectiveSkillCatalogInspection`与doctor保留每个path-specific invalid/shadowed issue及其完整diagnostics，不截断、不去重不同candidate；
- Runtime Skill context adapter只取得本次inspection中出现过的public `SkillDiagnosticCode`集合；每个code至多投影一项，使用closed code table中固定的severity与canonical public message，不携带candidate path、origin/source label或exception text；
- Runtime projection按`SkillDiagnosticCode.value`稳定排序，是diagnostic semantic-set view，不是inspection truth或第二scanner；
- architecture guard必须机械证明当前全部可投影Skill diagnostic codes数量不超过existing compiler `64` diagnostics per-operation boundary；未来新增code若会越界，必须修订真实consumer，而不能截断inspection、丢code或增加Skill总candidate cap。

因此65个或更多同code invalid candidates仍产生COMPLETE inspection与完整doctor结果，但只产生一项对应Runtime semantic diagnostic。不得让compiler bound把catalog改成UNAVAILABLE。

### 7.3 List projection

`pulsara skills list`只显示effective winners，至少包含：

```text
name
description
location
source = workspace | user | bundled
origin_label = §3.6 closed label
```

这些字段从winner exact origin即时派生。List不显示shadowed/invalid candidates，不读取bundled manifest或loose install receipt。

### 7.4 Doctor projection

`pulsara skills doctor`投影同一个inspection并解释：

- effective winners及exact source label；
- every invalid loose candidate；
- every shadowed loose/bundled candidate及其winner；
- invalid `PULSARA_HOME`；
- bundled path/inventory/document unavailable；
- whole aggregate UNAVAILABLE。

Doctor不能声称识别“旧managed copy”。旧copy只会诚实显示为user loose winner、package bundled sibling显示为shadowed。用户提示可以说明删除该loose directory会在next safe point回退，但不得读取inert provenance推断ownership。

Production中四个loose roots始终启用，因此doctor没有disabled/excluded roots字段、JSON member或提示。Root alias按`LOOSE_ROOT_ALIAS`解释exact冲突bindings；tests所用root subset fixture不得出现在doctor或production DTO中。

---

## 8. Capability fact identity 与 fingerprint hard cut

### 8.1 Stable source lineage

以下existing stable identity在本轮保持exact：

```text
CapabilitySourceKind.LOCAL_SKILL_CATALOG
stable_source_id = "pulsara-local-skill-catalog"
```

虽然字符串保留`LOCAL`，它是已经有真实fact/registry/continuity消费者的stable source lineage，不是compatibility alias。不得并存一个新`SKILL_CATALOG` kind/id，也不得改写既有loose Skill `CapabilityIdentity`。Python owner/DTO名称仍应hard-cut为source-neutral；只有上述stable wire/canonical identity保留。

Source registration contract fingerprint必须hard-cut为以下exact call：

```python
context_fingerprint(
    "skill-source-contract:v4-bundled-loose-plugin-skills",
    {
        "parser_contract": "pulsara.agent-skills-core.v1",
        "placement_contract": "pulsara.skill-placement.v1",
        "producer_kinds": ("LOOSE", "PLUGIN", "BUNDLED"),
        "precedence": (
            "WORKSPACE_PULSARA",
            "WORKSPACE_AGENTS",
            "USER_PULSARA",
            "USER_AGENTS",
            "WORKSPACE_PLUGIN",
            "USER_PLUGIN",
            "BUNDLED",
        ),
        "bundled_names": (
            "pulsara-mcp-installer",
            "pulsara-plugin-installer",
            "pulsara-skill-creator",
            "pulsara-skill-installer",
        ),
    },
)
```

Production使用`AGENT_SKILLS_CONTRACT_ID`、`SKILL_PLACEMENT_CONTRACT_ID = "pulsara.skill-placement.v1"`与`EXPECTED_BUNDLED_SKILL_NAMES`提供上面的exact values，但architecture tests必须断言展开后的literal framing。Placement ID由sole placement validator模块定义，不存入candidate/inspection。`context_fingerprint`现有canonical serializer是唯一serialization owner：mapping key order不构成变化，上述tuple的成员与顺序构成变化。不得加入path、distribution version、observed inventory、diagnostics、root policy或package-view identity。

这是producer set与source semantics发生变化的真实兼容边界。不得继续声称旧`local-skill-source-contract:v2-agent-skills`，也不得双注册或协商old/new fingerprint。该变化只在new process/cold epoch或approved compaction successor构造root时生效，不允许热rebase已安装epoch。

### 8.2 Typed fact origin

`FrozenSkillCapabilityFact` hard-cut为携带exact `SkillDefinitionOrigin`，删除stored：

```text
winning_root_provenance_fingerprint
```

`BundledSkillOrigin.package_relative_skill_directory`必须是POSIX-normalized exact string：

```text
bundled_skills/<official-name>
```

它恰有两个non-empty components、无leading/trailing slash、无`.`/`..`、第一项exact `bundled_skills`，第二项exact等于candidate/official name。不得使用`pulsara_agent/bundled_skills/<name>`、仅`<name>`或platform separator。

`PluginSkillOrigin.package_relative_skill_directory`同样是POSIX-normalized exact `skills/<name>`，并与closed visibility/workspace key、plugin id及package install id共同进入fact builder；不携带descriptor、lock、lifetime anchor或digest field。

Fact builder是唯一摘要边界，并按origin arm执行以下exact framing：

```python
# Loose — existing framing remains byte-identical.
context_fingerprint(
    "local-skill-winning-root-provenance:v2-agent-skills",
    {
        "root_kind": origin.root_kind.value,
        "precedence_ordinal": precedence_ordinal,
        "stable_location_prefix": stable_location_prefix,
    },
)

# Bundled — new framing.
context_fingerprint(
    "bundled-skill-origin:v1-agent-skills",
    {
        "package": "pulsara_agent",
        "relative_skill_directory": origin.package_relative_skill_directory,
    },
)

# Plugin — Round 9.3 framing.
context_fingerprint(
    "plugin-skill-origin:v1-agent-skills",
    {
        "visibility_scope": origin.visibility_scope.value,
        "workspace_state_key": origin.workspace_state_key,
        "plugin_id": origin.plugin_id,
        "package_install_id": origin.package_install_id,
        "package_relative_skill_directory": origin.package_relative_skill_directory,
    },
)
```

Loose framing的两个derived values也exact冻结为：

| `root_kind` | `precedence_ordinal` | `stable_location_prefix` |
|---|---:|---|
| `WORKSPACE_PULSARA` | `0` | `.pulsara/skills` |
| `WORKSPACE_AGENTS` | `1` | `.agents/skills` |
| `USER_PULSARA` | `2` | `${PULSARA_HOME}/skills` |
| `USER_AGENTS` | `3` | `~/.agents/skills` |

所得ephemeral origin digest直接进入现有`skill-capability-fact:v1` payload的`winning_root_provenance_fingerprint`slot，以保持既有fact framing；该slot名只是canonical payload framing，不再是DTO field。Digest不保存回fact/manifest/inspection/registry，caller也不能传任意digest。`skill_capability_fact_fingerprint`的production API接收typed origin并在唯一builder内调用pure origin framing。

### 8.3 Retained legitimate digests

保留：

- exact `SKILL.md` raw document digest；
- manifest semantic fingerprint；
- body digest与existing fact/catalog/activation stable identity digests；
- retained selection/evidence fingerprints。

删除且不替代：

- bundled source/target tree hash；
- manifest ownership hash；
- producer batch/catalog discovery fingerprint；
- root-policy digest；
- origin DTO fingerprint；
- fingerprint registry/map；
- document/activation-evidence SHA。

---

## 9. Bundled distribution hard cut

### 9.1 Production deletion

同一implementation diff完整删除：

- `sync-bundled/status/reset` CLI parsers与handlers；
- Host、REPL和其他CLI启动时best-effort bundled sync；
- current bundled sync/status/reset DTOs与exports；
- bundled copy、atomic replace、backup、restore与manifest writer；
- `.bundled_manifest` owner；
- `.no-bundled-skills` opt-out owner；
- `.restore-backups` owner；
- bundled source-versus-target drift states；
- `installed/modified/deleted/unmanaged_collision/available_to_sync`状态机；
- `compute_skill_dir_hash()`及仅为copy ownership服务的recursive hash；
- bundled provenance read/write与source/target compare；
- corresponding tests、help、completion与active-spec assertions。

`src/pulsara_agent/capability/bundled_skills.py`必须删除或由new narrow bundled definition producer完整替换，不保留wrapper、old DTO export或hidden copy functions。

### 9.2 Provenance filename becomes ordinary data

`.pulsara-skill-source.json`失去所有reserved/control语义。实施必须同步删除：

- `BUNDLED_SKILL_PROVENANCE_FILE_NAME` production constant及consumers；
- publisher `RESERVED_CONTROL` disposition；
- disposition allowed-detail map；
- CLI exit-code/JSON mapping；
- copy rejection/filter branch；
- tests与bundled guidance中的reserved/excluded说明。

Loose source包含该文件时，validator不解释它；publisher按ordinary regular resource exact复制，staged verification覆盖其bytes。不得保留旧拒绝分支作为“安全fallback”。

### 9.3 Existing user artifacts 与 upgrade truth

Hard cut不自动删除任何真实用户filesystem内容：

- 已复制到four roots的bundled Skills成为ordinary loose Skills；
- 它们正常覆盖package bundled definitions；
- package升级只有在没有同名loose override时才改变effective winner；
- 用户删除loose directory后，next complete safe point回退到package definition；
- 旧manifest、opt-out、backup与provenance files不再被production读取、写入或赋予authority；
- 不增加legacy scanner、migration daemon、one-shot cleanup、repair queue或compatibility doctor mode。

Release/user guidance必须明确：现有user path可能遮挡新版built-in；`skills list/doctor`展示真实winner与shadowed bundled fallback；是否删除loose directory由用户决定。不得用“新process自动获得新版built-in”描述存在override的情况。

### 9.4 Bundled Skill guidance

Package内`pulsara-mcp-installer`、`pulsara-plugin-installer`、`pulsara-skill-creator`与`pulsara-skill-installer`本身成为direct bundled definitions：

- MCP installer只编排正式`pulsara mcp`配置/doctor、running Host reload与late-ready meta-tool路径；不直接编辑YAML、不建立第二套安装器、不猜测endpoint/tool schema，也不授予permission、workspace trust或remote-effect authority；
- plugin installer调用Round 9.3唯一Agent Plugins 1.0 validator/management/CLI path；只有deterministic Codex package-format差异才允许model-assisted authoring按需读取Codex source reference；若存在行为型Hook，再读取Pulsara Hook exact-target reference并生成新的standard candidate。Hook target必须覆盖event/matcher/stdin/environment/output/control/lifecycle语义。Bundled Skill本身不是Plugin parser、publisher、enablement/trust authority或compatibility Runtime；
- creator指导创建portable loose Skill并调用正式validator；
- installer只选择local source/scope并调用正式loose CLI；
- scope不明确时询问用户；
- install后先`skills list`，失败/未生效再`skills doctor`；
- 不引用sync/status/reset、provenance control或private installer scripts；
- Plugin guidance明确add/replace后disabled、enable的exact component review/acceptance、Hook单独trust，以及disable/remove/gc与running Host future-view reload语义；不把`--yes`解释为Hook trust或remote-tool permission。

旧private installer scripts及所有references/packaged copies保持删除：

```text
skill_utils.py
install-local-skill.py
list-installed-skills.py
```

---

## 10. CLI distribution 与 management boundary

### 10.1 Global launcher

`pyproject.toml`的：

```toml
[project.scripts]
pulsara = "pulsara_agent.cli:main"
```

继续是global launcher authority。正式wheel/tool install后，`pulsara`必须从任意non-source cwd运行，不依赖repository cwd、repository `.venv`、`PYTHONPATH`、source-tree symlink或`uv run`。

Development可使用repo `.venv`或`uv tool install --editable <repo>`；正式distribution使用non-editable wheel/package或Desktop installer安装同版本launcher。Bundled Skill guidance可以假设正式Pulsara distribution保证`pulsara`在PATH。

### 10.2 Workspace resolution

- `--scope workspace`使用explicit `--workspace`，未提供时使用cwd；
- `--scope user`完全不依赖cwd；
- `list/doctor`使用explicit `--workspace`，未提供时使用cwd；
- standalone validate只依赖source path；
- 所有workspace values在CLI edge lexical/resolved为absolute后进入typed request。

### 10.3 GUI/Web boundary

In-process Desktop GUI/local Host endpoint直接调用typed management service。Future Web upload只可作为独立acquisition/transport adapter，把已落地local source交给validator/installer；atomic publisher不承担archive/network/marketplace/credential authority。

本轮不需要durable UI job。CLI JSON不是GUI wire protocol；GUI与CLI必须来自同一个installed package version。

四个CLI的`--json`只属于terminal adapter：service返回typed operation carrier，CLI renderer选择human或JSON；Desktop/GUI/Web不得启动CLI subprocess、解析该JSON或把它当versioned IPC。

---

## 11. Owners 与 dependency direction

| Owner | 拥有 | 明确不拥有 |
|---|---|---|
| document parser | exact bytes与portable document diagnostics | basename、origin、filesystem |
| placement validator | declared name与frozen logical basename join | YAML parsing、precedence |
| narrow directory observer | descriptor-held candidate observation | source registration、winner selection |
| loose producer | four-root policy与complete candidates | bundled root、precedence |
| `KernelHostCore` / process bundled binding owner | one installed distribution root descriptor、all-session injection与top-level close | scanning、winner selection、refcount registry |
| bundled producer | borrowed process binding与official batch | owner close、user writes、sync/reset |
| Round 9.3 Plugin view/package owner | enabled instance selection、immutable roots、physical lifetime anchors | Skill winner/activation/read semantics |
| Plugin producer | one complete enabled view到candidates/issues/cause | package state、winner、materialization |
| central resolver | seven-tier winner/shadowed/conflicting/final bounds | scan、parse、package lifecycle |
| effective inspection service | one three-producer cut与typed result | CLI formatting、provider prefix mutation |
| Runtime Skill capability provider | catalog/active projection与source snapshot | producer mutation、rebase |
| retained Skill owner | historical installed FULL proof | current filesystem rejoin |
| loose atomic publisher | no-overwrite local install | bundled/Plugin management |
| CLI/GUI adapters | request parsing与typed projection | scanner、parser、precedence |

Forbidden dependencies：

```text
loose producer        -X-> bundled package reader
bundled producer      -X-> PULSARA_HOME/workspace write owner
plugin producer       -X-> Plugin durable state/package management
parser                -X-> origin/precedence/package lifecycle
resolver              -X-> filesystem reads
read_file             -X-> producer registry/materializer
compiler/compaction   -X-> bundled manifest/current filesystem rejoin
CLI                   -X-> private scanner/copy engine
```

`KernelHostCore`显式持有process bundled binding owner；Host composition显式借用该binding，并消费Round 9.3 published Plugin view，持有三个producer、resolver和inspection dependencies。`KernelHostCore.shutdown()`先关闭new-open admission、join已准入open attempts，再在all Hosts/services/workers quiesce/join后关闭binding与Plugin view anchors；shutdown先胜出的unregistered attempt不能注册或返回live session。Host不得关闭shared binding。Standalone list/doctor使用call-local owner/finally。全路径不通过global singleton、runner back-reference、manual refcount或dynamic registry解析。

---

## 12. Activated Round 9.3 Skill contribution boundary

Round 9.3 Plugin Skill已经接入本文already-owned path，没有重建Skill subsystem。其规格与production path已经闭合：

1. 真实Plugin package lifecycle owner及closed `COMPLETE | UNAVAILABLE` enabled-package snapshot；
2. immutable managed package root的descriptor containment与ordinary absolute path contract；
3. disable/replace/remove、GC与in-flight model/tool/retained resource lifetime；
4. 同scope/cross-component同名collision的全函数outcome；
5. Plugin-specific observation/bounds如何加入existing final catalog bounds；
6. exact diagnostics、cancel、deadline、CAS/install/close paths。

Final precedence把Plugin tiers插入loose与bundled之间：

```text
1-4. four loose roots
5. exact WORKSPACE Plugin Skills
6. USER Plugin Skills
7. bundled defaults
```

上述tiers已在真实owner下hard-cut扩展：

- `SkillDefinitionOrigin`第三arm；
- compositor显式third input；
- central resolver collision algebra；
- effective inspection causes/issues；
- Runtime view/resource lifetime；
- activation tests与dogfood。

Plugin Skill成为winner后与bundled/loose使用同一catalog、activation、read、compaction与fact path。禁止Plugin-private engine、four-root materialization、projection provenance、reconciliation、orphan cleanup或Skill-specific content ownership digest。

---

## 13. Implementation slices

### U-SKILL-0：Parser与contracts hard cut

- split document parser与placement validator；
- source-neutral parsed document/manifest/origin；
- source-neutral candidates/issues/effective inspection；
- closed derived source/labels；
- remove local wrappers与winner ordinal；
- update validator/publisher/staged validation single path。

### U-SKILL-A：Three explicit producers

- loose producer emits all candidates；
- four-root lexical/inode alias fail-closed；
- filesystem-backed pinned bundled root；
- build/runtime one exact official inventory classifier；
- explicit process binding owner、borrow与close；
- shared descriptor observer；
- enabled Plugin view adapter与third closed batch；
- closed producer availability causes；
- one absolute deadline与outer abort winner。

### U-SKILL-B：Central catalog composition

- exact seven-tier resolver与Plugin same-tier group conflict fallback；
- final winner/projection bounds；
- effective inspection operation；
- Runtime/list/doctor/GUI one truth；
- source-neutral capability facts while preserving stable source lineage。

### U-SKILL-C：Runtime与compaction

- one safe-point composition cut；
- exact `UNAVAILABLE_MINIMAL` successor；
- unavailable无hidden winner map，active fail-closed；
- Runtime diagnostic semantic-set projection；
- catalog/active projection；
- retained historical request-ordinal catalog/FULL read proof；
- remove current-manifest inherited rejoin；
- same-epoch continuity guards。

### U-SKILL-D：Bundled distribution subtraction

- delete sync/status/reset/startup sync；
- delete manifest/provenance/hash/backup/opt-out；
- remove publisher reserved control；
- update bundled guidance、CLI help、active specs与tests；
- keep real user artifacts untouched。

### U-SKILL-E：Activation

- full retained/PostgreSQL/architecture/oracle；
- wheel/global launcher/arbitrary cwd；
- bundled+loose local and real-provider dogfood；
- only afterall DoD update本文status/date/evidence。

所有slices最终形成一个production path；不得以feature flag、old/new mode、legacy CLI或local-only inspection分批激活。

---

## 14. Production modification map

实施时至少核对并按当前code truth hard-cut：

```text
src/pulsara_agent/capability/
  __init__.py
  contracts.py
  registry.py
  types.py
  local_skills.py
  local_skill_management.py
  local_skill_publisher.py
  provider.py
  resolver.py
  render.py
  bundled_skills.py                 # delete/replace; no wrapper
  <new narrow bundled definitions producer>
  <new effective Skill catalog contracts/owner>

src/pulsara_agent/conversation_kernel/
  host.py
  capability.py
  capability_composition.py
  context_sources.py
  provider_dispatch.py
  compaction/retained_skill.py
  Host Skill safe-point seams

src/pulsara_agent/model_input/
  compiler.py                       # retain existing diagnostic bound; no issue cap

src/pulsara_agent/tools/builtins/
  filesystem.py                     # actual ordinary read implementation
  workspace.py                      # request/path adapter if still active

src/pulsara_agent/cli.py
src/pulsara_agent/bundled_skills/
pyproject.toml
uv.lock

tests/
  bundled distribution/direct definitions
  local management/publisher
  capability contracts/registry/facts
  Round 9.1 Skills safe point
  Round 5B retained Skill
  CLI/packaging/wheel/oracle guards
```

还必须核对实际app/kernel top-level composition与shutdown owner，使process bundled binding在任何Host前构造、在all Hosts/services/workers之后关闭；不得只在Host内新增descriptor而继续让每个session各自pin。

Active spec synchronization至少覆盖Round 9.1 Agent Skills、local Skill installation completion和Round 5B retained Skill truth。Historical `contracts/`文档不是authority，不因本轮反向恢复旧设计。

---

## 15. Test plan

### 15.1 Parser与placement

- identical bytes经validate/install/loose/bundled得到identical parsed fields/document diagnostics；
- parser signature不接受expected basename/path/source；
- placement validator唯一产生directory mismatch；
- hidden staging使用frozen final basename；
- standalone validate、install initial/final source revalidation都使用frozen source basename，wrong-name source两者均INVALID；
- parsed name只在source placement成功后成为destination basename，stage/final使用该basename；
- optional fields与host-extension inert semantics一致；
- source-specific regex/YAML loader/bounds不存在。

### 15.2 Loose producer与central resolver

- producer输出all valid candidates，不含winner ordinal；
- four roots exact order；
- four roots在production无disable/exclude option；tests使用narrow fixture；
- normalized lexical alias与opened dev/inode alias都返回`LOOSE_CONFIGURATION_INVALID + LOOSE_ROOT_ALIAS`，不抛裸异常；
- 覆盖workspace=OS home、`${PULSARA_HOME}/skills`与`~/.agents/skills` alias及symlink/inode alias；
- every valid candidate exactly winner或shadowed；
- invalid high-tier允许lower valid winner；
- every invalid/shadowed issue完整保留；
- root replacement、membership change、document rewrite不能产生mixed COMPLETE；
- direct copy/edit/delete在next safe point可见。

### 15.3 Bundled direct definitions

- official expected tuple exact且unique；
- build/runtime复用all non-dot immediate entries classifier；extra README/file/directory、dot entry及unique regular `SKILL.md`规则一致；
- one process binding显式注入multiple Hosts，后构造Host不能adopt rebound root；Hosts不close，top-level quiesce后idempotent close；
- 确定性阻塞`start_mcp()`的unregistered open与shutdown竞态：binding保持到attempt cleanup/join，shutdown winner不注册/不返回session，shutdown后new open typed拒绝；
- standalone list/doctor call-local binding无descriptor leak且finally close；
- binding construction unavailable不阻止Host启动、process内不重开/adopt，observations稳定返回typed bundled cause；
- clean `PULSARA_HOME`下Host启动不创建skills root；
- official bundled Skills从wheel path直接进入catalog；
- ordinary `read_file`读取SKILL.md与supporting resource；
- non-filesystem Traversable不使用`as_file`并typed unavailable；
- missing/extra/invalid/raced official batch whole unavailable；
- process root rebound不adopt new bytes；
- arbitrary cwd/non-editable wheel证明package resources完整。
- `Path.home()`分别抛`RuntimeError`与`MemoryError`时，每次inspection只观察一次，返回`UNAVAILABLE / LOOSE_CONFIGURATION_INVALID / USER_HOME_CONFIGURATION_INVALID`且CLI只投影typed carrier；

### 15.4 Effective inspection与CLI

- Runtime/list/doctor/GUI adapter只消费effective inspection；
- COMPLETE/UNAVAILABLE constructor invariants；
- one/two/three producer failures的causes按`LOOSE, PLUGIN, BUNDLED` fixed-order且无partial facts；
- deadline/cancel在install前胜出时没有inspection/source observation/CAS；non-deadline unavailable在deadline前install才追加`UNAVAILABLE_MINIMAL`；
- bundled已知failure后loose期间到期、three COMPLETE后resolve/render期间到期、install linearization前到期均只返回outer abort；known cause不与deadline仲裁成snapshot；
- snapshot在deadline前FULL install后到期不追溯撤销，也不追加第二observation/CAS；
- catalog projection overbound产生无winner/fact的UNAVAILABLE，active固定`ACTIVE_SELECTION_UNAVAILABLE`且无hidden map；
- list只显示winners与derived source/label；
- doctor完整显示invalid/shadowed/root alias/unavailable且无disabled/excluded root surface；
- 超过旧128 issues仍不截断；
- 65+同code issues完整进入inspection/doctor，但Runtime context只投影一个stable semantic code且不触发compiler 64-bound；
- diagnostic enum hard cut exact为35 members；deadline/duplicate旧codes不存在，Plugin two additions的string values与mapping exhaustive；
- old bundled commands usage error且无hidden alias；
- validate/install/list/doctor四项`--json`均保留且只投影typed carrier，不被service/GUI解析。

### 15.5 Bounds与identity

- §5.4每项bound只有一个owner；
- 64 winners按final effective catalog计算；
- 16 MiB只属于one loose observation；
- bundled official set不偷加generic total cap；
- stable source kind/id exact保持；
- source contract domain/payload exact等于§8.1并删除old local-only domain；
- new source contract只在cold/new process或approved compaction boundary进入root，同epoch无热rebase/dual registration；
- existing loose capability/fact fingerprints exact保持；
- bundled relative path normalization与origin domain/payload exact等于§8.2；
- fact携带typed origin且无stored winning provenance fingerprint；
- architecture guards无batch/catalog/root-policy fingerprints。

### 15.6 Legacy subtraction

- no startup sync、manifest、opt-out、backup、tree hash或copy owner；
- old bundled copies仅ordinary loose override；
- doctor显示user winner与bundled shadow；
- `.pulsara-skill-source.json`作为ordinary resource成功安装且bytes exact；
- `RESERVED_CONTROL` enum/mapping不存在；
- bundled guidance无old commands/provenance/private scripts references。

### 15.7 Runtime、compaction与continuity

- deadline前成功install的non-deadline source unavailable append `UNAVAILABLE_MINIMAL`；owner deadline/cancel abort不伪造observation；
- predecessor不被当current catalog fallback；
- unavailable inspection无winner map，active不能从preflight intermediate偷取facts；
- same-turn active head保持；
- retained new read定位unique assistant request installed ordinal，并只join其前highest-ordinal canonical FULL/VALUE catalog；
- catalog A在request前、catalog B在request与ToolResult之间时必须join A；current head/B不得影响proof；
- retained path按historical row与Host-frozen workspace/home bindings机械join，placement basename来自historical `SKILL.md` parent；
- CLEARED/UNAVAILABLE/duplicate/unparseable historical catalog拒绝retention；
- inherited retained item无需current manifest仍保留；
- current catalog与historical retained/active body可并存；
- SYSTEM/tools byte-identical、messages suffix-only；
- no new database/event/subject/guard/relation/job；
- oracle保持`29 / 24 / 11 / 1 / 25 / 0`，Hook vocabulary保持`11`；
- 新增skip/xfail为0。

### 15.8 Final verification与dogfood

至少执行：

- retained full pytest；
- PostgreSQL full suite；
- Ruff、compileall、protocol generator、`uv lock --check`、`git diff --check`；
- non-editable wheel build与isolated tool install；
- arbitrary non-source cwd global launcher smoke；
- pure local service+CLI dogfood：bundled default -> install loose override -> list/doctor -> delete override -> bundled fallback；
- legacy loose copy shadow trace；
- bundled unavailable与loose unavailable traces；
- real provider activate/read bundled and loose Skills，保留actual prompt/messages/tool result/model reply；
- compaction retained historical Skill与same-epoch continuity trace。

只排除exact non-empty `PULSARA_API_KEY`值。Activation evidence记录真实commands、counts、outcomes与external availability，不记录逐文件/document/evidence SHA。

---

## 16. Definition of Done

只有全部满足才可把本文标记为ACTIVATED：

1. Bundled与loose使用同一个document parser和placement validator；standalone/install source placement truth exact一致。
2. Three explicit producers提交all candidates，central resolver是唯一precedence owner。
3. Seven-tier precedence、Plugin same-tier group conflict fallback与每个candidate exact disposition实现并测试。
4. Source-neutral manifest/origin/issues/inspection没有local wrapper、winner ordinal或open bag。
5. Effective inspection成为Runtime、list、doctor与future GUI共同truth。
6. Four roots始终启用；normalized path及dev/inode alias以`LOOSE_ROOT_ALIAS` whole-batch fail-closed，production无disable/exclude surface。
7. Bundled Skills从filesystem-backed installed package直接读取；build/runtime使用同一个exact non-dot inventory classifier。
8. 一个显式process binding owner服务all Hosts/GUI并在top-level quiesce后close；standalone inspection call-local/finally close，无singleton/refcount/lease。
9. Official inventory、path containment、root rebound与whole unavailable语义闭合。
10. Four roots只属于loose Skills，direct filesystem remains first-class。
11. `pulsara skills`只剩validate/install/list/doctor，四项human/`--json`都是typed operation的CLI-only projection。
12. Startup sync、sync/status/reset、manifest、provenance、hash、backup、opt-out及状态机全部删除。
13. `.pulsara-skill-source.json`为ordinary inert resource，publisher无reserved branch。
14. Existing copied bundled directories不自动删除，只按ordinary loose override处理并有诚实user guidance。
15. Stable capability source kind/id保留；§8 exact source-contract/bundled-origin framing实现；既有loose fact identity保持；redundant provenance fingerprint字段删除。
16. Owner deadline/cancel在snapshot install前只走outer abort，不构造deadline cause/observation；deadline前installed non-deadline UNAVAILABLE才追加`UNAVAILABLE_MINIMAL`，无predecessor semantic fallback。
17. UNAVAILABLE inspection无winner/fact/hidden map；active固定fail-closed。Complete inspection才可供active消费winner map。
18. Diagnostic enum exact hard-cut为35 members；inspection/doctor不截断path-specific issues；Runtime只投影stable diagnostic semantic set，existing compiler bound不成为Skill candidate cap。
19. Retained Skill按assistant request installed ordinal join其前latest canonical FULL/VALUE historical catalog及exact request/result path，不重新join current manifest/filesystem。
20. Same-epoch SYSTEM/tools exact、messages suffix-only；无第三种rebase boundary。
21. Long-horizon availability无新增总cap；small durability与oracle不增长。
22. Production只有真实`FrozenPluginSkillDefinitions`第三input与closed origin/cause/issue branches；没有empty placeholder、generic producer registry、Plugin Skill materialization或private catalog。
23. 无compatibility alias、dual path、fallback parser、service locator、generic mutable registry或watcher。
24. Active specs、tests、bundled guidance与CLI help只描述新单一路径。
25. Full tests、PostgreSQL、static、wheel、launcher与required dogfood全部通过，新增skip/xfail为0。

---

## 17. 最终冻结

本文激活后的当前产品语义是：

> Pulsara package中的bundled Skills是随当前Pulsara版本提供的只读defaults；四个loose roots中的用户/工作空间Skills是可直接copy、edit、delete和安装的高优先级overrides；enabled local Agent Plugin Skills构成exact WORKSPACE与USER两个中间tiers。三个显式producer经同一个document parser、placement validator和seven-tier central resolver形成唯一effective catalog。删除loose override会在下一complete safe point回退到next lower unique Plugin或bundled definition。系统不再同步built-in copies，不把Plugin Skills物化到loose roots，也不为此增加durability、fingerprint registry、watcher、总cap或provider prefix boundary。

Round 9.3已经把真实Plugin package Skill definitions接入这条路径；current production恰有Bundled、Loose、Plugin三个required explicit producers，并且保持一个完整、诚实的Skill product path。

---

## 18. Initial Bundled/Loose activation evidence（2026-08-25，historical baseline）

### 18.1 Final automated gates

以下命令均在repository root、当前working-tree与`.venv/`/`uv`环境执行：

```text
.venv/bin/python -m pytest -q -m 'not postgres and not retrieval_live'
  -> 763 passed, 223 deselected in 39.64s

.venv/bin/python -m pytest -q -m postgres
  -> 223 passed, 763 deselected in 100.17s

.venv/bin/ruff check .
  -> All checks passed!

.venv/bin/python -m compileall -q src tests tools
  -> exit 0

.venv/bin/python tools/generate_terminal_protocol_contract.py --check
  -> exit 0

uv lock --check
  -> Resolved 64 packages; exit 0

git diff --check
  -> exit 0
```

PostgreSQL full suite包含clean-v0 identity/resource tree、empty install、repeat migrate、binding-v2 checkout、catalog/grant/function/trigger drift与deep schema verification。Retained full与PostgreSQL full合计当前collection `986`个tests。新增`skip/xfail = 0`。

Architecture/oracle guards在上述full suites中证明：

```text
CommittedEvent descriptors = 29
LiveEvent types            = 24
Subject slots              = 11
Append guards              = 1
Product relations          = 25
new Skill durability       = 0
HookEventType              = 11
```

本hard cut没有PostgreSQL schema、event、subject、guard、relation或durable job变化。

### 18.2 Wheel、global launcher与pure-local dogfood

执行了non-editable wheel build与isolated tool install：

```text
uv build --sdist --out-dir <isolated>/sdist
uv build --wheel --out-dir <isolated>/wheel <isolated>/sdist/pulsara_agent-0.1.0.tar.gz
UV_TOOL_BIN_DIR=<isolated>/bin UV_TOOL_DIR=<isolated>/tools \
  uv tool install --from <wheel> pulsara-agent
```

结果：wheel `pulsara_agent-0.1.0-py3-none-any.whl`成功构建；isolated install安装51个packages与一个`pulsara` executable；在任意non-source cwd、unset `PYTHONPATH`、不依赖repository `.venv`或`uv run`时，`pulsara --version`返回`0.1.0`，`skills list --json`返回`COMPLETE`并从isolated wheel `site-packages/pulsara_agent/bundled_skills`读取closed official bundled Skill inventory。Wheel inventory gate确认bundled `SKILL.md`与installer supporting reference完整，旧private scripts没有进入wheel。`httpx>=0.28.1,<1`是direct project dependency，lock已一致。

同一isolated launcher与隔离`HOME`、`PULSARA_HOME`、workspace完成以下真实序列：

```text
bundled-only list
  -> COMPLETE; 2 bundled winners
validate bundled creator source
  -> VALID
install creator --scope workspace
  -> INSTALLED
list / doctor
  -> workspace winner + bundled SHADOWED issue
physical delete-by-move workspace override
  -> next list naturally falls back to bundled winner
install installer --scope user
  -> INSTALLED; user winner
direct cp installer into ${PULSARA_HOME}/skills
  -> next list selects ordinary user loose override
doctor
  -> bundled installer is SHADOWED by the user loose winner
pulsara skills sync-bundled
  -> argparse usage error, exit 2; no legacy alias
```

Direct in-process management service dogfood得到：

```text
validate_local_skill_source = VALID
install_loose_local_skill    = INSTALLED
inspect_effective_skill_catalog = COMPLETE
bundled missing binding -> UNAVAILABLE / BUNDLED_RESOURCE_UNAVAILABLE / 0 winners
loose root alias        -> UNAVAILABLE / LOOSE_CONFIGURATION_INVALID
                           + skill_loose_root_alias / 0 winners
Path.home RuntimeError  -> one lookup / UNAVAILABLE / LOOSE_CONFIGURATION_INVALID
                           + skill_user_home_configuration_invalid
Path.home MemoryError   -> one lookup / UNAVAILABLE / LOOSE_CONFIGURATION_INVALID
                           + skill_user_home_configuration_invalid
```

该local dogfood同时证明CLI不是installation receipt authority：直接copy/delete在next complete inspection可见，删除loose override无需reset或manifest即可回退package definition。

### 18.3 Real-provider、retained Skill与continuity

执行：

```text
.venv/bin/python tools/run_unified_skill_dogfood.py \
  --env-file .env \
  --trace-output benchmarks/suites/core/v1/unified_skill_definition_producers_trace.json
```

Production `DirectKernelModelPort`使用配置的`openai_chat_completions`真实provider与`meta/muse-spark-1.2-contributor`模型；tracer只包裹production model/transport，没有替换模型行为。最终结果：

```text
status                         = passed
foreground provider opens      = 7
compaction summary requests    = 1
canonical transcript rows      = 24
real read_file ToolResults      = 4 SUCCESS
bundled turn                   = 2 model calls / 1 tool call
loose turn                     = 2 model calls / 1 tool call
historical retained turn       = 3 model calls / 2 tool calls
bundled final reply            = BUNDLED_SKILL_READ_OK
loose final reply              = LOOSE_SKILL_READ_OK
retained final reply           = RETAINED_SKILL_READ_OK
snapshot count                 = 0 -> 1
compaction epoch changed       = true
RETAINED_SKILL_CONTEXT         = VALUE
retained exact loose body seen = true
```

Machine trace保留actual prompts、完整provider-visible SYSTEM/tools/messages、normalized provider stream、exact tool arguments/results、canonical transcript与model replies。它记录两个same-epoch groups；每组均为SYSTEM byte-identical、tools exact-equal、messages suffix-only。只有exact non-empty `PULSARA_API_KEY`值被替换为`<PULSARA_API_KEY>`；secret scan确认working-tree diff与2,186,107-byte trace均不含其exact value。

两个pre-gate编排探针曾诚实返回`semantic_failure`：compaction紧跟Skill read时，该FULL ToolResult仍在successor tail，selector按契约不生成冗余retained copy。最终轨迹在后续supporting-resource read后触发compaction，使先前Skill read成为historical eligible input；production retained规则未为dogfood修改。

### 18.4 Activation conclusion

合入前独立critic额外复现了两个P1：shutdown漏掉尚未注册的session-open attempt，以及OS home lookup的`RuntimeError/MemoryError`逃出typed inspection。当前同一diff已建立Host admission/settlement fence并让shutdown owner join exact attempts；同时建立一次性的typed `UserHomeResolution`，供Pulsara-home、four-root policy与ordinary path projection共享。新增四个回归节点覆盖两种home异常、open/shutdown竞态及closed bundled observation；随后重新通过retained full、PostgreSQL full、static、sdist→wheel→isolated launcher和local service/CLI dogfood。

该段记录2026-08-25首次Bundled/Loose hard-cut activation时的历史结论：当时§16全部25项DoD闭合，并以两个required producers标记`ACTIVATED`。2026-08-26以后current truth由本文前述Round 9.3同步及§19取代；历史数字不得被解释为current producer inventory。

---

## 19. Round 9.3 synchronization evidence（2026-08-26）

Round 9.3在同一production path中完成third producer hard cut：

```text
producer inputs = LOOSE / PLUGIN / BUNDLED
precedence      = four loose tiers / WORKSPACE_PLUGIN / USER_PLUGIN / BUNDLED
origin union    = LooseSkillOrigin | PluginSkillOrigin | BundledSkillOrigin
diagnostics     = 35 exact SkillDiagnosticCode members
source contract = skill-source-contract:v4-bundled-loose-plugin-skills
```

Focused、retained full、PostgreSQL、wheel/isolated launcher、pure-local lifecycle与real-provider Plugin dogfood均由Round 9.3 activation evidence记录。真实trace证明Plugin Skill进入seven-tier catalog并由ordinary `read_file`读取；same-epoch Plugin reload不改变SYSTEM/tools且messages suffix-only；compaction successor重新冻结current Plugin Skill/MCP；replace/disable/remove后的future view变化不建立materialization、registry、durable row或第三种rebase boundary。
