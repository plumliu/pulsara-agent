# 会话恢复、载入与开屏延迟

日期：2026-10-21；第四轮完成：2026-10-22

## 范围与产品边界

优化用户选择已有会话后，从「可恢复」到「已载入／当前会话」的物理准备时间。
侧栏的三个状态仍分别表示 durable 会话、当前进程已发布 Host、当前页面实际连接；
不得提前把正在准备的运行时显示为已连接。首屏历史及其上下文准备完成后才展示工作台，
快速切换、失败重试、目录丢失时只读历史、草稿保留和控制权接管语义不变。

本规格是本次实现的有效约束。`archived_docs` 中的旧实现规格仍只作历史背景。

2026-10-22后续授权：搜索延后维护从阶段性讨论转入生产硬切，详见
[整轮历史搜索延后维护规格](PULSARA_DEFERRED_HISTORY_SEARCH_IMPLEMENTATION_SPEC.zh.md)。
该规格取代第九轮“暂停搜索生产实现”的阶段限制，其他载入优化合同继续有效。

## 执行路径与所有权

1. `LocalSessionController` 继续拥有同会话恢复合流、生命周期门禁和 Host 发布。
   `KernelHostCore` 继续获取 canonical writer、扫描配置、构造并注册 Host。
   不缓存跨恢复的历史状态或配置，不改变 writer 和清理边界。
2. `McpHostSupervisor.start` 发起所有启用连接，按 required 优先的物理顺序使用现有
   discovery semaphore。只有 required 的 READY 是启动屏障；optional 继续由原连接任务、
   SDK、重试及关闭 owner 管理。删除原先 optional 最多三秒的启动等待和对应构造参数，
   不保留兼容入口。required 启动 watchdog 和原资源边界保留。
3. optional 的迟到发现继续进入现有 pending candidate 与 safe point 安装路径。
   打开会话不创建 provider epoch；真正调用模型时仍遵守原冻结和选择入口。
   不新增 provider prefix 重建边界，不改同 epoch SYSTEM、tools 和 append-only messages。
4. `BrowserRuntimeConnection.open` 仍按顺序完成 controller／observer HELLO admission。
   两条独立 attachment 的 canonical snapshot 与 observer live-control snapshot 重叠读取；
   必须等二者完成才能返回连接。失败或取消时取消并 join 衍生任务，再关闭 attachment，
   保留原错误种类。live-control 使用原 observer 订阅及自己的 epoch/revision。
5. `RendererBridge.connect` 在 attachment 初始化后，以已恢复 Host 的实际 workspace root
   执行 `observe_workspace`，在同一响应返回 `workspace_availability`。前端安装该观察，
   移除连接成功后阻塞打开的第二次 GET／canonical session lookup。
   观察仍允许目录在连接过程中消失；后续显式动作仍由原 owner 重新核对权限和目录。
   丢失目录的 cold history 分支保持原单一路径。

## 首屏 canonical snapshot

PostgreSQL 继续拥有事务隔离、关系约束和当前事实；protobuf SDK 继续拥有序列化和
`ByteSize()`。Pulsara 只调整同一个 REPEATABLE READ 事务内部的读取和报价顺序：

- session-wide control 只读取一次。同一事务内只有 unresolved tool attempts 受正在扩大的
  entry window floor 影响，逐候选只更新这一 slice；不把窗口外的旧 attempts 混进首屏。
- attempts 的总数和受既有显示边界约束的行由同一 SQL window count 返回，计数仍包含
  未展示的行。保持 ROOT 优先、开始时间和 ID 顺序。
- 每个候选仍精确计算完整 wire 大小，包含固定宽度的 SHA-256 字段与真实 cursor、control、
  notices。只为最终选中 snapshot 计算原 namespace 下的 fingerprint；返回 bytes 和 digest
  与旧路径一致。超预算候选的 control 修改不能污染此前已接受的 snapshot。
- 不删消息、不缩小原单页预算、不增总历史上限，不新增 durable 状态、缓存权威、表或事件。

## 验证与测量

实验脚本、原始时序及日志放在 `output/session_load_optimization/`；它们是可删除诊断产物。
生产配置通过 `require_pulsara_home`／`LocalSettingsStore` 只读加载，托管 PostgreSQL
通过现有验证入口确认。真实会话只在 READ ONLY 事务测量，不获取生产 writer；
集成测试只创建／删除 fixture 自有 `pulsara_test_*` 数据库。

必须验证：

- optional 阻塞时 start 已返回，required 仍严格门禁且优先调度；迟到工具通过原 safe point
  可见，取消／关闭等待真实物理清理。
- snapshot 精确 byte 截止、拒绝最后候选后的 attempts 窗口、受显示边界约束时的完整计数、
  interruption、fork、compaction、内容回读与 fingerprint 均不变。
- 连接并行时两种 baseline 各自继续观察；snapshot 失败、live baseline 失败和取消均关闭
  原 attachment；快速改选会话的迟到成功／错误不能覆盖新选择。
- 前端不再等待额外 workspace 请求，但安装的目录可用性必须来自该次实际连接。

已取得的局部时序（不将局部节省冒充完整桌面启动时间）：

| 场景 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 受控阻塞的 optional MCP 启动屏障 | 3.00125 秒 | 7–14 微秒 |
| 固定真实会话首屏 256 entries，五次中位数 | 461 毫秒 | 187 毫秒 |
| 同一首屏每次 SQL 查询数 | 4,439 | 1,366 |

真实全部 7 个会话 × 4 个 byte 预算，在同一只读 repeatable-read cut 中做 28 组严格对比，
deterministic protobuf bytes（含 snapshot fingerprint）全部一致。
两条 256-entry 会话的同事务对照分别约 402→162 毫秒、454→225 毫秒；
时序受本地数据库和测量开销影响，不据此声称稳定 p95。

补充完整后端连接实验：隔离 home／数据库，130 个正常 canonical 回合、260 条历史，
真实运行 `Core.resume → RendererBridge.connect → 两条 Unix attachment → 256-entry snapshot
→ connection payload`。cold 样本 188.6 毫秒，已载入后的四次连接为 142.3–146.5 毫秒；
五次返回时 loopback optional MCP 的 `tools/list` 均仍阻塞。
此实验不含 HTTP 传输、前端上下文请求与绘制，也不调用模型、用户 hooks 或外部 MCP。
真实 256-entry 会话的首屏 `browser_history_context` 另测为 66.9–76.4 毫秒，
未发现需要继续扩范围处理的秒级开销。

验证结果：MCP 广覆盖回归 540 项通过，最终相关四文件 117 项通过；
canonical／history／fork 回归 85 项、snapshot／workspace／runtime 回归 29 项、
bridge／protocol 回归 53 项、prefix／startup／architecture 回归 20 项通过。
这些批次有重复覆盖，不合计为独立用例数量。
前端 74 文件、1,146 项全量通过（两 workers）；TypeScript、ESLint、Python Ruff 和
desktop 前端构建通过。默认全并发首轮有一项 scheduled 页面的一秒等待超时，
该文件单跑 22 项及两 workers 全量重跑均通过，没有修改断言或等待时间。
ESLint 的五条既有 warning、构建的 bundle 大小提示保留，没有为本次优化扩展无关改动。

源码及配套 desktop 静态资源已更新；本次不替换正在运行的桌面进程或已安装应用包。

## 第二轮：剩余载入成本与开屏

### 首屏查询与序列化

- snapshot 在同一事务中增量组装一个 protobuf，按原顺序核算每个完整候选；超预算时精确
  回滚刚加入的 entry、notices、control 和 cursor。只在最终反转 entries 一次，保持原 wire
  顺序与最终 fingerprint，避免逐候选复制全部历史。
- entry selection 同时观察该行是否增加 unresolved attempt；仅在窗口实际增加这类行时
  更新 attempts control。RUNNING turns 的既有全局覆盖保持不变。
- notices 继续只检查当前真正纳入候选的 entry，按其 canonical owner kind 与 ROOT scope
  选择原 reader，不提前解析可能被 byte 预算排除的深处内容。
- assistant blocks 依照 canonical parent-kind 关系约束读取；user/tool-result 不再查询必为空
  的 assistant blocks。不会减少合法 assistant 的 reasoning、visualization 或 fork 校验。
- browser context 在原 exact entry set 授权、cut 校验和初始 byte admission 之后，于同一
  REPEATABLE READ 事务批量查询 neighbours／segment；沿用原关联谓词，不引入读缓存。

实测固定真实 256-entry 会话，第二轮相对第一轮：snapshot 五次中位数 181.48→93.43 毫秒，
SQL 1,366→556；context 65.19→47.02 毫秒，SQL 579→68。输出分别仍为 851,352 与
132,607 字节。七会话的 28 组 snapshot、28 组 history page、112 组 context／错误对比全等。

### 模型目录与本地就绪

`DesktopApplication` 继续负责本地 HTTP、protocol、数据库和关闭；`ModelCatalogOwner`
及其 SDK 客户端继续负责远程目录读取、解析和可选模型事实。应用启动发起一次初始刷新，
本地资源与 schema 准备完成后即可就绪，远程下载不再属于整体开屏屏障。

初始目录 task 是进程内 advisory 准备，不是 durable job。bootstrap 的
`model_catalog_loading` 只投影该次尝试是否尚未结束。UI 沿原 configuration read owner
后台观察，250 毫秒仅为单次观察间隔，前一读取结束后才安排下一次，不限制总观察次数；
成功或失败 settle 后停止观察，不自动重试目录下载。观察不反复切换全局 reconnecting、
不重复读取 session list，也不改变当前导航或会话连接。

