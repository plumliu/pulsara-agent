# Pulsara Local Skill Installation Product Completion Spec

> 状态：**ACTIVATED — 2026-08-24**
> 日期：2026-08-24
> 性质：这是对已激活 Round 9.1 Agent Skills 产品面的窄补充，不是新的 Round，也不改变 Skill discovery、provider continuity、durability 或 Plugin 架构。

## 0. 结论

Pulsara 已经能够从四个既有物理 root 发现、校验并向模型呈现 Agent Skills，但 loose local Skill 的用户操作面尚未完整闭环：

- `pulsara-skill-creator` 只能给出简化的创作说明，不能调用唯一生产校验器证明结果可被 Runtime 接受；
- `pulsara-skill-installer` 自带第二套简化 YAML parser、直接向最终目录执行 `copytree`，并把单一 workspace root 的 listing 误称为已安装 Skill 列表；
- 用户直接复制、编辑或删除 Skill 虽已被 Round 9.1 Runtime 正确观察，但尚无正式 CLI 用于解释 invalid、shadowed 或 raced scan；
- 未来 Plugin installer 需要建立在完整的 loose Skill 产品面之上，但不得复用 loose installer 冒充 managed Plugin materialization owner。

本任务先冻结一个Host-local Skill management core的三项产品操作：

```text
validate local Skill source
install loose local Skill
inspect effective local Skill catalog
```

并提供四个首批CLI projection：

```text
pulsara skills validate <path>
pulsara skills install --scope workspace|user <path>
pulsara skills list
pulsara skills doctor
```

四个命令是本任务需要交付的Agent/terminal操作面，但不是架构authority、GUI IPC协议或唯一产品入口。两个bundled Skill只负责指导Agent调用这些CLI；未来Desktop GUI与Web UI必须直接复用同一个management core。任何adapter都不再拥有parser、copy engine、catalog resolver或provenance状态。

## 1. Authority 与冻结边界

### 1.1 当前 truth

实现必须以当前生产代码和下列已激活契约为准：

- `ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md`；
- `src/pulsara_agent/capability/local_skills.py` 中唯一 four-root policy、production parser、bounded scanner、duplicate precedence 与 aggregate availability；
- `src/pulsara_agent/capability/bundled_skills.py` 中仅属于 bundled distribution 的 sync/provenance owner；
- `AGENTS.md` 与 `PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`。

Round 9.1继续提供Agent Skills product semantics，但其文档顶部的旧architecture计数已经被fingerprint hard cut与current guards取代；实现与activation必须使用本规格§8的current oracle，绝不能据旧数字恢复已删除的event/subject/guard/relation/job。

四个既有 root 及优先级保持不变：

1. workspace `<workspace>/.pulsara/skills`；
2. workspace `<workspace>/.agents/skills`；
3. user `${PULSARA_HOME}/skills`，未配置或empty时物理默认 `~/.pulsara/skills`；configured value经`expanduser()`后必须是absolute，relative value是typed-invalid configuration，绝不相对cwd解析；
4. user `~/.agents/skills`。

Current production Host与management service都使用全部四个bindings。Existing `include_user_skills=False`注入/test路径可以只选择前两个bindings，但它不是当前用户设置；management core及其CLI/GUI/Web adapter不得据此发明enable/disable产品面。任何路径都不得建立第五个root，也不得扫描Codex/Claude/Plugin cache猜测安装状态。

### 1.2 Skill 的产品地位

Loose Skill 是用户拥有的普通目录与低权威 Markdown guidance：

- 它不授予工具、permission、network、credential 或执行能力；
- `SKILL.md` frontmatter 不承载 Pulsara tool schema、Hook、MCP、preset 或 Plugin dependency；
- Runtime 不维护“已加载/已执行/已安装完成”的 durable Skill 状态；
- 普通 loose Skill 不创建 `.pulsara-skill-source.json` 或其他 managed provenance；
- filesystem 当前内容仍是 Runtime discovery truth，management operation或CLI成功结果都不是catalog receipt。

### 1.3 与 Plugin 的边界

本任务不实现 Round 9.3 Plugin，也不新增 `pulsara-plugin-installer`。

未来三项 bundled Skill 必须保持独立：

1. `pulsara-skill-creator`：创建或改进 loose Skill；
2. `pulsara-skill-installer`：安装用户拥有的 loose Skill；
3. `pulsara-plugin-installer`：只调用未来 Plugin manager 的正式安装接口。

未来 Plugin materialization 可以把包内 Skill 同步到既有 root，但 provenance、upgrade、uninstall 与 reconciliation 由 Plugin manager 拥有，不能委托给本 loose installer。

## 2. 本轮目标与非目标

### 2.1 目标

1. 建立一个可由CLI、Desktop GUI与未来local Host API直接复用的Skill management application service。
2. 为单个Skill目录提供与Runtime完全一致的typed validation operation。
3. 为Pulsara-owned workspace/user root提供拒绝覆盖、publisher自身从不逐文件暴露partial final目录、诚实限定same-UID namespace race与crash弱保证的typed installation operation。
4. 提供一次Runtime-equivalent inspection operation，统一承载effective winners与可行动diagnostics。
5. 把inspection分别投影为简洁的CLI `list`与完整的CLI `doctor`，不形成两套scan/authority。
6. 保留用户直接复制、编辑和删除Skill的合法性。
7. 删除bundled installer中重复的parser、copy/list engine，让两个bundled Skill调用正式CLI adapter。

### 2.2 非目标

本任务不实现：

- Git、GitHub、URL、marketplace、npm/pip 或远端下载；
- Skill update、force overwrite、merge、remove、rollback history或版本选择；
- `.agents/skills` 的 managed write；
- Plugin install、Plugin dependency、Plugin provenance 或 Plugin cache scan；
- Desktop GUI页面、Web UI页面、Host RPC/HTTP transport与浏览器目录/archive upload协议；
- 把CLI stdout或当前`--json`结果冻结成跨进程GUI/Web wire contract；
- Skill watcher、registry generation、installation receipt、history、replay、repair queue或后台同步；
- provider-visible Skill management tool；
- Skill resource dependency closure、recursive script verification或任意脚本执行；
- 新的总 Skill bundle bytes、resource file count或历史数量 cap。

## 3. 三个底层产品操作与四个初始CLI projection

### 3.1 唯一 management core

架构authority是一个Host-local `LocalSkillManagementService`（名称可按现有模块风格确定），而不是CLI parser或CLI JSON。它提供三项typed operation：

```text
validate_local_skill_source(resolved_local_directory)
install_loose_local_skill(resolved_local_directory, resolved workspace|user target, cancellation probe)
inspect_local_skill_catalog(resolved workspace + service-held settings context)
```

上述签名表达产品边界，不要求为远端source发明generic provider。当前唯一installation source是Host OS可读取的本地目录；未来Web upload若有独立产品必要性，应先由transport adapter把上传内容安全物化为临时本地目录，再调用同一service。

Service必须：

- 返回closed typed outcome，而不是让adapter按异常字符串判断；
- 复用唯一production Skill document validator；
- 让catalog inspection复用existing `LocalSkillProvider`，并由service从resolved workspace/settings构造scope-neutral physical four-root policy；
- 只通过一个narrow atomic publisher写loose Skill目标目录；
- 不依赖conversation repository、provider compiler、Hook dispatcher、Plugin manager或UI状态；
- 不保存跨调用registry、generation、receipt或background job。

当前production没有“关闭user Skill roots”的用户产品设置；`LocalSkillProvider(include_user_skills=False)`只是existing injection/test能力。标准management service与CLI因此固定使用current production four-root default，不能新增CLI-only开关并声称它代表某个正在运行Host的current policy。未来若产品确实增加user-root enablement，必须由Host、CLI与UI共享一个真实setting，再单独修订本契约。

Current `default_pulsara_home()`与`_default_user_product_skills_root()`各自使用`Path(raw).resolve()`，会让relative `PULSARA_HOME`绑定invocation cwd；这是本任务必须一并hard-cut的current bug。应提取一个shared、纯、无cwd输入的`resolve_pulsara_home` owner，供bundled sync/status/reset、`LocalSkillProvider`、management service与CLI共同调用：

```text
PulsaraHomeResolution
  RESOLVED(exact absolute path)
  INVALID(RELATIVE_PULSARA_HOME | USER_HOME_UNAVAILABLE | PATH_RESOLUTION_UNAVAILABLE)
```

