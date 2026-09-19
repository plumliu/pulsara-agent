# Pulsara Local Skill Installation Product Completion Spec

> 状态：**ACTIVATED**
>
> 初次激活：2026-08-24；unified catalog hard-cut同步：2026-08-25。
> 2026-09-06：GUI 来源导入与 managed-root 删除由能力页 MCP/可安装能力删除 hard-cut 规范扩展；仍复用本文的 source observation 与 atomic publisher，不增加 runtime discovery root。
>
> Effective catalog的current authority：[`PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。本文只拥有loose local management operations、source observation与atomic publisher契约。
>
> Fingerprint约束：[`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)

---

## 0. 产品结论

底层只有三个typed operations：

```text
validate_local_skill_source
install_loose_local_skill
inspect_effective_skill_catalog
```

CLI只有四个projection：

```text
pulsara skills validate <path>
pulsara skills install --scope workspace|user <path>
pulsara skills list
pulsara skills doctor
```

`list`与`doctor`消费同一个call-local immutable effective inspection。CLI不是安装receipt、GUI IPC或catalog authority；future in-process GUI/local Host adapter直接调用management service，不执行CLI subprocess、不解析stdout/JSON。

用户直接copy/edit/delete/rename loose Skill始终是一等合法路径。Runtime在next legal provider safe point重新观察four roots；没有`.installed` marker、receipt、watcher、confirmation或background reconciliation。

---

## 1. Shared foundations

### 1.1 Sole parser and placement path

Validation、installation、loose Runtime与bundled Runtime共用Round 9.1的root-neutral `parse_skill_document(bytes)`与separate `validate_skill_candidate_placement(parsed, expected_basename)`。

Standalone validation与install initial/final source revalidation使用frozen source basename；hidden stage/final使用已验证parsed name。Wrong-name source在validate/install都INVALID。CLI/UI不得实现第二套regex、YAML loader、fallback parser或bounds。

### 1.2 Absolute-only Pulsara home

唯一`resolve_pulsara_home`行为：

- unset/empty：default user home下`.pulsara`；
- configured：`expanduser`后必须absolute，再做lexical normalization；
- relative configured value：typed `RELATIVE_PULSARA_HOME`，绝不相对cwd调用`resolve()`；
- home/path lookup failure：closed typed reason；
- user install与effective inspection消费它；workspace install与standalone validate不因无关invalid user-home配置失败。

Runtime Host在construction冻结同一resolution与OS user-home binding，供four-root observation、ordinary read public-path projection与retained proof共用。

每次effective inspection或Host composition只执行一次typed OS-home observation；同一immutable result交给`resolve_pulsara_home`与`~/.agents/skills` binding。`Path.home()`的`MemoryError`、`OSError`或`RuntimeError`不得逃出inspection，也不得触发第二次lookup；它使需要OS home的loose batch成为`LOOSE_CONFIGURATION_INVALID + USER_HOME_CONFIGURATION_INVALID`。Workspace install与standalone validation仍不观察该无关配置。

### 1.3 Source path preparation

Validate/install共用一个source-binding preparation seam。Relative CLI source先相对invocation cwd变为absolute lexical path；Darwin只规范化OS-owned top-level `/tmp`、`/var`、`/etc` aliases，再逐组件no-follow打开。Arbitrary parent/final source symlink与source-tree internal symlink仍禁止。

---

## 2. Typed operation contracts

### 2.1 Validate

```text
VALID(parsed, diagnostics)
INVALID(diagnostics)
UNAVAILABLE(reason)
```

Closed unavailable reasons：

```text
SOURCE_MISSING
SOURCE_NOT_DIRECTORY
SOURCE_DIRECTORY_UNAVAILABLE
SKILL_DOCUMENT_READ_UNAVAILABLE
SOURCE_RACED
```

Operation持有source directory descriptor，冻结membership与regular `SKILL.md` identity，bounded read后final revalidate root/path/membership/document。Standard-invalid是`INVALID`；无法证明one coherent source observation是`UNAVAILABLE`。它不读取workspace、PULSARA_HOME或bundled definitions。

### 2.2 Install

只允许：