models.dev 配置在目录尚不可用时保持真实不可用，刷新后自动安装新的模型配置展示；
用户声明的目标仍按原解析入口处理。不虚构可调用模型或提前接受缺乏目标契约的执行。
scheduled tasks 在 schema READY 且初始目录尝试结束后启动，避免仅因刷新并行而误失败。
目录失败后沿原调度行为处理，用户仍可使用既有手动刷新。关闭先取消并 join 目录任务，
再关闭它可能唤醒的原服务，不允许关闭后的任务重启 scheduler。

### Electron 与 Python 准备重叠

旧内核的 OS lock 等待保持原 owner。其完成后，Electron 启动一个 Python 子进程，
让应用 import 与现有 `BundledPostgres.start` 重叠；数据库 owner 完成后才发送原 `start`
frame。无新增宿主消息或 readiness 阶段，数据库操作仍受原 frame 和 Python lock 门禁。

PostgreSQL 准备失败时，未启动的子进程通过 stdin EOF 退出，宿主等待真实 close 后再
抛出原数据库错误。`stop` 等待该准备与清理；准备期间的退出状态查询不能抢在 start frame
之前进入管道。子进程提早失败的拒绝会立即被观察，不能在数据库仍结算时成为未处理异常。

### 轻量导入与资源构建

移除 `capability` 包根的全量重导出，生产调用方直接从实际 owner 模块导入；home resolver
不再因包初始化而加载全部 Skill／Plugin 管理机制。不保留 facade 兼容或 lazy 属性分派。

Python 依赖同步和应用 wheel 安装由现有 uv 的 `--compile-bytecode` 在构建时预编译。
uv 与目标解释器拥有字节码生成和失效规则；不复制编译器、不维护自己的缓存登记或指纹。
安装、重装和资源目录搬移都必须验证导入正确，不能把首次用户启动当作字节码构建阶段。
electron-builder 的资源复制会改变源 mtime，故编译通过 uv 的原生
`PYC_INVALIDATION_MODE=CHECKED_HASH` 配置使用 CPython 自己的内容校验。
搬移后的测试禁止源编译和写入新 pyc，仍须成功加载并正确重定位 `co_filename`。

### 开屏测量边界

第二轮证据放在 `output/session_load_optimization/phase2/`。原真实远程目录刷新耗时
约 1.49–2.43 秒，完整 baseline child 的进程启动至 READY 为 3.17 秒。
真实 Electron 导航至 renderer startup-ready 预热约 80–83 毫秒，之后原淡出约 200 毫秒；
最小 entry 拆分的预热收益约 2 毫秒，未保留该实验改动。保留原开屏文档、淡出、隐藏／
最小化／销毁／替换清理以及 inert 门禁，不通过提前 ready 声称提速。

同入口 fresh-process 真实公共目录对照，本地内核 READY 从 3.170→1.497 秒；后台目录本次
仍耗时约 1.969 秒。READY 时 bootstrap 必须返回 HTTP 200、数据库 ready 和
`model_catalog_loading=true`，目录完成后变 false。本地工作台可用时间改善，不代表目录
相关模型能力已同时可调用。独立 Node＋随包 Python（当前 source 注入）＋真实隔离
PostgreSQL 对照，暖启动的串行准备 2.016→并行 1.508 秒，数据库准备各约 482／484 毫秒。

轻量入口七次样本中位数：repo home resolver 257→29 毫秒、wait-owner 283→162 毫秒；
随包解释器加当前 source 的对应样本约 275→22 毫秒、284→153 毫秒。
完整 application import 未出现稳定下降，不把轻入口收益加算为全量导入收益。

字节码实验使用 output 下的隔离资源副本：清除 site-packages pycache 后首次导入
application 耗时约 12.33 秒，现场生成 1,747 个 pyc；构建时预编译后导入样本为
1.91／1.64／1.57 秒。该实验包含文件系统与编译缓存冷热差异，只作为将编译移到构建期的
证据，不据此宣称每次实际开屏固定减少十秒。

最终 repo `desktop/resources` 已走原 uv sync 与应用 wheel 安装流程刷新，未替换已安装应用。
纯随包 `-I -B` 导入禁止 site-packages 源编译并验证通过，共准备 4,753 个 pyc。
原 `--wait-owner` 路径进一步把 settings 导入放回实际运行方法，只保留 OS lock 所需模块；
最终纯随包七次样本中位数为 home resolver 21.65 毫秒、wait-owner 36.78 毫秒。
这是无旧 owner 占锁时的入口开销，实际旧 owner 收尾仍会按原协议完整等待。

第二轮验证：snapshot／history／fork／context 90 项及额外内容／协议回归 97 项通过；
capability／CLI／home／runtime 相关 371 项、真实 wheel 安装／重装／复制资源测试 5 项、
catalog／HTTP／launch／runtime 42 项、数据库 reset 8 项、桌面宿主 Node 19 项通过。
最终轻入口另测 13 项通过，批次间有重复，不累加为独立用例数。
前端 75 文件、1,149 项全量通过（两 workers），TypeScript、Ruff、ESLint 和前端构建通过；
ESLint 保留原五条 warning。真实 Electron StartupCover smoke 通过，所有新启动实验均
使用隔离 home／数据库并由原 owner 清理，未调用用户 hooks、外部 MCP 或模型。

最终纯随包复测清除了 `PYTHONPATH`，直接运行已构建 wheel 和配套静态 UI：
独立 Node＋全新隔离 PostgreSQL 样本从 home 解析至 local READY 为 2.392 秒，bootstrap
HTTP 200、数据库 ready、目录 loading=true，随后正常停止并清理。
进一步用真实 BrowserWindow、StartupCover、production preload、HTTP 与 React 运行，
从 home 解析开始至实际可交互为 3.385 秒；其中工作台导航至可交互 379 毫秒，
包含原真实淡出 205 毫秒。进入 startupSettled 时 `inert=true`，淡出后由原 React 路径
解除为 false，未提前取消门禁。全部内核／数据库／Electron owner 正常退出并删除临时 home。
这一全新数据库的单次完整开屏没有同口径旧版对照，不能与上文内核 READY 对照混作总提速。
完整原生证据是 `output/session_load_optimization/native_boot_phase2_bundled.json`。

## 第三轮：减少关系查询与数据库内部重复扫描

第三轮沿用全部产品和资源边界，只调整同一只读事务内部的查询。PostgreSQL 继续拥有
隔离和集合关系运算；Pulsara 继续按原候选顺序投影、验证并报价，protobuf／JSON 的
返回顺序、内容、fingerprint、cut 与错误种类和出现位置不变。

- 已有单页 admission 所界定的 entry 集合可一次读取 tool result、输入来源、ROOT final
  和 visualization／notice 是否存在等有 schema 硬约束的小字段。来源关系保留旧查询的
  单行选择语义，不通过普通 JOIN 扩展一个 entry 的行数。
- assistant blocks 与 provider replay 可在同一实际候选内合并查询，UNION 不得把整个
  replay body 按 block 数复制。ROOT 的 blocks、fork body、replay 保留原读取和验证顺序。
  replay、TEXT／blob、notice detail、model binding 等正文不整页预取，也不改为跨候选缓存。
- 原路径会投影并验证首个超预算候选，然后拒绝它；更深的候选仍不触及。批量读取小字段
  不得把深处的正文校验或损坏错误移到这一边界之前。
- browser context 的 exact entry 授权、cut 验证与空 envelope 预算先于关系读取。owners 的
  边界、ROOT 邻居和任务 ancestry 使用集合查询，仍按原逐 owner／entry 顺序 append 和
  check_budget。imported interruption 只批读是否存在的布尔值，outcome JSON 读取、字段
  访问及 int 校验延迟到 group 实际触及；segment start 只在实际需要时读取并在本次调用内复用。
- creator attempts 继续使用原 named cursor 与物理 fetch size，按实际交付 chunk 集合
  统计 accepted task 数；batch ID 仍由唯一 attempt 经现有 ID builder 正向推导。不得
  添加总 attempt 上限，不物化整个 cursor，不用另建关系或反向 hash 索引替代。
- 数据库内部对同 owner／事件 cut 的重复扫描也属于优化对象；只采用保持 exact cut、
  mount、邻居和 segment 语义的集合计算。不得为减少语句数扩大正文物化范围。
  segment 只物化页内 owner presence 集合；每个 owner 的存在性探测使用原事件 cut 与
  owner 条件并按 event_sequence DESC LIMIT 1 停止。candidate event 不物化，实际边界
  仍按 event_sequence DESC → 原 lazy mount 关系 → LIMIT 1 选择；无 segment owner
  一次判空即可跳过各 entry 的重复扫描，不对长期 owner 的全部 adoption 计算 mount。
- 历史窗口的 entry JSON 正文只计量一次，后续候选只重算真实 envelope／cursor／coverage
  与标准 JSON list 分隔符。继续用同一个 JSON encoder，不估算字符串转义或缩小预算；
  首个超预算候选的投影和校验顺序保持原样。
- protobuf history page 与 snapshot 同样在单个 SDK message 中报价；quote 使用整个
  ServerFrame.ByteSize，拒绝候选完整恢复 entries／notices／has_more／cursor，最终只反转
  entries 一次。通知顺序与 request_id、完整 envelope 的 varint 开销保持原值。
- controller 的逐调用授权和 resume owner 的生命周期重新观察保持原边界；不为省去一次
  查询改变未授权请求与参数错误的优先级，或复用跨事务的会话事实。
- 物理连接仍逐次验证 endpoint、数据库身份与完整 clean ledger。相同 deadline 导出的
  statement_timeout／lock_timeout 由 PostgreSQL 同一 SELECT 设置，保留原 signed-int32
  可表示边界、远 deadline 的 0 语义、autocommit 恢复及失败关闭，不引入连接缓存。

