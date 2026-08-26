# Round 9.1：Agent Skills Standard 与 Append-only Skill Capability 实施规格

> 状态：**ACTIVATED**
>
> 当前语义同步：2026-08-26（Round 9.3 activated hard cut）
>
> 当前definition-producer架构由
> [`PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)
> 唯一细化。本文只保留仍然active的Agent Skills格式、Runtime authority、progressive disclosure与provider-prefix契约。
>
> Fingerprint约束：[`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)
>
> 当前architecture oracle：`29 / 24 / 11 / 1 / 25 / 0`；`HookEventType = 11`。

---

## 0. 当前产品结论

Skill是untrusted prompt/context capability，不是executable tool。当前production只接受三个显式definition producer：

```text
installed pulsara_agent package/bundled_skills（read-only defaults）
four loose roots（workspace/user editable overrides）
enabled local Agent Plugin packages（exact WORKSPACE / USER）
                         │
                         ▼
               one effective Skill catalog
```

Plugin package lifecycle由Round 9.3唯一拥有；Skill侧只接收`FrozenPluginSkillDefinitions`第三input。它不扫描managed state猜enablement，不把Plugin Skill物化到four loose roots，也不存在Plugin-private parser、catalog、activation/read path或empty placeholder branch。

Runtime行为：

```text
SKILL_CATALOG routing row
  -> ordinary read_file(exact listed SKILL.md)
  -> read references/scripts/assets only when needed
  -> use existing builtin/MCP/terminal tools
  -> ordinary permission/effect/attempt owners remain authoritative
```

不新增`load_skill`、`skill_view`或`skill_use` provider tool，不把Skill注册成tool，也不从Skill正文或frontmatter生成permission、Hook、MCP或execution authority。

---

## 1. 唯一production document path

### 1.1 Root-neutral parser

所有source共用一个root-neutral `parse_skill_document(bytes)`：

- 输入只含exact `SKILL.md` bytes与existing per-document byte bound；
- 不接受path、root、scope、source、expected basename或winner信息；
- 只使用production unique-key safe YAML loader；
- 无第二套regex、fallback YAML parser或source-specific bounds；
- 输出portable fields、body、closed diagnostics及legitimate content identities。

Portable frontmatter：

- required `name`：lowercase letters/digits/hyphens，UTF-8最多64 bytes；
- required `description`：trim后1..1024 chars；
- optional `license`：non-empty，UTF-8最多1024 bytes；
- optional `compatibility`：1..500 chars；
- optional `metadata`：最多64个unique string-to-string pairs，既有key/value/aggregate bounds保持；
- body是closing frontmatter delimiter后的exact decoded Markdown。

Host-extension与unknown extension只产生closed INFO diagnostics，保持inert；它们不进入tool、permission、dependency或execution语义。YAML anchors/aliases、explicit tags、duplicate keys、multi-document input及超出既有node/depth/frontmatter bounds均invalid。

### 1.2 Placement validator

唯一 `validate_skill_candidate_placement(parsed, expected_basename)` 只负责目录名与parsed `name` exact相等。Parser不产生placement diagnostics。

- loose Runtime candidate：expected basename是physical direct-child name；
- bundled definition：expected basename是official tuple member；
- Plugin definition：expected basename是同一次immutable package observation中的`skills/` immediate child name；
- standalone validate/install source observation：expected basename是frozen source directory name；
- installer hidden stage/final：expected basename是已验证parsed name，不使用随机stage basename。

---

## 2. 三个current definition producers

### 2.1 Loose producer

`LooseSkillDefinitionProducer`唯一拥有scope-neutral four-root policy与physical observation：

1. `<workspace>/.pulsara/skills`
2. `<workspace>/.agents/skills`
3. `${PULSARA_HOME}/skills`
4. `~/.agents/skills`

四个roots始终active。相同normalized lexical path或相同opened `dev/inode`使whole loose batch成为`LOOSE_CONFIGURATION_INVALID + LOOSE_ROOT_ALIAS`；不静默去重、不扫描两次、不抛裸异常。

