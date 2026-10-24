# POSIX-first Phase A：后端文件绑定与组织方案

2026-10-24。仅提案；生产/根spec/保存设置/数据库均未修改，未执行G0或本轮tests。依据当前真实源码与上轮caller盘点；本轮文件只在`backend/posix-first/`。

## 决策

同意按新用户指令先做 **Phase A：Linux/macOS真实接口接入与回归**，再做 **Phase B：Windows G0→验证后实现/激活**。这应明确撤销原spec“所有生产接口抽取必须在G0全部通过后”对A的限制；不是声称Windows风险已解。A是可独立验收的完整POSIX生产改动，不是mock Windows或仅画接口。

Phase A文件绑定应是必做完整链；ProcessScope只抽当前真实共用的POSIX机制，不冻结未证实的Job/ConPTY能力；三IPC不需为对称预建Win leaf/命名管道/控制线程。G0后可以对A接口做一次受影响全caller的hard cut以满足Windows事实，不保留旧别名、`int|Binding`双签名、互相fallback或永久compatibility shim。

## 1. A阶段具体落点与排除项

```text
src/pulsara_agent/
  local_source_binding.py  # typed contract/实际支持平台入口，无raw fd导出
  _local_source_posix.py   # 本轮唯一真实fd/dir_fd/no-follow/IO/flock实现
  exclusive_publish.py    # 现有public publisher入口；root_fd改DirectoryBinding
```

Darwin renameatx_np/Linux renameat2可留`exclusive_publish.py`私有POSIX实现中，或仅因职责独立提到`_exclusive_publish_posix.py`；不为形式对称拆两套publisher。`prepare_local_source_path`保留现有词法与Darwin顶层alias合同。A不创建Windows空实现、`NotImplemented`方法墙、Job/HANDLE字段、ACL NA占位、可用backend注册表，不新增用户平台配置。不能把A通过用作Windows supported。

**A不新增**postgres-auth.json/TCP SCRAM、DB安全文件helper、named-pipe diagnostic、NSIS、ConPTY、pwsh载荷、Windows权限/可执行位替代规则。这些仍是B在G0前置约束下的真实适配。Linux/macOS仍使用现有PG Unix socket与现有设置真相。

## 2. 最小文件protocol：从真实caller需要出发

以下是方法候选，数量/名称在根spec冻结；不需要照搬成独立的通用storage包。POSIX实现复用`os`/标准库/syscall，不复制文件系统。接口只同步执行一个有限文件操作；operation的deadline、取消、字节总量、扫描/复制步骤仍由原owner持有。

### 2.1 身份与metadata不能替代产品比较谓词

推荐Phase A只有真实POSIX类型：

```python
@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int

@dataclass(frozen=True, slots=True)
class FileMetadata:
    identity: FileIdentity
    kind: FileKind  # REGULAR / DIRECTORY / SYMLINK / SPECIAL
    size: int
    mtime_ns: int
    ctime_ns: int
    posix_mode: int
    owner_uid: int
```

这些是完整typed观察，不是fingerprint、跨重启实体身份或Windows representation。mode保留实际`st_mode`（含类型/特殊位），kind供普通文件分类；不在A发明Windows variant或把权限bits置零。未来G0若需要扩展为Windows真实identity/ACL观察，改同一契约和消费者，旧representation退出，不堆兼容双字段。

**不得用`metadata_before == metadata_after`统一所有owner判断**。当前语义不同：

