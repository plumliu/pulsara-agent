以下结论基于两类材料：一是你提供的 Pulsara 时间点附件，我只把它们视为本地 agent 核对后的实现事实/设计基线，不声称审计了未随附的 Pulsara 源码；二是我在 2026-09-13 实际查阅的公开上游固定 commit 源码、测试、issue/PR。附件明确说明当前生产链路仍是文本，D3 的 body/ref、confirmation、GC/fork 引用和 D4 的 retention/carrier 仍未闭合。 主设计也仍标为 DRAFT，D2 §9.3 是最终冻结候选，但未由用户正式冻结，也未实施。

本轮调查了 5 个项目：Codex、OpenCode、OpenHands 当前 canonical backend `software-agent-sdk`、Goose、Pi。没有发现一个项目完整实现了 Pulsara 所要求的组合：不可变图片 bytes + 跨重启完整命令确认 + canonical/blob 引用 GC + installed-prefix 普通压缩 + active exact 单一 placement + anchor-exact fork 独立生存。因此结论不是“照抄某一个项目”，而是有几个可复用的局部机制，D3/D4 的关键闭合仍需要 Pulsara 自己实现。

---

## 一、结论与推荐

### 1. D3：推荐“确定性有序 body + 显式 owner-specific image occurrence refs”

这是我认为可以直接闭合实施规范的方案。

不要把图片 blob ID 仅塞进 canonical JSON，也不建议新建一套规范化 media/part registry。Pulsara 已有不可变 blob owner，而且当前 GC 只认识 `context_snapshots`、`transcript_entries`、`assistant_message_blocks`、`prompt_queue_items`、`tool_results` 等显式数据库引用；JSON 中嵌套一个 blob ID 不会形成 FK，也不会被 `delete_orphans()` 看见。

推荐形状是：

```text
PromptContent
  parts: ordered tuple[
    Text(text)
    | Image(original_bytes, declared_media_type)
  ]
```

经过 D2 验证后，冻结为类似：

```text
TextPart:
  type = "text"
  text = exact original string

ImagePart descriptor:
  type = "image"
  ref = occurrence ordinal
  digest = sha256:...
  size = exact encoded byte length
  media_type = verified MIME
  width = trusted decoded width
  height = trusted decoded height
```

canonical body 按确定性 canonical JSON 编码保存顺序和 occurrence；图片 payload 只放既有 immutable blob。图片 A 在一条消息中出现两次，就有两个 occurrence/ref ordinal，但都可指向同一个 blob。

然后增加一张**最小引用关系**，例如 `canonical_image_refs`：

```text
ref_ordinal             int NOT NULL
blob_id                 FK -> blobs

queue_item_id            nullable FK
transcript_entry_id      nullable FK
context_snapshot_id      nullable FK

CHECK exactly one owner FK is non-null
UNIQUE(owner, ref_ordinal)
```

若 direct accepted prompt 在 transcript 建立前还有另一个现有 durable owner，应该给这张关系增加那个**真实 owner 的 FK 列**；这点必须由本地 agent 回查，不能凭附件虚构。

这张表不是第二份内容真相：body 决定语义、顺序、重复 occurrence、MIME/尺寸/digest；blob 决定不可变 bytes；ref 表只负责“这个 canonical owner 正在持有哪一个 blob occurrence”，从而给 FK、GC、fork 和 exact confirmation 一个数据库可验证的可达性边界。

这种分工与 OpenCode 的“完整结构值参与 admission equality”思想相符：当前 V2 `SessionInput.equivalent()` 比较 delivery/session 以及完整编码后的 Prompt JSON，而不是文本投影。 但 OpenCode 的 Prompt 图片本身只是 `{uri,mime,...}`， 所以不能直接照抄它的数据真相。

### 2. D3 的事务边界：Hook 后发布，blob + body + refs + command/queue 一次事务

推荐不要依赖“先 publish blob，再等 grace-period GC”的正确性。

顺序应为：

1. 在 Hook reservation / command freeze 前取得完整 bytes，完成 D2 全 multipart 验证。
2. 形成完整 frozen `PromptContent`，作为 in-flight equality 的值。
3. 从 frozen content 派生 Hook/Skill 文本投影。
4. Hook allow 后进入现有 command/repository writer 事务。
5. 在**同一数据库事务**内插入/复用 blob、写 canonical body、写所有 occurrence ref、写 command/queue owner。
6. 一次 commit 后才可能得到 FULL。
7. transaction rollback 时，新的 blob/ref/body 一起消失。

因此场景“多图 blob 已写但 Hook 拒绝”在推荐设计中直接被消除：Hook 拒绝发生在发布之前。

如果目前 `PostgresCanonicalBlobStore.publish()` 不能加入调用者已有 PostgreSQL transaction，这不是引入 durable lease/job/receipt 的理由，而是需要给现有 blob owner 增加一个 transaction-bound 内部入口。这是本轮少数真正需要本地确认的实施依赖。

Codex 的当前本地输入有一个可借鉴点：`LocalImage` 会在排队前被 snapshot 成 portable、已读取的 data URL，从而把可变路径冻结掉。 Pulsara 应借“freeze before asynchronous ownership transfer”，但不能照搬 Codex 的 resize/normalization，因为 Pulsara D2 要保留原 bytes。

### 3. D3 full confirmation：确认 body + refs + immutable bytes，而不是总大小

现有 Pulsara confirmation 仍比较 content digest/size，并硬编码 `text/plain` / `utf-8`；附件已经把这一点列为 D3 hard-cut 消费者。

建议确认流程为：

* 比较既有 command/delivery/target/permission/model-binding 语义；
* 比较 deterministic canonical body digest/size；
* parse body；
* 枚举 image occurrence refs，要求 ordinal、数量和 body descriptor 精确对应；
* 对每个 ref exact-join blob descriptor 的 digest/size/MIME；
* 对**不同 blob**执行一次现有 `read_exact()`，验证实际 bytes；
* 同一 blob 重复 occurrence 可以只物理读取一次，但逻辑上仍比较/计费两次。

完整一致才是 FULL。缺 ref、dangling ref、错误 blob、不同 MIME、不同 occurrence 顺序都不能判 FULL。

这样请求写入成功但 ack 丢失时，可以继续沿用 Pulsara 已有“只读确认而非重放 writer/Hook/执行”的产品语义。附件明确要求保持这一职责。

### 4. D3 hydration：metadata-first，unique physical read，occurrence logical charge

reader 应分两阶段：

第一阶段只读 canonical body、ref 行和 blob descriptor，不读 blob `body`。用可信 `size/MIME/W/H` 完成：

* ref 完整性；
* `C_quote`；
* `L`；
* projected `W`；
* D1 visual quote；
* selection/admission。

第二阶段预算通过后才 hydrate blobs：

* 每个 unique blob `read_exact()` 一次；
* 同 candidate 中重复 occurrence 复用同一 immutable byte object；
* 但是 `C/L/W/D1` 按 occurrence 重复计费。

这与 D2 已冻结候选的口径一致：共享存储/读取不能降低重复图片的逻辑 charge。 实验中同一约 3.003 MiB 图片出现 16 次时，编码 occurrence 约 48.046 MiB、wire 已约 64.063 MiB，也直接证明不能按 digest 去重逻辑预算。