实验和严格旧新对照位于 `output/session_load_optimization/phase3/`。SQL 计数包含真实
driver dispatch 的 SELECT／DECLARE／FETCH／CLOSE；此前 context 的 68 次仅计
connection.execute，完整物理命令口径为 74 次，不能将两种口径混用。

第三轮验证：entry／snapshot／history／fork／protocol 125 项通过；context 的 28 项通过，
其中包含 97 组 fixture 内同一只读 RR cut 的旧新精确 JSON bytes／错误对照。
最终增量报价相关回归 56 项、request_id／notice 回滚／多 entry 双向 cursor 等边缘 7 项通过。
上述批次存在重复覆盖，不相加为独立测试数。
新增回归证明逐条 tool result／输入来源 SQL 被集合查询替代、跨 session 在查询前拒绝、
ROOT fork 正文只在实际候选触及时读取；replay／notice 深处损坏在旧新 reader 上均保持
原预算边界。imported outcome 的缺 key、无效字符串、null、巨大对象与超长整数也不能
抢在先前 group 的预算拒绝之前报错。

连接设置与 clean migration 共 18 项在独立临时 PostgreSQL 通过。托管随包数据库缺少
迁移测试用于验证「保留无关扩展」的 pgcrypto；该失败保留在 connection-regressions.log。
生产代码没有 pgcrypto 依赖，故只向临时 PostgreSQL 副本提供本机 PG17 已有扩展并按原
配置 preload pg_search，所有 server／fixture DB／临时目录均正常关闭删除。没有新增
运行时扩展、改变 clean-v0 或安装系统依赖。

最终应用 wheel 已重建并安装到 repo 的 desktop/resources，未替换已安装应用。
纯随包解释器以 -I -B 导入真实 wheel，禁止 site-packages 源编译，验证本轮 entry batch、
segment owner presence、增量报价、timeout batch 均已装入，且使用 CHECKED_HASH pyc，现场源编译为 0。
日志是 phase3/final-resource-build.log 与 phase3/final-resource-import.log。

第三轮最终性能对照保留上一轮 reader 及 fork owner，真实全部 7 会话共 279 组 protobuf／
JSON bytes、异常类型及细节严格相同，包含 102 组预期错误、双向 cursor、boundary context、
精确 size／size-1 边缘。证据是 phase3/exact_oracle_current.json。

主时序采用同一 READ ONLY exported RR cut，每次 API 新建原 verified connection 并导入
该 cut，避免单连接反复执行时的 prepared/generic plan 影响。交替旧新、丢弃两轮预热、
每个场景保留五次样本的中位数，连接准备与 API body 分开记录。下表为真实 xyse 会话：

| API | 事务内 SQL（旧→新） | API body 中位数（旧→新） |
| --- | ---: | ---: |
| snapshot，256 entries | 556→194 | 96.51→73.85 毫秒 |
| history page，256 entries | 545→183 | 150.96→68.31 毫秒 |
| anchor window，128 entries | 205→77 | 88.71→31.11 毫秒 |
| exact context，256 entries | 74→17 | 47.77→28.11 毫秒 |

另一真实 kkx 会话的对应 SQL 是 685→170、674→159、221→90、39→16，API body 中位数
分别为 117.40→79.99、164.98→74.77、84.66→35.84、41.42→19.32 毫秒。
每次连接准备中位数约 4.4–4.9 毫秒；原单连接 RR 对照也保留，不混作主基准。
完整数据位于 phase3/exported_timing.json 与 phase3/alternating_timing.json。

表中 SQL 包含 API body 真实 DECLARE／FETCH／CLOSE，不包含连接设置、schema verification、
事务控制和 controller 外层授权。controller 全连接计量另存在 phase3/controller_reads.json；
timeout 设置合并使每个新连接少一次 SQL。完整 cold resume 的两次 summary 及 loaded
resume 的一次 summary 保留，未用跨生命周期缓存换取更小计数。
最终 controller 的 xyse 样本包含全部上述物理开销：snapshot 91.42 毫秒／215 SQL、
context 39.23 毫秒／38 SQL、window 44.04 毫秒／98 SQL、history page 78.71 毫秒／204 SQL，
均为两次独立物理连接。单次 read_session／loaded resume admission 均为 11 SQL。

最终 owner presence 计划的样本执行时间为 23.074→7.222 毫秒，buffer hits 24,680→9,147。
32 次合法同 ROOT adoption 的最新单 entry 计划中，两次有序事件探测均只读取一行，mount
只执行一次；initial、混合 sparse set、首次 adoption 前 old cut 也与旧路径逐字节相同。
未保留强制物化全部历史 segment 的中间实验，也未增加索引或新 schema。

独立 fixture 中 260 条历史、真实两条 Unix attachment 的最终 connect→payload 样本为
cold 118.6 毫秒、loaded 四次 64.3–70.4 毫秒；snapshot 256 entries、optional loopback
tools/list 仍阻塞，未调用模型、用户 hook 或外部 MCP。原同 harness 的早期样本为 cold
188.6 毫秒、loaded 142.3–146.5 毫秒；该 synthetic 测量不含 HTTP、context 请求和 UI 绘制，
不当作生产总会话或完整开屏的提速值。证据是 phase3/full_connect_final.json。

## 第四轮：有界正文查询与完整进程冷启动

继续减少实际 snapshot SQL，并用真实 Electron 主入口测量父进程创建到 shell UI 实际
接收可信点击、输入的耗时。保留 home 解析、single-instance、旧 owner lock、PG、Python、
bootstrap／sessions、StartupCover 淡出与 inert 门禁；只测后端 READY 或取消遮罩都不算
完整可交互。新 home 首次初始化与已有 home 的 fresh-process 冷启动分别统计；OS 文件
缓存没有清空时必须明确，不将其称为重启机器后的磁盘冷启动。

OpenAI SDK 继续拥有客户端、响应解析、错误和 HTTP 行为。Pulsara 的 timeout policy、
纯 provider projection 和检索配置不需要提前导入整个 SDK；实际建立 client／发送检索
请求时通过 SDK 原公共入口导入。无自制懒模块、代理对象或 SDK 复制实现；认证 omission、
process credential boundary 与清理仍由原 owner 管理。所有只是单纯导入的优化都须检查
首次实际调用路径，不能隐藏错误或静默丢掉能力。

用户明确选择「界面先可交互，SDK 可在后台继续准备」。两个 SDK 的预热只做正常模块
导入，不创建 client、MCP 连接、模型请求或 provider epoch，不成为 shell ready 的新门禁。
DesktopApplication 继续拥有这项 process-local 后台任务，关闭必须 join 实际导入线程，
不能只取消 asyncio 包装 task 就遗失线程；错误通过原日志可观察，实际动作仍由实际
SDK／service owner 准入和报错。不增加跨操作缓存权威、持久状态或 readiness 协议。
MCP 的轻量 contract／空 supervisor 和未用 OAuth 管理不应因模块 facade 而载入 SDK；
实际 SDK 边界使用普通局部导入。OAuth service 首用对象由同步锁保护，并保留原 settings、
lane、credential boundary、browser callback 与关闭语义。没有 MCP 配置的首次 resume
必须无需等待 SDK 预热，后台未完成和已完成两种情况都要实测。
首次实际模型／检索调用若遇到尚未完成的 SDK 导入，必须异步等待普通导入线程，不在
HTTP event loop 上等待 Python import lock。该 import-only 准备不创建网络 client；取消
必须等实际线程结算再返回，重复取消也不得遗失线程。客户端随后仍由原同步 SDK factory
建立，原请求、credential admission 和关闭行为保持不变。

开屏文档、旧 owner 等待与 Python 只读导入可重叠。旧 owner 成功结算后才准备 PostgreSQL，
开屏加载与 PG 都成功才发送原 start frame；cover／PG／old-owner 失败与退出均完整 join
已开始的原 owner，防止迟到 cover 覆盖失败页。保留原 200ms／reduced-motion 淡出和
React startupSettled 后解除 inert 的路径。

第四轮正文关系查询继续只使用同一只读 RR 事务和原候选顺序，新增的只是操作内物理
读取窗口。复用既有 replay carrier／canonical blob 的 16 MiB 物理边界，将窗口内
binary DataRow 的真实／保守上界字节合计限制在该值：每行固定报价
`1 + 4 + 2 + 20×4 = 87` 字节，分别覆盖消息类型、消息长度、列数和 20 个列长度；
再加实际 text／bytea 字节及固定宽度整数。行开销也界定 Python 容器数量，不增加
entry／block／总历史产品上限。libpq result 与 Python raw rows 在 fetchall 时仍可能
同时存在，资源结论是一个 wire 窗口及其固定倍数表示，而非总进程 RSS 只有 16 MiB。

参数的初始计量只读取 TOAST compression／physical size，不将整页 JSONB 转成 text。
压缩参数保留原单候选读取；未压缩 JSONB 暂按 physical size 的六倍报价。只有整体已被
16 MiB quote 准入的分类窗口才调用 PostgreSQL `jsonb_each` 检查顶层类型，不递归遍历
嵌套值。可合批的是由 scalar 字段组成的 object；numeric 使用原生 JSONB→numeric
转换检查 scale 为零且位于 signed int64 范围。字符串 JSON escape 每原始字节最多六
字节，整数最多 20 字符，均由六倍 datum／JEntry storage 覆盖，标点、bool、null 和
键名也在该保守上界内。nested、压缩和其他 numeric 仅退出优化窗口，实际触及时继续
原读取；这不建立新的产品类型、深度或逻辑体积限制。