每次effective inspection/Host composition只冻结一次typed OS user-home observation；`PULSARA_HOME` default与`USER_AGENTS`复用同一value。OS home lookup失败成为`LOOSE_CONFIGURATION_INVALID + USER_HOME_CONFIGURATION_INVALID`，不逃出typed inspection或重新观察。

Producer通过held no-follow descriptors冻结root/direct-child/regular `SKILL.md` identity与membership，descriptor-relative读取后final revalidate。Root replacement、candidate replacement、document rewrite或membership变化均使whole batch UNAVAILABLE；不重试、不返回partial winners。

Loose producer输出all valid candidates与all standard-invalid candidate issues，或one closed whole-batch unavailable cause。它不选择winner，不产生winner ordinal，不读取bundled package，不保存discovery fingerprint。

### 2.2 Bundled producer

`BundledSkillDefinitionProducer`只借用installed distribution中filesystem-backed absolute `pulsara_agent/bundled_skills/` descriptor。Official set exact为：

```text
pulsara-skill-creator
pulsara-skill-installer
```

Build与Runtime共用同一个pure all-non-dot immediate-entry classifier。Missing/extra/non-directory、non-regular `SKILL.md`、invalid definition或root rebound使whole bundled batch UNAVAILABLE；不发布partial defaults。

`KernelHostCore`在任何Host session前构造一个process-local binding owner，同一process的all Hosts只借用它。Session open在borrow前进入narrow admission/settlement fence；shutdown原子拒绝new open并join已准入attempt，未注册loser必须cleanup且不得注册/返回，然后才在sessions/workers quiesce后幂等close binding。Standalone list/doctor使用call-local binding并在finally close。Running process不adopt rebound package path；正常package upgrade由new process读取。

Bundled definitions直接从package absolute path进入catalog和ordinary `read_file`，不复制到four roots，不创建manifest/provenance/backup/opt-out状态。

### 2.3 Plugin producer

`PluginSkillDefinitionProducer`只消费Round 9.3一次完整、immutable、不能mixed的enabled-package view，并复用同一个root-neutral parser与placement validator。它输出ordinary `SkillManifest`、invalid issues或one closed Plugin producer unavailable cause；它不读取instance state、不管理package lifecycle、不选择winner，也不建立第五个Skill root。

Plugin origin exact携带`USER | WORKSPACE` visibility、WORKSPACE state key（仅WORKSPACE）、plugin id、package install id与`skills/<name>`relative directory。Supporting resources仍由ordinary `read_file`读取；old in-flight consumer通过Round 9.3 generic package lifetime anchor持有旧immutable root，disable/replace/remove只改变future view。

### 2.4 Central resolver

`SkillCatalogResolver`是唯一precedence owner：

```text
WORKSPACE_PULSARA
> WORKSPACE_AGENTS
> USER_PULSARA
> USER_AGENTS
> exact WORKSPACE Plugin Skills
> USER Plugin Skills
> BUNDLED
```

同一Plugin tier出现多个同名valid candidates时形成one group-level `CONFLICTING` issue，不按plugin id、安装时间或path选winner，并继续寻找lower-tier unique fallback。除此以外，每个valid candidate恰好成为winner或one shadowed issue；invalid high-tier不遮挡lower valid definition。Complete inspection保留all invalid/shadowed/conflicting issues，不受旧diagnostic cap截断。任一required producer UNAVAILABLE或final winner/catalog projection overbound时，effective inspection为UNAVAILABLE且没有winner/fact/hidden map；active selection同样fail closed。

---

## 3. Provider-visible projection

### 3.1 Catalog

Complete effective winners按name排序并以exact canonical JSON投影：

```json
{"skills":[{"name":"...","description":"...","location":"..."}]}
```

Catalog只含routing truth。Bundled location是installed package absolute `SKILL.md` path；loose location使用existing model-visible workspace/user prefixes；Plugin location是enabled immutable package root下的exact absolute path。

### 3.2 Active Skill

Explicit `$name`、`skill:name`或Host-configured activation只从同一frozen complete winner map选择，发送parsed Markdown body，不重复raw frontmatter。Missing selection或active projection overbound产生closed active-unavailable outcome，不改写argument、ToolResult或output。