- unset或empty value使用`Path.home()/.pulsara`，并证明结果absolute；
- configured environment value或explicit `pulsara_home` override都先`expanduser()`；若展开后不是absolute，返回`INVALID/RELATIVE_PULSARA_HOME`，绝不调用会把它绑定cwd的`resolve()`；
- absolute value只做不依赖cwd的lexical normalization与后续descriptor-relative physical verification；resolver本身不创建目录、不follow target components；
- user-scope install把invalid resolution映射为`TARGET_CONFIGURATION_UNAVAILABLE`；`SkillCatalogUnavailableReason`在本hard cut加入`USER_HOME_CONFIGURATION_INVALID`，Runtime/list/doctor形成whole catalog `UNAVAILABLE/USER_HOME_CONFIGURATION_INVALID`并携带`skill_user_home_configuration_invalid`，不退回partial workspace winners；bundled commands无filesystem mutation并返回其typed configuration failure/nonzero CLI status；
- workspace-scope install与standalone validate不需要user root，不得因无关的invalid `PULSARA_HOME`阻断physical workspace install/validation。

Loose source path另由validate/install共享的narrow source-binding preparation seam拥有。它先做absolute lexical normalization；在Darwin只把冻结的OS-owned top-level aliases `/tmp → /private/tmp`、`/var → /private/var`与`/etc → /private/etc`规范化，然后才从`/`开始逐component no-follow descriptor walk。它不得对任意source调用recursive `realpath()`，不得follow final source directory或source tree内部symlink；因此常见Darwin临时路径可用，但用户创建的final/interior symlink仍typed-fail。

`cancellation probe`是narrow、process-local operation-control port，不是第四项产品operation。CLI以SIGINT、未来GUI以自己的cancel signal驱动同一probe；service不得把`asyncio.to_thread()`或platform publisher detach到后台。进入exclusive publish syscall前可以消费cancel；syscall开始后必须shield并join exact physical result。该port不带总timeout、重试计数、receipt或跨进程恢复语义。

CLI、Desktop GUI与Web UI都只是adapter：

- bundled Skill或human terminal通过CLI调用service；
- Desktop GUI通过in-process application API或local Host API直接调用service；
- Web UI通过authenticated local Host endpoint调用service；
- GUI/Web UI不得启动CLI subprocess、解析CLI stdout或把CLI `--json`当成IPC协议；
- 本任务只实现CLI adapter，但management core必须不依赖CLI类型，并可由后续GUI/Web adapter直接复用。

四个CLI命令名与必需参数是本任务的初始产品操作面；人类输出措辞与当前`--json`shape不是永久跨Host wire contract。任何未来稳定automation schema都必须单独版本化，不能反向成为management authority。

### 3.2 Validation operation与`pulsara skills validate <path>`

用途：判断一个独立目录中的`SKILL.md`是否满足当前Agent Skills core contract。

底层validation outcome精确闭合区分：

- `VALID`：完整normalized manifest与bounded diagnostics；
- `INVALID`：已成功观察source directory，但缺少required `SKILL.md`，或已成功读取exact document但不满足standard contract；
- `UNAVAILABLE`：source directory缺失、不是目录、无法读取，或无法证明一次完整filesystem观察。

`UNAVAILABLE` reason精确闭合为`SOURCE_MISSING | SOURCE_NOT_DIRECTORY | SOURCE_DIRECTORY_UNAVAILABLE | SKILL_DOCUMENT_READ_UNAVAILABLE | SOURCE_RACED`。Exact-read document overbound或standard-invalid属于`INVALID`，不伪装成filesystem unavailable。

Operation必须：

- 只读，不创建root、不复制文件、不修改source；
- 解析`<path>/SKILL.md`的exact bounded bytes；
- 复用Runtime唯一parser、YAML shape validation、name/description/optional field bounds与目录名一致性规则；
- 与install共用上述source-binding preparation；CLI只交付absolute lexical request，不自行follow或重新解释filesystem；
- 明确区分manifest invalid与filesystem read/race unavailable。

现有`SkillDiagnostic.code: str`必须hard-cut为closed `SkillDiagnosticCode` vocabulary。Exact value set冻结为：

```text
skill_missing_document
skill_document_overbound
skill_invalid_utf8
skill_missing_frontmatter
skill_frontmatter_overbound
skill_invalid_frontmatter_yaml
skill_host_extension_ignored
skill_unknown_extension_ignored
skill_invalid_name
skill_directory_name_mismatch
skill_invalid_description
skill_invalid_license
skill_invalid_compatibility
skill_invalid_metadata
skill_location_overbound
skill_body_over_500_lines
skill_body_estimate_over_5000_tokens
skill_root_escape
skill_root_not_directory
skill_direct_child_bound_exceeded
skill_directory_escape
skill_file_escape
skill_enumeration_raced
skill_read_raced
skill_discovery_byte_bound_exceeded
skill_discovery_deadline_expired
skill_duplicate_name
skill_winner_bound_exceeded
skill_catalog_projection_bound_exceeded
active_skill_not_found
skill_projection_overbound
skill_user_home_configuration_invalid
```

这只是把现有production/authoring diagnostic vocabulary、explicit-validation missing-document code与shared user-home configuration failure改成closed enum，不增加provider-visible状态。Existing `SkillAuthoringDiagnosticCode`可以继续作为manifest authoring-code tuple的narrow subset，但每个`SkillDiagnostic.code`必须是上述完整`SkillDiagnosticCode`，不能继续是`str`。Free-text message只用于展示，不能驱动service、CLI或未来UI分支。Validation `INVALID`必须至少携带一个typed diagnostic；`UNAVAILABLE`携带closed source-unavailable reason。Provider context adapter必须对该enum做exhaustive mapping，不得继续按字符串相等、prefix或substring分支：INFO authoring diagnostics保持local omission；`active_skill_not_found`映射existing active-skill-not-found；active-only `skill_projection_overbound`映射existing active-selection unavailable；`skill_catalog_projection_bound_exceeded`及其余catalog discovery/validation/configuration codes映射existing catalog incomplete。删除没有production producer的`skill_catalog_budget_truncated`与`skill_not_found`旧分支。

Current `projection_overbound_diagnostic()`被catalog render与active render两个catch共用，却始终生成`skill_projection_overbound`；该code无法在不解析message的情况下同时表示`CATALOG_OVERBOUND`与`ACTIVE_SELECTION_UNAVAILABLE`。本任务必须拆成closed causal producers：catalog catch生成existing `skill_catalog_projection_bound_exceeded`并保持catalog-unavailable reason；active catch才生成`skill_projection_overbound`并保持active-unavailable reason。可以使用两个narrow builder或一个接受closed cause的exhaustive builder，但不能再让一个code具有两个public mappings，也不能读free-text区分。

不得：

- 维护第二套regex、YAML loader或fallback line parser；
- 扫描四个roots、计算duplicate winner或声称该Skill已经生效；
- 把unsupported host extension当成Pulsara permission/dependency；
- 因body authoring recommendation超过500行而把合法manifest判为invalid。

唯一parser必须hard-cut为两层单路径：

```text
root-neutral Skill document parser
  input: exact bounded SKILL.md bytes + owner-supplied expected placement basename
  output: parsed standard fields/body/raw-document evidence
          + authoring diagnostics + closed validation diagnostics

Runtime placement enrichment
  input: valid parsed document + owner-issued PreparedSkillRootBinding
  output: LocalSkillManifest(path/base_dir/location/root_kind included)
```

Validation、source install、staged validation和Runtime scanner都调用第一层；只有Runtime four-root scanner调用placement enrichment。Expected placement basename由各owner机械提供：standalone validation/source install与Runtime scan使用实际candidate directory basename；hidden stage使用从已验证source manifest冻结的final destination basename，绝不能使用随机点号staging basename。Root-neutral result不得伪造`root_kind`、model-visible `location`或provenance。现有per-document raw digest与manifest semantic fingerprint继续只服务其真实content/provider边界；本任务明确删除后文所述不完整的aggregate `discovery_semantic_fingerprint` join，不为installation或inspection增加替代identity、registry或receipt。

CLI projection支持人类可读输出与`--json`；`INVALID/UNAVAILABLE`使用非零退出状态。CLI只投影typed outcome，不重新校验或翻译错误字符串。

### 3.3 Installation operation与`pulsara skills install --scope workspace|user <path>`

用途：把用户提供的完整loose Skill目录安全复制到Pulsara-owned root。

目标固定为：

- `workspace` → `<workspace>/.pulsara/skills/<skill-name>`；
- `user` → `${PULSARA_HOME}/skills/<skill-name>`。

Typed request必须显式携带`workspace|user`，CLI以required `--scope`投影。不得保留任意`--dest-root`，也不得通过该operation写入`.agents/skills`。

