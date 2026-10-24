# 后端代码组织建议

范围：仅组织方案调研，2026-10-24。未改生产/根规格，未运行G0或无关测试。沿用上一轮仓库约束；当前源码只读检查。`callers.json`是本轮AST导航证据（14个直接import消费者、另外2个间接raw-fd消费者），数字只是候选范围，不是缺陷或Windows验证结果。

## 结论

同意保留 `local_source_binding.py` / `exclusive_publish.py` 作为共用façade，并在相邻私有`_posix`/`_windows`实现中隔离系统机制。**必须同时hard cut façade的int-fd契约及全部消费者**；不能仅换函数实现而把Windows HANDLE整数冒充CPython CRT fd。业务owner/解析器/状态机/错误结算不按平台复制。

跨terminal/MCP/hooks/截图只共享窄 `ProcessScope` OS生命周期机制；不共享业务executor、输出owner、MCP协议、hook policy、重试/调度或全局process registry。IPC按既有三条用途保留独立owner，不新建平台RPC层。

## 1. 文件接口的真实迁移范围

`local_source_binding.py:42,59`当前返回`int`，调用方直接`os.open(...,dir_fd=fd)`、`fstat/fchmod/fsync/dup/listdir/fdopen/close`；`exclusive_publish.py:15,19`直接接受`root_fd:int`。Windows目录HANDLE不能给这些API使用。当前至少涉及以下文件：

| 现有文件（均在src/pulsara_agent/） | 保留共用的业务部分 | 必须改成typed binding的物理部分 |
|---|---|---|
| `settings.py` | codec、schema、mutation串行、credential capture与unknown publication裁决 | read/write_local_settings里的绑定、私有权限、临时写、flush、replace、cleanup |
| `hooks/source.py`, `hooks/trust.py` | hook声明、subject、trust语义、source snapshot/accepted transition | 私有文档读写、source跨进程锁、no-follow/metadata/replace |
| `capability/local_skills.py`, `bundled_skills.py` | SKILL parser、inventory、placement、definition/diagnostic、prefix-facing projection | HeldSkillRoot/Child、observed root/read/revalidate/close与distribution root binding |
| `capability/skill_import.py`, `local_skill_management.py` | import/preview/admission、operation结果和取消 | source walk/read/revalidate、held source字段 |
| `capability/local_skill_publisher.py` | 冻结inventory的产品含义、copy/verify/publish步骤、失败仲裁/typed outcome | `_TargetRoot.descriptor`, `_StageBinding.descriptor`、source/stage held objects、stream copy/metadata、exec属性/私有权限、发布/cleanup |
| `capability/local_skill_removal.py` | inspected identity、stale绝不删除替代物、config removal结果 | 绑定后unbind/no-replace/身份复核/递归只清己方对象；删除顶层POSIX flags |
| `plugins/package_core.py` | plugin/MCP/hook/Skill解析、包格式、entry类别与admission规则 | HeldPluginPackageObservation.descriptor、root/file绑定、walk/read/revalidate、platform source execution observation |
| `plugins/package_store.py` | instance/layout/state语义、install/borrow/uninstall与stale/unknown裁决 | staging/state IO、publish、sealing/ACL、锁与PhysicalLifetimeAnchor、snapshot当前目录身份 |
| `desktop_app/file_import.py` | user-copy streaming、controller/origin授权、导入结果/失败清理 | root/stage/file held bindings、flush/read-only ACL、publish/verify |
| `desktop_app/file_preview.py` | token、preview kind、表格/分页、文件变更/过期错误、HTML reading-root含义 | Preview.fd/root_fd、metadata/read_at/stream、resource no-follow、classify样本读取、BudgetLines reader |
| `tools/builtins/filesystem.py` | permission/workspace resolution、工具参数与结果、published三态 | `_atomic_create_bytes/_atomic_replace_bytes`、no-clobber/flush/metadata/cleanup |
| **间接** `plugins/source_import.py` | 源格式适配/转换、plugin import结果 | 从package_core借出的`_open_source_root/_open_regular_entry`返回int、SourceImport.descriptor、os.read/fstat/close |
| **间接** `desktop_app/file_preview_http.py` | aiohttp range/HEAD/CSP、response流、授权/变化检查 | `:202 os.dup(item.fd)`、`:235 fstat`、`:279 pread`、fd关闭；必须接受HeldFile/read_at，不能只迁Preview而漏HTTP层 |