仅 client encoding 为 UTF-8 的连接启用正文窗口；其他编码继续原候选读取及原 loader
bytes，不改变产品支持范围或连接状态。正文查询采用 binary output，JSONB text 作为
UTF-8 bytea 运输。当前候选先从 raw
窗口移除，再在原 source attribution 后、原 fetchall adaptation 位置，使用当前
connection 注册的 `JsonbLoader` 公共 `load` 解码；不替换成默认 JSON parser，不宣称
支持无关 cursor-local adapter 覆盖。只有当前候选的参数／reasoning 对象被解码，已
消费的 raw 行不保留。ROOT assistant、可能读取 notice／visualization 的候选及退出
合批的正文都是释放屏障；ROOT 的 blocks→fork model／TEXT→replay 顺序保持原样。
单 entry 超过保守窗口报价仍走原单候选路径，不拒绝额外逻辑工作。首个超 byte 候选
仍完整投影和验证，更深候选的损坏、深 JSON、超长整数均不提前报错。

最初的整页 text-size 实验虽做到 106／58 SQL，但可能提前格式化被预算排除的巨大
JSONB，已撤回，不用于最终收益。最终保守版真实 snapshot SQL 是 xyse 的 194→167、
kkx 的 170→94；所有 driver dispatch 计量包含 DECLARE／FETCH／CLOSE。沿用 exported
READ ONLY RR cut、每次 fresh verified connection 的交替测量，snapshot API body
中位数分别为 78.58→81.57 毫秒、86.09→87.14 毫秒。本地 snapshot 没有提速且略慢，
因此只声明减少实际 SQL／往返，不将 SQL 降幅当成加载延迟降幅。

第三轮 frozen reader 保存在 `output/session_load_optimization/phase4/canonical_v3_phase3.py`。
最终 279 组旧新 protobuf／JSON bytes 与错误类型、细节完全相同，包含 102 组预期错误；
证据为 phase4/exact_oracle_current.json 与 phase4/exported_timing.json。
entry／snapshot／history／fork／protocol 最终 138 项通过；新正文窗口单独 13 项通过，
与前一批有重复，不相加。其中包括 LATIN1 默认错误及 custom loader 原始 bytes 等价 gate。
新 gate 覆盖 libpq 真实 binary row 报价、connection-specific
loader 延迟、真实大参数跨窗口、超窗口单 entry 退化、ROOT fork 屏障、非 ROOT replay
损坏延迟，以及 1e100000、16000 scale、1800 层 JSONB 和 3 MiB TOAST 的旧预算／错误
位置。Ruff 与局部 diff whitespace 检查通过。未修改 schema、持久关系或用户配置。

SDK 首用准备纳入原 required-first discovery reservation：先按原顺序取得唯一物理
lane，再异步等待实际 SDK import、构造 client／slot；connection-attempt watchdog 仍
从实际 `client.open` 开始。这样 import worker 的唤醒顺序不能让 optional 抢在 required
前握手。空 MCP 和 optional-only 的 `start` 不增加 import 等待，取消仍 join 物理 import。
两个受控新 gate 分别证明 blocked required import 保持优先、optional-only 不等待冷
import；连同原 SDK readiness／cancel／failure gate 共 13 项通过。

OAuth 首次实际访问在异步 HTTP／插件入口通过同一 service 的线程准备取得原 manager，
避免等待 import lock 阻塞整个 HTTP loop。manager 已存在时保持原同步 getter、不新增
yield，以保留 immediate replacement／cancel 的顺序；首次线程结算后再次核对关闭状态，
不能把关闭期间已释放的 manager 交给后续动作。同步配置绑定仍在原 filesystem worker
执行；插件 normalize 复用原 shielded worker 及取消后的 anchors 清理，不增独立调度器。

SDK／OAuth 的最后受影响回归 171 项、architecture／provider prefix 54 项通过；此前
MCP 广覆盖 492 项、Host／插件 124 项通过，批次之间重复，不合计为独立用例数。
OpenAI 实际 SDK、retry、检索凭据、long-horizon 和导入／取消回归 135 项通过；桌面壳
24 项、StartupCover 真实 Electron smoke、构建缓存 5 项通过。
全 src／tests／desktop scripts 的 Ruff 与 git diff whitespace 检查通过。

最终 wheel 经原构建入口安装到 repo 的 desktop/resources，原 SDK 依赖仍在构建包中，
不需要用户启动时安装。纯随包 -I -B 验证应用初始导入不载入 OpenAI／MCP SDK，随后
从实际首次使用入口导入两个 SDK 成功；site-packages 源编译被禁止且实际为零，应用和 SDK
均使用 CHECKED_HASH pyc。未替换用户已安装应用，未改 saved settings 或生产数据库。
证据为 phase4/final-resource-build.log、final-resource-import.log 与各测试日志。

最终 SDK 预热采用同一应用 task 内的一个后台 worker，顺序导入 OpenAI 与 MCP；各 SDK
分别记录失败，前者失败也继续准备后者。用户的界面优先选择不要求两者同时完成，顺序
导入减少它们与 HTTP loop 同时争用解释器的机会。不加等待时长、sleep、新就绪门禁或
跨操作任务 owner。关闭仍 join 原单个实际 worker。最终顺序版本的 63 项针对性回归通过，
包括两阶段分别阻塞时 HTTP 都可用、第二阶段重复取消关闭仍等待同一真实线程，以及
失败后继续另一 SDK 并记录各自错误。证据为 phase4/sdk-sequential-final-tests.log。

最终真实主入口时序由独立父进程在创建 Electron 前开始计时，执行未替换的 production
dist/main.js 和最终随包 wheel／静态资源，经过原全部 owner、淡出及 inert 门禁后，
实际可信点击导航并输入 coldprobe，核对可见输入框值。诊断只观察原 SDK owner 的开始／
结束，不替换其执行体，不使用顺序预热原型 flag。三组独立隔离目录各运行新目录、正常
退出后已有目录两次，共六次；全部 Electron／Python／PG 正常退出，目录被删除。

| 父进程创建至真实点击／输入成功 | 第三轮基线中位数 | 最终中位数 | 最终三次样本 |
| --- | ---: | ---: | --- |
| 新 home，包含数据库初始化 | 2.492 秒 | 1.866 秒 | 3.225／1.866／1.772 秒 |
| 已有 home，全部进程重新创建 | 2.215 秒 | 1.517 秒 | 1.525／1.500／1.517 秒 |

这是开发 Electron 主入口运行真实生产资源，隔离空模型配置的 shell 测量，不含安装包／
LaunchServices 的额外入口，也不宣称模型可发送。OS 文件缓存未清，三次中位数不是磁盘
冷缓存或稳定 p95；新目录首样本 3.225 秒原样保留。首次 APFS clone 路径访问曾有
22.089 秒才解除 inert 的异常样本，其诊断 selector 未完成交互验收，保留在 README，
不能悄悄删除或当作已通过样本。最终六次 SDK 均在真实 UI 可交互后完成，SDK 结束观察
中位数分别为 2.074／1.702 秒。目录刷新、SDK 完成和 shell 可操作分别记录。

最后使用完全私有 PG 的两 fresh application 首次恢复对照：SDK 尚未完成时 58.29 毫秒，
预先等 SDK 完成后 27.06 毫秒。并发 bootstrap HTTP 中位数／最大值分别为
0.887／5.069 毫秒和 0.820／0.917 毫秒。两者都实际完成恢复，前者返回时 SDK 仍在准备；
只能证明恢复没有等待整个 SDK，不证明背景 CPU 争用为零。该单对样本不估算稳定差值。
同一私有 owner 中只读注入 saved settings 的目录测试，local READY 为 1.061 秒，
HTTP 200、数据库 ready；9 个已配置 target 当时 3 个 status=ready，远程目录尝试耗时
1.401 秒，READY 后再 1.265 秒观察到全部 9 个 status=ready。未发送模型请求，这组是
owner 层测量，不能与完整 Electron 时序混算。私有 fixture／PG／home 均已清理。

测量期间用户从 macOS Dock 退出原 Pulsara，原 managed PostgreSQL 随之失联，造成一次
fixture 补测及清理失败。没有重启用户原服务；其自有测试库
`pulsara_test_main_48927_cbcd133ca8` 仍待原服务恢复后删除，未触碰生产会话。
失败及待清理目标保存在 phase4/managed_fixture_cleanup_pending.json；成功补测改由
全私有 PG 完成。最终汇总为 phase4/measurement_summary.json、final_native_main.json
及 README.md，旧 parallel 生产数据和未采纳实验均另存并注明边界。

## 第五轮：多次压缩的长会话、历史驻留与边界查询

本轮使用真实 canonical 写入与 compaction adoption 生成的长会话，在独立私有 PostgreSQL
和实际 Electron renderer 中对照。实验规模只定义测试输入，不建立总历史、epoch、turn
或 compaction 数量上限。保留前四轮所有既有行为；不改变模型输入准备、provider prefix
连续性、durable 表／事件／guard／job，以及用户级页面的读 owner。

前端继续由 CanonicalHistoryReader 拥有可删除的历史页、正文引用和进行中的读取，
Workbench 的现有 scroll／semantic anchor 拥有阅读位置。浏览器的滚动、观察器和布局
机制拥有可见性判定；本轮先复用原分页和 anchors，不引入另一个虚拟列表 scroll owner。
驻留工作集包含 viewport 交叠的已准入页、前后各一个相邻已准入页，以及最新 tail 页；
相邻页仅为跨边缘滚动和锚点恢复的物理缓冲。各页继续采用已有 128 entries／2 MiB
请求，初始 tail 继续采用原 snapshot admission，不以总访问次数限制继续翻页。