### 5. D4：普通 compaction 的图片输入不是策略项

这一点应直接冻结，不再讨论。

当前 Pulsara 普通 compaction 使用 origin call 的同一 target，在 source legal prefix 后追加一条 USER summary request；actual wire 还要求 installed root/tools/input prefix 没被重写。

因此：

> 任何处于已安装合法 prefix 中的图片，都必须以原 bytes、原 MIME、原 occurrence/order 和原 wire semantics 进入普通 summary call。

不能先转文字、placeholder、blob ID，也不能因为 compaction 而重新 hydrate 已经退出有效上下文的更老图片。

这与调查到的 OpenCode、Goose、Pi、OpenHands 的普通 summary 行为都明显不同：

* OpenCode V2 把图片/文件序列化成 `[Attached ...]` 文本，再用一条 text USER 请求做 summary。
* Goose compaction formatter 把图片变成 `[image: MIME]`。
* Pi 先 `serializeConversation()`，user/tool 图片并不作为 image block 进入 summarizer。
* OpenHands summarizer 通过事件的字符串表达形成文本 prompt；其 `MessageEvent.__str__()` 对图片只形成 `[Image: N URLs]`。

这些都是对 Pulsara 的**反例**，不是模板。

### 6. D4 successor 的完整保留单位：request，而不是 part

建议明确五种 unit：

* active request：完整 typed HUMAN request；
* recent human：完整 non-active HUMAN request；
* retained historical request：完整 typed historical request；
* canonical suffix：现有完整 canonical entry/group；
* tool tail：现有 protected complete tool group。

图片和同一 request 中的文本不能拆开单独 retention。

Codex 当前 remote compaction v2 有一个值得借鉴的局部机制：在 retention 边界遇到图片时，会把图片及相邻 harness 标签作为 atomic group；图片放不下时不会用剩余预算回填更老消息。

Pulsara 应比它更严格：原子单位不是“图片块”，而是**已定义的完整 human request**，因为 active exact 和 recent/historical 的产品语义都在 request 层。

### 7. recent human：保留 3 条和 64 KiB，但 64 KiB 只计 Text parts

这是 D4 最需要显式修订的现有文本政策。

当前 `maximum_recent_human_messages=3`、`maximum_recent_human_utf8_bytes=65536`，selection 按整条文本累积；最新一条自己超过 64 KiB 时返回空，而不是跳过它找更老的消息。

我的建议：

* 保留 `maximum_recent_human_messages = 3`；
* 将现有 byte 字段语义明确收窄/重命名为 `maximum_recent_human_text_utf8_bytes = 65_536`；
* 只累计所选 request 的所有 Text parts UTF-8；
* Image bytes **不计入这 64 KiB recency heuristic**；
* 但图片绝不是“免费”：选中的完整 typed request 仍全额进入 D2 的 C/L/W、D1 和 item 检查；
* 如果 global successor budget 不允许当前 optional recent set，则按**最老已选 recent request**逐条整条退出，直到合法；
* 最新 recent request 单独仍放不下时，recent 为空；不能删图留文字，也不能跳过最新的超大 request 去捞更老的小 request；
* active exact 不属于这个 optional shrink。

产品后果很明确：一条小文字 + 3 MiB 图片的请求可以继续作为 recent，只要 C/L/W/token 允许；若全局预算不允许，它的文本与图片一起退出 successor 的有效模型输入，但原 transcript/blob 仍保留。以后用户说“再看看刚才那张已退出上下文的图”，除非它仍在其他 retained unit 中，否则模型本轮看不到它；“按历史 ID 重新取图”是新的产品能力，本轮不能假设存在。

### 8. fork：复制 child-local refs，不复制 bytes，也不依赖 parent

这是 D3/D4 交叉处应当直接定下来的规则。

Goose 的 fork/copy 会建立新 session，再把整个 conversation 写一遍；图片 base64 因而成为 child-local message 数据。 这满足“child 不依赖父消息”的产品方向，但对 Pulsara 来说复制 bytes 浪费已有 immutable blob。

Codex 最新 paginated fork 则走另一端：child history 可以通过 `history_base` 继续引用祖先 rollout。`RolloutLineage` 明确追踪这些 immutable ancestor segments。 因此 Codex hard delete 必须扫描 external fork references，仍有 child 时拒绝删除 parent。 上游目前甚至仍有关于 detach/re-root fork 以便删除祖先的开放问题，这正说明这种 lineage dependency 有实际生命周期成本。([github.com][1])

Pulsara 已经有更适合自己的契约：fork 的 child entry/snapshot 是 child-local，只共享 immutable bytes。附件明确要求 child 自己建立可达引用，父 close/变化不能使 child 失效。

所以 fork transaction 中应：

```text
read anchor-effective source under existing REPEATABLE READ
→ create child-local entries/snapshot/carrier
→ create child-local canonical_image_refs
   pointing to same workspace blobs
→ commit
```

不复制 blob body，不留下 parent entry ID 作为图片恢复 authority。

---

## 二、跨项目对照

证据等级：**A** = 固定 commit 生产代码 + 对应测试/已合并 PR；**B** = 固定 commit 真实代码链，但缺少我能确认的端到端测试或完整生命周期；**C** = issue/docs/局部推断，需保守使用。“未证实”表示本轮没有足够公开证据，不等同于“不存在”。