| 现有代码 | 原比较必须保留 |
|---|---|
| `desktop_app/file_preview.py:128-129` | dev/ino/size/mtime，故意不比较ctime/permission；preview读边界还同时检查visible path和held对象 |
| `plugins/package_core.py:495-542` | kind、dev/ino/size/mtime/ctime及regular executable布尔；目录也沿用原字段比较 |
| `capability/local_skill_publisher.py:889-906` | 目录主要identity；regular文件kind/identity/size/mtime/ctime/原始执行位 |
| `capability/local_skill_removal.py:179-182` | root与child的dev/ino，stale结果和配置清理归原owner |
| `settings.py:335-370` | regular、当前POSIX私有mode、size bound；不是新加uid/ctime约束 |
| `desktop_app/file_import.py:91-108` | samestat即dev/ino与regular、目录成员恰好一项；bytes仍是实际stream累计 |
| `plugins/package_store.py:1810-1823` | 特定data root需要当前euid+mode恰好0700，不应被“只检查mode”弱化 |

owner可以从typed metadata构造其已有冻结scalar tuple，**对外DTO/稳定canonical ID/已有digest输入顺序不变**。不要求为了统一identity对象而改public LocalSkillRemovalIdentity键或数据库schema。也不把原始stat对象长期藏在新metadata里再公开`.raw_stat`给业务层绕回os API。

### 2.2 DirectoryBinding候选

```python
class DirectoryBinding(Protocol):
    def metadata(self) -> FileMetadata: ...
    def names(self) -> tuple[str, ...]: ...
    def inspect(self, name: str) -> FileMetadata: ...
    def open_directory(self, name: str) -> DirectoryBinding: ...
    def open_regular(self, name: str, *, access: FileAccess = READ) -> FileBinding: ...
    def create_regular(self, name: str, *, access: FileAccess, mode: int) -> FileBinding: ...
    def mkdir(self, name: str, *, mode: int) -> None: ...
    def replace_entry(self, source_name: str, target_name: str) -> None: ...
    def link_file_noreplace(self, source_name: str, target_name: str) -> None: ...
    def unlink(self, name: str) -> None: ...
    def rmdir(self, name: str) -> None: ...
    def set_posix_mode(self, mode: int) -> None: ...
    def sync(self) -> None: ...
    def duplicate(self) -> DirectoryBinding: ...
    def close(self) -> None: ...
```

public绝对路径入口保留当前函数名但返回owned binding；`open_or_create_absolute_directory_nofollow(path,mode=...)`保留现有“创建缺项/重新无跟随绑定”的合同。`open_regular`的读路径保留O_NONBLOCK+nofollow后regular检查，不允许特殊文件阻塞等候；参数access只含实际READ/WRITE/READ_WRITE，不公开任意OS flags。

所有name是单个叶名，无NUL/斜杠/`.`/`..`；不在A把Windows保留名/反斜杠规则塞进POSIX。`names()`不自行过滤`.`前缀、不递归、不替owner排序或给目录设总数量上限；目前plugin会观察隐藏成员，而loose Skill可能只取可见成员。

`mkdir`不假称返回了原子绑定的刚创建对象：POSIX mkdir与后续open间仍有namespace竞争，stage的inspect/open/identity验证仍保留在现有owner同样的cut上。`unlink/rmdir`也只是绑定parent下的实际namespace操作；接口不能声称单次atomic compare-identity-delete，而实现实际却是stat+unlink两步。谁能清理、当前名是否还属本请求由原owner现有检查/错误仲裁决定。

若现有caller确需跨两个绑定目录的rename/link，再增加具名的target_directory参数；没有caller就不预留通用mount、跨卷copy或multi-root transaction。

### 2.3 FileBinding与reader/borrow

```python
class FileBinding(Protocol):
    def metadata(self) -> FileMetadata: ...
    def read(self, maximum_bytes: int) -> bytes: ...
    def read_at(self, offset: int, maximum_bytes: int) -> bytes: ...
    def write(self, data: bytes | memoryview) -> int: ...
    def reader(self) -> BinaryReader: ...
    def set_posix_mode(self, mode: int) -> None: ...
    def sync(self) -> None: ...
    def duplicate(self) -> FileBinding: ...
    def close(self) -> None: ...
```

