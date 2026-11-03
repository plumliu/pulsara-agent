# Pulsara 工具输出与分页重构实施方案

日期：2026-11-03。状态：已实施，三项技术合同与逐条语义审计完成；单一路径 hard cut，无兼容入口。本轮合同相关验证通过；第 8 节行为回归已闭合，完整 Python 回归零失败。视频理解继续延后，不在本轮实现。

## 1. 目标与边界

模型只需要三个动作：继续源数据时复制 next；补读本次已产生正文时调用给出的 read_file；需要真实文件时用 write_file(source=原结果)。正常源页一次完整交付，不再套一层正文分页。

- 完整删除 read_artifact、export_artifact、ARTIFACT Target、artifact ID kind、旧 mode/disposition/字段和专用正文路由。不保留 alias、双读写或兼容路径。
- C/P/V 分开：原结果正文可恢复，native 展示有界，Code Mode 获得完整公开业务值；本次正文完整不代表整个源完整。
- 保留源权限、取消物理清理、已发生副作用、文件编辑观察、memory provenance 和 Code Mode 主动披露边界。
- provider 的 SYSTEM/tools 在同一 epoch 内逐字节不变，messages 只追加。只有新 cold epoch 与明确采用的 compaction successor 可重建根前缀。
- 不新增正文/搜索/分页身份、持久注册表、事件、产品关系、任务或总历史/总结果/总调用上限。现有单次协议、blob、bridge 和物理资源边界继续有效。
- 所有 Pulsara 身份复用共享 ID 层。现有通用 blob、canonical 图片、文件内容修订、pg_search、PostgresConnectionLane.ARTIFACT 资源通道保留各自用途，不能按关键词误删。
- 数据库使用 clean-v0；核验本地目标后可重置。验证优先独立临时库，保存配置只读，实际密钥不进入证据。

## 2. 三项技术合同

本节是实施共同接口；不允许各组自建近似的正文引用、分页器或预算公式。修改必须保持第 1 节的 provider prefix、身份与持久化边界。

### 2.1 合同一：正文、展示、来源与事务

**C/P/V 三个值的职责。** C 是一次接受结果拥有的唯一可读、可复制正文；P 是本次 native 实际交付的展示；V 是 Code Mode 的完整公开业务值。P 可以省略，不能冒充 C/V。源分页工具的 C/P/V 都只涉及本次实际源页，P 完整交付，不再给该页追加第二层正文分页。图片沿原多模态 owner，不转换为文本或 Base64。

| 工具类别 | C 的固定定义 | 必须完整保留的事实 |
| --- | --- | --- |
| exec_process / manage_process 日志观察 | 同锁选中范围的完整 sanitized UTF-8 TEXT | PROCESS、动作、真实状态/exit_code、cursor、retention gap；非整个进程生涯 |
| manage_process close_stdin/kill | 实际控制回执固定 JSON，无日志候选 | 真实控制及物理结算状态；不把日志元数据包装为空正文 |
| MCP META/direct/resource | 本次完整公开响应的固定 UTF-8 JSON | 工具接受状态、来源与远端效果是否未知；不扩充远端参数 |
| read_file / search_content / find_files 与源目录页 | 实际交付页的固定 JSON | 原 source、范围/位置、覆盖/诊断及完整 next |
| edit_file | 本次实际完整 diff TEXT；若无 diff 则实际短回执 JSON | 成功/不确定状态、PATH、新 revision、变更数量；不重做 edit |
| write_file / memory mutation / run/send/管理回执 | 实际已提交的固定 JSON | 对应业务状态、身份/修订及必要 advisory，不用旧源查询替代原回执 |
| Work/schedule 单项、记忆查询、其他大值 | 本次完整公开业务值固定 JSON/TEXT | 原 target、业务状态、查询限制；列表先精简，不能以读正文代替正常源 next |
| wait/stop 大集合 | 当次真实绑定/结算集合固定 JSON | 等待原 Work、实际取消范围、集合数量与读取入口；不重 wait/stop |
| 正式 CELL_OUTPUT | 已接受 CellOutputContent.body 的原 UTF-8 JSON envelope | record 与原 cell provenance；不重新挑选 text 或改变 surrogate 转义 |