| 项目 / 调查 commit                               | 输入冻结与内容持久化                                                                                        | 引用 / GC                                                                                 | compaction input                                                                          | compaction 后图片                                                                                     | active exact                | fork / branch                                                              | 关键失败路径                                                                    | 强度                          |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | --------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------- | --------------------------- |
| Codex `main@1715e550…`                       | `LocalImage` 在异步 owner 前 snapshot 为 portable Image/data URL；rollout 可持久化 `ResponseItem` data URL。 | paginated fork 用 ancestor rollout lineage；delete 会阻止仍被 fork 引用的 parent。                 | local：clone history + summary USER，超窗时可删最老 source item；remote v2 是独立 provider compaction。 | local replacement 的用户保留是 text-oriented，测试明确 image-only 不进入提取；remote v2 可按图片原子预算保留。                 | Pulsara 式 exact carrier 未证实 | 当前 paginated child 可引用祖先 rollout；有历史 cutoff；删除因此受引用约束                      | local summary overflow 会 drop oldest source；delete parent 可因 fork refs 被拒 | A                           |
| OpenCode `dev@95daf906…`                     | V2 Prompt 是 `{text, files[uri,mime,...]}`；admission equality 比完整 encoded Prompt JSON。             | V2 `session_input.prompt` / `session_message.data` 是 JSON；本轮未见独立 immutable media FK/GC。 | serialize 为文本 `[Attached …]` 后 text-only summary                                          | successor 保存 summary + serialized recent，不是 exact image carrier。                                   | 未证实                         | `SessionTable.parent_id` 存在，但本轮未把 V2 fork 真实生产链路闭合；不与 legacy 拼接            | summary prompt 自己超预算时返回 false；provider stream fail 不采用                    | A（input/compaction）；C（fork） |
| OpenHands `software-agent-sdk main@c370074…` | canonical Message 有 `ImageContent.image_urls`; EventLog 把整个 Event JSON 持久化。                       | event tree append-only；2026-07 合并 branch/fork PR 明确 v1 abandoned branch **no GC**。      | LLM summarizer 把 forgotten events 字符串化成一个文本 prompt                                        | image 在字符串中成为 `[Image: N URLs]`，不是视觉输入。                                                            | 未证实                         | `parent_id` + movable HEAD + `fork(from_event_id)`，有 persisted tree tests。 | append 校验 parent；严格 event JSON 类型也带来 schema-resume 风险；已有相关 open issue     | A                           |
| Goose `main@50666ae…`                        | `MessageContent::Image` 带 inline data/MIME；SQLite `content_json` 持久化整份 message。                   | 无独立 image ref/GC；session 删除消息即删除 inline image                                           | summary formatter 把 image 变成 `[image: MIME]`; summarizer看 agent-visible projection。       | 原消息变 agent-invisible 但仍 user-visible；summary 成 agent-visible；仅 text-only latest user 可机械 preserve。 | 未证实                         | `copy_session` 全量复制 conversation；历史 fork 再截断，child-local 数据                | replace conversation 是 DB txn；compaction 失败则不替换                           | A                           |
| Pi `main@71dca871…`                          | typed Message 有 inline base64 `ImageContent`; session 为 JSONL tree，`parentId` 分支。                 | inline base64，无独立 image GC；session 文件是生命周期单元                                            | cut planner 有 image token heuristic，但 summary 先 `serializeConversation()` 成文本。            | `firstKeptEntryId` 后内容机械保留，之前由 text summary 替代；可 split turn                                        | Pulsara 式 active exact 未证实  | 同一 JSONL 内 parentId tree/branch；compaction cut 参与 active branch context    | incomplete/length summary 不应成为 checkpoint；cut 不落到 tool result             | A/B                         |

一个重要的新意是：这些项目实际上提供了三种不同的“保留”语义。

* Goose/OpenHands：原 canonical/audit 历史还在，但 agent-visible context 可以只留下文本 summary。
* Pi：旧 session JSONL 还在，active branch 的模型上下文由 compaction cut 重新选择。
* Codex remote v2：replacement context 可以机械 retained 一部分原始消息，包括图片。

这正支持 Pulsara 已要求严格区分的三件事：**summary 生成时模型是否看图、adopted successor 是否继续含图、canonical storage 是否仍持有图**。这三者不能再压缩成一个 `retain_images` 开关。

---

## 三、关键机制的源码链路

### Codex：路径冻结、两种 compaction、lineage fork

输入链路的关键节点是 `snapshot_local_user_input()`：`LocalImage(path)` 在进入后续异步处理前读取 bounded bytes，再变成 portable `UserInput::Image`。 证明点是“可变路径不应继续作为 durable/async input authority”；不能证明其 resize/transcode 适合 Pulsara。

local compaction 的真实链路是：

```text
run_compact_task_inner_impl
→ sess.clone_history()
→ history.record_items(summary USER)
→ history.for_prompt(...)
→ provider compaction call
→ ContextWindowExceeded:
     history.remove_first_item()
     retry
→ success
→ original sess history snapshot
→ collect_annotated_user_messages()
→ build_compacted_history(...)
→ replace_compacted_history(...)
```

固定源码可见这一完整分支。 测试又明确约束 `content_items_to_text_ignores_image_only_content()` 和 `collect_annotated_user_messages_extracts_user_text_only()`。 所以它不能证明“Codex compaction 会 exact 保留用户图”；恰恰相反，local replacement 语义与 Pulsara D4 不兼容。

remote v2 是另一条真实链，不能与 local 拼成一个行为。`build_v2_compacted_history()` 会过滤 retention groups，再按 64k retained-message budget 截断；启用 image budget 时，图片参与预算。 图片边界函数从尾部处理 image + adjacent image labels，只有整个 image atom fit 才保留。 这一小机制适合借鉴“optional retention 不拆视觉原子”，但 Pulsara 的原子层应上移为 request。

fork 则是第三条独立链：

```text
SessionMeta.history_base
→ LocalThreadStore.resolve_rollout_lineage()
→ ordered immutable rollout segments
→ historical cutoff
```

hard delete 前：

```text
scan RolloutReferenceIndex
→ ensure_no_external_references()
→ if external fork ref exists: reject delete
```

这对 Pulsara 的启示不是使用 history-base pointer，而是**任何共享不可变内容都必须有删除/GC 可见的 durable reachability**。

### OpenCode V2：完整 Prompt equality，但媒体只是 URI

当前 V2 ingress：

```text
SessionInput.admit()
→ find(id)
→ publish PromptAdmitted
→ projectAdmitted()
→ SessionInputTable.prompt JSON
```

如果相同 ID 已存在，最终兼容性函数比较 delivery + session ID + canonical encoded Prompt JSON。

Prompt schema：

```text
text
files[]:
  uri
  mime
  name?
  description?
  source?
```

因此外部机制真正支持的是“完整 typed-ish request 用于 idempotency equality”，不是 immutable image content identity。Pulsara 应借前者，不能借 URI 作为内容 authority。

其 compaction 真实链：

```text
session entries
→ serialize(message)
   user file => "[Attached MIME: name/uri]"
→ select(head,recent)
→ buildPrompt()
→ LLM.request(messages=[Message.user(summaryPrompt)], tools=[])
→ Compaction.Ended(summary,recent)
```

这也说明之前附件 §16 的结论没有低估差异：OpenCode 当前 V2 仍是文本化 summary source，不适用于 Pulsara installed-prefix summary。

### OpenHands：event tree 是 fork 机制，不是 media lifecycle

当前 backend 的 transport-neutral `ImageContent` 本身只是 URL 列表。 `MessageEvent` 保存 exact `llm_message`，然后 EventLog 在 append 时 `model_dump_json()` 写入持久化 event file，并检查显式 parent 存在。

分支能力在 2026-07-02 合并 PR #3922 中变成：

```text
Event.parent_id
ConversationState.leaf_event_id
path_to_root(leaf)
navigate_to()
fork(from_event_id)
```

而 storage 保持线性 append-only。该 PR 还明确说 abandoned branches v1 不做 GC。 当前测试覆盖 historical fork 的 branch membership。

因此这对 Pulsara 最有价值的结论是：**branch identity 与媒体存储/GC 是两层问题**。有一个 event tree 并不会自动解决 image reachability。

### Goose：最清楚地展示 storage retention ≠ model-context retention

Goose 的 SQLite message owner：

```text
Message
→ serde_json::to_string(message.content)
→ messages.content_json
```

读取时再 deserialize。

copy/fork：

```text
get_session(source, include conversation)
→ create_session(child)
→ replace_conversation(child, full copied Conversation)
```

所以图片是 child-local，但 base64 bytes 也被复制。

compaction：

```text
agent_visible messages
→ format image as "[image: MIME]"
→ summarizer
→ all old msgs metadata := agent_invisible
→ add summary(agent_only)
→ add continuation
→ optionally preserve most recent text-only user prompt
```

这是我认为本轮最清楚的外部反例：压缩后原图仍可存在于历史存储，却已经不在模型有效上下文。