`read/write`保留顺序文件位置和partial read/write事实；现有`_write_all`/有限read loop仍由owner执行并检查deadline/cancel/byte limit，不在FileBinding里增加retry-count/total-size上限。`read_at`用POSIX pread（同一方法不改变共享offset），返回至多请求bytes；必须保留preview range并发/CSV分页不会互串的位置语义。A不为了Windows模拟在共享fd上无锁seek+read。

`reader()`用于`BudgetLines`/TextIOWrapper/Zip/Office等已有Python IO消费者，返回有close/seek/read的标准受限reader view；底层fd仍私有。它应持有明确duplicate/borrow，reader关闭只释放这份持有，不偷偷关闭父FileBinding；从已close对象创建新reader/duplicate拒绝。业务不通过reader.fileno回到raw OS操作。reader seek/read的cursor语义按现有caller验证；不需要额外generic file proxy框架。

显式duplicate保持同一已打开对象，不能重新按path打开；POSIX dup可共享顺序文件offset，不把duplicate默认宣称为独立cursor。位置独立性由read_at/pread及需要它的reader view明确提供。parent close不使已取得的duplicate立刻失效。inline preview仍在每次stream读前后检查token/visible-file，明确download借用可在原preview关闭后finish（`test_revoked_inline_stream_stops_but_explicit_download_finishes`）；物理借用不等于继续获准inline授权。跨await/to_thread的已开始read必须join后才能释放相应borrow，保留`file_preview_http.blocking`现有cancel-safe settlement。

两类binding都有context manager/幂等close；close异常仍透出，**先撤销本wrapper所有权后close，不可在不确定时重试同一个整数fd**以免关闭已复用描述符。已有artifact final-close failure必须继续保存published事实并尝试释放所有持有，不因一份close失败跳过另一份。

### 2.4 权限：A明确POSIX，不冒充已跨平台

Phase A可以保留明确`set_posix_mode(mode)`和metadata.posix_mode的实际值，原owner保留0600/0700/0400/0500、执行位复制、replace保留原mode等判断。比现在强行造“universal ACL policy”再偷偷映射为chmod更诚实。只有确实多个owner需要的检查才提供小predicate，不把owner policy都塞binding。

这不妨碍A减少OS泄漏：业务表达原有mode意图和字段比较，具体chmod/fstat/geteuid机制在POSIX实现；Phase B会根据G0将这一部分一次hard cut到已冻结的Windows ACL/NA语义。A验收不要求证明Windows可表达每个mode或permission predicate。

## 3. Lock binding：生命周期对象，不是新lease机制

锁需要特别保留当前`PhysicalLifetimeAnchor`合同，不能仅把flock挪到另一个函数：

- `plugins/package_store.py:1033-1069`在**同一个已打开锁文件**上按原绝对deadline/cancel轮询LOCK_SH，成功与取消同时到达时再次检查；不得每轮按路径重开，改变接受的物理锁对象。
- `PhysicalLifetimeAnchor:265-301`duplicate共享open-file-description；某borrow close不LOCK_UN，不释放其他borrow。全部副本close后由OS释放。禁止`duplicate`变成重新resolve+acquire。
- `_exclusive_lock:1513-1542`与`hooks/trust.py:377-420`保留原共享/独占、blocking vs非阻塞、caller deadline语义；不增加轮数、总等待寿命或跨重启回收。

最小候选为 `open_lock_file(path或bound-parent,name)`返回`FileLockBinding`，暴露`lock(mode=SHARED|EXCLUSIVE, blocking:bool)->bool`、`duplicate()`（只对已持有锁的对象）、`close()`。未能非阻塞取得只返回contended/False；其他OSError保留。caller保留现有轮询、最后cancel复核及业务in_use判断；binding不引入锁调度器。held wrapper关闭按fd lifetime释放，不在一个borrow close时对整个OFD主动LOCK_UN。