**共享类型。** 新 `ports/tool_output.py` 定义 `ToolOutputBodyCandidate(text, source_format, source_coverage, source_coverage_reason=None)`、`ToolOutputBodyView`、`ToolOutputContentError`、`ToolOutputReadPort` 和共享 SESSION_ENTRY Target schema。source coverage/format 继续使用既有闭集语义，不新建另一组近义枚举。正文 carrier 不携带独立 ID、role 或可推导的原始字节计数。

`ToolExecutionResult` 增加/保留单一新字段组：`output_body_candidate`、`native_output`（str 或 None）、`page_complete`（bool）、`required_facts`（冻结 JSON mapping）。KernelToolResult 对应字段相同，native_output 为 bytes 或 None。原 output/content 是完整公开 V 的编码；native_output 是业务 owner 已形成的 P 候选，未提供时公共投影负责产生预算内 P。required_facts 是实际状态及可行动身份，不能被通用缩略丢弃。page_complete 页不得再次裁剪。先 materialize C 确定真实可用性，再选择 P，最后由纯 prepare 组合 C/P；存储 owner 不第二次决定预览。终端 V.output 为完整日志，省略时 native 使用 output_preview，并准确返回 read。

**存储。** PreparedToolResultAcceptance 使用 `required_facts`（冻结 JSON mapping）、`output_body: CanonicalContent | None`、`output_unavailability_reason`、`source_coverage/reason`、`output_omitted: bool`。transcript 的 canonical_preview_content 保存 P；tool_results 用现有 CanonicalContent 六字段形状保存 C：`output_body_inline_content/blob_id/digest/size/media_type/codec`。同一 tool_results 行的 `result_facts jsonb` 保存明确声明的结构化控制事实，必须是 object；历史与精确正文读取复用它，不从 P 或 JSON 外形的业务 TEXT 猜测。短正文按既有 64KiB inline 边界，长正文按既有 blob 物理边界；不以 8,000 字节创建另一实体。descriptor/reason 推导可读性，不再存 artifact disposition。body unavailable 的闭合原因是 BODY_TOO_LARGE、BODY_PUBLICATION_FAILED、BODY_PUBLICATION_UNCONFIRMED；不能伪装空正文。

普通接受事务同时提交 transcript P、C descriptor、业务状态、原 memory provenance 和原有接受 occurrence。body publication 的失败/不确定确认沿原接受规则收束，不能重执行业务。直接记忆 mutation 必须在同一接受事务生成最终 canonical receipt 后 materialize C，禁止保存 provisional `{}`。ACK 确认比较实际正文 descriptor 或不可用原因；不能只比较 P。fork/import、GC 引用、clean-v0、expected catalog 和 UI 一起 hard cut，不增加事件、关系、receipt 或新 ID。

**精确来源。** `ToolOutputReadPort.read_body(target)` 在构造时绑定 session/workspace，仅接受 SESSION_ENTRY：session 可省，显式值必须等于绑定 session；entry 必须属于本 scope 的已接受 TOOL_RESULT，或 input_origin=CELL_OUTPUT 的正式 TERMINAL_OBSERVATION。普通聊天及其他 observation 不成为 source。返回补全 session 的原 Target、原 content bytes、media_type、codec、coverage/reason、原 model_visible_memory_fact_ids 与 required_facts；read_file 的结果页同时呈现这些结构化事实，write_file(source=…) 只复制 C 原字节。KeyError 表示不存在/不可访问；descriptor/size/digest/codec不符或 unavailable 返回 ToolOutputContentError，不重跑源。history 沿既有 domain/cut 授权复用低层 `read_tool_output_content(connection,row)`，不借精确入口放开冷历史。

保持原单一 memory exposure 集合语义，不增加第二套持久集合；结果正文读取继承它，source 写文件不标为正文已见。Code Mode 普通内部结果不自动向 ROOT 投递或枚举；精确读取维持原 session/workspace 边界，不新增披露 registry。