```text
workspace -> <workspace>/.pulsara/skills/<name>
user      -> ${PULSARA_HOME}/skills/<name>
```

安装不写`.agents/skills`，不支持overwrite/update/rollback/merge或remote acquisition。
删除是独立 typed operation，允许移除用户有权管理的既有 loose roots 中 exact Skill，并清理其
enablement override；不是安装覆盖、目录级通配删除或历史 transcript 改写。

Closed dispositions：

```text
INSTALLED
SOURCE_INVALID
UNSUPPORTED_ENTRY
DESTINATION_EXISTS
CANCELLED
SOURCE_UNAVAILABLE
SOURCE_RACED
TARGET_CONFIGURATION_UNAVAILABLE
STAGING_UNAVAILABLE
PUBLISH_UNAVAILABLE
CLEANUP_UNAVAILABLE
```

Target configuration与publish的subreason保持closed；CLI exit status只按typed disposition/reason映射，不解析exception text。

### 2.3 Inspect

Management service构造scope-neutral four-root loose policy与call-local bundled distribution binding，依次取得two immutable producer batches并调用同一个`SkillCatalogResolver`。返回唯一：

```text
CompleteEffectiveSkillCatalogInspection(winners, all candidate issues)
UnavailableEffectiveSkillCatalogInspection(all ordered causal failures)
```

`list`只投影effective winners。`doctor`从同一value投影four roots、invalid、shadowed、root alias与availability causes。Inspection无disabled/excluded roots、generation、cache、enumerated count或observed-byte fields。

---

## 3. Frozen source observation

第一次validation、copy、source final revalidation与staged verification属于同一个descriptor-relative frozen source observation：

1. no-follow打开source root并冻结root `dev/ino`；
2. 递归枚举ordinary membership，冻结每个relative path、kind与regular-file identity/mode；
3. `__pycache__` directories与`.DS_Store` files被exact忽略；
4. symlink、socket、device、FIFO及其他non-ordinary entry得到`UNSUPPORTED_ENTRY`；
5. `.pulsara-skill-source.json`是ordinary inert resource，按普通file复制，无provenance/ownership语义；
6. regular resources用constant-memory streaming copy；
7. copy后重新stream source/stage并比较call-local digest，随后再次验证source root/path/membership/identity；
8. digest只证明本次copy，settlement后丢弃，不进入DTO、registry、receipt或fingerprint map。

Source replace/add/delete/type/mode/identity变化为`SOURCE_RACED`；source read I/O为`SOURCE_UNAVAILABLE`；stage create/write/readback/mode/verification失败为`STAGING_UNAVAILABLE`。同时发生时由physical owner的source revalidation/arbitration选择closed priority，不按exception字符串猜测。

不增加bundle bytes、resource count、directory depth、Skill lifetime或history总cap。Existing per-document/YAML/provider projection bounds保持各自真实owner。Memory allocation failure必须形成typed settlement；stage创建后任何failure都进入cleanup wrapper。

---

## 4. Narrow atomic publisher

### 4.1 Fresh target preparation

Publisher是Pulsara-owned target roots的唯一write owner。它从resolved workspace/Pulsara-home anchor逐component执行`mkdirat/openat` no-follow准备：

- fresh control directories mode `0o700`；
- existing directories只验证，不擅自chmod；
- symlink/non-directory/race typed fail；
- target root descriptor持有到settlement。

Hidden sibling stage名为dot-prefixed random name，four-root scanner忽略；stage root/directories mode `0o700`，files mode `0o600 | frozen executable bits`。Staged validation的expected basename是frozen final name，不是random stage name。

### 4.2 Exclusive no-replace publish

Final binding cut紧邻publish并验证held target root path identity、private stage basename identity/evidence及final basename absent。

- Darwin只使用exclusive no-replace rename primitive；
- Linux只使用equivalent no-replace primitive；
- unsupported platform/syscall返回`PUBLISH_UNAVAILABLE/UNSUPPORTED_EXCLUSIVE_PRIMITIVE`；
- 禁止普通`rename`、check-then-rename、direct-final `copytree`或overwrite fallback。

Exclusive primitive只保证held-root namespace内final-name no-replace。Final cut后同UID搬移/rebind root或替换private stage属于out-of-contract namespace interference；API不虚构exact-path、verified-stage或reply-time liveness保证。