`desktop_runtime.acquire_kernel_lock()`同批改返回owned锁，run/main/`--wait-owner`不再os.close int；wait仍仅是等旧owner释放，不初始化settings/PG/app。hook当前fcntl None直接yield的不安全分支在A删除；A只在真实POSIX实现上成立，不用Windowsstub补齐。

## 4. 三种发布、flush和known/unknown必须保持分层

### 不合并的三个操作

1. **独占目录namespace移动**：`ExclusiveDirectoryPublisher.publish(parent:DirectoryBinding, staging_name,final_name)`，仍只做Darwin renameatx_np(RENAME_EXCL)/Linux renameat2(RENAME_NOREPLACE)。不自动flush、不隐藏额外post-stat失败、不递归、不替owner处理source-race。root/stage/final前后identity验证在原publisher/import/removal owner；系统调用返回不等于用户可见整操作完成。
2. **设置/state文件替换**：`DirectoryBinding.replace_entry(temp_name,target_name)`，允许替换既有目标。settings/hook/plugin state继续按payload write→file sync→close→replace→parent sync次序处理，不能改为exclusive no-replace，不能为接口统一偷偷延长/改变接受cut。目标和临时文件的权限/身份检查保留当前owner规则。
3. **file/export no-clobber建立新目录项**：`DirectoryBinding.link_file_noreplace(temp_name,target_name)`，保持原hardlink方法、源临时项仍存在、最终名不能覆盖；follow_symlinks=False与绑定parent保持。跨卷/不支持仍是现有typed no-clobber unavailable；不能fallback copy或rename覆盖。

### 失败归属

低层透出真实OSError/errno与已有primitive-unavailable，不建立通用MutationReceipt/Checkpoint/commit ledger。尤其不要把rename+flush+metadata一起藏进低层方法后只抛generic error，使owner丢掉“namespace成功但flush失败”的事实。

| owner/现有代码 | A应保留的结果逻辑 |
|---|---|
| `settings.py:391-419` | replaced后parent-sync失败触发既有_LocalSettingsCommitUnknown→原store按当前内容reread/结算；不要把它改成未写入 |
| `hooks/trust.py:341-374` / `hooks/source.py` | 保留其HookMutationPublishedError/原accepted transition裁决，不用settings错误替代hook产品语义 |
| `tools/builtins/filesystem.py:1480-1591` | publication_attempted与published=False/True/None；调用抛错后bound final identity可证明成功/不存在/unknown；验证或close失败不能把已出现文件声称未写 |
| `local_skill_publisher.py` / plugin store | 原source-raced、stage-raced、published/cleanup注意结果与failure arbitration；不自动把所有OSError解释为source变更 |
| `local_skill_removal.py:110-166` | unbind后目标替换/override清理失败/剩余隐藏树分别CLEANUP_ATTENTION；不删替代物、不伪造完全REMOVED |
| `file_import.py:111-143` | rename后更新本请求stage位置、seal/sync/验证再发布；失败仅清仍属己方identity的对象，不删同名替代路径 |

`sync()`只做真实file/directory sync并透出error；具体caller目前的ENOTSUP/EINVAL可容忍政策仍由相应owner保留。settings parent sync要求不能被filesystem helper现有宽容策略意外弱化；也不能反过来把所有原可容忍平台结果变成新产品失败。

## 5. 全caller切片矩阵与实际tests

下表是单个A分支内的实施/验证顺序；不是允许把新façade接一半就发布。**A交付退出条件是整表消费者/held槽全部迁移，旧raw入口和shared flags退出。** 若需多PR，必须各自构成完整可运行的typed切片，不允许同一入口同时返回int与binding或旧别名迁移期。