### 2.2 合同二：调用预算、完整页面与副作用顺序

共享 `primitives/tool_result_budget.py` 提供 `ToolResultPageBudget.fits(body: str, *, model_visible_memory_fact_ids=(), include_read=False) -> bool` 及明确的 `ToolResultPageBoundary`。fits 使用唯一完整 Q，包含正常/晚到 carrier、真实 call ID、闭合最大 timing、实际 provenance、完整 next/read 和转义；同时调用原 provider quote owner 检查本次 allowance。include_read 使用本次预分配结果 entry 的准确 read_file 调用，不用字符近似替代最终格式。

生产 KernelToolInvocationContext 和 ToolCall 均携带本调用 `page_budget`；同步 ToolCall 另携带 `cancel_requested() -> bool`，runtime 将其连接到现有物理调用取消回调。生产 native 调用不允许缺少分配后退回固定字节数；独立低层调用及 JS 内部非 provider 交付可使用同一个 Q 的物理单页预算，不伪造实时 provider 余额。

预算顺序固定：

1. response 后、效果前，以冻结 prefix、实际 assistant/replay、必要小回执构造 base；canonical/epoch/items 物理预留保留，不再为每个普通文本页假设需要满 40k 展示。
2. 使用原 provider quote owner 计算本批可用 wire/token，确定各调用额度；复用已有 image allowance 的等分和精确 quote 思路。文本替换本调用 base body，不作为新附件重复计费；不假设 token 差值任意可相加。
3. 域 owner 用同一个 fits 装页。最小原子记录放不下是 typed boundary，不跳项、不返回原地 next。控制/写操作在可确定实际最小成功回执时，必须先验证再执行效果；记忆事务在首次写之前验证最终小回执。
4. 公共投影为非分页大值生成 P；保留 required_facts，仅省略正文时添加准确 read。域已提供 native_output 时不重建另一份 head/tail；仍须完整预算验证。
5. 已发生效果的真实结果即使超出原 provider 份额，也必须先沿物理 Q 保存事实；不能转为 UNKNOWN 或重执行业务。最终派发仍须通过原预算 owner，不能把保存成功当成 provider 交付成功。
6. 存储 C/P 后最终整批由原预算 owner 校验、安装。已冻结页不得被下游缩为裸引用。资源不足沿既有中断/允许的 compaction successor 收束，不重装已安装 prefix，也不重执行已接受效果。

`page_complete=True` 的正常源页与完整短控制回执要求完整交付；旧历史进入批准的 compaction successor 时仍可归摘要。不增加总页数、总调用数或历史上限。显示默认沿现有量级，单结果 40k 是物理上界，不是必须填满的目标。

### 2.3 合同三：模型接口、源续查与激活

**完整调用引用统一为** `{"tool": name, "arguments": {...}}`。源页的 next 是原工具下一次完整调用或 null；省略的一次正文用 read 指向 read_file 的原结果 Target。不得同时暴露另一套必须自行计算的 next_offset/has_more 作为模型合同。