`plugins/view.py` / `inspection.py` / `mcp_adapter.py`使用`PhysicalLifetimeAnchor`而不是自行解包fd，应保留这些typed borrow边界。`PhysicalLifetimeAnchor.duplicate()`必须继续“一个借用关闭不释放其他借用的锁”，不能用按路径重开/重新acquire替代而产生空窗。Windows LockFileEx/HANDLE复制语义由G0验证；若需要process-local共享持有对象维护最后close，这只是原anchor的内存所有权，不是lease或持久refcount。

### 最小公用接口

推荐同一façade声明 `DirectoryBinding` / `FileBinding`（名称可按仓库习惯微调），由`open_absolute_directory_nofollow`等现有入口返回owned对象，不兼容`int`。只承载本表实际需要的系统操作：

- Directory：close/context lifetime、从绑定目录无跟随打开子目录/普通文件、枚举/叶名metadata、exclusive创建、当前绑定身份复核、删除已绑定己方对象、私有权限与flush。
- File：close/显式duplicate或borrow、metadata/identity、read-at bytes（用于preview range和并发读取）、有限byte stream读写/flush、平台权限/执行属性观察。read-at不能靠在共享fd上无锁seek+read模拟，破坏现有`pread`并发语义。
- 锁：共享/独占OS lock held对象、同一个已绑定锁文件的生命周期、原operation deadline由调用owner传入。无锁分支不成功；不要把fcntl None变成read-only产品或永远in_use。
- Namespace publish：`exclusive_publish`接受绑定目录和实际held stage identity，进行no-replace；settings replace是另一个明确操作，不能与no-clobber混为bool flags的随意调用。

共享代码不得取`.fd/.handle/.fileno()`后调用`os.*`；CRT fd、Windows HANDLE、flags、ctypes/pywin32 imports和POSIX stat mode判定仅留对应实现。普通文件接入Python IO需要转换/duplicate时由实现持有唯一资源转移责任；目录HANDLE绝不伪装CRT fd。可以把极少数元数据值统一成typed metadata，但不创建通用VFS、后端注册表或DTO fingerprint；物理身份仍是当前观察，不能持久化为新authority。

caller已有身份tuple和执行观察字段一次hard cut到新typed值；POSIX observable ID/digest输出保持现有规范，不能为迁移改变canonical bytes。平台属性NA/ACL规范和所需metadata必须按已冻结§8/G0-F闭合。

### 具体文件形态

```text
pulsara_agent/
  local_source_binding.py          # 共用typed契约、入口与静态平台选择
  _local_source_binding_posix.py   # 原fd/no-follow/metadata/flush/权限实现
  _local_source_binding_windows.py # G0确定的HANDLE/reparse/ACL/IO实现
  exclusive_publish.py            # 共用public Protocol/typed errors/入口
  _exclusive_publish_posix.py      # Darwin/Linux现有no-replace syscall
  _exclusive_publish_windows.py    # b312 FileRenameInfo等经G0验证的调用
```

把平台flag从façade导出中移除（`DIRECTORY_NOFOLLOW_FLAGS`不能作为跨平台API）。Darwin OS别名政策属于POSIX实现的词法准备接缝；包内PurePosixPath仍在共用parser，不因Windows把它换ntpath。不要为了模块“对称”再拆Darwin/Linux业务owner。系统调用无法互相复用时在上述私有实现内小分支即可。

settings本身可以继续共用原子写入步骤、outcome仲裁，私有权限/read/flush/publish交新binding；只有无法表达的多步骤物理段才需要 `settings`局部私有实现，不预建`settings_posix/settings_windows`两份codec+store。其他表中owner同理，不必每个文件都成对复制。

## 2. ProcessScope：共享机制，保留各执行owner

### 当前没有统一helper