离开工作集的页及其 entries／context 可退役。剩余边缘恢复双向 cursor 时必须携带原
session、entry cut 与 event cut；所有旧内容都能通过原分页／anchor API 再次读取。
向旧页之前翻阅保持原 pair；若旧页 cut 早于已观察的后续 gap，则显式向后翻阅可使用
仍驻留 tail 从 server snapshot／window／context 实际返回的完整 cut pair，加当前
已验证 edge，交原 window API 验证并读取。不拆配两个 cut，不以 observation 数字
伪造 pair，不改原页 cut 或推进 observation cursor；tail context 未结算时不能据此
丢弃使该 gap 可重读的页面，失败仍沿原上下文重试。
不保存无限增长的已访问页目录、正文副本或高度表，不新增服务端缓存权威或持久索引。
正在定位、进行中的读取、真实文本选择、焦点／交互及 live handoff 可临时固定所需页；
固定结束后再回收，不截断选择、不丢失未提交输入或模型回复。显式展开／收起选择由
现有前端 UI owner 保留最小状态，不能通过永久固定所有曾展开页逃避正文回收。

已验证正文驻留在当前需要的 reference 中，跨对象同内容读取仍可合并进行中的操作；
删除独立长期 settled body map。原 page／snapshot inline content 已通过该页 byte admission，
随驻留页保留并在页退役时释放，避免缓冲页的小正文被卸下后增加 read-content 往返。
另外补读、或合并到仅含引用的新记录中的正文，以弱引用标记其来源；可回读的这部分
离屏 inline content 可卸下，不保存正文副本、额外 map 或新大小阈值。viewport／选择／
定位／当前交互及 live handoff 所需正文保留。一个较大合法正文仍按原 content 协议完整
读取，不添加新的单条或总历史拒绝条件。页预算不涵盖后来补读的全部正文，显式固定
也可扩大工作集，因此不能声称整个 renderer heap 只有 2 MiB 或恒定页数。

晚到的页／context／正文读取不能重新安装已经退役的页或污染新会话。工作集改变必须
保留现有 scroll anchor、前后 gap、跨页 group、compaction／interruption 标记、copy、
搜索定位、展开、失败重试和 live draft 交接。真实测试需前后翻阅及重新定位被退役内容，
同时记录强制 GC 后 JS heap、DOM 数、接口读取次数与实际交互时间，不只数 React 行。

边界查询继续在同一个只读 REPEATABLE READ cut 中运行。优化可由页内 ROOT edge 的
acceptance 区间反查 compaction occurrence，替代遍历所有 adoption 后逐个找挂载；不得
假设数据库／当前产品契约没有保证的事件与 entry 单调一一对应。保留无 ROOT mount、
同 edge 多次 adoption、旧 cut、sparse page、imported／fork 与 owner scope 语义。
segment 边界保留第四轮的候选选择与挂载语义，不提前解释未触及的正文。首版 segment
suffix window 在多 ROOT 长会话实测变慢，先行舍弃；后续单 ROOT 1,113 entries／4 次
adoption 补测发现原 segment 仍需 29–48 s，需继续优化。允许用反向增量 acceptance
上界选择页内实际需要的事件，再以页内 distinct(owner,event) 只计算一次 mount／
segment_start；不对每 entry 重算，也不预计算该 owner 全部历史事件的挂载。中间事件
集合最多来自当前已准入 entry 页，不产生新的总历史限制。只有两类历史形状均有实际
等价证据与收益后才采用最终统一查询，不保留 old／new 兼容路径。
继续原 named cursor 流式返回和逐项 byte admission／错误顺序，先证明精确 bytes／错误
等价，再采用有实测收益的查询。尾页、中段、最早页均测实际 rows／buffers／耗时；
SQL 条数与服务器内部扫描量分别报告，不通过扩大物化范围凑较少语句。

基线源文件、fixture、实际时序与失败记录位于 output/session_load_optimization/phase5/。
用户原 managed PostgreSQL 保持原状态，本轮不依赖其运行，也不为测量重启原应用。

第一阶段先修改 browser_history_context 的流式 compactions 选择。页内 edge 用其实际
subject occurrence 的最早 acceptance 作开区间下界，用所有更大 ROOT entry 的最早
acceptance 作闭区间上界；反向增量 MIN 避免重复 suffix aggregation。不假设 entry／
event 单调一一对应，当时先保留旧 segment 查询。5,000 entries／2,500 ROOT turns／
20 次真实 adoption 的最终 44 组同 RR bytes／errors oracle 全部相等，后端 production
focused 28 项通过。

每 API 使用新验证连接，旧／新交替，2 次 warmup 后各取 3 次。完整 context body
中位数：最早页 2,923→116 ms，中段 2,892→120 ms，尾页 2,948→139 ms，稀疏页
2,935→41 ms；连接验证／BEGIN／导入 snapshot 另计约 5–7 ms。每次成功 context 的
物理 SQL 仍为 16 条，包含 DECLARE／FETCH／CLOSE，本轮不宣称减少该接口的语句数。
独立带统计的 EXPLAIN 单 compaction SELECT 从约 5.6 s 降为 3–7 ms；正式完整接口
时序以 context_fresh_5000_20.json 为准，不混用两类测量。

前端使用原 page owner、配对 cut 和滚动锚点。补充 gate 证明 snapshot 已到 200 而
observation 尚从 20 重放时 tail 不会倒退；旧页晚到 context 也不能覆盖当前 paired
tail。折叠标题只固定该片段的首语义 entry，aliases 仅供恢复该片段，不转为全轮固定。
工具／reasoning 的显式展开选择使用 assistant owner 与局部 ID 组合，防止不同回复
重用 tool_call_id 或 live reasoning fallback ID 时串态。前端全量 75 files／1,159 项
通过，最终 tail 增量后的重点回归 139 项通过；批次有重复，不相加。

最终 frontend static 与 application wheel 已按原入口构建。随包 -I -B 验证应用仍延迟
两个 SDK 导入、首次实际 SDK 访问成功、CHECKED_HASH pyc 且 source compilation 为零；
最终 interval SQL 已在 wheel 中，252 个静态文件与当前 build 逐字节一致。TypeScript、
全 src／tests／desktop scripts Ruff 和本轮 whitespace 检查通过；ESLint 0 errors、
5 个既有 warnings。没有替换用户已安装的应用。

完整 HTTP 恢复补测另暴露 ROOT final 密集页面的逐条空读：本轮 5,000 条多 ROOT
fixture 的 connections 区间有 669 次物理 dispatch，其中 610 次 SELECT，与原 xyse
的 ROOT／final 分布及计数范围不同。允许在既有 _entry_relations 中只读取 replay
存在性 bool；只有确实不存在 replay 时跳过原必为空的 reasoning SELECT，有 replay
仍在原 candidate 位置读取／验证，不能预取 payload 或提前其损坏错误。

fork eligibility 的唯一权威仍为 read_fork_anchor。协议当前 candidate 已经读取的
blocks 可通过明确可选参数交该 owner 复用：先执行其原 metadata 查询与资格检查；
仅当所有相关 TEXT／utf-8 blocks 均已有 inline bytes（包括空集合）时，复用这些行
进入原 digest／size／UTF-8／nonempty／model binding 的唯一验证循环。若任一相关
TEXT 需要 blob，则仍以原完整单 candidate SQL 查询所有 TEXT／blob；不增逐 block
回读，不提前未来正文，不改变原 fork barrier、模型输入或创建 fork 的权限判断。
直接 fork 创建等未提供 blocks 的调用仍由同一 owner 查询它实际需要的正文；这是
已有 owner 的数据注入接口，不是兼容旧产品路径、另建 eligibility predicate 或缓存。
需用 inline／mixed blob／损坏／模型 binding／候选 byte 边界证明原结果和错误顺序。

后续最终 segment 查询只聚合一次该 session 的 acceptance 小字段，为页内确有边界
的 owner 形成可删除的 SQL metadata 集合；该集合不按 entry cut 删除更高 sequence，
以保留旧 cut 与乱序 acceptance 的原 max-mount 语义。反向 running MIN 为每个请求
entry 选择实际事件，页内 distinct(owner,event) 才计算一次 mount／segment_start，
segment_start 独自保留原 entry cut。没有 Python 全历史物化或正文／JSON／model
binding 预取，确切最新一条 entry 也不会要求挂载该 owner 的全部旧 adoptions。

最终 5,000+2 live entries 的 44 组 context 同 RR bytes／errors oracle 全等；单 ROOT
1,113 entries／4 adoptions 的完整 JSON bytes 两页也严格等价：中段 45.973 s→46.821 ms，
尾页 28.868 s→46.825 ms（各一对同 RR 样本，不称稳定中位数／p95）。两页 context
仍为 16 条应用 body SQL。最终 segment production focused 30 项通过，涵盖原
context／paging／interval 回归及 selected event mount 只执行一次的结构 gate。

ROOT 存在性／inline 复用的 snapshot／history／window／预算／cursor／错误矩阵
29 组全部相等；本 fixture 首屏 256 条中含 128 个 ROOT assistant，snapshot body
真实 dispatch 从 527 降为 271。该统计不含完整 HTTP 恢复链路的其他 owner 与连接
验证，也不能因两次 Unix attachment 就倍乘 snapshot 收益。ROOT、raw body window、
entry batch 与完整 fork 回归共 66 项通过，保留 mixed blob、坏 replay／model binding
及未来 candidate 的原预算／错误位置。新 gate 的两次初始 fixture 错误与修正均保留
日志，没有弱化断言或增加 skip／xfail。

最终源码冻结后再次通过原入口构建 application wheel，随包 -I -B 验证明确核对新
compaction interval、selected segment mount、ROOT 空读扣减均已安装；SDK／pyc／
252 个 static 文件的验证同时通过。没有仅更新源码而留下旧仓库内应用资源。