- `read_file` 首读 PATH：target、可选 offset（1起）、limit（保留现有单次行数边界）。续读 target、相同 limit、continuation `{line, char_offset, content_revision}`，offset 与 continuation 互斥。ENTRY 首读仅 target，续读 `{line,char_offset}`，不接受 PATH 行范围/revision。统一文本 packer 按完整行及行内字符前进；PATH 变版 stale，完整行实际交付后才授观察，ENTRY 永不授 PATH 观察。
- `write_file` 目标固定 PATH，content:string 与 source:共享 SESSION_ENTRY Target 严格二选一。源权限、目标写权限、成功位置回执预算先验证；owner 内复制原 body 全部字节，保留原子 create-only/no-clobber 与不确定发布结算，不经过模型/JS4MiB搬运。
- search_content/find_files 删除 offset。continuation 自含相对遍历 path、sticky partial、content 模式可选 line（有 line 为该文件内继续，无 line 为该文件完成）；完整 next 保留原 query/root/glob/mode/单次 limit。采用第 3 节 A++ 与默认 best effort。局部诊断保留命中及 PARTIAL、可继续；无效查询、解析、取消等真正失败不伪装推进。默认无全局总数，count 保每文件匹配行数。
- 源目录 Agent/Work/schedule/capability/history 也用同一 fits 与 next 合同，但保留各自既有 cursor/授权/动态观察语义；不造万能 cursor。对象当前详情引用使用 inspect；read 专指冻结正文或既有历史精确读取，不把当前详情查询当成旧结果恢复。
- CodeMode V 直接经过既有 4MiB bridge。复用现有可捕获 value_boundary：保留原业务 state/result IDs，附准确 read、正文可用性、覆盖及 required_facts，删除 artifact 字段；不调用原 V 的 delivery callback，不重做业务。正式 console/return 仍由原队列提交。
- frontend/protobuf/HTTP/gateway 精确正文读取改为 result entry，删除 entry→artifact→body 绕行；原文展开、迟到结果隔离、源缺失提示、复制和继续读取保留。共享 UI 文本页位置使用同一行/行内数据，不另造正文身份。
- 删除 Python/JS artifact kind、两旧工具/Target、旧 disposition/mode/unused enum、旧字段/DDL/index、提示词/skill/测试/兼容入口。通用 blob、提示词/图片存储资源通道保留。所有 ID 复用统一层。


## 3. 搜索续查：文件位置推进，不保活全局搜索

采用文件位置续查：普通定位查询早返回，完成文件不反复扫描内容，模型只复制 next。

### 3.1 扫描边界及代价

每页重新枚举当前合格文件，但跳过已完成文件的内容搜索；只对未完成的游标文件及后续文件运行 rg。页结束时所有本页 producer 都退出，不留下等待下一轮模型的进程或搜索会话。

对静态目录可持续遍历到结束，不新增结果总数、总页数、查询寿命或重试次数上限。对变化目录是逐页观察，不保证某一瞬间的全目录快照。

### 3.2 一页怎样产生

1. 解析并授权原 root，在该 root 用内置 `rg --files --null --sort path` 流式枚举候选。用户 glob 继续交给已有 wcmatch，不另写 glob 解析器。
2. continuation 的边界由原 root 相对的遍历路径及匹配行位置表示。跳过已完成文件，仅游标文件需要重新搜索并丢弃已交付行。展示 PATH 的 resolve 结果不作为遍历排序 key。
3. 将遍历中连续属于同一父目录的候选组成批次；批次受本页需求和真实 OS argv 资源约束。不能跨子目录把同父目录的文件全部合并而改变顺序。
4. 内容搜索保持目录方式：`cwd=parent`，参数 target 固定为 `.`，使用 `--max-depth 1 --sort path` 与每个 eligible basename 的锚定 literal glob。转义复用 `wcmatch.glob.escape(..., unix=True)`。不能改成 explicit FILE 参数冒称语义等价。
5. 单个 rg record 解码后立即生成定位摘录并交给 E 的页 packer，按本调用 allowance 和完整 Q 装页。文件路径、count 等记录保持原子性。
6. 达到页预算或语义范围后停止本调用 producer，关闭管道并等待物理退出；返回完整的原工具 next。没有跨调用 child、枚举器或后台全量缓存。

原请求本身就是显式单文件时，保持其显式文件分支，而不是强行转目录搜索。目录策略与单文件策略各自沿依赖原有行为。

### 3.3 模型只复制 next

初始调用仍提供查询条件；续查调用的完整 next 包含原操作、查询、已解析 root、glob、模式、位置和单次条数上限。模型不计算 offset，也不管理 search ID、PROCESS 或关闭动作。

若当前页停在 `b.py` 第 120 行，下一页跳过排在它之前、已完成的文件内容；仅重新搜索 `b.py` 并忽略已交付行，再处理后续文件。这里没有保存体读取层，next 始终指向原 `search_content` 或 `find_files`。

next 表示还有扫描空间，**不保证已知还有一条真实命中**。页满不为一次 lookahead 扫描巨大稀疏后缀；最终可能有一次空页加 `next:null`。只有确实正常走到序尾才报告结束。

