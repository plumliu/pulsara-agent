## 总体判断：NOT READY

修订稿已经真正移除了 Plugin 阻塞：当前轮只有 bundled 与 loose 两个 required producers，可在完全没有 Plugin subsystem 的条件下独立实现、测试和激活。Future 9.3 也只是约束将来的接缝，没有预埋第三 input、origin arm、diagnostic 或 borrow path。

但当前仍有 3 个 P1 合同缺口：four-root 物理别名没有 typed outcome；deadline 与 `UNAVAILABLE_MINIMAL` 的 settlement 不可同时实现；catalog overbound 时 inspection 丢弃 winner map，却又要求 active 消费该 map。因此暂时不能直接交给 coding agent。

## 我理解的 happy path 与 future seam

进程先固定 installed package 内 filesystem-backed bundled root；safe point 用一个 deadline 依次观察 bundled、loose；两个 producer 提交全部 valid candidates/invalid issues；central resolver 按四个 loose tiers 高于 bundled default 的五层顺序一次选 winner；完整结果进入同一 capability facts、catalog、activation、ordinary `read_file` 和 retained proof 路径。同名 loose 被删除后，下一个 complete safe point 自然回退 bundled。

未来 9.3 只有在真实 Plugin lifecycle owner 提供 immutable、closed `COMPLETE | UNAVAILABLE` snapshot 和资源生命周期后，才 hard-cut 增加第三 input/origin arm，并把 Plugin tiers 插在 loose 与 bundled 之间。不得物化到 four roots，也不得建立 Plugin-private parser/catalog/activation/read engine。该 seam 已足以约束未来实现。

## P1 findings

### P1 — Four-root 物理别名没有 closed outcome

- 文档定位：[§1.3](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:154)、[§3.5](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:346)、[§7.1](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:672)。
- 当前代码证据：root policy 强制物理 path 唯一，[local_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skills.py:263)；四个 path 经 `resolve()` 后构造，[local_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skills.py:525)。
- 失败路径：以用户 home 作为 workspace 时，两个 workspace roots 分别与两个 user roots 重合。当前代码直接抛出 `ValueError: Skill root bindings are not ordered and unique`，无法形成 `LOOSE_CONFIGURATION_INVALID`。若简单允许重复扫描，同一物理文件会携带两个 origins，并可能在两次观察间产生 mixed bytes。规格又只允许新增 bundled diagnostics，没有可诚实表达 loose root alias 的 diagnostic。
- 最小修订：冻结 physical-alias 判定和 outcome。建议相同 normalized path 或打开后相同 dev/inode 时，整个 loose batch 以 `LOOSE_CONFIGURATION_INVALID` + 新的 exact `LOOSE_ROOT_ALIAS` diagnostic fail closed；inspection 仍携带可解释的 prepared policy。补测 workspace 为 home、`PULSARA_HOME` 与 `~/.agents` alias、symlink/inode alias。
- Durable machinery：不需要。

### P1 — Deadline cause、outer dispatch abort 与 Runtime successor 不能同时满足

- 文档定位：[cause union](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:368)、[producer settlement](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:494)、[one composition cut](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:569)、[UNAVAILABLE behavior](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:589)。
- 当前代码证据：dispatch 把 outer planning deadline 原样交给 Skill scan，返回后立即重新检查；超时会变成全局 `DEADLINE_EXPIRED`，[provider_dispatch.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/provider_dispatch.py:632)。当前 scanner 虽会把超时转成 discovery unavailable，[local_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skills.py:592)，但 caller 随即 abort，根本到不了 context-source append。
- 失败路径：
  - bundled 耗尽 deadline，loose 按规格返回 `LOOSE_DEADLINE_EXPIRED`；outer check 随即终止，无法追加规格要求的 `UNAVAILABLE_MINIMAL`。
  - 两个 producer 都 COMPLETE，但 deadline 在 central resolve/render 期间耗尽；resolution union 只有 winner/catalog overbound，没有合法 deadline outcome。
  - 已知 configuration/resource failure 与同时到期时，每 producer 选择哪个唯一 cause 也没有 arbitration rule。
- 最小修订：二选一并冻结：
  - 定义 caller-owned Skill subdeadline及明确 settlement headroom，增加 `RESOLUTION_DEADLINE_EXPIRED`，保证 source snapshot 在 outer deadline 前可安装；或
  - 定义 `ABORTED_BY_OWNER_DEADLINE` 为 inspection/source observation 之外的 safe-point abort，并把“总是 append”限定为已经形成并进入 install 的 UNAVAILABLE snapshot。

  同时规定 producer 内 configuration/resource/race/deadline 的优先级。不要刷新 deadline。