### Pi：tree + cut point 值得参考，但会文本化图片并允许 split turn

Pi 的 session tree 使用 `parentId`，branch 在同一 tree 中产生新 child。 compaction planner 会按 recent token 倒推 cut，并明确避免直接切到 toolResult；但允许在 turn 中部切点。图片按固定估算成本进入 cut 计算。

summary 则：

```text
messagesToSummarize
→ convertToLlm()
→ serializeConversation()
→ one text user prompt
→ summary
→ firstKeptEntryId
```

`serializeConversation()` 对 user/tool result 只用 text extraction。

因此可借鉴的是“summary replacement 有明确 cut + retained suffix”，但不能借它的 part/turn split 策略。Pulsara 已要求 protected tool group 和 exact request 不能被拆。

---

# 四、D3 推荐草案

## 4.1 两个最小可行方案

|                  | 方案 A：ordered body + owner-specific refs            | 方案 B：规范化 part rows                           |
| ---------------- | -------------------------------------------------- | -------------------------------------------- |
| semantic body    | 一个 deterministic body，含所有 Text 与 Image descriptors | body/message row + N 个 part row              |
| image bytes      | 既有 `blobs`                                         | 既有 `blobs`                                   |
| order            | body part order                                    | `part_ordinal`                               |
| GC               | 一张 image occurrence ref relation                   | image part row 本身 FK                         |
| confirmation     | body digest + refs exact join                      | join 全 part rows                             |
| snapshot nesting | 一个 snapshot body + ordinal refs                    | snapshot parts 也需规范化                         |
| 改造范围             | 主要扩展现有 content owner                               | 会把 queue/entry/snapshot 都迁入新的 part hierarchy |
| 风险               | body/ref 双层必须 exact-join                           | 新关系事实上成为第二套 canonical content truth          |
| 推荐               | **是**                                              | 否                                            |

方案 B 技术上可行，而且关系模型更“纯”；但它会把现有 `CanonicalContentPublisher`、queue/entry/snapshot 单体 content 模型整体升级成新的 content tree。对本轮 USER 图片首版来说，这超出产品必要性。

另一个看似更小的“方案 C：JSON 里写 blob ID，不建 relation”应直接排除。当前 Pulsara GC SQL只扫描显式 FK owner。

## 4.2 canonical body

建议 deterministic body 类似：

```json
{
  "version": 1,
  "parts": [
    {"type": "text", "text": "比较"},
    {
      "type": "image",
      "ref": 0,
      "digest": "sha256:...",
      "size": 3148754,
      "media_type": "image/png",
      "width": 1295,
      "height": 810
    },
    {"type": "text", "text": "和"},
    {
      "type": "image",
      "ref": 1,
      "digest": "sha256:...",
      "size": 2800000,
      "media_type": "image/jpeg",
      "width": 1200,
      "height": 900
    },
    {
      "type": "image",
      "ref": 2,
      "digest": "sha256:...",
      "size": 3148754,
      "media_type": "image/png",
      "width": 1295,
      "height": 810
    }
  ]
}
```

`ref=0` 和 `ref=2` 可以映射同一 blob；不能在 body 中合成一个 A。

blob 表已经存 immutable bytes、digest、size、MIME/codec，且 `read_exact()` 有完整性检查。 所以 body 中这些字段是**semantic proof descriptor**；真实 bytes authority 仍是 blob。

## 4.3 新关系为什么是必要的，而不是重复事实

建议关系不保存：

* digest；
* MIME；
* width/height；
* logical_size；
* body path；
* provider URL。

它只保存：

```text
canonical owner + occurrence ordinal -> blob FK
```

产品必要性有四项，而且现有数据结构无法覆盖：

1. GC 需要 FK-visible reachability。
2. full confirmation 需要证明 body ref 没 dangling/mispoint。
3. snapshot nested exact/recent/historical 需要独立 hold image。
4. fork child 必须在不依赖 parent 的情况下 hold same blob。

这足以证明它不是为了“方便测试”新增的表。

## 4.4 GC 与并发

将 current orphan SQL 增加：

```text
AND NOT EXISTS (
  SELECT 1 FROM canonical_image_refs r
  WHERE r.blob_id = b.id
)
```

owner 删除利用 FK `ON DELETE CASCADE` 删除 refs。

关键不是 grace period，而是**attach 与 publish 同事务**。这样不存在“blob 已 commit、owner 尚未 commit、GC 抢先删除”的窗口。

对于已有 blob 的复用，需要沿 PostgreSQL FK/key-lock 正常语义防止并发 delete；具体是当前 blob writer/GC 已使用的 transaction/locking primitive，还是需显式 `FOR KEY SHARE`，应由本地 agent结合实际 SQL owner核实，不能从附件臆造。

## 4.5 snapshot

carrier 本身继续是 canonical continuation，不持久化 provider wire。附件对此已经明确。

建议 snapshot body 里的 typed request 与普通 Prompt body 使用相同 part encoding。一个 snapshot 中若有：

* active exact request；
* recent requests；
* retained historical requests；

则按 canonical traversal 顺序给所有 Image descriptors 分配 snapshot-local `ref_ordinal`，统一由 snapshot owner refs 持有。

不需要 `json_path → blob` registry。

---

# 五、D4 推荐草案

## 5.1 完整保留矩阵