Installation disposition精确闭合为：

```text
INSTALLED
SOURCE_INVALID
SOURCE_UNAVAILABLE
SOURCE_RACED
TARGET_CONFIGURATION_UNAVAILABLE
UNSUPPORTED_ENTRY
RESERVED_CONTROL
DESTINATION_EXISTS
STAGING_UNAVAILABLE
PUBLISH_UNAVAILABLE
CANCELLED
CLEANUP_UNAVAILABLE
```

`TARGET_CONFIGURATION_UNAVAILABLE`的closed reason是`RELATIVE_PULSARA_HOME | USER_HOME_UNAVAILABLE | PATH_RESOLUTION_UNAVAILABLE`；它只适用于user target resolution。`PUBLISH_UNAVAILABLE`再以closed reason区分`UNSUPPORTED_EXCLUSIVE_PRIMITIVE | ROOT_BINDING_RACED | PUBLISH_IO_FAILURE`。Free-text只用于展示；CLI exit status与未来UI状态只能由typed disposition/reason映射。

`INSTALLED`携带configured destination path与successful namespace-linearization事实，并且只声明publication在下述同UID并发模型内完成、需由后续Runtime inspection确认catalog；它不是reply时刻的path-liveness证明，也不得声称某个正在运行Host已经采用该Skill。`CLEANUP_UNAVAILABLE`携带原始失败disposition、attempted hidden staging path与closed `location_status=KNOWN_AT_LAST_OBSERVATION | UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE`；它不能无条件声称该lexical path仍存在，也不创建repair job。

安装顺序必须是单一路径：

```text
resolve exact requested target configuration
  (workspace ignores unrelated user-home config; user invalid settles here)
→ prepare the shared source binding (including frozen Darwin system aliases)
→ open and hold the source-directory descriptor
→ freeze filtered membership and exact entry identities
→ read and validate exact SKILL.md bytes from that observation
→ reject reserved managed provenance
→ safely prepare/open the Pulsara-owned target root
→ reject observed destination collision
→ copy complete ordinary directory into a hidden sibling staging directory
→ prove the staged copy is the attempted source observation
→ validate the exact staged SKILL.md bytes against the frozen final basename
→ no-overwrite atomic publish to the final directory
→ return typed destination and discovery guidance
```

物理要求：

- staging必须位于目标root内并使用点号开头的临时目录，使现有scanner在发布前忽略它；
- source、target root与staging必须先做exact containment/overlap检查；target root或本次staging/final位于source subtree内时拒绝，避免copy遍历吸收自己的输出；
- source tree使用从operation开始一直持有到source final revalidation完成的source-directory descriptor锚定、descriptor-relative、no-follow、single-attempt observation；同时冻结source root path binding，并在publish前验证该path仍no-follow指向held directory的同一`dev/ino`。不得依赖先`resolve()`再递归path walk证明不逃逸；
- 第一次正式validation不是copy前的独立path read。Publisher先冻结过滤后的完整membership与每项exact identity，再从该同一observation读取`SKILL.md` exact bytes交给唯一production parser；copy、source final revalidation、staged exact verification与staged validation都必须join该call-local frozen observation。Stage comparison与source final revalidation先于staged parser；任何membership、identity或exact `SKILL.md` bytes变化一律为`SOURCE_RACED`，不得把稳定替换后的V2当成新attempt继续发布或误报`STAGING_UNAVAILABLE`；
- frozen source identity至少包含relative path、entry kind、`dev`、`ino`、regular-file `size`、`mtime_ns`、`ctime_ns`与`st_mode & 0o111`。它只存在于当前call，完成后丢弃，不成为fingerprint、catalog identity或receipt；
- ordinary directory与regular file进入portable copy domain；symlink、socket、device、FIFO等非普通entry产生`UNSUPPORTED_ENTRY`；
- exact basename `__pycache__`目录与`.DS_Store`文件在任意层级被过滤，并从source membership与staged verification domain同时排除；不支持通配或caller-supplied ignore rule；
- source root direct child `.pulsara-skill-source.json`及未来active spec明确声明的managed provenance filename产生`RESERVED_CONTROL`；loose installer不得复制、生成、伪造或继承它；
- portable destination mode冻结为：regular file显式`fchmod(0o600 | (source_st_mode & 0o111))`，directory显式`fchmod(0o700)`；不得依赖umask或复制source read/write/directory mode。Ownership、timestamp、xattr与ACL不属于Skill semantic copy contract；
- 每个regular file在no-follow open前后验证entry identity，并在同一descriptor上stream bytes；完成后重新验证已冻结directory membership与每项identity。任何replace/add/delete/type change产生`SOURCE_RACED`，本attempt不重试；
- resource copy、source/stage exact compare与final stage digest revalidation必须使用固定chunk的constant-memory streaming；不得把任意resource收集为`list[bytes]`后整体join，也不得以新增bundle/resource bytes cap代替streaming。只有已有64 KiB产品界限的`SKILL.md`可以bounded materialize供production parser消费；
- successful source final revalidation是本attempt的source-observation cut；cut之后对原source的修改属于新的filesystem事实，不追溯改变已冻结copy，也不要求publisher持续锁住用户目录；
- staged tree必须对relative path、entry kind、regular-file exact bytes和上述explicit destination modes进行exact verification，并在exclusive syscall前紧邻地再次验证stage directory binding/membership/identity。该pre-syscall check不是与rename原子绑定的stage-inode conditional；其保证受下述same-UID namespace contract限制。实现可以使用call-local streaming byte comparison或digest；digest在settlement前丢弃，不进入DTO identity、cache、registry、provenance或receipt；
- destination在exclusive publish linearization point已存在时产生`DESTINATION_EXISTS`，不覆盖、不合并、不备份；
- copy phase按physical I/O owner分类，不得用generic copy exception或free-text猜测：source descriptor open/read/acquisition/final-revalidation I/O failure且未证明identity变化=`SOURCE_UNAVAILABLE`；source root binding、membership或entry identity变化=`SOURCE_RACED`；staging create/write/readback/`fchmod`/membership verification或staged validation inconsistency=`STAGING_UNAVAILABLE`；
- source-side与stage-side failure同时可见时，owner先完成一次best-effort source final revalidation，优先级冻结为`SOURCE_RACED`（已证明变化） > `SOURCE_UNAVAILABLE`（source完整观察不可完成） > `STAGING_UNAVAILABLE`（source仍exact、仅stage失败）。该仲裁不重试copy，也不把source EIO映射成磁盘空间不足；
- source race/unavailable、staging failure、staged validation failure、publish failure或publish前cancel应best-effort清除本次staging；process-live清理失败产生`CLEANUP_UNAVAILABLE`并保留prior typed disposition，不得谎称已经清理；
- stage创建以后发生的`MemoryError`或source/stage read/write/digest allocation failure必须进入现有typed settlement并执行同一cleanup wrapper；source/stage仍exact但host allocation失败归`STAGING_UNAVAILABLE`，cleanup本身无法完成则归`CLEANUP_UNAVAILABLE`。不得让异常逃逸并遗留未报告hidden stage；
- 不保存content digest registry、installation receipt或跨进程恢复记录；如实现使用process-local byte comparison/digest证明本次copy完整，它只能服务于该次真实copy verification，不得成为新的身份或trust authority。

Exclusive publisher必须是一个窄platform adapter，并同时拥有首次安全准备Pulsara-owned target root的唯一write authority。它以resolved workspace或resolved Pulsara home的verified descriptor作为anchor，逐级使用no-follow `mkdirat/openat/revalidate`取得固定control components，再以已经打开、重新验证的target-root directory descriptor锚定staging与final basename：

- workspace target从existing resolved workspace descriptor创建/打开固定`.pulsara`与`skills`；user target只接受shared resolver签发的exact absolute Pulsara home。Default home从resolved user-home descriptor创建/打开固定`.pulsara`与`skills`；configured absolute home从verified existing filesystem anchor逐级no-follow创建/打开missing components；relative configuration在进入publisher前已经typed-fail，publisher绝不自行相对cwd解析；
- 新建Pulsara-owned control/root directories显式mode `0o700`；existing directory只验证且不擅自chmod；
- 每一级existing symlink、non-directory、permission failure、containment/identity race都不得follow或check-then-use。Staging创建前的root preparation失败映射`STAGING_UNAVAILABLE`；final binding cut前发现的重新绑定或root race映射`PUBLISH_UNAVAILABLE/ROOT_BINDING_RACED`；
- 首次安装时root不存在是合法输入，不能要求预先运行bundled sync或手工`mkdir -p`；
- 本attempt已安全创建的固定空control/root directory可以在后续失败后保留；不得为“清理”递归删除可能已经被用户使用的`.pulsara`或`skills`目录。该空目录不是Skill publication、staging residue或receipt；