- Durable machinery：不需要；这是现有 per-operation deadline 的 settlement contract，不是总时长 cap。

### P1 — `CATALOG_PROJECTION_OVERBOUND` 后没有 active 可消费的 winner truth

- 文档定位：[UNAVAILABLE inspection 不含 winners](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:346)、[resolution cause](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:384)、[source 无 facts](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:591)、[active 始终消费 map](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:600)。
- 当前代码证据：当前 preflight overbound 会把 discovery 整体转为 unavailable，[local_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skills.py:720)；capability composer 只为 COMPLETE discovery 构造 facts，[capability.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/capability.py:103)；resolver 对 unavailable 同时令 catalog 与 active unavailable，[resolver.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/resolver.py:75)。
- 失败路径：合法文档可以实际触发 catalog overbound。此时 public inspection、source snapshot和registry都没有 winners/facts，但 §6.2 又要求 active 从“同一 frozen winner map”激活。保留隐藏 map 会产生第二 authority；直接激活则违反 unavailable 无 facts；丢弃则违反“始终消费”。
- 最小修订：最小且一致的选择是：winner map 只在 COMPLETE inspection 中存在；任一 effective inspection unavailable 时 active 也返回 `ACTIVE_SELECTION_UNAVAILABLE`。若产品确实要在 catalog render 失败时仍允许 active，则必须引入一个单一 typed `ResolvedSkillWinnerSet`，让 inspection、facts和active共同引用，并同步修订“UNAVAILABLE 无 winners/facts”和唯一 truth 不变量。
- Durable machinery：不需要。

## P2 findings

### P2 — Retained historical catalog 的 causal join 仍不够机械

- 文档定位：[§6.5](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:632)。
- 当前代码证据：当前实现按 compaction 时的 `discovery.skills` 找 manifest，[retained_skill.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/compaction/retained_skill.py:105)，inherited item 也重新 join current manifest，[retained_skill.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/compaction/retained_skill.py:332)。现有 epoch view 已保留全部 installed messages及平行 placements，[continuity.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/model_input/continuity.py:351)，所以无需新证据机制。
- 失败路径：assistant request 基于 catalog A 发出 read；same-turn follow-up 可在 ToolResult 前安装 catalog B。若实现者取 current source head或“ToolResult 前最新”，会错误使用 B，拒绝合法历史 read或关联错误 row。步骤 7 也没有说明 placement validator 的 expected basename。
- 最小修订：冻结算法：唯一定位 assistant tool-request canonical item及其 installed message ordinal；只在该 ordinal 之前选择最高 ordinal 的 `SKILL_CATALOG` observation；要求它为 canonical FULL/VALUE observation；解析唯一 row；read request和result path均 exact join；placement basename从 historical row location 的 `SKILL.md` parent派生。明确拒绝 CLEARED、UNAVAILABLE、重复 row或不可解码 body。
- Durable machinery：不需要；现有 canonical items、messages、placements和ToolResult decisions足够，禁止 receipt/history registry。

### P2 — Unlimited inspection issues 会碰撞现有 Runtime 64-diagnostic bound

- 文档定位：[COMPLETE 不截断 issues](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:401)、[issues 无额外 cap](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:563)、[diagnostic ownership](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:694)。
- 当前代码证据：每个 candidate diagnostic 当前都会进入 projection，[local_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skills.py:1054)，context adapter 一对一转换，[context_sources.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/context_sources.py:2067)，compiler 在 64 条 diagnostics 后整体拒绝，[compiler.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/model_input/compiler.py:2138)。
- 失败路径：65 个 ordinary invalid loose candidates仍应得到 COMPLETE inspection，但沿当前 adapter 会让整个 Runtime call以 `SOURCE_PHYSICAL_BOUND_EXCEEDED`失败。给 inspection 加 64/128 cap又违反本规格和 long-horizon规则。
- 最小修订：明确 inspection/doctor保留全部 path-specific issues；Runtime context diagnostics只做稳定的语义集合投影，例如每个 `(public diagnostic code, source kind, severity)`至多一项，不携带每条 candidate path。现有 compiler 64 是消费者的 per-operation bound，不应变成新的 Skill issue cap。
- Durable machinery：不需要。

### P2 — Bundled build/runtime inventory 的枚举集合不一致