同一open attempt使用同一frozen inspection；catalog与active不能分别重扫。Supporting resources始终通过ordinary `read_file`按当前filesystem/permission truth读取。

### 3.3 Availability

Effective UNAVAILABLE在已经形成并赶在outer owner deadline前install时，以`UNAVAILABLE_MINIMAL`追加并使active fail closed。Owner deadline/cancellation本身是safe-point abort，不创建deadline diagnostic或successor observation，也不回退predecessor semantic truth。

Runtime public diagnostics只投影stable semantic code set；inspection/doctor保留all path-specific issues。Existing compiler diagnostic bound不能反向成为Skill candidate cap。

---

## 4. Prefix continuity

同一Host、same scope、same installed epoch必须满足：

```text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
```

Skill add/edit/delete、bundled availability变化、Plugin reload/enable/disable/replace/remove、loose override与fallback只在next legal provider safe point形成successor source observation；不创造第三种rebase boundary，不重写installed prefix。

Cold epoch与explicitly adopted compaction successor才可重建provider input roots。Same-turn tool follow-up继续使用installed source heads；current filesystem变化不能回写已open attempt。

---

## 5. Compaction retained boundary

Round 5B只从predecessor installed provider history证明ordinary `read_file(offset=1)`实际FULL交付：

- 由exact ToolResult/call定位唯一assistant request installed ordinal；
- 只取该ordinal之前highest-ordinal canonical FULL/VALUE `SKILL_CATALOG`；
- request path、historical row、public result path与successful complete result exact join；
- 从actual `N|line` records恢复模型收到的normalized document，复用sole parser与historical basename placement；
- 不访问compaction-time current filesystem/catalog；
- inherited retained item只解码predecessor installed retained observation，不重新join current winner。

Retained仍使用existing 8 items/40,000-token product boundary，不建立receipt、loaded ledger、manifest history、registry或durable row。

---

## 6. Identity与durability

- Stable source kind/id保持`LOCAL_SKILL_CATALOG / pulsara-local-skill-catalog`；
- source contract使用`skill-source-contract:v4-bundled-loose-plugin-skills` exact framing，producer order为`LOOSE, PLUGIN, BUNDLED`，precedence为four loose tiers、`WORKSPACE_PLUGIN`、`USER_PLUGIN`、`BUNDLED`；
- manifest/fact携带closed `LooseSkillOrigin | PluginSkillOrigin | BundledSkillOrigin`；
- bundled origin exact为`bundled_skills/<official-name>`；
- Plugin origin exact为visibility/workspace key/plugin id/package install id/`skills/<name>`；
- origin digest只在fact builder真实canonical boundary即时计算，不存回DTO；
- 删除`discovery_semantic_fingerprint`、root-policy digest及所有consumer；
- 不新增digest map、generation、watcher、receipt、history、replay、repair job或database schema。

当前oracle保持`29 / 24 / 11 / 1 / 25 / 0`，Hook vocabulary保持11。

---

## 7. Management与distribution

Loose management的active authority是
[`PULSARA_LOCAL_SKILL_INSTALLATION_PRODUCT_COMPLETION_SPEC.zh.md`](PULSARA_LOCAL_SKILL_INSTALLATION_PRODUCT_COMPLETION_SPEC.zh.md)：

```text
validate_local_skill_source
install_loose_local_skill
inspect_effective_skill_catalog
```

CLI只有`pulsara skills validate/install/list/doctor`。旧bundled `sync-bundled/status/reset`及其sync/manifest/provenance/hash/backup/opt-out owner已经完整删除。Existing copied bundled directories不自动删除；它们只是ordinary loose overrides，删除后next complete safe point回退package default。

---

## 8. Final active truth

Round 9.1当前唯一真相是：一个portable Agent Skills parser与placement path，Bundled/Loose/Plugin三个显式definition producer，一个seven-tier central resolver，一个source-neutral inspection/fact/projection path，以及append-only provider exposure。Bundled是package内只读default；four-root loose definitions是可直接copy/edit/delete的highest-priority overrides；enabled local Agent Plugin definitions位于loose与bundled之间。没有bundled或Plugin Skill materialization、compatibility path、generic producer registry或第二套Skill engine。