### 3.4 模式、排序和计数

- `content`：每条匹配行返回围绕实际 submatch 的明确摘录和原文件行位置；长匹配只展示窗口并标明片段性质。完整当前源经 read_file 取得，不假装保留搜索当时从未保存的整行。
- `files_only`：每个匹配文件一个记录，继续点越过该文件。
- `count`：每条记录是该文件的匹配行数，需完成该文件搜索。它不是 regex occurrence 数，也不是全库总数。
- `find_files`：只枚举和筛选路径，不运行内容搜索。
- 删除默认精确全局 total_count，避免首屏仅为数字被迫扫描到 EOF。确实需要全量统计的任务可沿 next 遍历并汇总。
- 顺序明确采用 rg 的原生目录组件遍历序，替代旧 Python 全路径字符串排序。例：`a/b` 可位于 `a+` 之前。游标比较必须与这个顺序一致，不能使用会隐式 case-fold 或 resolve 的展示对象。
- `limit` 保持单次请求最多返回多少记录的含义，实际还可因 runtime 预算减页；next 沿相同上限继续。用户只要 5 条可请求首屏最多 5 条并停止；想遍历全部则继续 next。不能将现有默认 50、单次最大 1,000 偷换成整条查询的总结果上限，也不额外维护 remaining-total。

### 3.5 变化、重复读取、错误和取消

- continuation 是位置，不是消费型流，也不授予权限。每次重新检查原 root 和当前调用 scope；拒绝错误模式、越界或无效位置。完整 next 携带全部查询条件，调用者应直接复制；位置本身不是查询身份或已冻结快照。
- 重复同一个 next 是从同一边界重新观察。源和有效页预算相同才会形成相同语义页；源或预算变化可改变页长度，不能承诺精确旧页重放。每次使用实际采用页返回的新 next。
- 游标文件被删除时，从严格大于该遍历边界的文件继续，不将它误认为 EOF；root 本身消失返回源错误。插入到已越过位置的结果可能不再出现，改名到后面可能再次出现，这是 live 查询边界。
- 页满的内部主动停止不等于源 EOF。必须 drain stderr 并物理 join；真实失败不能被预期终止信号掩盖，不自写 rg stderr 文本分类器。
- 搜索默认 best effort：局部不可读文件或 stderr 诊断不阻断整页，有效命中照常返回，保留诊断并继续后续范围。扫描可能不完整时明确标记 PARTIAL，不将它冒充全源完整；不增加 strict/best-effort 参数或 stderr 文本分类器。PARTIAL 描述源扫描覆盖，不表示本页可以少交付字段；本页仍须按 E 完整交付。
- 查询本身无法执行、解析失败、超时或取消不能伪装成成功推进；原 continuation 可重试，先前已经成功交付的页不撤回。无诊断的页切出不证明 EOF，只有自然正常结束才能据此声明当前源穷尽；允许 PARTIAL 时还需保留整条续查的未完整事实。
- 删除当前第三次重复查询警告、第四次阻断。合法重读不应被当成查询次数上限。
- 没有进程内搜索游标过期；重启后只要来源和权限仍有效，就能从自含位置继续。

### 3.6 生命周期与实际复杂度

复用现有 IO 执行与并发 owner，每页私有路径 producer 和最多一个活动内容 producer；取消必须实际终止并 join 子进程，不能只取消 asyncio/to_thread 包装。用户不翻页时不遗留工作。并行查询继续排队执行，不增加任意 active-query 总量限制。

设页数为 P、路径量为 F：路径元数据仍约重复枚举 P 次，还包括父目录批次的直接子项。内容工作为后继文件搜索、游标文件重搜，以及页末批次的可能超前读取；不能声称每个文件在物理上恰好只读一次。一个巨文件横跨许多页时，仍可能重复搜索它的前缀。

不使用 `tail | rg` 或原文件字节 seek 假装消除重扫，否则会改变正则锚点、BOM、转码和行号语义。