- 文档定位：[§4.3](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:460)，尤其 build gate [line 479](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:479) 与 runtime [line 488](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:488)。
- 当前代码证据：当前 wheel配置打包整个 Python package，[pyproject.toml](/Users/plumliu/Desktop/python_workspace/pulsara_agent/pyproject.toml:28)；现有 bundled reader仍是待删除的 `as_file()`/copy owner，而没有可继承的 exact inventory observer。
- 失败路径：额外的非隐藏 `README` 或不含 `SKILL.md` 的目录按 build gate 文义可被过滤掉并通过，但 runtime 的“exact official children/extra child”可以将其判为 unavailable。两个实现者会得到不同 wheel acceptance。
- 最小修订：定义一个共同枚举集合。建议 build/runtime均枚举所有 non-dot immediate entries，名称必须与 tuple exact 相等，并要求每项为 no-follow directory且含唯一 regular `SKILL.md`；若要允许其他 entries，则必须在两处冻结完全相同的过滤规则。
- Durable machinery：不需要。

### P2 — Bundled descriptor 的 process owner、共享和 close 路径未冻结

- 文档定位：[construction/pin](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:481)、[running-process contract](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:490)、[owner table](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:885)。
- 当前代码证据：`KernelSkillProjectionComposer` 当前按 Host session 构造，[host.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/host.py:519)；Host close序列没有 capability/Skill producer close owner，[host.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/host.py:3749)。
- 失败路径：若每个 session各自 pin，新 session可在同一进程的 package namespace被替换后采用新 root，违背“正常升级由 new process读取”的陈述；若改成 process共享，则当前没有共享签发或最后 close owner，易泄漏 descriptor。CLI和Desktop GUI的 call-local/process-local lifetime也不明确。
- 最小修订：明确一个 process-level bundled distribution binding，由各 Host sessions/GUI inspection借用，Host core在所有 workers join后幂等 close；独立 CLI invocation在 `finally` 中关闭其 call-local binding。若产品选择 session-level，则必须相应收窄“process pinned/new process”保证。
- Durable machinery：不需要，只是 typed process-local ownership。

### P2 — Standalone validate 与 install 对同一 source 的 placement truth 分裂

- 文档定位：[placement basename table](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:131)、[validate/install operations](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:207)。
- 当前代码证据：当前 validator和publisher都使用 source directory basename，[local_skill_management.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skill_management.py:310)、[local_skill_publisher.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skill_publisher.py:467)；destination basename随后取 parsed name，[local_skill_publisher.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skill_publisher.py:542)。
- 失败路径：目录名为 `wrong-name`、document name为 `foo` 时，standalone validate按规格 INVALID；install先解析出 `foo`，再以 frozen final destination `foo`做 placement，检查成为恒等式并成功安装。Bundled guidance同时说 source folder必须匹配 name。
- 最小修订：建议 initial/final source observation使用 frozen source basename；hidden stage和final destination使用 frozen destination basename。若确实希望安装任意命名 source folder，则必须明确这是有意的 validate/install非对称，并同步 validator定义、guidance和测试。
- Durable machinery：不需要。

### P2 — 新 canonical fingerprint framing 没有冻结到 exact bytes

- 文档定位：[§8.1–8.2](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:736)。
- 当前代码证据：现有 source contract domain和loose provenance payload是 exact literals，[capability.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/capability.py:95)、[capability.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/capability.py:197)；fact目前重复保存 provenance digest，[contracts.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/contracts.py:525)。
- 失败路径：规格只说新的 source-contract domain/version和 bundled domain-separated framing，没有给出 literal domain、payload field顺序/shape，也未定义 bundled package-relative目录究竟是 `bundled_skills/<name>`、`pulsara_agent/bundled_skills/<name>`还是仅 `<name>`。不同实现会产生不同 canonical compatibility/fact identities。
- 最小修订：冻结新 source-contract domain/payload和 bundled-origin domain/payload的 exact canonical值，以及 package-relative path normalization。Loose framing保持当前 exact domain/fields；digest仍只在唯一 fact builder内即时计算，不存回 DTO。
- Durable machinery：不需要；这些是合法的 provider/canonical identity边界。

## P3 finding

### P3 — `disabled/excluded roots` 与 CLI JSON仍有 dormant/ambiguous surface