- `terminal_process/manager.py:98`的_ProcessState持同步Popen，物理reader/watcher线程和output/monitor/foreground decision都在现有owner；environment probe也持Popen/pipe/selectors。
- `hooks/executor.py:126,249-278,497-525`持asyncio Process，有自己的slots、timeout/hostclose、JSON stdin、overflow drains、credential boundary；Windows分支只terminate root。
- `conversation_kernel/mcp/sdk_facade.py:299-500`持asyncio Process与AnyIO memory streams，有精确wire bytes/depth、secret_env、generation、MCP失败分类、独立stderr drain与transport close。
- `conversation_kernel/visualization_screenshot.py:18-68`持asyncio Process，实际browser由worker内Playwright拥有，cancel要回收完整受管树。
- `conversation_kernel/process_local_settlement.py`只有cancel-safe asyncio task join，不是OS进程owner；可以复用其现有调用语义，不能改造成process registry。

### 建议的文件和方向

```text
pulsara_agent/process_scope.py          # 窄owned scope契约/静态平台构造
pulsara_agent/_process_scope_posix.py   # session/group现有范围与信号
pulsara_agent/_process_scope_windows.py # 创建前Job归属、精确handle、集合查询/kill/close
```

`ProcessScope`只提供创建前纳管所需机制、root exact lifetime/exit、managed-set-empty查询/等待唤醒、请求终止、scope资源close。IOCP是唤醒，查询是事实；scope不收集终端输出、不负责provider工具结果、不知道hook/MCP请求ID，不发monitor或live event。持有的root/Job/handles是实例资源，不建立全局scope map或durable ID。

**不应让跨owner共享接口锁定asyncio.Process或Popen**：terminal是sync+threads、MCP/hooks是async，两边有自己的pipe/PTY reader适配。G0确定同一native创建机制能分别接入同步/异步IO；不能先spawn活进程再把它“交给scope”来复用SDK helper（早期子孙窗口）。ProcessScope应参与创建边界，不能接受任意既有PID认领。若已有binding提供创建属性/JOB_LIST应使用；suspended路线也在相同机制封闭。

terminal保留 `terminal_process/manager.py`、`output.py`、`monitor.py`、`models.py` 共用，只有physical backend和environment probe/launcher在本owner私有平台实现；不调用 `ProcessRegistry` 来跑MCP/hooks，不把两种调用的slots/deadline合并。跨模块的唯一依赖方向是各owner→process_scope→所选平台实现→维护依赖/OS。

MCP仍保留 `_BoundedStdioTransport` 与SDK ClientSession，若提取physical部分可设 `conversation_kernel/mcp/_stdio_process.py`（需要时再加平台私有文件）；hook命令选择/COMSPEC/overlay留`hooks/executor.py`或该owner的 `_command_launch.py`；screenshot仍是既有owner调用机制。不要因三处都会wait/terminate而合并成共享“run_tool_process”状态机。

credential_guard、semaphore/queue/admission以及cancel-before/after spawn的结算仍在各现有owner最后临界区；scope不得重建环境/读取settings/API keys，不能在guard之后异步重新解析可变命令。允许共享Windows环境key大小写去重的小函数（如 `process_environment.py`），不合并terminal deny策略、MCP必要baseline/secret_env和hook继承policy。

## 3. IPC与composition保持三条用途

### 内部Protocol-v3

现在 `DesktopApplication.start():146-156` 创建runtime dir并传`socket_path`；gateway拥有server、launch capability、frame/attachment/auth；`protocol_client.connect():105`重开Unix socket。两端Python同进程，这是最适合保留的窄stream接缝。

建议新增owner局部：

```text
terminal_protocol/local_transport.py
terminal_protocol/_local_transport_posix.py
terminal_protocol/_local_transport_windows.py
```

共同契约只含owned listener、`open_client_stream()`和close/admission（asyncio reader/writer），POSIX实现Unix路径，Windows实现exclusive loopback listener。gateway继续拥有protobuf帧/launch secret/role/generation/attachments/settlement，不把auth搬入transport。`protocol_client`通过该gateway拥有的endpoint/open方法连接，**删除公开socket_path依赖**，不把port字符串重新当server真实性。listener close与client connect/hello保密要求在same owner gate内线性化；endpoint对象不可交renderer，也不加public URL。

`DesktopApplication`只组合目标实现并传入原gateway；原runtime临时目录可以是POSIX transport资源，Windows无需为统一shape创建无用.sock文件/目录。不要新增用户级PlatformService注入全部业务。