Pulsara 驻留为页、argv/路径批次、管道缓存和**最大单条 rg JSON record**；不是严格 O(page)。逐 record 使用成熟 JSON 解码后立即释放，避免整份 stdout 与全部匹配常驻。rg 自身也可能需要容纳完整单行。不新增任意 record 总字符 cap，不为理论常量内存另造 JSON/编码框架；用最大单行峰值验收真实物理成本。

### 3.7 必须验证的行为

稳定源多页无遗漏；命中在第 500 字符之后；巨型单行峰值；游标文件删改与改名；重复 next 与不同页面预算；模式/位置冲突；最后空页；局部错误保留命中、诊断、可继续和 sticky PARTIAL；无法执行时不伪装推进；页满停止、取消和重启无遗留进程；每文件 count；ignore/hidden/binary/symlink 与路径转义。

内容扫描计数必须证明完成文件未再次提交内容搜索。游标巨文件跨页重扫是当前明确代价，不添加自动新旧算法切换路径。

## 4. 各工具的有效合同

| 工具 | 输出与续读 | 必须保留的语义 |
| --- | --- | --- |
| read_file | PATH 或 SESSION_ENTRY；统一行/行内续读，完整 next | PATH revision 与实际交付观察；ENTRY scope 独立校验，不授 PATH 编辑资格 |
| search_content / find_files | 位置续查，完整记录装页，无全局 total_count | 三种搜索模式、best effort、每文件 count、live 源、取消与物理 join |
| write_file | PATH 目标，content/source 严格二选一；source 复制全部原 C | 独立源与目标权限，create-only/no-clobber，副作用前回执准入，发布不确定事实 |
| edit_file | C 为完整 diff，P 为回执及实际可交付 changed_windows | 单次实际修改，精确 revision，未交付窗口不得授观察；Code Mode 回调在完整 V 交付后执行 |
| exec_process / manage_process | C 为本次同锁完整日志或控制回执，V.output 完整；P 仅尾部预览一次 | PROCESS、退出码、cursor、gap、stdin/PTY、kill 等待；读旧正文不推进进程 cursor、不重做动作 |
| query_processes | 精简元信息与管理入口，不产生输出观察 cursor | 进程库存不是日志读取 |
| query_capabilities | 自适应目录页，完整 next；MCP_TOOL schema 整体可用 | 原 list/inspect、准确 Target/invocation 与 schema 超界语义；UI slash 分页不受模型合同替换影响 |
| manage_capability / reload_capabilities | 保护真实状态、原目标与采用事实，细节存 C | 最小控制回执在效果前准入；新包 ID/随机 staging 路径等细节不强制占 P；采用失败不等于配置未修改 |
| use_new_mcp_tool / 动态 MCP / read_mcp_resource | 保存本次完整公开 JSON，省略时 read 原结果 | 原远端 schema/权限/绑定；再次 resource read 是新观察；不得为恢复结果重放远端操作 |
| search_sessions / search_session_content / read_session_content | 源 next 与精确正文/历史 read 分工；源页完整交付 | paired cut、ROOT/子代理目标权限、冷历史范围、候选 cursor 生命周期、占位图片原子性 |
| run_agent / send_agent_message | 短完整接纳回执、准确 Work/recipient 与 inspect | 接纳不等于完成；发送绑定原 turn；效果前实际回执准入，不为补结果重发 |
| query_agents | 精简 Agent/Work 列表与完整 next；Work 长详情存 C | 原可见性、身份与顺序；列表不携带完整 instruction/final/diagnostic |
| wait_agent / stop_agent | 保存当次绑定/结算集合；P 保护状态、数量与原结果 read | 不按展示预算缩减真实等待/取消集合，不追随后继 Work，不重 wait/stop 充当翻页 |
| query_schedules / manage_schedule | 精简列表、完整 next/inspect；事务生成真实短回执 | expected_revision、switch 原子性、delete 原回执；用户级页面保持用户级 owner |
| search_memory / get_memory | 固定 top-k 的 C 保全文，P 明示摘录，正文/关系各自说明覆盖 | 非库存分页；关系 100/64、exposure 50 仍是既有单次相关投影边界，PARTIAL 不冒充全部；旧正文不从当前记忆重建 |
| remember / mark_memory_relation | 同一接受事务生成最终 receipt、C 和 facts，写前预算检查 | canonical 写入与单一 exposure；禁止保存 provisional {}，结果恢复不重写记忆 |
| exec_cell / query_cells / manage_cell | 接纳/控制短回执；冻结源码页用完整 Q 与 next | 正式输出仍由既有队列接受；普通内部结果不自动向 ROOT 披露；不消费输出来查询状态 |
| open_in_pulsara | 整份展示控制执行前准入，不给展示效果做结果分页 | 顺序、逐项 errors、最终选中目标；不新增任意 maxItems |
| view_image | 沿原图片 owner 整体准入与真实多模态交付 | IMAGE_REF、精确附件、完整成功/错误分支预算，不变成 Base64 文本 |
| todo | 原计数短回执，安装前检查 | 原 checklist 及安装/取消结算，不新增持久状态 |
| enter_plan / ask_plan_question / exit_plan | 原短控制结果，C/facts 为公开投影 | 独占执行、批准/修订/取消、完整答案及 continuation；不把内部 storage carrier 当公开正文 |