最终真实 HTTP pair 使用同 entry cut 5,002／event cut 7,523，首屏 256 条包含
128 user＋128 ROOT assistant，context JSON 都是 154,665 bytes。connections 全区间
SQL 从 669 降为 413，context 全区间为 37 不变，合计 706→450；包括 owner／连接
验证／BEGIN／COMMIT／ROLLBACK／DECLARE／FETCH／CLOSE，不与 reader body 计数混用。
请求耗时为 connections 160→169 ms、context 3,119→296 ms，合计 3,279→466 ms。
仅一对样本，不能声称连接请求本身变快或稳定 p95；该秒表不含 renderer／observe，
不能当成新的整应用冷启动成绩。完整 SQL templates 与次数保存在 http_final_*.json。

最终前端公平组使用同一个最终后端与同 head 5,002，旧／新都翻至 edge 1,419，
均实际读取 26 个 history 与 27 个 context。旧版自动补页使显式操作为 20 次，新版
为 26 次，按相同实际历史覆盖比较，不虚称相同次数物理滚轮。相同两次 GC／RAF／
静止复采后，旧版驻留 3,584 records／27 pages／83,018 DOM nodes，新版为
512／3／12,312。旧 heap 三次约 127.673／127.991／128.184 MB，新约
25.495／25.492／25.492 MB（十进制 MB）。正文每条约 1 KiB 且为 page-inline，
没有 read-content 增量，不能冒称大规模 supplemental blob 水平的实测。
初期单 GC 的 68.9 MB 异常以及不同 GC 方法的 96.95／25.45 MB 阶段值均保留，
最终比较使用 renderer_*_finalbackend_fair_gc.json，不删除异常来制造恒定 heap。

最终实际 Electron UI gate 已完成：原 Workbench 的首／中／尾定位、被退役区域的
newer 128→256 回读、native copy 1,042 字符与选区相等；CDP wheel isTrusted=true，
scrollTop 103420→103180。真实点击回最新后，原 repository 追加 2 条记录，head
5,004→5,006，新 assistant 唯一正文在视口可见、pendingContext=0、386 个 DOM
message 全部 ready。最终 UI failure=null、应用正常退出码 0。
此前输出探针在成功写至 5,004 后序列化 PreparedProviderInputCut 失败，原数据与
失败日志保留；只修输出脚本，未改生产关闭 owner。最终验收正确记录从 5,004 开始，
不覆盖公平组 5,002 的时序。所有本轮 UI 子进程和自身 Chromium 重建 cache homes
已按 owner 清理，详见 renderer_final_ui_smoke.json 与 ui_home_cleanup.json。

本轮私有 PG 最终经原 Node owner 正常 stop、退出码 0，私有 home 已删除；父进程
后验无 active probe，6 个自身 Chromium cache homes 全部移除。measurement_summary.json
状态为 complete、pending=[]，private_pg_cleanup.json 记录原 managed owner 未触及。
第四轮原服务停止时留下的自有测试库仍按此前 pending 文件记录，本轮未为其重启原服务。

## 第六轮：剩余快照 SQL 与长会话完整冷启动

本轮继续以第五轮最终源码／构建为基线，冻结到 output/session_load_optimization/phase6/。
ROOT final 密集的 256-entry snapshot 中，271 次 reader body dispatch 包含 128 次
当前 ROOT blocks、128 次 fork metadata 与 15 次其他读取。只有存在严格结果／错误
等价与资源边界证据的扣减才能进入生产，不为凑数字跳过 fork、replay 或源内容验证。

fork_history 继续拥有唯一 eligibility 判断和 source metadata。允许由这个 owner
提供当前 candidate 的 blocks＋fork metadata 联合读取；每次只读实际正在投影的一条
candidate，不预取未来 candidate 的 model binding、正文或 replay。优先使用实际随包
psycopg Cursor.stream 的 single-row mode，单 SQL dispatch、BLOCK 行先于 metadata
行按原次序适配，SDK 负责取消／drain／游标生命周期；不自建 JSON／协议 parser。
必须证明自定义 loader、非 UTF-8 client encoding、坏 blocks／model binding、mixed
blob、异常中断与连接后续状态，以及已有候选 byte admission 的原错误顺序。
当前 candidate 的 bytes／metadata 在 owner 结算后释放，不保留跨页 registry／缓存。
有 entry_relations 的既有 bounded projection 可复用这次联合读；缺少这些已验证事实
的直接单 entry 读取仍保留原 blocks→root_final→fork 顺序。这是现有调用边界差异，
不是旧／新产品兼容机制；两者必须复用同一 fork predicate 与 metadata owner。

前端现有 overview 启动同时后台恢复 saved session，shell cover 不等待恢复结束。
最早可信点击正在恢复的同一会话可能触发第二次 connect：output-only controlled
PulsaraApp gate 已证明同一 id／takeover=false 的两次调用与第一结果被 stale 关闭。
允许原 connection attempt owner 在相同 target、相同 intent 的进行中恢复之间共享
原 Promise；不新增持久 owner／全局会话缓存，不提前发布 active connection，不用
openingSessionId 的可见状态代替底层 attempt 身份。不同 target、takeover、重连及
会话删除／归档／卸载继续沿原取消与 stale-close 语义，失败结算后仍可正常重试。
变更不得让用户级页面依赖 active session，也不改变原 overview 默认导航。

完整冷启动时序必须从独立父进程创建 Electron 之前开始，执行最终 production main、
随包 Python／application wheel、原 PG／runtime／StartupCover／HTTP／Unix owners。
已生成的多次 compaction 长会话在启动前持久存在，PG、Python、Electron 均完全停止；
测量 shell 可交互、原自动恢复完成，以及原可信导航后正文／context ready、实际滚动、
复制和输入成功的完整端点，不以 READY、ref 出现或 inert 解除单独冒称会话可交互。
输入不发送，不调用模型。记录后台恢复与最早点击的实际 connections 次数。

保留真实生产 saved settings 为只读；禁止把实际凭据复制进实验 YAML、环境或日志。
隔离 fixture 可用普通配置 owner 创建无凭据的 user_declared 本地模型连接，明确其
性能范围不代表用户生产模型目录的完整配置。若比较 saved 配置，必须用既有只读
settings 注入、内存中的私有 DB override，不能写回配置。记录 SDK／目录是否仍在
后台准备，不把模型可发送与 UI 可交互混同。
主测可用原 Electron production main＋最终构建资源；如未走安装包／LaunchServices
入口必须明确说明。不能为了测 packaged 分支而改写用户 CLI launcher／协议注册，
不得覆盖 HOME 或重启用户原 managed PG。至少三次进程冷启动，OS 文件缓存未清则
原样注明，不捏造磁盘冷缓存或 p95。基线构建的时序完成前不重建覆盖该随包 wheel。

### 第六轮 SQL 候选的激活结论

原生 single-row stream 候选虽将固定首屏 body dispatch 从 271 降到 143，但同 RR、
fresh verified connection、交替运行的两 warmup／五正式样本显示 snapshot body
38.953→109.114 ms，拒绝激活。实际驱动 stream 绕过自动 prepare；不为此自建
PREPARE registry、协议 parser 或依赖私有 driver 状态。

相同 typed UNION 使用 SDK 普通 execute 的实验也不激活：完整 snapshot body
36.295→39.876 ms，history/window 同样回退。普通结果在全部 SQL 成功后才进行
客户端行适配。中文 model metadata＋LATIN1 client encoding 与 BLOCK loader 故障
同时存在的门禁已证明：旧版先抛 BLOCK loader 故障，普通 execute 候选先抛
UntranslatableCharacter，违反原错误顺序；保持原断言的失败证据保留。
这两条均仅是 output 内的实验，不作为生产兼容／备用路径；生产 fork owner 和
reader 保持第五轮最终实现，固定首屏 reader body SQL 仍为 271。

本轮激活范围仅为上述已验证的前端相同目标／意图连接尝试共享，最终构建与完整
冷启动实测照常完成。SQL 次数变少不是独立的激活依据，不能将未采用候选的数字
当作生产收益。全部原始失败、等价对照与耗时证据保留在 phase6。

## 第七轮：真实 DeepSeek 绑定与首轮完整冷启动

用户指定使用其保存的 DeepSeek `deepseek-flash`、chat-completions 配置。
通过 `require_pulsara_home()` / `LocalSettingsStore` 确认唯一匹配连接与真实 catalog
resolution；fixture 的 session/genesis/每一 turn binding 必须经既有 model runtime、
repository/admission/compaction owners 建立，不能沿用测试 helper 的 synthetic
binding，不能直接 UPDATE 历史来掩盖不匹配。先完成小 fixture 的恢复、模型绑定
可解析及正常退出门禁，再创建 5,000 entries / 20 adoptions 长 fixture。
不用发送模型消息来代替 UI 可交互验证，输入仍不提交。

原保存配置只读，凭据只留运行中内存，不复制到实验 YAML、环境、报告或 trace。
隔离 PG 使用已验证的私有 owner。正式配置读取优先原 LocalSettingsStore 的只读
read-through + 内存 private postgres override，保留读取/解析成本；如使用冻结
snapshot injection，必须另列范围，不能混作原配置读取的时序。沿用原 main、随包
解释器/wheel、原 child protocol；output-only application_factory 注入的差异必须
明确记录，不假称未经注入的 `-m` 入口。原用户 PG/CLI/协议注册仍不触及。

先冻结当前最终构建并完成至少三次进程冷启动基线，再覆盖任何随包资源。首次较慢
样本原样保留，不改作 warmup。独立诊断轮可记录父进程 spawn→最早 JS entry、
observer 自身开销、home resolution、previous owner、PG、Python import/prepare、
application ready、开屏、连接/context、可见正文和可信动作的分段。CPU/Timeline/
import tracing 的诊断轮不混入无 trace 的正式结果；不把复制/逐字符输入验证成本
算成产品等待，不把 Electron/OS 的 entry 前时间误归因于 React。仍不声称 OS 文件
缓存已清或样本足够估 p95。