| 场景                           | source                                                | 原子单位                                | 图片在 model wire?           | durable ref owner                        | 预算                                | 失败                           |
| ---------------------------- | ----------------------------------------------------- | ----------------------------------- | ------------------------- | ---------------------------------------- | --------------------------------- | ---------------------------- |
| 正常调用                         | effective snapshot + canonical suffix                 | request / existing tool groups      | 是，所有 selected images      | entry/snapshot                           | C/L/W/D1                          | install/dispatch 前资源或内容失败    |
| 透明 transport retry           | 同一个 frozen final-wire materialization                 | 整个 frozen plan                      | **是，byte-identical**      | 不新增 owner                                | 原 plan                            | 不改图、不换 API；原 terminal/retry  |
| 同模型 compaction input         | legal source prefix + synthetic summary USER          | installed prefix 不可改                | **是；prefix 中所有图原样**       | 原 entry/snapshot                         | summary W/D1                      | summary fail → 不采用           |
| 同模型 successor                | summary + mandatory/optional retained units + suffix  | typed complete request / tool group | selected unit 中有图则是       | successor snapshot + suffix entry        | PRE_FULL C/L/W/D1                 | 不合法则不 adopt                  |
| cold epoch                   | 合法 canonical selection                                | 完整 units                            | 是                         | 原 canonical refs                         | target/content/W/D1               | install 前失败                  |
| Tier 1 B cold                | B selected cold input                                 | 完整 units                            | 是，若 selected              | canonical refs                           | B actual budget                   | B 不支持/太小→不切换                 |
| Tier 2 A summary input       | A installed prefix                                    | installed prefix                    | **A 必须看原图**               | A 原 owners                               | A summary                         | A fail→无 B successor         |
| Tier 2 B successor           | A summary + retained exact content                    | 完整 units                            | retained 图在 B wire        | new snapshot/entry refs                  | B PRE_FULL                        | B incompat→旧 epoch 不动        |
| Tier 3 B projection          | canonical-derived temporary projection + exact active | 完整 typed units                      | selected 图必须原生给 B         | temp projection 非 durable；源 owner 仍持 ref | B projection + successor          | incompat/预算 fail，无 adoption  |
| active `SNAPSHOT_EXACT`      | predecessor/successor carrier                         | **一整条 active request**              | 是                         | snapshot refs                            | mandatory                         | 不能 shrink/描述化                |
| active `CANONICAL_SUFFIX`    | canonical suffix entry                                | 同一 active request                   | 是                         | entry refs                               | mandatory                         | carrier 不复制第二份               |
| idle                         | settled summary/recent/history/suffix                 | 无 active                            | 按 selected                | owner refs                               | normal successor                  | optional retention 可 shrink  |
| recent human                 | latest contiguous non-active humans                   | **整 request**                       | selected 则图在 wire         | successor snapshot refs                  | 3条 + 64KiB Text heuristic + 全局 D2 | 从最老 selected recent 整条退出     |
| retained historical          | predecessor historical list                           | **整 historical request**            | 机械 retained 时是            | snapshot refs                            | full successor                    | 只有被新 summary source 覆盖才吸收    |
| retained tool group          | 原 protected complete group                            | complete group                      | v1 无 image tool output，沿用 | 既有 owner                                 | max3 / tail 2MiB 等现有政策            | 不拆 call/result               |
| fork / fork-of-fork          | anchor-effective material                             | effective canonical units           | child 后续模型调用照常有图          | **child-local refs**                     | fork read + child context         | txn abort，无半 fork            |
| ROOT 实际 provider call        | normal ROOT typed input                               | request                             | **是**                     | normal owner                             | normal W/D1                       | target不支持即失败                 |
| ROOT parent context advisory | public turn-unit projection                           | part order；image→omitted marker     | **否，沿用**                  | 不新增 media ref                            | text advisory budget              | projection fail 必须 install 前 |

这里最后两行必须分开，否则很容易误写成“ROOT 不传图”。ROOT 主调用传原图；只是 parent-context advisory 不传图。附件已经把这个区分冻结。

## 5.2 exact carrier

现有 `FrozenCompactionActiveRequest` 只有 `text: str | None`。

应 hard cut 为类似：

```text
FrozenCompactionActiveRequest:
  entry_id
  entry_sequence
  location:
    SNAPSHOT_EXACT | CANONICAL_SUFFIX
  content: FrozenPromptContent | None
```

约束不变：

```text
location == SNAPSHOT_EXACT  <=> content is present
location == CANONICAL_SUFFIX <=> content is absent
```

但 `content` 是 exact typed content，不是 summary、text projection、blob ID list 或 provider wire。

`FrozenRetainedHistoricalRequest` 同样改为 typed content，但不获得 active identity。

## 5.3 多次 compaction

如果 active 已进入 predecessor `SNAPSHOT_EXACT`：

```text
compaction #1:
  active exact in snapshot S1

compaction #2:
  canonical reader可能已经看不到原 active entry
  → 从 S1 carrier机械恢复相同 typed content
  → S2 仍只有一份 authoritative active placement
```

不允许从 summary prose 恢复，也不允许摘要模型输出一个 blob ID“声明”恢复成功。

retained historical 的规则保持现有语义：

* 新 summary prefix 覆盖 predecessor snapshot base → 老 retained historical 已被摘要吸收，可从新 carrier 清空；
* 没覆盖 predecessor base → 必须机械完整带入。

附件已经规定这一规则，D4 只需把 `text` 单元升级成 typed request。

## 5.4 模型切换

不新增第四档。

Tier 1：

```text
canonical selection
→ build B dry cold input
→ typed content validation
→ B modality
→ C/L/W/D1
→ only then install
```

Tier 2：

```text
A installed prefix + summary request
→ A sees exact images
→ A summary output
→ construct B successor
→ B PRE_FULL on retained typed images
→ adopt or fail
```

Tier 3：

```text
canonical-derived temporary B projection
+ exact active typed request
+ summary instruction
→ B summary
→ dry successor
→ PRE_FULL
→ adopt
```

现有 Tier 3 现在是 current source + projection USER + active USER + summary USER。 图片 hard cut 后，其中 exact active USER 必须是 typed content。

B 明确不支持 image 时，不允许把 retained image 改成 placeholder；失败发生在 adoption 之前。主设计 §4.1 已经确定这一点。

---

# 六、12 个强制场景推演

**1. `Text("比较") → Image(A) → Text("和") → Image(B) → Image(A)`**

body 保持 5 个 ordered parts；三个 image occurrence 的 ref ordinal 依次为 0/1/2。ref0 和 ref2 可指向同一个 A blob；C/L/W/D1 中 A 仍计两次。任何 compiler、carrier、fork 都不能把最后一个 A 去重掉。

**2. 同一 command ID、同文字，A 换 B**

frozen content equality 已不同，canonical body digest 也不同，in-flight join 与跨重启 confirmation 都返回冲突。另一个 command ID 再发 A 是合法的新 occurrence；可物理复用 blob，但不会被全局内容去重吞掉。

**3. 两张约 3 MiB 图同一 request**

先完整 D2 verify，然后作为一个 candidate 做 M/C/L/W/D1 admission。不能写第一张 ref 后发现第二张超界。失败则整个 request 未 accepted；后面的独立 queue item 仍保持 pending。两张图若属于两个独立 queue items，则原 FIFO prefix selection 可只接纳前者，但不能拆一条 multipart。

**4. 多 blob 已发布，Hook reject / transaction fail**

推荐实现中 Hook 在 blob publication 前，所以 Hook reject 不产生新 blob。Hook allow 后 blob + owner body + image refs + command/queue 同一事务，transaction fail 全 rollback。若本地 blob publisher 做不到 caller transaction，这是实施 blocker，应修改它，不引入 lease。

**5. commit 成功，调用者未收到 ack**

走 read-only confirmation。完整检查 body、refs、owner 和 unique blob `read_exact()`。FULL 后返回原结果，不再 Hook、不再次 writer、不执行第二次。悬空 ref/错 blob 永远不能 FULL。

**6. active 请求只有图片，多次 compaction**

Hook text projection是 `""` 但提交合法。每次 adopted successor 的 `SNAPSHOT_EXACT` 都机械保留 typed image request；恰好一个 Runtime authoritative placement。summary output 不具 restoration authority。

**7. recent 遇到 3 MiB 图片**

64 KiB 只累计 Text parts，所以不会单纯因为 3 MiB 文件而退出 recent。如果全局 C/L/W/D1 能放下，adopted successor 保留原图；放不下则整条 request 从 optional recent 退出。原 transcript/blob 不删除。随后用户若引用已退出图，本轮没有自动 history-image retrieval，必须如实视为视觉不可用；该能力范围外。

**8. 同图在 old 和 recent 两条 request**

物理 blob 一个、owner refs 多个、逻辑 occurrences 多个。old occurrence 从 effective context 被 summary 吸收，不会使 recent occurrence 的 ref 消失，也不会降低 recent 的 C/L/W/D1 charge。只有最后一个 durable owner ref 真正消失后，blob 才有资格成为 orphan。