### diagnostics

将 `desktop_runtime.py` 现有有限JSON validation/database_diagnostic语义保留，在 `desktop_diagnostics.py`（共用schema/client入口）及 ` _desktop_diagnostics_posix.py` / `_desktop_diagnostics_windows.py`提取物理通道。Windows named-pipe/SID DACL/local-only/first-instance/peer owner，POSIX Unix private endpoint；同步CLI client和async服务需要各自适配，但不得复制PG diagnostic业务/建通用RPC框架。`desktop_runtime`调用其serve/close，CLI只调用request，不接触PG auth配置或直连。

kernel OS lock继续由`desktop_runtime.acquire_kernel_lock`入口组合相同安全lock primitive，返回owned锁，不再`int`+外层`os.close`；`--wait-owner`同一入口。不要另建lock registry。

### Node→Python宿主JSONL

`desktop_runtime.main():335-366`与Electron child owner继续拥有控制/状态协议。把UTF8字节读写、Node传入匿名pipe reader及join放 `desktop_app/host_control_pipe.py`（确有必要再有该owner私有平台实现）；只提供本机reader/writer+lifetime，不知道start/inspect-quit/shutdown语义。runtime仍进行state/readiness/application close顺序，不能将三条通道合成新RPC server。

### Node PG auth文件的跨语言接缝

Node `BundledPostgres`拥有物理实例和postgres-auth材料，但不能直接import Python binding。组织规格要明确：优先现有Electron/native维护binding能表达的安全操作；若G0证明缺口，允许一个**desktop专用、有限safe-auth-file操作的bundled-Python/native helper**，经原私有控制pipe返回必要结果，语义owner仍是BundledPostgres、PG schema仍在Python现有owner。helper不成为PG manager，不持有另一个secret registry，不在任意时刻接受renderer文件路径。实际二选一必须在G0后回填；不能各语言各写一套通用安全文件框架，也不能以跨语言不便放弃ACL/no-follow。desktop专题应具体核对其可用binding/打包路径。

## 4. 推荐迁移顺序和hard-cut原子单位

1. **G0先证物理机制与API**：不建立生产接口后再试是否能满足。冻结最小binding/scope/listener类型的操作面和异常/close/borrow语义；OS root fd/HANDLE不可跨shared façade。
2. **文件机制+全caller一次hard cut**：先在改动分支内做POSIX实现提取并接齐上述14+2消费者，再接Windows实现；这只是同一未发布work package内部顺序，不能提交/激活“新façade+旧fd caller”双轨。删除shared flags和int签名、移走直接POSIX syscalls；同步迁移tests的注入点。settings/skill/plugin/import/preview/fs不能遗漏一类后宣称完成。
3. **ProcessScope+各physical调用链**：先提取现有POSIX范围机制，保持terminal同步thread/owner不变；接Windows scope和terminal physical；随后同一窄机制接MCP/hooks/preview及environment probe，删除各处killpg/terminate-root Windows placeholder。不共享业务state，不能把MCP暂接terminal的8槽或把terminal变成async executor。
4. **三条IPC分别hard cut**：先gateway/client/Application一起引入typed listener，删除Windows socket_path；再diag server/CLI一起迁有限channel；最后Node anonymous-pipe reader/writer/join与runtime main同步改UTF8wire。每条都有旧owner close/cancel/失败路径，不改变外部protobuf/控制消息。
5. **DB/auth与desktop资源**：依赖安全文件/IPC接缝；Node/postgres-auth helper或native binding必须实证、只组合owner，不另起DB服务。resolver/recipes/平台路径由desktop工作包接齐，避免在每个Python owner猜`bin`/`Scripts`。
6. **集中验证与删除检查**：原有产品合同tests在POSIX、Windows target跑；平台机制用对应target物理tests；static import/依赖方向检查确保Win不会导入pty/fcntl/termios/Unix flags，不以skip换绿。旧systemcalls允许留在对应POSIX实现，不能留shared业务owner的不可达分支或fnctl-None成功路径。

这不新加G0或测试轮数，本轮也没有执行这些未来步骤。已有G0和W1–W7仍是激活权威；新组织节应只补文件映射/依赖顺序和hard-cut条件，不扩张业务scope。