| 切片 | 当前真实生产文件 | 方法/held槽变化 | 必须聚焦的现有suite |
|---|---|---|---|
| A-F0 primitives | local_source_binding/exclusive_publish | typed roots/files/metadata、raw flags私有、真实POSIX syscall | 新增极少量primitive合同测试：all-component nofollow、read_at并发、duplicate lifetime、real no-replace并发；非SDK conformance复刻 |
| A-F1 settings/hooks/kernel lock | settings.py、hooks/source.py、hooks/trust.py、desktop_runtime.py | private mode、finite read/write、sync/replace、lock对象，全部finally close | test_settings.py；test_round9_2_hook_subsystem.py；test_user_hooks_tab.py；test_desktop_runtime.py |
| A-F2 source/Skill read | capability/local_skills.py、bundled_skills.py、skill_import.py、local_skill_management.py | HeldSkillRoot/Child、distribution/source observations、names/open/read/revalidate | test_capability_skills.py；test_bundled_skills.py；test_skill_import.py；test_round9_1_agent_skills_standard.py；test_local_skill_management.py |
| A-F3 Skill写/删 | capability/local_skill_publisher.py、local_skill_removal.py | _TargetRoot/_StageBinding绑定、copy/execmode/flush、exclusive publish、stale cleanup | test_local_skill_management.py；test_local_skill_removal.py |
| A-F4 plugin链 | plugins/package_core.py、package_store.py、source_import.py、skill_producer.py；view/inspection/mcp_adapter只借typed anchor | HeldPluginPackageObservation、SourceImport、state、PhysicalLifetimeAnchor及shared/exclusive locks、sealing | test_round9_3_agent_plugin_product.py；test_round9_3_plugin_physical_semantics.py；test_plugin_source_import.py；test_plugin_connection_inputs.py；test_plugin_mcp_connections.py |
| A-F5 import/preview/http | desktop_app/file_import.py、file_preview.py、file_preview_http.py、path_completion.py | LocalImport根/stage/file、Preview.fd/root_fd、resource held file、BudgetLines/HTTP read_at/borrow/close | test_local_file_import.py；test_session_file_preview.py；test_workspace_path_completion.py；相关HTML/SVG/image browser隔离tests |
| A-F6 tool filesystem/export | tools/builtins/filesystem.py及调用者 | no-clobber link、replace保留mode、实际published三态、final close | test_artifact_export.py；test_content_revision_line_edit.py；test_search_tool_split.py；test_builtin_default_null_contracts.py；文件工具的现有集成节点 |

其中必须保留的具体回归例子：

- `test_settings.py`已有after-replace-parent-fsync、unknown+unreadable、cancellation join、secrets exact mutation，不能只跑codec。
- `test_round9_3_plugin_physical_semantics.py`已有anchor blocks GC until native MCP drain、package anchor obeys deadline、paired source-read failure归属；重构注入点时保留断言意义。
- `test_session_file_preview.py`已有UTF8分页、CSV BOM/multiline/page retry、symlink资源、preview revoke vs download finish、unlink/atomic replace不会重用旧cursor；还需一个真实read_at双consumer偏移独立例子。
- `test_artifact_export.py`已有同target concurrent exports one winner、publication ack failure、final-close failure、cancel-after-publication、parent-rebinding；这些是新绑定必须承受的产品合同，不可改成“调用了新方法”测试。
- 目录/文件名和隐藏项规则、chmod可执行位及source metadata比较必须保持原fixtures；canonical/prefix tests不得重录golden换绿。

最终A验证要在**macOS arm64、Ubuntu x64/arm64**当前支持矩阵实际跑原有适用suite，至少全Python非postgres、相关postgres能力集成、资源/wheel/desktop/frontend原有检查（按受影响范围）。新binding模块随正常wheel打包，安装目录外smoke确认没有output/check-out依赖。无Windows证据不标失败，也不能标Windows通过。新tests不镜像os完整conformance，只测Pulsara新增接缝的竞争/lifetime/失败语义。

本轮不执行这些命令。实际实施时优先root uv `.venv`、已验证local disposable postgres，保存配置只读/实际secret精确脱敏；不能因为A是抽象工作就跳过对有副作用失败语义的验证。