**9. pending 输入推动 compaction，但最小 successor + active exact + FIFO head + G 仍放不下**

不要再次摘要、不要等 active “也许以后释放”。返回现有 typed resource failure；附件已明确 steer 使用既有资源失败/中断语义，其他入口沿自身失败结果。 queue head 不部分消费，旧 epoch 不推进。

**10. 切到不支持图片或预算较小 B**

dry successor 恢复全部 mandatory typed units后做目标模态和预算检查。失败发生在 canonical adoption 之前；binding、snapshot pointer、context pointer、旧 epoch nonce/revision/prefix 都不改变。不能删图重试。

**11. 从旧 anchor fork，parent 后来已 compaction/追加**

现有 fork reader按 anchor 当时 base/cut取材料，而不是 parent 最新摘要。 在该 REPEATABLE READ txn 内建立 child-local refs。source active 在 child settled carrier 中转 historical，`AWAIT_NEXT_USER`；fork-of-fork 再复制 child 当时有效 refs。父后续变化都不会泄漏进旧 fork。

**12. ROOT 没 child，纯图**

正常 ROOT model input 含原图。与此同时，每次 ROOT dispatch 都仍执行 parent-context projection；纯图得到稳定的 “image part N omitted…visual unavailable” advisory，然后 install。没有 child 与有 child 的 projection 规则相同。附件明确 parent-context 不以 child 已创建为条件。

---

# 七、可直接写回主文的建议文本

以下是建议稿，不表示用户已经冻结，也不表示代码已经激活。

### §6 Canonical source 与 compiler 接入——D3 闭合补充

#### 6.1 Canonical Prompt body、不可变图片与完整身份

经 D2 验证后的 USER 提交形成唯一、不可变、有序的 `PromptContent`。Text part 保存原始文本；Image part 保存对原始不可变图片 bytes 的语义描述，包括 occurrence ordinal、真实内容 digest、编码字节长度、验证后的 media type 及可信宽高。Image payload 不内嵌进 multipart body，而由既有 immutable blob owner 保存。

Canonical body 使用唯一确定性编码，完整保留 Text/Image 类型、part 顺序及重复 occurrence。相同图片在同一提交中出现多次时，每次均有独立 occurrence；允许这些 occurrence 共享同一 immutable blob，但不得在命令身份、上下文 selection、C/L/W 或 D1 中去重。

现有 command content digest 计算确定性 canonical body bytes；它继续与原 delivery、target、permission、model binding 等语义由既有 command digest owner 联合。Hook/Skill 文本投影不参与该身份，也不新增 DTO fingerprint、图片 fingerprint 或 fingerprint→object registry。

#### 6.2 Canonical 图片引用关系

新增一份最小的 canonical image occurrence 引用关系，用于将 canonical owner 中的 image occurrence 映射到既有 `blobs` 行。关系必须保存 `ref_ordinal`、`blob_id`，并通过真实数据库外键指向其 owner。第一版至少覆盖 pending queue item、transcript entry 和 context snapshot；若 direct accepted prompt 在 transcript 建立前存在另一既有 durable owner，则实施时按其真实 schema 增加该 owner 的外键，不使用无外键的通用 `owner_kind/owner_id`。

每个引用行恰好属于一个 canonical owner；每个 owner 的 `ref_ordinal` 唯一。该关系不复制 MIME、digest、尺寸或图片 bytes；这些事实分别由 deterministic body 和 immutable blob owner 持有。关系的产品职责仅为 FK 完整性、GC 可达性、full confirmation、snapshot exact retention 和 fork child-local ownership。

将 blob ID 单独嵌入 JSON 而不建立 GC 可见关系不构成合法实现。

#### 6.3 发布、事务和 full confirmation

D2 完整 multipart 验证必须在 Hook reservation 和命令候选冻结前完成。Hook 使用已冻结内容派生的文本投影。只有 Hook allow 后才进入 canonical publication。

新图片 blob 的发布/复用、canonical body、全部 occurrence 引用以及原 command/queue owner 写入必须位于同一现有 repository transaction 中。若现有 blob publisher 不能参与调用者事务，应为现有 blob owner增加 transaction-bound 内部接口；不得以 durable lease、receipt、job、媒体注册表或 GC grace period代替原子 publication。

Hook 拒绝不发布新 blob。事务失败不得产生半条 accepted prompt；该事务内新 blob、body、引用和 command/queue 状态一起回滚。

写入结果不确定时继续使用原只读 confirmation，不重调 writer。FULL 必须同时证明：

1. 原 command/delivery/target/permission/model 等语义一致；
2. canonical body digest、size 及完整 body 一致；
3. image occurrence 数量、ordinal、顺序及 body descriptor 一致；
4. 每个 occurrence ref 指向与 body digest/size/media type 精确 join 的 blob；
5. unique blob 的 `read_exact()` 完整性检查通过。

任何缺失、悬空、错指或内容不一致均不得返回 FULL。重复 occurrence 可以物理只读一次相同 blob，但必须逻辑比较每个 occurrence。

#### 6.4 Bounded hydration 与 GC

Reader 首先读取 body、image occurrence refs 和 blob descriptor，不读取图片正文。metadata preflight 必须对同一 cut、同一完整 selection 计算覆盖实际 C_charge 的 C_quote，并逐 occurrence 计入图片长度；同时提供 L、W 和 D1 所需的可信 MIME、尺寸和 occurrence 信息。

仅当 selection 和预算通过后，reader 才对 unique blob 执行有界 `read_exact()`。同一 candidate 中重复 occurrence 可以复用同一 immutable bytes 对象；C/L/W/D1 的逻辑 charge 不因此减少。正常 hydration 不重复像素 decode；原始图片有效性和可信尺寸来自 ingress D2 验证与 immutable-content exact join。

现有 blob orphan GC 增加对 canonical image occurrence 引用关系的 `NOT EXISTS` 检查。owner 的物理删除可通过外键级联释放 refs；退出有效模型上下文、compaction cut、archive 或 close 不自动等于删除原 transcript 图片。

Queue→entry、snapshot adoption、fork 等 owner 转移必须先在原事务中建立新 owner refs，再释放旧 owner refs。failed/unadopted successor 不获得 durable refs。

---

### §8 前缀连续性、重试与上下文生命周期——D4 闭合替换

#### 8.2 完整 typed retention 单位

以下内容均以完整 typed 单位选择，不以单个 image part、文本投影或内容 digest 去重：

* active request：完整 HUMAN request；
* recent human request：完整非 active HUMAN request；
* retained historical request：完整 historical HUMAN request；
* canonical suffix：原 canonical entry/group；
* retained tool tail：现有 complete tool-call/result group。

同一图片在多个 request 或同一 request 多次出现时，各 placement 独立计量。共享 immutable blob 只影响物理存储/读取，不改变 occurrence 语义。

普通同模型 compaction 的 summary source 沿用 installed-prefix 契约：合法 source prefix 内的图片必须以同一原始内容、MIME、顺序、次数和 wire shape进入 summary call，随后只追加 synthetic USER summary request。不得在 summary call 前移除、描述化或 placeholder 化图片，也不得恢复早已退出 effective source 的全部历史图片。