生产优化必须由真实分段证据驱动，保留现有 schema/权限检查、预算/错误顺序、
原取消/关闭 owner 和 SDK 后台准备策略。不为开屏跳过资源准备、不重写 provider
prefix、不缓存跨边界的可变权限/目录、不增加持久化或任意总历史限制。第六轮
被拒绝的两条 SQL 合并路径不复活；新候选需独立证明正确性、资源与实际时延。
源码冻结后重建最终资源，按同配置、同 fixture 重测完整冷启动并验证正常退出。

### 已验证的输入渲染候选

真实 parser 的受控诊断显示，Workbench 订阅草稿 store 的一次普通字符变化，会
再次调用 128 个正文未变的历史 assistant MarkdownBody，单次 React commit 约
132 ms。该数字是 JSDOM 结构/成本证据，不当作原生用户输入时延。

允许仅以 React 默认完整 props 比较 memoize MarkdownBody，并在原 assistant owner
用完整依赖稳定 visualization 对象（entry ID、items、读取/thumbnail callbacks）。
保留 body、streaming、annotation、assistantChat、notify、visualization 全部更新；
不写忽略 callback/字段的比较器，不建立全局 AST/正文缓存，不增加历史上限，不
改变 scrolling/selection/hydration owner。输出探针必须保留原 parser 与 memo 行为，
不能把统计 wrapper 的执行次数误当成实际 Markdown parser 的执行次数。初次渲染
与重复草稿输入收益分别测量，不因 memo 就宣称首屏也变快。

### 真实配置诊断后的 backend 激活范围

独立诊断（不混正式秒表）测得：startup prepare/list 约147 ms，其中原资源准备
约119 ms，余下约28 ms为返回值被丢弃的 session summary/idle 枚举；catalog JSON
decode约44 ms、既有 projection parser约144 ms在事件循环同步执行；catalog ready
后九配置 HTTP summaries 的重复 selectable projection约40 ms。允许下列最小优化：

- KernelHostCore 显式 prepare_resources 仍调用原 _ensure_resources，保留 schema
  service、privilege验证、repository/blob-store/GC全部原准备。Controller.prepare
  仅等待资源并返回None，不再读取被丢弃的会话列表。database_state READY表达资源
  可用；session summary/idle的语义错误由实际list API继续报错，不被缓存、忽略或
  改为空列表。更改所有内部调用/测试到单一路径，不留旧prepare-list兼容接口。
- 单次 HTTP bulk connection summaries 在原同步无yield区间内取一次 selectable
  catalog，显式传给原逐connection投影；单条create/update调用独立取值。catalog
  为空及原可捕获错误仍得到原unavailable payload，不能变成新HTTP500。无全局或
  跨request cache，完整public payload、credentials边界和用户级owner不变。
- HTTPX继续拥有HTTP、response.json与client关闭，既有parse_models_dev_catalog
  继续拥有projection。仅将decode与projection移到asyncio现有worker执行；保留
  原client关闭先于projection的顺序、错误类型和完整snapshot publication。取消时
  shield并物理join已开始的worker，重复取消也不能留下未结算工作；原取消不被
  worker后续错误替换。不建新parser、长期cache、worker框架或持久job。

SDK import继续原后台任务/关闭join，HTTPX client构造不另改；完整schema/privilege
证明不裁减。此范围不承诺精确毫秒收益，须功能门禁及最终同配置构建实测验收。

独立 importtime 另测得 eager `PIL._imaging` self约40–42 ms，唯一开屏 eager入口
是头像模块。允许将Pillow import移到原上传worker首次执行：Pillow仍拥有全部
decode/crop/EXIF/像素边界；公开normalize_avatar的直接异常保持。HTTP上传adapter
在同一worker分类原四类无效图片错误并回原400，不吞其他错误；状态/读取/清除
头像不需要解码。保原8MiB请求边界、原子发布、锁与取消时不写入的语义，不建新
图片parser、后台任务或缓存。以原头像功能门禁及fresh-shell无PIL.Image导入验收。

### 第七轮最终构建验收结果

真实 DeepSeek fixture 经小会话恢复/关闭门禁，再生成5,000 entries、2,500 ROOT
turns、20次canonical compaction adoption；全部历史turn与当前session绑定为保存
的deepseek-flash/chat-completions实际resolution，未发送provider请求。

前后各普通进程冷启动3次、清private Chromium code/HTTP cache后3次，首次样本
保留。普通组开屏结束中位1.538→1.501 s，末尾正文/context可见2.193→2.028 s，
可信滚动/复制/15字输入完成3.738→2.175 s；清renderer cache组对应
1.489→1.416 s、2.151→1.960 s、3.694→2.105 s。每次完整动作均实际验证可信
事件、滚动变化、复制一致和nonce精确回显，零command/message POST。

首轮正文2.934→2.946 s没有变快，首轮完整动作4.337→3.102 s。最终首轮
spawn→最早observer首句546 ms（后两次105/133 ms），main require完成→runtime
start287 ms（后两次86/110 ms），PG done→READY747 ms（基线首轮1235 ms）。
首轮大部分额外等待已在Python之前出现，不能推断是SDK硬等待或单一OS缓存因素。
旧synthetic fixture的5.112 s仅作历史诊断，不与真实配置成绩混算。

独立instrumented原生pair的六字符按键→双RAF机会中位80.2→8.5 ms；DOM mutation
echo1.45→1.70 ms未改善，typing Markdown ancestry390.86→0 recorded samples，
另轮coverage无MarkdownBody/react-markdown执行。精确context HTTP complete→可见
末尾正文388.85→204.87 ms，首绘仍真实解析。以上不是正式冷启动分布或GPU呈现
时间。最终spans确认JSON/catalog projection在worker，bulk selectable只做一次。

随包真实reader只读审计仍为271条物理SELECT（cursor25/prepared246），首屏256条，
entry/event cut5000/7520，未激活此前被拒绝的SQL合并路径。减少SQL数不作为本轮
收益。源码与随包reader/fork保持第五轮最终路径。

完整前端1169项、后端相关98项、头像/延迟导入11项通过；TypeScript、Ruff、lint
无错误（保留既有warning）。最终frontend/desktop/application资源统一构建，
随包验证5个backend模块与当前源码逐字一致、252个static一致、SDK延迟导入与
后台join、Pillow上传时导入、memo完整依赖等门禁均通过。计量仍明确限制为
production main＋最终随包资源＋只读settings application_factory注入，不是安装包/
LaunchServices入口，未清OS文件缓存、未建立p95或真实服务成功结论。

完整原始计时、失败、profile、SQL、构建、门禁及清理证据归档于
`output/session_load_optimization/phase7/`，最终收口以该目录README与汇总为准。

## 第八轮：真实 macOS .app 与首轮 Electron/窗口启动

本轮以用户要求的真正打包 `.app` 为计量对象。现存 installed/release bundle 早于
第七轮代码，仅可作非正式环境检查；正式基线须先将第七轮最终 main、随包Python/
application/static完整打包并逐字核对，再冻结至少三轮基线。正式启动用原
LaunchServices、app.asar main、原 `-m pulsara_agent.desktop_runtime`、原start-frame、
正常Pulsara home/保存配置/managed PG，不使用require-wrapper、Python factory、
假isPackaged、复制凭据、symlink绕过或改写主入口。原七个会话不reset、不改历史。

现存真实会话最大仅261 entries、一次compaction，不能冒充多compaction超长会话。
允许在已核实本机managed DB通过原repository/admission/compaction owners创建
明确命名的5,000-entry /20-adoption benchmark session，全部实际DeepSeek
deepseek-flash/chat-completions绑定、零模型请求。seeder只持有新session的writer，
结束关闭自身host，不停止app拥有的PG。工作区为本机空临时目录；实际hook/capability
discovery如实记录，不伪造“无discovery”。验收后仅用既有session删除owner清除本轮
自建fixture，保留原七会话并核对entry/event/binding，恢复原UI preferences。

真实packaged启动会正常更新CLI launcher和协议注册，不能由探针跳过或monkeypatch。
保存原非secret launcher字节、mode和协议owner，记录正常副作用，收尾恢复；原保存
设置仍只读、不复制。保留已安装bundle，实验打包产物置于output/phase8。

完整退出必须走正常Quit owner并验证main/helpers/Python/PG均退出；窗口关闭不是
退出。优先实际Dock Quit；若自动化无法操作Dock，必须保留工具失败并明确阻塞，
不能把菜单Quit、kill、仅隐藏窗口假称实际Dock操作。允许通过CUA Terminal执行
系统open向精确.app路径发LaunchServices启动请求，并明确与Dock单击启动区别。
为被动观测可传loopback remote-debugging端口；正式分组写明该开关及其扰动边界。
单独无debug启动检查不能和带debug样本混成同分布。

UI操作全部通过CUA真实系统输入。外部开发探针仅被动采集Network、context/body/
inert/composer/viewport与时间，不调用CDP Input、不代替点击/设置active view。完整
计时从启动动作发出前开始，外部就绪端点、首次可信动作和工具往返/人工导航等待
分别标记，不把人工或工具调度成本误当产品卡顿，也不把可见正文当全部交互验证。
不清OS文件缓存、不把三样本称p95、不发送provider请求来证明编辑器可用。

诊断可另用Electron/Chromium原生startup tracing和系统CPU sampling观察pre-JS、
默认menu、协议注册、BrowserWindow/WebContentsView、原home解析和Python准备。
诊断开销不混正式基线；trace没有命名的native调用不能仅凭时间gap归因。优化仅在
真正packaged证据支持后激活，保持原单实例/home、startup cover/fade/inert、IPC/
安全设置、关闭/取消/PG owner、用户级页面与后台SDK策略。源码冻结后重新打包、
验证最终.app资源并按同fixture/同端点重测。