- Darwin使用`renameatx_np(..., RENAME_EXCL)`或经测试证明完全等价的exclusive directory rename；
- Linux使用`renameat2(..., RENAME_NOREPLACE)`或经测试证明完全等价的exclusive directory rename；
- 其他平台只有在存在并经过竞态测试的no-replace directory publish primitive时才能启用；否则返回`PUBLISH_UNAVAILABLE/UNSUPPORTED_EXCLUSIVE_PRIMITIVE`；
- 绝不回退为`exists()` + `os.rename()`、普通`Path.rename()`或任何可能替换既有空destination的check-then-rename；
- staging与final必须是同一target root的siblings，以保持same-filesystem atomic rename。

Exclusive rename syscall是publication linearization point：

- 在进入该点前观察到cancel：不publish，清理staging后返回`CANCELLED`或`CLEANUP_UNAVAILABLE`；
- syscall前的final binding cut必须重新验证：configured target-root pathname仍指向held root `dev/ino`、descriptor-relative stage basename仍指向verified stage `dev/ino`、final basename尚不存在。Cut前不相等按exact owner映射`ROOT_BINDING_RACED | STAGING_UNAVAILABLE | DESTINATION_EXISTS`；
- Darwin/Linux exclusive primitive只原子保证在传入的held root descriptors内进行no-replace rename；它不把上述stage inode或configured root pathname加入syscall condition。Official install因此明确采用non-adversarial same-UID namespace contract：从final binding cut到syscall physical completion之间，另一个同UID actor不得搬走/替换configured target root，也不得替换/删除publisher-private stage basename；
- 该约束不声称提供sandbox/security isolation。另一个同UID actor在该窗口内修改private stage/root namespace属于用户拥有filesystem的out-of-contract interference；此时`INSTALLED`、exact configured location与verified-stage identity不提供产品保证，后续`list/doctor`只能报告当时filesystem truth；不得用pre-syscall check声称原子关闭该窗口；
- official guarantee仍覆盖同一held root内并发创建final basename：exclusive primitive必须返回collision且不替换；
- syscall一旦开始，owner必须等待其exact physical结果，不detach；在上述并发contract内成功=`INSTALLED`，即使cancel同时到达也不得回滚或改报`CANCELLED`；
- syscall失败按exact errno映射`DESTINATION_EXISTS | PUBLISH_UNAVAILABLE`，随后清理staging；
- caller丢失回复允许形成ACK-unknown：filesystem truth决定后续inspection，不重跑、不回滚、不创建receipt。

Cancellation owner必须把CLI SIGINT或future GUI cancellation桥接到同一call-local probe。所有由`asyncio.to_thread()`或等价机制承载的copy、verify、cleanup与publish physical worker都必须由operation追踪并shield/join，不能因async caller取消而detach。Publish前在bounded file/chunk与phase safe point消费cancel后停止后续copy/verify、清理staging并返回`CANCELLED`；已经进入的单次filesystem syscall可以先返回，但不得再开始下一项。Exclusive syscall一旦提交，caller cancellation只能等待其exact result，不能提前返回；physical result决定`INSTALLED`或typed failure。不得为此增加operation总deadline或自动重试。

安装成功只表示完整目录已经发布到声明root。它不保证当前已经打开的模型请求可见，也不替代下一safe point的Runtime scan。Crash语义精确分层：

- process-live settlement：在上述same-UID namespace contract内，publisher若赢得publication，只把已验证完整stage原子发布到configured root；否则不创建或替换final（并发前已存在的destination保持原样）。Publisher自身绝不逐文件写final；rename之后用户以同一OS身份直接修改或搬走目录仍是§4的普通filesystem truth；
- SIGKILL在exclusive rename前：final absent，scanner-inert hidden staging可能残留；
- SIGKILL在exclusive rename成功后、reply前：完整final可能存在，属于ACK-unknown；filesystem truth决定后续inspection；
- power loss：本轮不承诺file/staging/root `fsync`链或crash durability；重启后以filesystem实际truth为准。

不得把namespace-atomic rename描述成断电持久性，也不得为了消除上述弱窗口增加receipt、startup repair、watcher或background recovery。若未来产品要求power-loss durability，必须另行冻结data/staging/root fsync与failure settlement。

Current production policy始终包含两个user roots，因此本轮不产生“user roots disabled”advisory。无论workspace还是user scope，成功outcome都只报告physical publication，并指引调用inspection或等待下一legal provider safe point。

### 3.4 Inspection operation与`list`/`doctor`

`inspect_local_skill_catalog`每次调用形成一个call-local immutable inspection：scope-neutral physical root bindings、aggregate disposition、effective winners以及同一次scan产生的complete typed candidate issues。它是`list`与`doctor`的唯一共同事实类型，不得为两个projection分别实现目录遍历、parser或precedence；两个独立CLI调用是两次独立filesystem observation，不承诺同一时刻，也不共享cache/generation。

每个selected root必须在scan期间持有no-follow root descriptor，并冻结filtered non-hidden direct-child membership/type/`dev`/`ino`、每个directory candidate descriptor以及`SKILL.md` presence/exact identity。Candidate bytes只能从held child descriptor relative读取；形成in-memory winners/issues并完成render后、返回`COMPLETE`前，owner必须重新打开每个root pathname并重验root binding、完整filtered direct-child evidence、held candidate identity与`SKILL.md` presence/identity。Root整体替换、same-name child替换、add/delete/type change或document identity变化一律丢弃partial facts并返回`UNAVAILABLE/DISCOVERY_RACED`，不重试、不加generation/fingerprint。Supporting resources仍按Round 9.1 progressive disclosure不预枚举；hidden staging仍不进入filtered membership。

不得新增一份与current `LocalSkillDiscovery`重复的inspection DTO。应hard-cut/扩充current carrier，使它成为Runtime composer、management service、CLI和未来UI共同消费的唯一scan result：

```text
LocalSkillDiscovery / inspection
  selected root bindings
  COMPLETE | UNAVAILABLE
  unavailable reason when applicable
  effective winner manifests
  complete closed candidate issues when COMPLETE
  bounded winner-local diagnostics when COMPLETE
  aggregate failure diagnostics when UNAVAILABLE
```

`excluded_root_kinds`不得作为第二份stored state；若nonproduction test policy确实少选root，只能在projection时以closed four-root set减去selected bindings即时派生。Current `enumerated_candidate_count`与`observed_utf8_bytes`没有production consumer，必须在同一hard cut删除；doctor不得为了保留字段而展示实现计数。

Candidate issue是closed union：

- `INVALID`：exact candidate path/root + 该candidate完整closed validity diagnostics；declared name无法合法解析时不伪造name；这些diagnostics不再复制到top-level；
- `SHADOWED`：exact candidate path/root/name + `winner_ordinal` + 仅属于该valid shadowed candidate的local INFO/authoring diagnostic codes；ordinal只索引same-carrier `skills` tuple，consumer必须验证name与precedence，不再同时保存duplicate winner reference、`skill_duplicate_name`副本或fingerprint。

Effective winner的local diagnostic ownership精确冻结：`LocalSkillManifest.authoring_diagnostic_codes`继续唯一保存body authoring subset；separate winner-local tuple只按`winner_ordinal`保存未进入manifest的`skill_host_extension_ignored | skill_unknown_extension_ignored` INFO codes，不复制authoring subset、path或manifest fields。它的自然bound来自existing winner与per-document YAML/node bounds，不新增catalog lifetime cap。

当aggregate为`UNAVAILABLE`时，不携带partial winners、candidate issues或winner-local diagnostics；只携带closed unavailable reason与描述aggregate physical/configuration failure的typed diagnostics。此前已经解析到的candidate diagnostics全部丢弃，不能与“不存在partial catalog”相冲突。

因此同一事实只存一次：validity diagnostics只在`INVALID` issue，shadow structure只在`SHADOWED` issue，winner authoring subset只在manifest，winner ignored-extension INFO只在winner-local tuple，aggregate read/enumeration/deadline/config failure只在UNAVAILABLE diagnostics。Provider/context/CLI可以从这些typed owners生成bounded展示projection，但不得把derived `SkillDiagnostic`重新塞回discovery形成第二份truth。