#### 8.3 Active exact carrier

`SNAPSHOT_EXACT` 中的 active request 将原 text 字段 hard cut 为完整 typed request content；该 content 无损表达 Text/Image、顺序、MIME、不可变引用及重复 occurrence。

`CANONICAL_SUFFIX` 继续只保存 authoritative canonical location，不在 carrier 复制内容。同一 active request 在一个 Runtime successor 中只能有一个 authoritative placement。

Summary prose、图片描述或 blob ID 列表均无 active restoration authority。连续多次 compaction 若原 active entry 已不在当前展开 suffix 中，必须从 predecessor exact carrier机械恢复同一 typed active content。

`FrozenRetainedHistoricalRequest` 同样保存完整 typed request，但始终只具有历史身份。Fork 时 source active request 转为 historical content，不复制 active execution identity。

#### 8.4 Recent human window

保留 `maximum_recent_human_messages = 3`。现有 65,536-byte policy 明确解释为 `maximum_recent_human_text_utf8_bytes`：只累计候选 request 中所有 Text parts 的 UTF-8 bytes，不累计 Image encoded bytes。

Image 不因该 recency heuristic 免费：一旦 request 被选入 successor，完整 request 必须通过 D2 的 C/L/W、D1、item 和目标内容检查。

Recent selection 保持最新连续窗口语义，不跳过一个不合规的较新 request 去保留更老 request。初始 recent set 形成后，如实际 successor 的全局 C/L/W/token/resource budget 不合规，则从最老的 selected recent request 开始逐条整条删除并重新验证。单个最新 recent request仍不合规时，recent window 为空。不得仅删除其图片而保留文本。

Active exact、protected tool groups 和其他 mandatory content 不参加 optional recent shrink。

一条 request 从 recent successor 退出只表示它不再进入该 effective model input；原 transcript、image refs 和 blobs 仍由 canonical storage owner 持有。本版不提供自动重新读取任意已退出历史图片的产品能力。

#### 8.5 Retained historical 与重复 compaction

Predecessor retained historical requests 在新 summary source 未覆盖 predecessor snapshot base 时必须机械完整保留。新 summary prefix覆盖整个 predecessor base 后，这些历史请求可视为已被本轮 summary 吸收，新 carrier 按既有规则清空该 retained list。

该吸收不删除原 transcript/blob，也不赋予 summary prose exact restoration authority。失败或未采用的 summary/successor 不改变 predecessor carrier 或其 refs。

#### 8.6 模型切换

继续仅使用现有三档切换：

Tier 1 B cold successor 从 canonical typed content构建，并在 adoption 前完成目标模态、内容、C/L/W/D1 和现有 budget 检查。

Tier 2 A handover summary 沿 A 已安装 prefix 执行，因此 A source 中已安装图片原样进入 summary call；A summary完成后，对 B 的真实 dry successor执行完整 PRE_FULL 检查。

Tier 3 B projected-source compaction 的 temporary projection使用 canonical-derived 完整 typed units；exact active request必须以 typed image content进入 projection。Temporary projection 不持久化为正常 epoch，也不成为新的 media owner。

任何被合法 selection 选中的图片在 B 明确不支持 image 或预算不足时均不得替换为 placeholder、描述或文本。失败发生在 canonical adoption 前，不推进 binding、snapshot pointer、turn context pointer或旧 epoch continuity。不得新增第四档绕过路径。

#### 8.7 Fork

Fork 继续由现有 historical anchor 和 REPEATABLE READ owner决定来源。仅复制 anchor 当时有效 canonical context，不读取 anchor 后父会话新增消息或后续 summary。

Child 使用新的 child-local entry/snapshot/binding 身份，并为所有保留 image occurrences 建立 child-local canonical image refs；immutable blob bytes 在同 workspace 可共享。Child 不依赖 parent entry/snapshot作为图片恢复 authority。

Source active request在 child 中转为 historical typed request；child snapshot 为 `AWAIT_NEXT_USER`，不复制 live turn、permit 或当前执行身份。Fork-of-fork 对当时 child effective context重复相同规则。

#### 8.8 ROOT parent context

ROOT 实际 model request仍使用完整 typed input并发送合法图片。Parent-context source仍为独立文本 advisory：每个 image part在原 part位置产生稳定的视觉缺失标记，不携带图片 bytes，不获得 blob 读取能力。

该投影在每次 ROOT dispatch、无 child和有 child时均执行，并在 continuity install 前完成所有可能失败的 typed traversal和资源校验。它不得回流成 Hook/Skill文本，也不是目标模型不支持图片时的降级方案。

---

### §13 实施前决策——建议闭合状态

**D3 建议闭合为：** 采用 deterministic ordered canonical body + existing immutable blobs + owner-specific image occurrence reference relation。完整命令 identity 基于 canonical body及既有 command语义；publication 与 owner refs 在原 repository transaction内原子提交；full confirmation验证 body/ref/blob exact join；reader metadata-first、unique physical hydration、per-occurrence logical charge；GC、snapshot和fork均通过同一 refs关系获得可达性。不得使用 JSON-only blob IDs、媒体 registry、durable lease/receipt或第二套 context truth。

**D4 建议闭合为：** 普通 summary input继承 installed prefix全部图片；active/recent/historical 以完整 typed request为单位，tool retention沿用 complete tool groups；active authoritative placement严格为 SNAPSHOT_EXACT/CANONICAL_SUFFIX 二选一。Recent window保留最多 3 条及 65,536 UTF-8 bytes的文本政策，图片不进入该文本 recency额度，但完整 selected request仍受 C/L/W/D1实际 successor预算；可选 recent只能从最老 request开始整条退出。模型切换沿现有 Tier 1/2/3，图片不兼容或预算失败在 adoption前终止。Fork建立 child-local refs并共享 immutable bytes，不依赖 parent。ROOT parent context继续为明确缺图的文本 advisory。

D3/D4 只实例化 D2 §9.3 已给出的 M/C/L/W、G、U_W/U_T、multipart atomicity、validation和资源失败语义，不另选一套图片资源算法。

本地若现有 owner/type 名与这里的建议名不同，应替换符号名而不是改变上述 ownership/transaction 语义。

---

# 八、D2 中需要被 D3/D4 精确实例化的项目

D2 不需要重开，但以下变量只有 D3/D4 落地后才能成为真正可执行规则：

1. `M(request)` 中“deterministic canonical body bytes”必须使用 D3 实际 serializer，不假定固定 metadata overhead。主文已经要求这一点。
2. `C_quote/C_charge` 必须展开 snapshot body 中的 image refs；不能只看 post-base suffix。
3. `L` 每个 occurrence 的 `E_i + MIME` 必须来自 ref/body exact-join 后的 trusted descriptor。
4. `W` 必须从 hydrated exact bytes产生真实 base64 长度，不从短 blob ID估。
5. D1 逐 occurrence尺寸取可信 D2 dimensions，不再次 decode 像素。
6. INPUT candidate 的 `S_B` 包含 D4 mandatory active carrier、selected recent/historical、suffix及 pending FIFO head。
7. 最小 successor 可行性必须计 carrier body + image refs，不把 continuation包装当零。
8. PRE_FULL 的 B target compatibility使用 D4 实际 selected typed units。
9. U_W/U_T 仍由 unchanged normal compiler实际可能输出的表示报价；D3 ref storage不允许让它按“短 ID”少报。
10. fork/hydration 的物理共享不能改变 occurrence-level M/C/L/W/D1。