## 5. 持久化、协议与 UI

正文 descriptor、必要 result_facts、source coverage 和 output_omitted 归现有结果行。接受事务与 ACK 确认必须比较真实 C descriptor/不可用原因和 facts，不只比较预览。fork/import/GC 与 formal CELL_OUTPUT 保持同一路径；SQL 对六字段 availability、coverage/reason、事实 object 与互斥不可用原因设闭合约束，NULL 不得绕过。

前端与 desktop/protobuf/HTTP 直接按原 result entry 读取正文；使用共享行/行内位置。P 标作本次展示，完整正文独立展开/复制；省略、不可用、迟到结果与原业务状态明确分开。关闭/重开 UI 不重执行业务。不新增专用正文实体或权限旁路。

离线 wheel/editable 构建还须比较已准备 Code Mode 身份模块与共享源码的完整字节值；不一致时要求运行既有 prepare_code_mode，禁止打包旧身份词表。此检查不引入文件指纹注册表。

当前实现入口：

- [完整页预算](src/pulsara_agent/primitives/tool_result_budget.py)
- [唯一 native 投影](src/pulsara_agent/conversation_kernel/tool_result_display.py)
- [正文存储及 scope-bound 读取](src/pulsara_agent/conversation_kernel/tool_outputs.py)
- [精确来源共享类型](src/pulsara_agent/ports/tool_output.py)
- [统一文本分页](src/pulsara_agent/primitives/text_paging.py)
- [文件读取、复制与编辑](src/pulsara_agent/tools/builtins/filesystem.py)
- [搜索扫描 owner](src/pulsara_agent/tools/builtins/filesystem_search.py)

## 6. 语义审计与激活门槛

主代理已在各组落地后逐项核验以下合同；本轮回归必须修复，不得删断言、skip、保留旧路径或将保存成功冒充交付成功。

1. 活跃代码、schema、身份、模型文档、协议与 UI 无 artifact Target/工具消费；没有新增替代 ID、注册表或持久事件。
2. C/P/V 唯一含义、正文可用性、实际 scope、正式 Cell envelope、直接记忆/Plan 接受事务与 ACK 一致。
3. 完整 Q 包括转义、正常/晚到 carrier、实际 provenance、next/read；provider 单调用份额和整批 quote 均通过，不为每个小查询预留满 40k。
4. 源页不可再裁剪；空 EOF 与无进展边界分开；超长行可推进；精确历史/body read 保留事实与权限。
5. 控制效果前最小回执准入；已发生效果只结算一次，正文发布/显示失败不得诱发重试或假回滚。
6. terminal 完整 C/V、一次 P、cursor/gap、控制回执、取消物理清理；MCP 使用原 schema，未知远端效果不伪装安全重试。
7. memory 单一 exposure、文件实际交付观察、Code Mode 完整 V 和 4MiB value_boundary、read/facts 保留且 callback 不误触发。
8. clean-v0 与 expected catalog 由真实独立 PostgreSQL 核验；原 DB 未经核验不重置；协议生成、frontend/desktop 构建与测试通过。
9. 真实 provider 验证批量小查询、连续源 next、原正文 read、直接 source copy、中间日志不重执行、Code Mode 与明确 compaction successor；同 epoch tools/SYSTEM 不变且 messages 前缀连续。