Current generic `LocalSkillDiscovery.diagnostics`不能继续在`COMPLETE`下混装candidate、winner与aggregate facts。实现可以把它hard-cut为按disposition封闭的字段，或让closed discovery union分别拥有COMPLETE/UNAVAILABLE payload；无论具体命名如何，constructor必须机械禁止上述重复与foreign field combination，不能只依赖调用方约定。

Inspection必须：

- public management request只接受exact resolved workspace与service-held settings context，不接受caller-supplied `PreparedLocalSkillRootPolicy`、conversation scope或subagent task id；
- 将current `PreparedLocalSkillRootPolicy` hard-cut拆成scope-neutral physical four-root policy和Runtime model-scope placement：`LocalSkillProvider`与management inspection只消费前者；ROOT/child scope只由Runtime source snapshot/placement owner添加，CLI/GUI不得伪造`ROOT/None`；
- 使用exact resolved workspace与current production four-root ordered physical policy；
- 复用`LocalSkillProvider`的aggregate discovery；
- 保持aggregate `COMPLETE | UNAVAILABLE`与no-partial-winner语义；
- 标准CLI使用current production four-root default；nonproduction injected policy未选择的root kinds只能按closed-set difference即时投影，不得枚举或虚构其中candidate inventory；
- 表达valid winner、invalid manifest diagnostic、duplicate first winner/shadowed candidate，以及root enumeration/read race、deadline、containment与aggregate bound failure；
- 保持read-only，不修改、修复、移动、删除或重新安装任何目录。

Runtime inspection只把direct child中存在`SKILL.md`的目录视为candidate；缺少`SKILL.md`的普通child继续被忽略，不产生`INVALID` issue。Standalone `validate`或`install`直接指向该目录时，缺少required document仍分别是`INVALID`/`SOURCE_INVALID`。Exact-read且standard-invalid的Runtime candidate不会使whole catalog `UNAVAILABLE`：inspection保持`COMPLETE`，该candidate被省略并产生一个`INVALID` issue，其余valid winners仍完整。只有root enumeration/read race、deadline、containment failure或aggregate bound导致无法证明complete observation时才产生whole `UNAVAILABLE`。

`COMPLETE`必须为每个standard-invalid与shadowed candidate保留exact one deterministic issue，按root precedence、lexical candidate path、issue kind排序。该tuple的自然上界来自existing four-root/direct-child admission bounds；不得继续使用current `MAX_INTERNAL_DIAGNOSTICS=128`或任何新增较小cap静默丢掉第129项candidate issue。Provider-visible/public diagnostic projection可以按其既有物理预算降级，但不能反向删减sole process-local inspection truth。Inspection disposition只有`COMPLETE(empty|nonempty) | UNAVAILABLE`；`CLEARED`只属于safe-point source successor transition，不是inspection outcome。

`pulsara skills list`是简洁projection：

- 只展示effective winners；
- 每项至少显示`name`、`description`、稳定location/source root；
- `COMPLETE + facts=()`显示为空catalog；
- `UNAVAILABLE`明确显示不可证明，不得退回partial winners。

`pulsara skills doctor`是诊断projection：

- 展示inspection中的root policy、invalid、shadowed与UNAVAILABLE原因；
- 可展示winner-local INFO/authoring guidance，但只能从上述唯一owner投影，不复制或升级为invalid；
- 对nonproduction injected policy，只解释某root kind未被选择，不声称看见其中内容；
- 不执行automatic repair。

### 3.5 Discovery join fingerprint hard cut

Current `FrozenSkillProjectionInput.discovery_semantic_fingerprint`只摘要winner与部分diagnostic字段，无法证明expanded exact inspection；例如仅invalid candidate path不同的两个`LocalSkillDiscovery`可以对象不等但digest相等。本任务必须在同一hard cut删除：

- `FrozenSkillProjectionInput.discovery_semantic_fingerprint`字段；
- `skill_discovery_semantic_fingerprint()` helper；
- 删除上述helper后失去唯一消费者的`local_skill_root_policy_identity_digest()`；
- capability composer、registry framing、context-source identity及tests对该摘要的依赖。

替代路径不得是更大的fingerprint或digest map。Existing `PreparedLocalSkillCatalogSourceSnapshot`已经同时持有exact `source_snapshot`、exact frozen `LocalSkillDiscovery`、private issuer/authenticity；`FrozenSkillProjectionInput`直接持有这两个exact object refs。Sibling compose必须验证private issuer/authenticity、`owner.source_snapshot is frozen.source_snapshot`与`owner.discovery is frozen.discovery`，不得重新摘要或接受“字段子集相等”。Registry framing只引用真实source/fact framing；需要provider lineage或context identity时从exact source snapshot、frozen facts与实际rendered semantics即时构造，不保存aggregate discovery summary。Call-local inspection issues不会把directory digest升级成capability identity。

两个CLI都可支持`--json`，但只渲染typed inspection；当前JSON shape不供GUI/Web解析，也不成为第二个catalog contract。

### 3.6 CLI availability、workspace与distribution

`pyproject.toml`中的现有console entry point继续是唯一launcher definition：

```toml
[project.scripts]
pulsara = "pulsara_agent.cli:main"
```

当前production存在一个必须在本任务一并收口的packaging bug：Pulsara production modules直接`import httpx`，但`pyproject.toml`没有声明direct `httpx`dependency；隔离wheel/editable tool环境因此可以在CLI import阶段失败。本任务必须把实际使用的`httpx`声明为direct runtime dependency并更新lock；当前兼容基线为`httpx>=0.28.1,<1`，不得依赖其他包偶然transitively安装它。

Global CLI contract：

- development convenience：`uv tool install --editable <pulsara-repository>`；
- 正式发行：从non-editable wheel/package执行`uv tool install pulsara-agent`，或由Pulsara Desktop installer安装同版本、用户级且位于PATH的launcher；
- `pulsara`必须可从任意非源码cwd执行，不依赖repository cwd、repository `.venv`、`PYTHONPATH`、source-tree symlink或`uv run`；
- 正式安装是user-global tool install，不要求root/system-wide Python；installer必须保证其user bin已进入Pulsara terminal的PATH，或提供明确、一次性的PATH setup guidance；
- Desktop GUI不依赖global CLI，但GUI调用的application service与global launcher必须由同一Pulsara package version构建；
- bundled Skill在`pulsara`不可解析时只报告明确的发行/安装问题，不回退到repository path、`.venv/bin/pulsara`、`python -m`或`uv run`。

CLI path/workspace contract：

- `skills validate <path>`：source path为absolute或相对invocation cwd；没有workspace参数；
- `skills install --scope workspace <path>`：接受`--workspace PATH`，默认invocation cwd，并复用existing project workspace resolver；source相对path仍相对invocation cwd，而不是相对resolved workspace；
- `skills install --scope user <path>`：destination只由shared absolute Pulsara-home resolution决定，与cwd和workspace无关；relative configured `PULSARA_HOME`返回typed configuration failure，绝不相对cwd展开；source相对path仍相对invocation cwd；`--scope user`与显式`--workspace`组合是CLI usage error，不得静默忽略该参数；
- `skills list`与`skills doctor`：接受`--workspace PATH`，默认invocation cwd，复用existing env-file/settings prefix处理和shared Pulsara-home resolver并按current production four-root default构造policy；invalid user-home configuration返回whole catalog unavailable，不退回workspace-only inspection；
- standalone CLI不声称读取某个正在运行Host的process-local policy；相同package version、workspace与settings只保证使用相同product rules，filesystem仍可能在两次调用间变化。

Distribution smoke必须从不属于Pulsara source tree的临时cwd执行，并且清除repository `PYTHONPATH`/virtualenv影响：

```text
build non-editable wheel
→ install wheel into isolated tool environment
→ pulsara --version / --help
→ pulsara skills validate
→ pulsara skills install --scope workspace --workspace <temp-workspace>
→ pulsara skills list / doctor
```

Smoke还必须检查wheel包含两个最终bundled Skill及仍被其引用的resources，并明确断言已删除的旧private installer scripts不在wheel中。Editable smoke只能补充开发体验，不能替代wheel/dependency-closure证明。

## 4. 用户直接复制、编辑或删除

### 4.1 直接 filesystem 操作始终合法

用户可以不经过CLI，直接向当前policy中的任一既有root复制、编辑或删除Skill。Runtime必须继续以filesystem scan为truth：