附件中的旧 D2 实验报告把 16,777,216 像素和 PNG 文本 1 MiB 称为“候选”， 而后来的主文 §9.3 已把它们纳入“最终冻结候选”。 这是材料时间点演进，不是实质设计冲突；D2 的当前权威应按用户要求取主文 §9.3。PNG 单位补测也支持“MAX_TEXT_MEMORY 是字符计数而非统一 UTF-8 byte cap”：1,048,576 个中文字符可对应 3 MiB UTF-8 仍通过，emoji 可对应 4 MiB。

---

# 九、本地实施与验证清单

| Owner                    | 必须改动                                                                                                    | 关键测试                                                                           |
| ------------------------ | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Host ingress             | `PromptContent` hard cut；D2 validation 前置；full `_IngressHookReservationKey`; Hook/Skill text projection | pure image、mixed、A/B/A、same-command conflict、Hook empty text                   |
| steer/command identity   | body digest/size替代 text bytes；保留原 semantic digest组合                                                     | same ID A→B conflict；different ID same A success                               |
| repository prompt writer | blob/body/ref/command/queue同 transaction                                                                | commit fail、ack lost、retry confirmation                                        |
| schema / clean-v0        | 新 image occurrence ref relation；真实 owner FKs；unique ordinal；GC query                                    | dangling FK impossible；owner delete cascades；orphan GC race                    |
| blob owner               | transaction-bound publish/reuse；existing bytes exact verify                                             | concurrent reuse/GC；digest/MIME/bytes conflict                                 |
| canonical reader         | metadata-first refs traversal；C_quote覆盖 snapshot+suffix；unique hydration                                | duplicate blob physical reuse但 logical double charge                           |
| model input contracts    | `FrozenProviderInputItem` / `LLMMessage` typed parts                                                    | ordered text/image; duplicate occurrence; pure text golden unchanged           |
| compiler/lowering        | source attribution through typed content                                                                | entry/sequence/origin不丢；no extra image side channel                            |
| Chat adapter             | typed content → `image_url` data URL/detail auto                                                        | mixed-order exact wire golden                                                  |
| Responses adapter        | typed content → `input_image`; `store=false` unchanged                                                  | mixed-order exact wire golden                                                  |
| estimator                | D1 v2 + actual W traversal                                                                              | duplicate A; text data URL not mistaken image                                  |
| compaction planner       | typed active/recent/history；recent text-only byte metric；whole-request shrink                           | 3MiB recent；multiple compactions；old historical absorb                         |
| compaction summary call  | installed prefix typed exact                                                                            | image bytes/MIME/order identical                                               |
| model switch             | Tier1/2/3 typed PRE_FULL                                                                                | B no-image; B too-small; no adoption side effect                               |
| fork repository          | clone child-local refs in existing REPEATABLE READ txn                                                  | old anchor after future parent summary；fork-of-fork；parent delete independence |
| ROOT parent context      | pre-install typed advisory                                                                              | no-child pure image；with-child same markers                                    |
| GC                       | `canonical_image_refs` reachability                                                                     | one of two occurrences exits context；blob remains while any owner exists       |

最少应把你给出的 12 个代表场景全部变成 PostgreSQL-backed kernel tests；此外主设计现有 I08/I09/I14/I22/I23/I24/I26 已经基本覆盖本结论应落入的验收槽位。

clean-v0 应一次 hard cut：修改 baseline schema、DTO、repository readers/writers、snapshot codec、fork copier、GC SQL 和测试 fixture；不保留 text-only/image 双读迁移路径。主设计已经明确仓库当前不需要兼容双路径。

---

# 十、仍需本地 agent 精准回查的 8 个问题

这些是外部研究无法回答、且确实会改变 D3 SQL/transaction 实现的少数问题，不需要上传整个仓库。

1. `CanonicalContentPublisher` / `PostgresCanonicalBlobStore.publish()` 当前是否能接收现有 repository connection/transaction？若不能，应在哪个最低层加 transaction-bound variant？
2. direct prompt 在“command 已 durable、transcript entry 尚未建立”期间，真实 durable content owner 是什么关系？`session_commands` 是否只存 digest，还是还有其他 body owner？这决定 image-ref relation 是否需要第四个 owner FK。
3. queued prompt 转 transcript entry 时，`prompt_queue_items` consumed row会长期保留、删除，还是仅状态更新？refs 的 transfer/copy 应落在哪一个现有 transaction？
4. `context_snapshots` 当前 canonical body encode/decode 的唯一 choke point 是哪个函数？能否在那里给 nested active/recent/historical image occurrences 做 deterministic ref ordinal？
5. fork transaction 当前创建 child entries/snapshot 时是否始终复用同一个 `REPEATABLE READ` connection？附件看起来是，但需要确认所有 child body insert helper 没有内部开新 connection。
6. 当前 blob GC 与 blob insert/reuse 的 PostgreSQL locking/FK behavior具体是什么？新增 ref attach 与 delete 是否只靠 FK 已足够，还是现有模式要求 key-share/其他 lock？
7. `confirm_prompt_ingress()` 当前遇到 durable row存在但内容物理损坏时，既有错误分类应映射到 CONFLICT 还是 repository integrity failure？建议沿原错误体系，但无论哪种都不能 FULL。
8. compaction reader 对 base snapshot body、retained historical 和 canonical suffix 的 metadata preflight 目前分别从哪些 helper 取 size？需要确认新增 nested image refs全部汇入同一个 cut 的 `C_quote`，避免只改 post-base 路径。

---

## 最终判断

D3 可以收敛，不需要通用“媒体层”：**复用 existing blobs，新增一张仅表达 canonical owner → image occurrence → blob 的 FK 可达性关系；语义仍由 deterministic ordered body 持有。** 这是在当前 Pulsara GC 和 fork 契约下，新关系确有产品必要性的最小方案。

D4 也可以收敛：**普通 summary source 图片继承是沿用，不是选择；successor retention 的最小合法单位是完整 typed request；现有 64 KiB recent byte window应明确为 Text-only heuristic，真正图片容量由 D2 C/L/W/D1 负责；active exact永不参加 optional shrink；fork建立 child-local refs；模型切换仍只有现有三档。**

外部项目中最值得直接借鉴的是 OpenCode 的完整 structured input equality、Codex remote v2 的图片原子 retention 思路、Codex fork delete 的引用可达性保护、Goose/OpenHands 的 child-local history/fork语义。最不应该照搬的是 Codex ancestor-dependent lineage、OpenCode/Goose/Pi/OpenHands 的图片文本化 summary，以及任何仅靠 JSON URI/blob ID 就宣称媒体生命周期已经闭合的设计。

[1]: https://github.com/openai/codex/issues/41713?utm_source=chatgpt.com "Allow forked sessions to be re-rooted so obsolete ancestors can be deleted · Issue #41713 · openai/codex · GitHub"