## 6. ProcessScope与三IPC：A做哪些、延后哪些

### ProcessScope

有实际A收益：把当前terminal manager/environment、MCP/hooks/preview中重复的POSIX group操作收口；保留terminal同步threads与非终端async I/O，移除共享owner中direct pty/fcntl/killpg。**前提是每个提取的接口在至少真实caller完整接入**，不是建立一个空ProcessScope后让大家仍绕回旧实现。

建议A最小只覆盖当前可证明的：受控POSIX创建（start_new_session/现有PTY路径）、精确原root对象观察、signal_group、group_exists、资源close；调用者已有deadline/term→kill升级和readers join仍在原owner。其名字可以仍是ProcessScope，但不能在A冻结Windows JOB_LIST、HANDLE、IOCP、ConPTY半关闭或“任意后代可回收”的承诺。root exit与managed group empty始终分开，不在抽取时意外修正/重写MCP协议失败或terminal状态机。

不值得A预建：跨语言native helper、Popen/asyncio统一runner、全局scope map、统一输出/slots/重试/monitor。G0后scope契约需要改就全caller一次hard cut，不保留PosixProcessScopeV1/WindowsV2永久双轨。若某个方法只有Windows候选需要且POSIX暂无真实consumer，留B，不为了“接口完整”制造fake POSIX实现。

### IPC

1. **内部V3**：A可把现有Unix listener/open-client/close抽到`terminal_protocol/transport.py`及POSIX实现，真实收益是gateway/client/Application同一typed endpoint与owned lifetime，减少公开socket_path耦合。auth/framing/attachment仍共用，A仍用原Unix socket，无loopback端口/token新字段。若抽取只在原gateway增加owned `open_client_stream`已足够，也可先不拆额外薄文件。
2. **diagnostics**：A最多将当前Unix专用有限request/serve物理I/O提到runtime局部adapter，保持private socket与有限消息、kernel lock typed化。named-pipe/SID/peer验证留B；不创建NamedPipeStub或通用RPC。
3. **Node→Python JSONL**：A可提取实际已用的UTF8 byte reader/writer close/join接缝，保留现有消息与EOF→原application.close；Windows匿名pipe reader线程/Proactor约束留B。若当前POSIX控制I/O没有重复/耦合痛点，不必为“平台全覆盖”先造空base class。

NodePG safe-file helper/TCP凭据是纯Windows新增认证路径，在A无真实调用者，应明确延后；不能用先造helper来证明跨语言组织方案可行。resource locator只有当前确有多入口布局复用才A提取，只读、不增加manifest或进程owner。

## 7. 根spec需要明确改写的地方

- 文首/§1/§3.1/§4/§11.1：从“G0后才抽接口”改为A可立即做POSIX接口与完整回归；G0仍是**全面Windows适配/生产实现/支持激活**前置，不变成A前置。不能只在末尾加一段A而让前面所有禁令继续相反。
- §3目录树标清A真实文件、B经G0后新增文件；Windows候选仍是计划，A不创建stub。平台选择A只安装当前支持POSIX实现，不返回抽象成功让Windows继续运行。
- §3.4给上面的metadata投影、锁OFD借用、read_at、三种发布与known/unknown归属；方法只是OS接缝，不变成产品事务框架。
- §11增加A任务/出口：全caller+held槽接齐、POSIX原有结果/ID/prefix不变、三平台回归/安装smoke、旧int/flags/重复制程调用退出；声明A通过≠G0通过≠Windows支持。
- 成本将原W1/W2/W3里已做A的部分拆出，不能在A与B重复计费；G0不通过导致接口hard cut/上游修复仍按事实重估。
- G0后若证明A接口不能表达Windows真实语义，明确一次hard cut修protocol+metadata+所有caller/tests，删除旧路径；不加alias/dualread/fallback或把“已经抽象”当产品正确性证明。