- valid完整目录在下一次合法provider safe point进入catalog successor；
- exact-read但standard-invalid的candidate被确定性省略并产生typed diagnostic，aggregate inspection仍为`COMPLETE`；剩余完整winner set决定catalog successor是`VALUE`还是`CLEARED`；
- 只有enumeration/read race、deadline、containment或aggregate bound使完整观察不可证明时，whole aggregate才为`UNAVAILABLE`；
- 修改或删除在下一safe point按完整winner set形成相应`VALUE | CLEARED | UNAVAILABLE` transition；
- 已经打开的provider request不被追溯修改；
- same epoch中SYSTEM与tools保持byte-identical，messages只追加suffix；
- 不新增rebase boundary、loaded set或install confirmation。

### 4.2 直接复制期间的bounded observation

现有Round 9.1语义保持不变：

- child目录尚无`SKILL.md`：本次scan忽略，未来safe point重扫；
- exact `SKILL.md`成功读取但standard-invalid：只忽略该candidate并记录typed issue/diagnostic，whole scan保持`COMPLETE`；
- enumeration后candidate消失、读取失败或scan deadline耗尽：整个aggregate snapshot为`UNAVAILABLE/DISCOVERY_RACED`，不得发布partial catalog；
- 同名Skill按既有root precedence选择first winner；
- 一个合法`SKILL.md`引用尚未复制完成的resource时，Skill可能已经进入catalog；随后ordinary file read可以得到missing。这是普通filesystem弱一致性，不建立resource receipt或依赖闭包。

文档应建议手工安装者：

- 优先使用`pulsara skills install`；或
- 在root中先复制到点号开头的临时目录，完成后以平台exclusive no-replace primitive发布为最终skill name；或
- 先写resources，最后写`SKILL.md`。

这些只是降低partial-visibility窗口的手工建议，不提供official collision settlement。普通`rename`可能覆盖并发出现的空destination；用户无法使用exclusive primitive时必须先确认目标不存在并接受手工操作的弱竞态语义。它们不是Runtime admission marker，不得要求`.installed`文件、manifest receipt、watcher或durable transaction。

### 4.3 Safe-point语义

所有CLI与bundled Skill必须使用统一表述：

> Filesystem changes are discovered at the next legal provider safe point. They do not retroactively change an already-open model request.

不得继续声称必须“开始一个新的Pulsara turn”才能发现。下一safe point可能位于同一run的完整tool batch之后，也可能是后续用户turn。

## 5. 两个 bundled Skill 的 hard cut

### 5.1 `pulsara-skill-creator`

保留该Skill，更新其说明：

- Skill是portable、untrusted guidance，不授予能力；
- `description`同时表达做什么与何时使用，是catalog routing contract；
- frontmatter支持required `name`、`description`与optional `license`、`compatibility`、bounded string-to-string `metadata`；optional字段不强制创建；
- 编辑既有Skill时保留合法字段与仍有消费者的resources；
- `references/`、`scripts/`、`assets/`只在有真实用途时创建；root `SKILL.md`负责progressive routing；
- 不自动添加`agents/openai.yaml`、Pulsara metadata、tool schema、permission、Hook、MCP或Plugin dependency；
- 若需求是Skill + MCP/Hook/preset的组合包，说明应创建未来Plugin，而不是污染loose Skill frontmatter；
- 完成后必须调用global `pulsara skills validate <path>`并报告真实结果；launcher不可解析时报告Pulsara发行/安装问题，不回退repo `.venv`或`uv run`。

该Skill不得自带或生成另一套validator。

### 5.2 `pulsara-skill-installer`

保留该Skill的产品角色，但删除其私有安装机制：

- 完整删除bundled目录中的`skill_utils.py`、`install-local-skill.py`与`list-installed-skills.py`，并删除所有import、SKILL.md引用与packaged copies；不得仅“停止使用”后继续发行dual path；
- 删除宽松YAML fallback、私有name regex与直接final-directory `copytree`路径；
- `SKILL.md`只指导Agent确定source与`workspace|user` scope，然后调用正式`pulsara skills install`；
- workspace scope必须传递用户目标workspace（默认当前workspace，必要时显式`--workspace`）；user destination不得从cwd猜测；
- 安装后调用`pulsara skills list`确认effective winner；若未生效、被shadow、invalid或aggregate unavailable，再调用`pulsara skills doctor`；
- 用户没有明确scope时先询问，不默认为user或workspace；
- 不调用raw `cp`模拟官方安装，不建议force overwrite；
- 正确说明下一safe point discovery。
- global `pulsara`不可解析时只报告明确安装问题，不搜索源码仓库或执行`uv run`。

`references/directory-contract.md`若保留，必须按本规格重写为workspace/user、filesystem truth、exclusive publish与next-safe-point语义；若不再提供独立于`SKILL.md`的真实指导价值，则完整删除。Wheel smoke必须证明上述三个private scripts不存在。

普通Skill安装仍通过现有terminal与permission owner执行。Bundled Skill不绕过permission，也不需要新增provider tool。

### 5.3 Bundled distribution owner不变

`src/pulsara_agent/capability/bundled_skills.py`继续独立负责Pulsara随包分发的两个bundled Skill：

- current bundled manifest/provenance、用户修改保护、删除保护与reset语义不因本任务改变；
- bundled sync/status/reset必须改用§3.1 shared Pulsara-home resolver；relative configuration typed-fail且不写入cwd-relative目录；
- bundled sync不能被loose `skills install`替代；
- loose installer不能读取或写入bundled sync manifest来宣称ownership。

Existing `pulsara skills sync-bundled`、`pulsara skills status`与`pulsara skills reset`命令必须保留，继续只是bundled distribution owner的CLI projections。新增的`validate/install/list/doctor`是loose management projections；“四个初始CLI”不是替换整个`skills` namespace，也不得把bundled status/reset/sync路由到三个loose management operations。

## 6. 推荐实现拓扑

目标拓扑：

```text
bundled Skill / human terminal
  → CLI adapter ───────────────────────────────┐

Desktop GUI
  → in-process or local Host adapter ──────────┤

future Web UI
  → authenticated local Host endpoint ────────┤
                                               ▼
                              LocalSkillManagementService
                                ├─ validate source
                                ├─ install loose Skill
                                └─ inspect effective catalog
                                     │
                                     ├─ production Skill document validator
                                     ├─ existing LocalSkillProvider
                                     └─ narrow atomic directory publisher

pulsara-skill-creator
  → terminal: pulsara skills validate

pulsara-skill-installer
  → terminal: pulsara skills install/list/doctor
```

允许从`local_skills.py`中提取或公开一个纯Skill document validation入口，但必须保证Runtime scanner与management service最终调用同一实现。不得让CLI或UI import一个复制出来的parser，也不得为了复用而把filesystem write authority塞进`LocalSkillProvider`。

当前service只接收resolved local directory。未来浏览器upload、archive或remote acquisition若被单独批准，应由对应transport/acquisition adapter先完成其自己的安全边界，再提供一个临时本地source；不得让atomic publisher同时承担网络、archive extraction、marketplace或credential authority。

推荐owner划分：

- production document parser：纯解析/校验，无写权限；
- physical root-policy owner：从resolved workspace/settings签发scope-neutral four-root bindings；不携带conversation/subagent装饰；
- `LocalSkillProvider`：唯一physical four-root scan、precedence与aggregate availability owner；Runtime model scope由source snapshot/placement owner另行加入；
- `LocalSkillManagementService`：三个typed operation的唯一application orchestration owner，不参与provider compilation；
- atomic loose-directory publisher：唯一safe root preparation、source observation、staging/copy/publish owner，只被installation operation调用；
- cancellation probe/bridge：call-local operation control，由CLI SIGINT或future GUI signal驱动，不拥有catalog或publication truth；
- CLI、Desktop GUI、Web/Host endpoint：输入与展示adapter，不拥有业务truth；
- bundled Skill：Agent guidance only。

## 7. Failure semantics