- 文档定位：four roots固定在 [§1.3](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:154)，doctor却要求显示 disabled/excluded roots [§7.4](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:718)；final CLI syntax未列 `--json` [§2.4](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_UNIFIED_SKILL_DEFINITION_PRODUCERS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md:239)，测试计划又保留 CLI JSON。
- 当前代码证据：`include_user_skills=False` 只被测试注入，production无对应 setting；`excluded_root_kinds`和JSON projection仍存在于当前 carrier/CLI，[local_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skills.py:288)、[cli.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py:605)。
- 失败路径：实现者可能保留一个没有产品消费者的 root-disable branch，或因 final namespace漏写而删除仍被测试/用户使用的 `--json`。
- 最小修订：从当前产品 DTO/doctor删除 disabled/excluded projection；测试需要的 root subset直接作为窄测试 fixture，不进入production contract。明确四个保留命令是否都继续支持 `--json`。
- Durable machinery：不需要。

## 上一轮 findings closure matrix

| 上轮 finding | 状态 | 结论 |
|---|---|---|
| P1：没有真实 Plugin owner | CLOSED | 当前轮明确只需要 bundled+loose，Plugin不是 input/gate/DoD。 |
| P1：loose预选与central双 authority | CLOSED | loose提交全部 candidates；central resolver唯一拥有五层 precedence。 |
| P1：parser/placement矛盾 | PARTIAL | 两阶段API和issue carrier已闭合，但 install 使用 destination basename造成 validate/install truth分裂。 |
| P1：Plugin borrow/active/retained lifecycle | CLOSED | 从当前产品范围完整移除；future seam要求真实owner自行闭合。 |
| P1：Runtime UNAVAILABLE有两个答案 | PARTIAL | predecessor semantic fallback已明确禁止，但 deadline path无法实际到达要求的 successor append。 |
| P2：bundled path/process/inventory不足 | PARTIAL | filesystem-backed、expected tuple、revalidation和干扰语义明显改进；inventory集合及process owner/close仍未闭合。 |
| P2：inspection/projection/bounds不是closed DTO | PARTIAL | DTO与bound owner大幅闭合；catalog-overbound active map、deadline和diagnostic consumer仍矛盾。 |
| P2：modification map/stable fact identity | PARTIAL | consumer map已覆盖，kind/id和loose identity已冻结；新 contract/bundled framing仍非 exact。 |
| P2：旧 bundled copies遮挡升级 | CLOSED | 已明确作为ordinary loose override，并要求诚实doctor/release guidance。 |
| P2：publisher仍保留 reserved provenance | CLOSED | 规格明确删除constant、enum、mapping、rejection和tests。 |
| P3：open bags/stored projections/winner ordinal | CLOSED | origin closed、source/label derived、winner exact reference，stored provenance/ordinal均要求删除。 |

## 已确认正确的部分

- 本轮确实没有 Plugin blocker、placeholder或 dormant third-input branch，可形成独立 two-producer产品。
- Parser/placement拆分、source-neutral manifest/origin及central-only precedence方向正确。
- 五层 precedence、invalid不占 slot、每个正常 candidate必须 winner或shadowed一次的代数正确。
- COMPLETE/UNAVAILABLE禁止partial facts、两 producer failure固定排序的方向正确。
- Existing bounds已基本迁移到唯一 owner，没有新增总Skill、历史、turn、call或lifetime cap。
- Stable `CapabilitySourceKind.LOCAL_SKILL_CATALOG`和`pulsara-local-skill-catalog`保留正确。
- Typed origin替代stored provenance fingerprint符合 fingerprint subtraction；raw document、semantic/fact及retained evidence digests均有真实消费者。
- Bundled absolute filesystem path、ordinary `read_file`、root rebound fail-closed和same-UID interference外部前提是诚实语义。
- Legacy user artifacts不自动删除、不读取旧authority；loose copy遮挡新版 bundled的描述正确。
- sync/status/reset/startup sync、manifest/provenance/hash/backup/opt-out必须同 diff hard cut删除的范围完整。
- Future 9.3 section足够窄，并明确禁止 materialization、private engine、Skill层扫描 Plugin state和提前扩展enum/tests/DoD。

## 仍无消费者或属于过度设计的字段/mechanism

应删除或禁止引入：

- Production `include_user_skills`、`excluded_root_kinds`和doctor disabled-root projection。
- Current `winner_ordinal`、winner-local ordinal joins和generic `DUPLICATE_NAME` projection。
- Stored coarse source、human-readable origin label及`winning_root_provenance_fingerprint`。
- Local-only wrapper类型和并行 bundled manifest/discovery DTO。
- 为解决 catalog-overbound而临时保留的隐藏 winner map或第二 registry。
- Plugin placeholder input/origin/diagnostic/borrow、producer registry、generation、receipt、history或fingerprint map。

应保留，因为已有真实消费者：