### 4.3 Cancellation and cleanup

Cancellation通过真实call-local probe在enumeration/read/copy/digest/final-cut前被观察。Filesystem worker若由adapter放入thread，必须shield并join；cancel不能留下detached writer。

Stage存在后所有cancel/I/O/allocation/verification/publish failure都先形成prior typed disposition，再尝试descriptor-owned cleanup。Cleanup成功返回prior disposition；失败只返回：

```text
CLEANUP_UNAVAILABLE(
  attempted_staging_path,
  location_status = KNOWN_AT_LAST_OBSERVATION
                  | UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE,
  prior_disposition,
)
```

它不声称attempted lexical path当前仍存在，不建立repair queue/job。

---

## 5. Crash and ACK semantics

- ordinary caught failure/cancel：publisher在process存活时join并清理stage；
- SIGKILL：没有Python finally保证，hidden stage可残留但scanner忽略；
- ACK-unknown：caller只能inspect filesystem/effective catalog，不能假设install成功或自动覆盖重试；
- power loss：不承诺fsync durability；final可能不存在或完整存在，existing destination从不被本attempt覆盖；
- no receipt、journal、recovery、rollback、repair或cross-Host resume。

---

## 6. CLI and package distribution

`pyproject.toml`发布`pulsara = pulsara_agent.cli:main`并声明all direct runtime dependencies。正式wheel/desktop distribution必须保证launcher从任意non-source cwd可执行，不依赖repository cwd、repository `.venv`、`PYTHONPATH`、source symlink或`uv run`。

Workspace resolution：

- install workspace scope、list、doctor支持explicit `--workspace`；未提供默认cwd；
- user install完全不依赖workspace/cwd（source relative resolution除外）；
- validate只解析source；
- human/`--json`都只是typed outcome projection，JSON不是GUI wire protocol。

Official bundled installers 优先使用现有 typed running-Host 控制面；MCP/Plugin 使用
`manage_capability` 与用户表单，Skill 使用既有安装/读取路径及 GUI 导入。global CLI 是
out-of-band 管理入口，变更后需显式 reload；first-party GUI/tool 变更自动在 safe point 采用，
不例行二次 reload。它们不回退到 source checkout 或 private installer；旧 private 脚本仍保持删除。

旧`sync-bundled/status/reset` CLI与对应sync/manifest/provenance/hash/backup/opt-out owner已经由unified hard cut删除。Package bundled Skills是read-only catalog defaults，不属于loose management service或publisher。

---

## 7. Continuity, durability, availability

- Install/直接filesystem变化只在next legal provider safe point可见；
- same installed epoch中SYSTEM/tools byte-identical，messages只append suffix；
- management operation不创建provider-visible tool或new rebase boundary；
- PostgreSQL schema/event/subject/guard/relation/job不增长；
- 无watcher、receipt、history、replay、repair、generation、durable UI job或hidden total cap；
- direct copy remains installation truth之一，CLI不拥有catalog admission。

Oracle保持`29 / 24 / 11 / 1 / 25 / 0`，Hook vocabulary保持11。

---

## 8. Definition of Done（active）

1. Three typed operations与four CLI projections只有one production path。
2. Validate/install source placement exact一致并复用sole parser。
3. Same frozen observation贯穿copy/revalidation/stage verification。
4. Fresh roots no-follow准备；exclusive no-replace无普通rename fallback。
5. Resource copy/compare constant-memory；MemoryError typed且stage cleanup闭合。
6. Direct copy/edit/delete remains first-class；no receipt/watcher/marker。
7. Inspection复用bundled+loose effective resolver；list/doctor无重叠authority。
8. Shared absolute-only Pulsara-home与Darwin source alias seam闭合。
9. Old private installer scripts和old bundled distribution state machine删除。
10. Wheel/global launcher/arbitrary cwd/local service+CLI dogfood通过。
11. No schema/oracle growth、new total cap、skip/xfail或compatibility path。

本文当前只描述loose local management与atomic publication。Effective definition/catalog语义以unified hard-cut规格为准；两者共同构成current Skill产品，不存在旧bundled copy distribution path。