| 失败 | validation disposition | installation disposition | inspection / projection |
|---|---|---|---|
| source directory missing/non-directory/unreadable | `UNAVAILABLE + closed reason` | `SOURCE_UNAVAILABLE`，无destination | n/a |
| relative/invalid `PULSARA_HOME` | validation不读取user root | user install=`TARGET_CONFIGURATION_UNAVAILABLE`；workspace install不受影响 | Runtime/list/doctor whole `UNAVAILABLE/USER_HOME_CONFIGURATION_INVALID`，bundled command typed-fail |
| required `SKILL.md` missing | `INVALID + typed diagnostic` | `SOURCE_INVALID`，无staging/final destination | Runtime inspection对missing-document child目录继续忽略，不产生candidate/issue |
| exact-read `SKILL.md` standard-invalid | `INVALID + typed diagnostics` | `SOURCE_INVALID`，无staging/final destination | `COMPLETE` scan中exact one INVALID issue |
| source observation membership/identity变化 | `UNAVAILABLE` | `SOURCE_RACED`，不重试 | Runtime direct scan race为whole UNAVAILABLE |
| source read/acquisition/final-revalidation I/O失败且identity未变 | `UNAVAILABLE` | `SOURCE_UNAVAILABLE`，cleanup stage | n/a |
| source/staged tree含不支持entry | manifest可单独VALID | `UNSUPPORTED_ENTRY`，无final destination | Runtime按ordinary discovery/read规则 |
| source含reserved managed control | manifest可单独VALID | `RESERVED_CONTROL`，无final destination | bundled/plugin owner语义不被loose installer继承 |
| target root preparation遇symlink/non-directory/permission failure | n/a | staging前=`STAGING_UNAVAILABLE`；publish binding revalidation=`PUBLISH_UNAVAILABLE/ROOT_BINDING_RACED` | 不follow、不创建partial final |
| destination已存在或exclusive syscall报告collision | n/a | `DESTINATION_EXISTS`，绝不覆盖 | future inspection显示filesystem current winner |
| staging create/write/readback/mode/verification失败且source仍exact | n/a | `STAGING_UNAVAILABLE`，无final destination | n/a |
| source与stage failure同时可见 | n/a | final source revalidation后按`SOURCE_RACED > SOURCE_UNAVAILABLE > STAGING_UNAVAILABLE` | 不重试 |
| platform无exclusive primitive/root binding race/publish I/O失败 | n/a | `PUBLISH_UNAVAILABLE + closed reason` | next scan观察filesystem truth |
| publish前cancel且cleanup成功 | n/a | `CANCELLED`，无final destination | n/a |
| exclusive syscall开始后cancel | n/a | 等待physical result；成功=`INSTALLED`，失败=exact typed failure | filesystem truth决定next inspection |
| process-live staging cleanup失败 | n/a | `CLEANUP_UNAVAILABLE + attempted hidden path + location status + prior reason` | 不建立repair job，不声称lexical path仍存在 |
| final binding cut后同UID actor替换stage或搬走root | n/a | out-of-contract namespace interference；syscall success不保证configured location/verified stage identity | 后续inspection只报告filesystem truth |
| SIGKILL before exclusive rename | n/a | 无reply/receipt；final absent，hidden staging可残留 | scanner忽略dot directory |
| SIGKILL after successful rename、reply前 | n/a | ACK-unknown；完整final可能存在 | filesystem truth决定next inspection |
| power loss | n/a | 不承诺fsync/crash durability或outcome | reboot后的filesystem truth决定inspection |
| aggregate inspection race | n/a | 不影响已完成physical publication | whole catalog `UNAVAILABLE`，无partial winners |

所有失败都不得触发无限retry、deadline刷新、fallback parser、partial catalog或automatic overwrite。CLI exit status只由上述typed disposition映射。

## 8. Continuity、durability 与架构减法

本任务必须保持：

- same epoch SYSTEM/tools不变、messages append-only suffix；
- Skill filesystem变化只在既有safe-point refresh机制产生catalog successor；
- Round 9 capability registry仍只有一个aggregate `LOCAL_SKILL_CATALOG` source；
- PostgreSQL schema、Committed/Live event、subject、guard、relation与durable job计数不增长；
- 无installation receipt、history、checkpoint、replay、repair、watcher、generation、lease或cross-Host recovery；
- 删除不完整的aggregate `discovery_semantic_fingerprint` join；无替代discovery digest、digest→object registry、directory fingerprint identity或Plugin-private trust状态；
- 无新的总history、Skill lifetime、resource count或bundle size cap；现有SKILL.md/YAML/catalog bounds继续由Round 9.1唯一拥有；
- installation operation失败清理是当前process filesystem settlement，不是durable workflow。

本规格直接以current post-hard-cut architecture oracle为authority，不引用Round 9.1文档中的历史数字：

```text
Committed / Live / Subject / Guard / Relation / Durable Job
29 / 24 / 11 / 1 / 25 / 0

HookEventType public vocabulary = 11
```

本任务各维均保持不增长；management disposition、diagnostic enum与process-local inspection issue都不是Committed/Live/Hook event。

## 9. 测试与完成条件

### 9.1 行为覆盖

实现至少证明：

- `validate`与Runtime scanner对valid/invalid UTF-8、frontmatter、duplicate key、anchor/alias/tag/multi-document、name、description、optional fields及bounds给出相同判定；
- root-neutral parsed document经Runtime placement enrichment后与原production `LocalSkillManifest`逐字段一致；standalone/Runtime使用actual candidate basename，hidden stage使用frozen final basename而不是随机stage basename；validation与staged install不伪造root/location/provenance；
- `SkillDiagnosticCode`覆盖全部production producers与本任务新增configuration/missing-document codes；catalog/active render overbound分别产生`skill_catalog_projection_bound_exceeded`/`skill_projection_overbound`并映射各自unavailable reason；context adapter做exhaustive enum mapping，删除无producer旧字符串/prefix分支；CLI不按exception/stderr字符串判断业务分支；
- 不存在PyYAML缺失时的第二fallback contract；
- workspace/user安装分别落到exact Pulsara-owned root；fresh workspace/home root preparation逐component证明no-follow、existing directory、symlink、non-directory、permission与binding race语义；
- arbitrary destination、`.agents/skills` managed write、overwrite与reserved provenance被拒绝；
- staging目录不被Round 9 scanner枚举，成功publish后下一scan可见；
- source/target overlap、symlink/special entry、filter domain与portable copy domain逐项锁定；regular file `0o600|exec`、directory/control-root `0o700`不受umask影响，existing roots不被擅自chmod；
- source descriptor在第一次validation前取得；exact frozen membership/identity/SKILL.md bytes贯穿validation、copy、final source revalidation与stage verification。V1→稳定V2 replacement、add/delete/type/mode/bytes race只观察一次并返回`SOURCE_RACED`，不重试；call-local digest不逃出publisher；
- validate、CLI install与typed service在Darwin对真实`/tmp`、`/var`source成功，并证明final source symlink与source内部symlink仍不被follow；shared source-binding seam无第二套alias resolver；
- arbitrary-size supporting resource的copy/compare/digest只申请固定chunk，large-resource probe观察不到随resource增长的read allocation；stage创建后注入`MemoryError`返回typed `STAGING_UNAVAILABLE`并清除hidden stage，cleanup allocation failure则诚实返回`CLEANUP_UNAVAILABLE`；不增加总bundle/resource cap；
- source `read()` EIO、source identity race、stage `write()` ENOSPC/readback/mode failure与同时发生分支逐项锁定`SOURCE_UNAVAILABLE | SOURCE_RACED | STAGING_UNAVAILABLE`及其固定优先级；cleanup wrapper保留prior typed disposition；
- Darwin exclusive publish竞态证明既有空/nonempty destination都不被替换；Linux adapter在Linux CI使用等价test；无verified primitive的平台返回`PUBLISH_UNAVAILABLE`且不fallback普通rename；
- Darwin/Linux platform test同时复现root搬移与stage-basename replacement，证明primitive不提供stage-inode/root-path conditional；测试只断言本规格明确保留的final-name no-replace保证，并检查out-of-contract same-UID interference不会被文档/API误报为exact path或verified-stage guarantee；cleanup无法定位时只返回attempted path + unresolved status；
- CLI SIGINT/future GUI cancel经同一narrow probe进入service；cancel-before-publish、cancel-during-exclusive-syscall、cleanup success/failure逐分支证明linearization，thread worker不detach；process-live路径无final partial directory；
- SIGKILL-before-rename允许hidden staging且final absent，SIGKILL-after-success允许完整final/ACK-unknown；power-loss test不得假装本轮提供未规格化的fsync durability；
- expanded sole `LocalSkillDiscovery` carrier与production four-root precedence、`COMPLETE(empty|nonempty) | UNAVAILABLE`逐项一致，不新增重复inspection DTO，inspection本身不存在`CLEARED`；
- root整体替换（old `alpha` → new `alpha + extra`）、same-root membership add/delete、candidate replace与`SKILL.md` identity change均不能产生mixed `COMPLETE`；held root/candidate descriptors与final complete membership revalidation使它们统一成为whole `UNAVAILABLE/DISCOVERY_RACED`；
- `list`与`doctor`只投影该typed inspection，不各自实现scan/parser/precedence；
- validity、shadow structure、winner authoring、winner ignored-extension INFO与aggregate-unavailable diagnostics逐类只存在于§3.4唯一owner；provider/context/doctor derived projection不把同一diagnostic写回top-level第二份；
- `doctor`用typed issues解释invalid、duplicate shadow exact `winner_ordinal`与aggregate race；missing-document普通child被Runtime inspection忽略；每个invalid/shadowed candidate都有按root/path排序的exact one issue，即使超过旧128 diagnostics门槛也不被静默截断；excluded root只按closed-set difference即时投影，不stored、不虚构candidate inventory；
- standard-invalid candidate保持whole inspection COMPLETE并保留其他valid winners；
- scope-neutral physical root policy由service从resolved workspace/settings构造；management/CLI不伪造conversation或subagent scope，Runtime placement仍正确绑定ROOT/child；无消费者的candidate/byte counters被删除；
- aggregate `discovery_semantic_fingerprint`字段、两个helper（包括随之无消费者的root-policy digest）、registry/context/planner consumers与tests完整删除；仅diagnostic path不同的discovery不能再通过摘要碰撞加入sibling，且没有替代fingerprint/map；
- 用户直接copy、modify、delete仍在下一safe point生效；
- direct copy race不发布partial catalog；
- 两个bundled Skill只调用正式CLI，不再包含私有parser/copy/list path；
- existing `sync-bundled/status/reset` CLI及bundled distribution semantics保留，不并入loose management service；
- unset/empty、`~/configured-home`与relative `PULSARA_HOME`在两个不同cwd逐项验证shared resolver：前两者得到同一absolute root，relative value在Runtime/inspection/bundled/user-install中得到相同typed-invalid；workspace install与standalone validate不受无关invalid user-home配置阻断；
- management service不依赖CLI parser/stdout/JSON类型，可由in-process或Host API adapter直接调用；
- architecture guard证明management module不依赖CLI类型，CLI只向内调用service；未来GUI/Web adapter同样不得通过CLI subprocess或stdout parsing复用产品逻辑；
- bundled sync/provenance retained behavior不变；
- `pyproject.toml`声明direct compatible `httpx`dependency，isolated wheel import不依赖transitive accident；
- wheel中包含最终两个bundled Skill及其仍被引用的resources，且不包含三个deleted private installer scripts；
- 从非源码cwd调用global launcher时不依赖repo `.venv`、`PYTHONPATH`、source tree或`uv run`；workspace/user path resolution符合§3.6，`--scope user --workspace`为usage error；
- same-epoch continuity retained tests保持SYSTEM/tools exact与messages suffix-only。