- Official bundled expected tuple。
- Process-local held descriptor及revalidation。
- Source registration contract fingerprint。
- Raw document、manifest semantic、fact及retained evidence digests。
- Existing per-operation physical bounds。

## 应删除的旧路径

- `capability/bundled_skills.py`中的 sync/status/reset、copy/replace、backup/restore、manifest、opt-out、tree hash和drift state machine。
- CLI旧 parsers/handlers/help及 Host/REPL startup best-effort sync。
- `.bundled_manifest`、`.no-bundled-skills`、`.restore-backups`和provenance的production authority。
- Publisher `RESERVED_CONTROL` enum、allowed-detail/exit mapping和copy rejection。
- `LocalSkillProvider.winners_by_name`、winner ordinal及local-only catalog truth。
- `FrozenSkillCapabilityFact.winning_root_provenance_fingerprint` stored field。
- Retained Skill对current discovery/current manifest的new-read和inherited rejoin。
- Active Round 9.1/local completion/Round 5B中被新 hard cut取代的copy/sync/local-only/current-manifest assertions。
- Bundled installer guidance中“bundled provenance excluded”的旧句；真实用户目录本身不得删除。

## 明确答复

- 是否可直接交给 coding agent：**否**。至少先关闭上述三个 P1，并把相应测试加入 §15/DoD。
- 是否能在没有 Plugin 时独立 ACTIVATE：**产品范围上可以，当前文档版本尚不可以**。Plugin已不再是 blocker；修完本轮自身合同后，无需任何 Plugin代码、测试或dogfood。
- Same-epoch continuity：SYSTEM/tools byte-identical、messages suffix-only和无第三 rebase boundary的主体规则正确；但 deadline successor安装边界及 retained historical causal算法尚未完全闭合。
- Long-horizon availability：没有新增总 cap；但当前未定义的 Runtime diagnostic投影会把 64 diagnostics意外变成 candidate admission失败，需按语义去重而不是截断issues。
- Small durability：闭合。所有 finding都可由typed process-local objects、closed unions和pure algorithms解决，不需要event、receipt、checkpoint、history、lease、generation或repair machinery。
- Oracle：规格要求的 `29 / 24 / 11 / 1 / 25 / 0` 与 Hook vocabulary `11`保持不变；现有定点测试通过。
- Operation/CLI/GUI truth与修改图：主 owner和文件地图已经足够广；剩余问题是上述合同缺口及 `--json`/disabled roots的小歧义，而不是缺少新的服务层。
- Future 9.3：没有提前实现 Plugin产品，同时足以禁止未来 four-root materialization和private Skill engine。

## 实际只读探针与测试

- 完整阅读：
  - `AGENTS.md`：61行；
  - 主审规格：1186行；
  - 上轮 findings：179行；
  - fingerprint subtraction规格：1060行。
- 窄读 active Round 9.1、local Skill completion、Round 5B及所列 production/parser/provider/management/publisher/types/contracts/registry/resolver/render/CLI/Host/context/read/retained/packaging/tests；未读取现有 Round 9.3实施文档。
- `find . -name AGENTS.md -print`：仓库内仅 `./AGENTS.md`。
- 环境：Python `3.12.12`、uv `0.10.7`、pytest `9.0.3`。
- Four-root alias内存探针：workspace=`Path.home()`时得到 `ValueError: Skill root bindings are not ordered and unique`。
- Catalog bound探针：
  - 64项、每项1024个emoji description：COMPLETE，`330946 / 393216` bytes；
  - parser接受1024个 YAML `\0` escape为合法description、0 diagnostics；64项catalog得到 `UNAVAILABLE CATALOG_OVERBOUND`，证明冲突分支真实可达。
- Diagnostic投影探针：129个 invalid Skill diagnostics被一对一映射为129个 Runtime context diagnostics；compiler maximum为64，`would_exceed=True`。
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -p no:cacheprovider tests/test_bundled_skills.py tests/test_local_skill_management.py tests/test_capability_skills.py tests/test_round9_1_agent_skills_standard.py`：`120 passed in 2.03s`。
- 同参数运行 `tests/test_round5b_long_horizon_context_compaction.py`：`30 passed in 0.90s`。
- 两个 architecture/oracle定点测试：`2 passed in 0.95s`。
- `git diff --exit-code -- .`：exit 0；`git diff --cached --exit-code -- .`：exit 0。
- 最终 `git status --short` 与开始一致，仅两个预先存在的未跟踪规格文件；没有修改、stage或commit任何文件。