实施使用原 8 个 gpt-6.1-sol / high 代理分别负责 core/CodeMode、filesystem/search、terminal、capability/MCP、history、agent/schedule、memory/runtime、frontend/protocol。root 负责公共预算、执行集成与最终逐条严格审计。视频理解待本方案完整验收后再讨论接口。

## 7. 本轮验收记录

- 完整 Python 回归：5228 通过、0 失败、20 个既有 Linux/WSL 环境跳过、15 subtests 通过；未新增 skip/xfail。
- 前端 1570 项、desktop 使用项目 Node 22 的 140 项已通过；本轮未改它们的生产代码/CSS，真实 Chromium 行为测试由完整 Python 回归覆盖。
- 工具输出完整真实 provider 验证覆盖大结果、准确 source copy、Code Mode bridge 和明确 compaction；当前行为收尾另以 3 次真实调用验证 next 与两对 prefix 连续性。
- 打包、效果前拒绝、效果后保真、来源/权限、查询次数、精确观察等专项通过；生产 Ruff、协议生成与 diff 检查通过。
- 45 项原失败全部闭合。有意义的行为断言继续保留；仅冻结旧数量、字段、哈希、CSS 或接口实现的部分删除，不恢复已移除实体或接口。
- verified 本地测试优先独立数据库。最终全套使用临时原生 PostgreSQL runtime/cluster，保留无关扩展的测试资源不进入产品依赖。全部临时运行时、数据库和连接已清理；保存配置、现有 pulsara 库与已安装应用未改动。

## 8. 现有行为回归的统一合同

- 测试只冻结旧对象形状、字段或具体 CSS 数值且无独立行为价值时删除该部分；真实权限、状态、顺序、取消、清理、来源与 prefix 连续性要求继续验证，不通过兼容 API、宽松断言或新增 skip 获得通过。
- 进程控制反馈固定为 `user_control_feedback.v3`。原 `origin_subagent_task_id` 删除，不接受旧形状。canonical 内容保留实际进程的 `origin_turn_id`、`origin_conversation_scope_kind`（ROOT/SUBAGENT）、`origin_session_id`（共享 session ID）。这些值从接纳时冻结的 TerminalProcessOrigin 取值，不从接收 ROOT 推测。provider 来源投影为 ROOT 或既有 AGENT Target；当前反馈接收 session 与实际进程来源 session 可以不同。不新增 Task、ID、表或事件。
- ROOT 非 dry 的自动记忆来源准备，与基础语义编译并行执行。二者复用同一冻结 canonical facts；仍由原调用 owner 完成最终预算、来源组合与 prefix 安装。源准备 task 只属于本次 prepare；异常或取消时 cancel 并 join 后再释放原 handle/borrow。preview 与原 dry/compaction family 的召回资格不因并行而扩大；不建立后台任务、超时或重试上限。
- relay 的通用 opaque byte、seal、取消与 writer 回收在标准 Unix API 上验证；Linux O_PATH/SCM_RIGHTS/SO_PEERCRED/pidfd/CLOEXEC 的绑定仍由原原生集成矩阵验证。不得为测试增加 Mac 生产 fallback、弱化 Linux 鉴权，或删除原生安全验证。
- migration 必须保留预先存在且无关的 PostgreSQL 原生扩展，包括其 OID、版本与可用行为。测试采用 PostgreSQL SQL-only intagg；它是测试环境先决条件，不成为 Pulsara 的运行依赖。资源只准备到临时独立 PostgreSQL 分发与 cluster，不修改安装包资源、生产设置或现有 cluster。