### 9.2 最终验证

编码期间可运行targeted tests；合入前统一报告：

- retained full pytest；
- PostgreSQL全量（确认无schema/event/oracle增长）；
- Ruff、compileall、`uv lock --check`与`git diff --check`；
- 一个纯本地service + CLI dogfood：create → validate → install workspace → inspect/list → direct-copy duplicate/invalid → inspect/doctor → cleanup；
- non-editable wheel build + isolated tool install + arbitrary non-source cwd smoke；editable tool smoke只能作为补充；
- 新增skip/xfail为0。

本任务不需要真实provider、网络下载或Plugin dogfood；若执行一个本地Host safe-point轨迹，应只用于证明catalog refresh与continuity，不得把它升级为新的activation evidence authority。

## 10. Definition of Done

只有同时满足以下条件，本补充才算完成：

1. 三个typed management operations存在，并共享唯一root-neutral production parser、scope-neutral physical root policy、sole enriched discovery/inspection carrier与一个atomic publisher。
2. 四个CLI作为初始adapter存在；management service不依赖CLI，未来Desktop GUI/local Host API可直接复用。
3. GUI/Web不得以CLI subprocess、stdout或当前`--json`shape作为产品IPC。
4. 官方install从同一descriptor-relative frozen source observation完成第一次validation、constant-memory streaming copy与verification，按source/stage physical owner（包括stage后allocation failure）返回typed failure并cleanup，安全准备首次Pulsara-owned root，并使用隐藏staging与platform-exclusive no-overwrite publish；不支持的platform typed unavailable，绝不fallback普通rename。
5. Publication并发边界诚实：exclusive primitive只保证held-root内final-name no-replace；same-UID final-cut后root/stage namespace interference明确out-of-contract，`INSTALLED`不是reply-time path-liveness证明，cleanup只能携带attempted path与location status。
6. Cancel/cleanup/crash linearization闭合：process-live publisher不逐文件写final；cancel worker不detach；SIGKILL before/after rename与power-loss弱保证分别陈述，且无recovery machinery。
7. 用户直接复制仍是合法、一等的filesystem路径，不依赖CLI receipt或marker；standard-invalid与scan-unavailable语义不混淆。
8. `list`表示effective winners，`doctor`完整解释每个invalid/shadowed candidate与不可用原因；diagnostic各有唯一owner，两者只投影sole call-local inspection carrier；held root/candidate descriptor与final complete membership fence禁止mixed COMPLETE，且不建立第二套catalog authority、128项静默截断或跨调用cache。
9. Shared Pulsara-home resolver被Runtime、bundled owner、management与CLI共同使用；relative value不依赖cwd并typed-fail，workspace install/validation保持独立；validate/install另共用Darwin-system-alias-aware、final/interior no-follow的source-binding seam。
10. 两个现有bundled Skill已改用正式loose CLI，重复parser/copy/list scripts从源码与wheel中完整删除；existing bundled `sync-bundled/status/reset`命令继续保留。
11. Direct `httpx`dependency closure、non-editable wheel、user-global launcher、arbitrary cwd与workspace/user resolution全部通过。
12. Loose Skill不生成bundled/plugin provenance；bundled sync retained不变。
13. Round 9.3边界清晰，尚未实现Plugin installer、Web upload或远端marketplace。
14. `discovery_semantic_fingerprint`不完整join已删除且无替代digest/map；same-epoch continuity、long-horizon availability、small durability与`29 / 24 / 11 / 1 / 25 / 0` oracle（Hook vocabulary另为11）全部保持。
15. active specs、代码、测试与用户提示只描述这一条新真相，不保留dual path或兼容alias。

## 11. Activation evidence（2026-08-24）

三项合入前P1已经在单一路径中闭合：inspection持有root/candidate descriptors并执行最终完整membership fence；validate/install共享Darwin system-alias-aware source binding；publisher对任意resource执行fixed-chunk streaming，并把stage建立后的allocation failure纳入typed arbitration与cleanup。随后重新执行全部§9.2 gate。没有新增PostgreSQL schema/event/subject/guard/relation/job，没有新增skip/xfail，也没有执行网络、真实provider或Plugin dogfood。

- retained full：`.venv/bin/pytest -q -m 'not postgres and not retrieval_live'` → `759 passed, 223 deselected in 45.21s`；
- PostgreSQL全量：`.venv/bin/pytest -q -m postgres` → `223 passed, 759 deselected in 113.98s`；
- Ruff：`.venv/bin/ruff check .` → `All checks passed!`；modified/new Python format check → `26 files already formatted`；
- compileall：`.venv/bin/python -m compileall -q src tests tools` → exit 0；
- lock：`uv lock --check` → `Resolved 64 packages`、exit 0；
- protocol：`.venv/bin/python tools/generate_terminal_protocol_contract.py --check` → exit 0；
- whitespace：`git diff --check`与全部untracked file whitespace check → exit 0；
- tests diff audit：新增`skip/skipif/xfail/pytest.skip` = 0；
- non-editable wheel与isolated tool install：构建`pulsara_agent-0.1.0-py3-none-any.whl`（242 files），三个仍被引用的bundled resources全部存在，三个已删除private scripts和`.pyc`均不存在；isolated tool environment从wheel安装51个packages并直接包含`httpx==0.28.1`；
- arbitrary non-source cwd launcher：清空`PYTHONPATH`与`VIRTUAL_ENV`，仅调用isolated tool launcher，得到version `0.1.0`以及`validate=VALID`、`install=INSTALLED`、`list=COMPLETE`、`doctor=COMPLETE`；package origin为isolated tool `site-packages`而非source tree；
- 纯本地service + CLI dogfood：typed service与CLI各完成一次`VALID → INSTALLED`；initial inspection/list均`COMPLETE`且目标隔离inspection有2个effective Skills；直接copy一个duplicate并放入一个standard-invalid candidate后，inspection仍`COMPLETE`、effective Skills仍为2、candidate issues精确为`INVALID + SHADOWED`；临时root退出时已清理；
- architecture/oracle retained guards随full pytest通过，冻结`29 / 24 / 11 / 1 / 25 / 0`，`HookEventType = 11`。

Activation blockers：none。