### 首次授权干预与构建缓存候选边界

首个真实.app样本的home解析子进程曾在CPython编码初始化的字节码os_replace/
rename处长等待，随后用户明确报告系统“允许Codex启动Pulsara”提示未及时确认。
此样本标为首次系统授权干预，原始marks、sample与等待保留，不计普通无提示
启动基线，不把约83秒单独归因于缓存、磁盘或应用代码，不绕过任何系统保护。

另有独立构建事实：prepared SDK的stdlib timestamp PYC在原资源中有效，真实
electron-builder资源复制改变源码mtime而保留旧PYC header；30个未被首轮运行
覆盖的配对仍可逐字验证源码相同但mtime不匹配。依赖/application已有checked-hash
编译路径，stdlib未覆盖。这不证明系统授权等待由它造成，但说明打包后会产生
可避免的源码编译和.app资源写入。

允许最小构建修复：CPython继续拥有compiler/importer/invalidation，Pulsara原
prepare_python_dependencies owner使用随包目标解释器的stdlib compileall，在
build期以force＋checked-hash模式编译stdlib，显式排除已有uv编译owner负责的
site-packages。保留源码和完整内容校验，不写自定义loader/parser/cache，不新增
指纹字段/registry，不修改运行时resolver、单实例、Python原-m协议或安全检查。
不以全局环境变量改变用户工具子进程，不添加未经证据支持的early menu/LS重排。

门禁必须实际证明：已有有效timestamp cache被转换；复制后源码mtime变化仍无需
source compile/写缓存且执行正确正文；源码内容变化仍被CPython发现并执行新正文。
最终新.app中stdlib cache模式、源码/资源、首次普通启动前后cache元数据及无提示
启动时序分别验收；不能把修复83秒等待当作已证明的收益，也不能把三轮中位差异
自动当作独立显著改善。


### 第八轮最终激活与计量结果

最终包保持原production main/原Python -m与真实保存配置；字节核对18 dist、596
application/static（含252static）一致。build-cache完整7项及Ruff通过。stdlib编译
排除表达式只锚定实际stdlib/site-packages，含同名祖先目录的正向测试仍需编译。
新包首次真实启动也由用户明确报告系统授权弹窗，保留原raw/sidecar并排除普通
启动统计；准确批准时刻未知，不能假造扣除等待后的全新包无干预首启。

同一最终包再经正常Quit owner完整停止main/Python/PG，三轮进程冷启动的描述性
中位数shell3.672s、target initial context4.278s、真实CUA导航调用→末尾正文305ms。
基线2/3/4 shell/context中位3.277/3.862s，新样本未证明冷启动改善；不将其中差异
归因于某一未独立定位调用，也不将微小nav差异宣称收益。最终三轮完整真实wheel /
精确1024字符复制 /15字符输入严格门禁全部通过，完整外部操作wall中位28.564s，
包含CUA/AX/导航前间隔/observer核验，不能当产品独占耗时。只有基线4通过当前strict
full门禁，不编造三轮paired full收益。观测使用显式loopback调试参数、50ms轮询，
未清OS缓存；启动方法是用户明确允许的system open/LaunchServices，不宣称Dock点击。

缓存修复作为独立构建正确性修复保留：新包5439个PYC（stdlib686）全checked-hash，
首启及全部四轮后same-app size/mtime/header对比没有added/removed/changed；内容变化
仍由CPython正确失效。这个事实不构成latency win。本轮未改SQL路径，不宣称271次
快照查询进一步下降。trace/sample未独立量化menu/LS/resolver，未激活运行时重排。

最终外部正式端点后仅通过正常controller删除本轮benchmark；原七会话entry/event/
model binding严格不变，原prefs字节/mode、CLI字节/mode及实际协议URL
/Applications/Pulsara.app全部恢复，owned空工作区移除。根独立核对无live .app进程和
managed postmaster.pid；LaunchServices各进程退出码未知，不能合成0。保存配置始终
只读，未reset数据库、未替换installed.app。完整证据在output/session_load_optimization/
phase8/README.md、external_runs_summary.{md,json}、cleanup.json与根复核artifact。


## 第九轮：附带发现的写入、目录与前端问题

用户明确授权处理搜索投影写入成本、HTTPX同步构造、草稿订阅范围、五条Hooks警告及
第四轮遗留测试数据库。保留前八轮修改，不更改provider输入/epoch/搜索召回排序/
总历史可达性，不新增durable事件、job、receipt、cache authority或兼容双路径。

HTTPX继续拥有原AsyncClient、TLS/CA/环境代理、超时、重定向、连接池、HTTP请求与关闭。
实际0.28.1源码确认构造包括同步传输依赖导入、证书加载和未打开连接池；网络后端的
异步使用始于后续原aenter/get。Pulsara仅用既有joined线程worker构造未打开客户端，
后续aenter/get/JSON/HTTP close/projection顺序不变。构造被取消必须先物理join，若
已生成client，须在原事件循环关闭且join，重复cancel不能让它泄漏；原取消错误优先，
late构造/关闭失败被消费。不得只移SSL加载却漏首次传输依赖导入，不改HTTPX默认
代理/证书策略，不把取消变成等待整个HTTP请求timeout。fetch_override不构造HTTP客户端。

搜索先以canonical读取为唯一正文事实，在同一原projection savepoint内复用构造结果
及去除重复question刷新；不把旧可丢弃projection作为canonical前缀，不丢QA/QAQ/
include_tools/前问题受后问题影响的刷新。完整搜索文档重写与BM25重索引仍有随长度增长
的物理成本，必须测量并如实保留其限制，不能将常数削减冒称严格线性。

前端用React既有订阅与完整props比较缩窄无关草稿变化的更新，保持当前session文本、
附件数量/状态、queued edit/accepted restoration、选区/焦点、技能菜单及已有历史
read/scroll owner。Hooks逐条按真实生命周期修复，不禁lint，不靠漏依赖冻结状态。

旧测试DB仅验证精确pulsara_test_main_48927_cbcd133ca8，使用原BundledPostgres维护
owner及原fixture删除owner，先核实本机datadir/角色/目标owner/无活跃连接，原七会话
entry/event/model binding与DB inventory前后对比；不reset原库，不删其它pattern匹配库。
若目标已不存在则记确认absent。正常stop并确认物理退出，不运行.app以避免无关注册。


### 第九轮范围收紧（用户后续指示）

用户要求本轮克制修改，并先讨论搜索投影与草稿订阅的语义；已经写入的修改无需撤回。
据此停止搜索生产实现（仅已有私有baseline诊断正常收尾）与前端扩大改造，不新增
projection增量/去重/BM25/schema变更，不新增ConversationMessages memo。已写的草稿
primitive订阅、技能子菜单text订阅、accepted layout单slot state及五条Hooks修正保留，
仅完成必要验证并说明边界，等待讨论后再扩展。HTTPX构造/取消/关闭最小适配和精确
遗留测试DB清理完成收尾。不重建静态资源、wheel或.app，不替换用户选择暂用的版本。

HTTPX最终6项新增门禁覆盖真实HTTPX环境代理路径、TLS证书校验配置、构造线程与原
事件循环HTTP/close边界、在途HTTP取消、构造中重复取消、late构造/关闭失败结算；
相关9个文件104项测试通过，Ruff通过，保留原aiohttp弃用warnings。未据此声称完整
冷启动提速；本轮未重新计时整包启动。


第九轮源码冻结验收：HTTPX相关104 tests、Ruff通过；已写前端缩窄与Hooks修正
focused 200 tests、TypeScript、lint 0errors/0warnings。accepted layout descriptor
与当前session不匹配时在原component状态边界丢弃，覆盖发送后切换、late接受后
切回不重放旧focus/动画。搜索生产及测试未改，仅诊断/讨论；独立PG/home已正常
关闭移除。精确第四轮旧DB已删除并后置确认absent，原七会话严格不变，旧pending
标记closed且保原失败证据。未重打包/替换已安装app，后续讨论再决定搜索或草稿
进一步改动，不能把本轮源码验证说成当前已安装版本生效。


## 第十轮：累计diff审查后的目录观察恢复修复

根与三名subagent并行审查累计diff，确认一个P2：初始目录仍loading时，后台bootstrap
临时失败会终止观察；用户手动全局重连成功若仍返回loading=true，原effect依赖不变，
目录后续settle无法自动进入UI。独立真实PulsaraApp/MemoryView复现失败保留在
output/session_load_optimization/review_phase10，未把既有1172项前端green当此场景覆盖。

用户授权仅修复此点并将全部性能改动stage/commit，无需再作subagent复审。修复复用
原userServiceStatus门禁与effect生命周期：只有全局服务online时观察目录；失败/
reconnecting使旧观察清理，重连完成online即使loading仍true也重新安排观察。前台
重连bootstrap/list尚未结算期间不会另起背景bootstrap，不新增owner/状态字段/重试
任务，不重试远程catalog下载，不限制总观察次数。原配置owner仍拒绝迟到旧响应。

新增正式回归覆盖临时poll失败→原记忆页手动重连→前台pending期间无竞争轮询→
重连仍loading→目录ready→原草稿保留、发送启用→settle停止轮询，无模型提交。
保留前九轮全部性能源码、测试及既有构建修复；提交前同步frontend static，不重打包
或替换installed.app。output诊断不纳入提交。


本次修复验证：目录/应用focused 188项通过，完整前端77文件1173项通过；TypeScript与
lint通过（0warning）。这次用户明确要求全量提交已审阅的性能改动，因此前九轮源码、
对应新增回归、构建修复及本规格一并纳入，output实验/测量与私有资源不进入Git提交。
