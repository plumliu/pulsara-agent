# Memory Recall System Survey

_Created: 2026-06-15_

本文记录对四个本地项目记忆召回系统的代码搜索与阅读结果：

- `/Users/plumliu/Desktop/python_workspace/MiMo-Code`
- `/Users/plumliu/Desktop/python_workspace/claude-code`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent`
- `/Users/plumliu/Desktop/python_workspace/openclaw`

目标不是评判哪套系统最大，而是提炼对 Pulsara 下一步 memory recall v1 有用的设计约束。

## 总结

四套系统都没有选择“每轮把全部长期记忆塞进上下文”。它们共同采用的是：

- curated / canonical memory 与 raw transcript recall 分层；
- 自动注入只给小而相关的上下文；
- 当用户明确问“之前/记得/偏好/决定/todo”时，引导模型使用显式搜索工具；
- 召回结果可能过时，涉及当前代码、文件、配置、外部世界时必须验证；
- 召回失败要显式承认，不让模型凭空补全。

对 Pulsara 来说，最重要的结论是：召回 v1 应该只读 canonical graph，不读 candidate pool；先做轻量 lexical recall + projection + `memory_search` / `memory_get`，不要第一版就上 embedding、QMD 或复杂 reranker。

## MiMo-Code

### 关键文件

- `/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/memory/service.ts`
- `/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/memory/fts-query.ts`
- `/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/tool/memory.ts`
- `/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/session/prompt.ts`
- `/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/src/session/checkpoint.ts`
- `/Users/plumliu/Desktop/python_workspace/MiMo-Code/packages/opencode/test/session/recall-reminder.test.ts`

### 召回形态

MiMo-Code 是最直接的 lexical recall 参考实现。记忆以文件为源，落在 `<data>/memory`，再进入 SQLite FTS5 索引。`Memory.search()` 在搜索前默认 lazy reconcile 文件系统，覆盖 off-tool 写入。

搜索接口只暴露一个 `memory` 工具，当前只支持：

```ts
memory({ operation: "search", query, scope, scope_id, type, limit })
```

### 检索策略

MiMo 的 FTS query 构造非常值得 Pulsara 借鉴：

- 使用 Unicode regex 提取 `letter / number / underscore` token；
- 每个 token phrase-quote，避免 FTS5 特殊字符导致 MATCH parser 崩溃；
- token 之间用 `OR`，不是 `AND`；
- 依赖 BM25 排序；
- 用相对 score floor 去掉 common-word-only 噪声；
- 永远保留 top-1。

这样做的理由是：`AND` 对自然语言查询太脆弱，一个描述性词不在文档里就会归零；`OR + BM25 + relative floor` 更适合 recall。

### 注入策略

MiMo 不是每轮 dump 所有记忆。它有两层：

- context rebuild 时注入 session checkpoint、project memory、global memory、notes、tasks ledger、memory keys index；
- 普通 turn 只在已有 memory/tasks 时追加一个短 recall reminder，提醒模型用 `memory.search`、Read、task、actor。

### 失败语义

`memory` 工具零结果时不会说“没有记录过”。它明确告诉模型：

- 用更少、更 distinctive 的词重试；
- 对 URL、端口、路径、命令等 literal，FTS 可能切碎，需要 grep memory dir；
- 对原话/精确命令，去 history/raw transcript 查；
- scope 从 session 扩到 project/global/history。

### 对 Pulsara 的启发

MiMo 的 lexical recall 是 Pulsara v1 最适合复用的方向：先做稳定、便宜、可测试的 lexical ranking，而不是一开始引入 embedding provider。尤其是 `OR + ranking + floor + zero-result guidance` 这组设计，很适合 graph canonical memory 的 statement/scope/kind 搜索。

## Claude Code

### 关键文件

- `/Users/plumliu/Desktop/python_workspace/claude-code/src/memdir/findRelevantMemories.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/memdir/memoryScan.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/memdir/memdir.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/memdir/memoryTypes.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/query.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/attachments.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/claudemd.ts`

### 召回形态

Claude Code 的方案不是全文 BM25，而是：

- `MEMORY.md` 作为入口索引，加载进 prompt/system context；
- topic memory 文件带 frontmatter，包含 description/type；
- 每轮根据用户 query 扫描 memory headers，格式化成 manifest；
- 用 Sonnet side query 从 manifest 中保守选择最多 5 个 memory 文件；
- selected memory 作为 `relevant_memories` attachment 注入主模型。

selector prompt 明确要求：

- 只选“明确有用”的 memory；
- 不确定就不要选；
- 空列表是合法结果；
- 若最近已经成功使用某工具，不要再召回该工具 reference docs，但可以召回 warnings/gotchas。

### 异步策略

`startRelevantMemoryPrefetch()` 每个用户 turn 只启动一次。它在主模型和工具执行期间后台跑，后续 loop iteration 如果已经 settled 就注入；如果没好，不阻塞当前 turn。

这避免了 recall 成为主路径 latency blocker。

### 去重策略

Claude Code 做了多层去重：

- selector 前过滤已经 surfaced 的 paths；
- 过滤模型已通过 FileRead/Write/Edit 接触过的 memory；
- `readFileState` 标记已经注入过的 memory；
- session compact 后旧 attachments 消失，允许重新 surface。

### 召回政策

`memoryTypes.ts` 中的政策对 Pulsara 很重要：

- 用户明确说 check/recall/remember 时，必须访问 memory；
- 用户说 ignore / do not use memory 时，当 memory 为空，不应用、不引用、不比较、不提及；
- memory 会过时，只能作为当时事实；
- 如果 memory 提到文件、函数、flag，行动前必须读当前文件或 grep 验证；
- 如果 current state 与 memory 冲突，信当前观测，并更新/移除过时 memory。

### 对 Pulsara 的启发

Claude 的 LLM selector 可以作为 v2，而不是 v1。Pulsara v1 更应该借鉴它的三件事：

- recall 可以异步、保守、可为空；
- projection 要有 session-level dedupe，避免每轮反复塞同一条；
- memory policy 必须明确“忽略 memory”和“召回后验证当前状态”。

## Hermes

### 关键文件

- `/Users/plumliu/Desktop/python_workspace/hermes-agent/agent/memory_provider.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/agent/memory_manager.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/agent/conversation_loop.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/run_agent.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/memory_tool.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/session_search_tool.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/agent/prompt_builder.py`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/agent/system_prompt.py`

### 召回形态

Hermes 有两条 recall 线：

1. Built-in bounded curated memory:
   - `MEMORY.md`：agent notes；
   - `USER.md`：user profile；
   - load 时生成 frozen system prompt snapshot；
   - live entries 可以被 tool 改，但 mid-session 不影响 system prompt snapshot。

2. External memory provider:
   - `MemoryProvider.prefetch(query)` 召回下一轮需要的上下文；
   - `queue_prefetch(query)` 在完成 turn 后预热下一轮；
   - `sync_turn(user, assistant)` 在完成 turn 后写入 provider；
   - provider tools 通过 MemoryManager 注册到 tool surface。

### 注入策略

Hermes 的外部 recall context 注入很克制：

- turn start 调 `on_turn_start`；
- tool loop 前只 `prefetch_all()` 一次；
- prefetch result 缓存在本轮，避免每个 API iteration 重复查询；
- 注入发生在 API-call-time 的当前 user message 副本上；
- 原始 `messages` 不被修改，不污染 session persistence；
- external recall context 不放 system prompt，避免破坏 stable prompt cache prefix。

`build_memory_context_block()` 还会包上 `<memory-context>` 与 system note，并清理 provider 输出里已有的 memory-context fence，避免嵌套/注入污染。

### 完成 turn 后的同步

`_sync_external_memory_for_turn()` 在 turn 完成后做两件事：

- `sync_all(original_user_message, final_response, messages=messages)`；
- `queue_prefetch_all(original_user_message)`。

如果 turn 被 interrupted，则完全跳过 sync 和 prefetch。理由是 partial assistant output 或 aborted tool chain 不是 durable conversational truth，写进去会污染未来 recall。

### Transcript Recall

Hermes 把 durable memory 和 raw transcript recall 分开。`session_search` 是专门的 long-term conversation recall 工具，有四种隐式模式：

- Discovery：FTS5 搜索，返回 session snippet、match 周边窗口、开头 bookend、结尾 bookend；
- Scroll：以 `session_id + around_message_id` 继续翻上下文；
- Read：按 session_id 读完整 session 的 head/tail；
- Browse：无参数浏览最近 session。

它强调：过去对话问答先查 session DB，当前世界状态再查文件/git/web。

### 对 Pulsara 的启发

Hermes 最值得借鉴的是 lifecycle：

- recall 注入必须是 API-call-time ephemeral，不写回历史；
- interrupted / partial turn 不应该进入 durable recall；
- curated memory recall 和 transcript/session recall 应该是两个不同入口；
- 如果未来 Pulsara 支持外部 provider，可参考 provider protocol，但 v1 不必先做插件化。

## OpenClaw

### 关键文件

- `/Users/plumliu/Desktop/python_workspace/openclaw/extensions/memory-core/src/tools.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/extensions/memory-core/src/tools.shared.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/extensions/memory-core/src/prompt-section.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/extensions/memory-core/src/memory/manager-search.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/extensions/memory-core/src/memory/hybrid.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/extensions/memory-core/src/memory/mmr.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/extensions/memory-core/src/memory/search-manager.ts`
- `/Users/plumliu/Desktop/python_workspace/openclaw/docs/concepts/memory.md`
- `/Users/plumliu/Desktop/python_workspace/openclaw/docs/concepts/memory-search.md`
- `/Users/plumliu/Desktop/python_workspace/openclaw/qa/new-scenarios-2026-04.md`

### 召回形态

OpenClaw 是最成熟的搜索产品路线。它明确暴露两个工具：

```ts
memory_search({ query, maxResults, minScore, corpus })
memory_get({ path, from, lines, corpus })
```

`prompt-section.ts` 明确告诉模型：在回答 prior work、decisions、dates、people、preferences、todos 之前，先 `memory_search`，再用 `memory_get` 拉精确行；低置信度就说已经查过但不能确认。

### 搜索实现

OpenClaw 支持多层 backend：

- builtin SQLite；
- vector search；
- BM25 keyword search；
- hybrid merge；
- optional MMR diversity rerank；
- optional temporal decay；
- QMD sidecar；
- wiki corpus supplement；
- indexed session transcripts。

`hybrid.ts` 中的 merge 逻辑将 vector 与 keyword 结果按 id 合并，保留 component scores，再按 weighted score 排序。MMR 默认关闭，启用后做多样性 rerank。

### 失败与降级

OpenClaw 对失败非常显式：

- `memory_search` 有 15s timeout；
- provider/embedding 不可用时返回 `disabled=true`、`unavailable=true`、`warning`、`action`；
- 对 unavailable 做 cooldown，避免模型每轮撞同一个失败；
- 配置了具体 remote embedding provider 且不可用时，不会静默 fallback 成 FTS-only；
- 用户可显式配置 provider none，才表示 deliberate FTS-only。

### 召回追踪

OpenClaw 会 best-effort 记录 short-term recall hits，用于 dreaming/promotion。这个记录不阻塞工具结果。

### QA 场景

OpenClaw 的 QA 明确测试：

- channel context 中问 prior notes 时，必须先 `memory_search` 再 `memory_get`；
- memory failure 时不能幻觉，要说明 checked but unavailable；
- MCP/QMD-backed recall 要验证真实路径。

### 对 Pulsara 的启发

OpenClaw v1 对 Pulsara 来说太重，但它给了重要边界：

- `search` 与 `get` 应分离；
- 工具描述要强约束“问过去事实前必须召回”；
- recall failure 应结构化，让模型报告原因和下一步，而不是补全；
- 搜索结果需要 citation / memory_id，方便用户与系统追踪；
- hybrid/vector/MMR/temporal decay 可以作为 v2/v3，而不是 v1。

## 横向比较

| 系统 | 自动注入 | 显式搜索工具 | 搜索方法 | 精确读取 | 失败策略 | 对 Pulsara 的价值 |
| --- | --- | --- | --- | --- | --- | --- |
| MiMo-Code | rebuild dump + per-turn reminder | `memory.search` | SQLite FTS5 + BM25 + OR query + score floor | Read file | 零结果给 retry/grep/history 指导 | 最适合 v1 lexical recall |
| Claude Code | MEMORY.md 常驻 + async relevant attachments | 无单独 search tool，靠 attachment prefetch/FileRead | LLM selector over manifest | FileRead | selector 可空，不阻塞 | 适合 v2 selector 与 policy |
| Hermes | frozen snapshot + provider prefetch | `memory` write tool + `session_search` transcript recall | provider 自定义；session_search 用 FTS5 | session scroll/read | interrupted turn 不 sync；provider failure best-effort | lifecycle 与 ephemeral 注入很值得借鉴 |
| OpenClaw | MEMORY.md / daily notes + tools | `memory_search` / `memory_get` | hybrid vector + BM25 + MMR/QMD optional | `memory_get` line range | structured unavailable + cooldown | search/get 工具边界与 failure contract |

## Pulsara Recall v1 建议

### 1. 召回只读 canonical graph

Pulsara 的写入系统已经把候选池与 canonical memory 分开。召回必须只读 canonical graph：

- include: `NodeStatus.ACTIVE`;
- optionally diagnostic: `NodeStatus.NEEDS_REVIEW`;
- exclude by default: `REJECTED`, `STALE`, `SUPERSEDED`, `CONTRADICTED`, `ARCHIVED`, `DELETED`;
- never read candidate pool for normal recall。

这可以避免 pending/invalid/corrected candidates 污染模型视野。

### 2. 先做 lexical recall，不急着 embedding

v1 可实现：

- query tokenization：Unicode letters/numbers/underscore；
- field matching：statement、scope、kind、maybe source authority；
- ranking：token overlap、rare token boost、scope affinity、kind priority；
- top-k：默认 5 或 8；
- score floor：相对 top score 裁剪；
- zero-result guidance：提醒模型换短 query、查 transcript/current files。

Oxigraph 后端未来可以 SPARQL 查询；in-memory 后端可直接扫描 JSON-LD documents，保证测试先跑通。

### 3. 保留 projection，但让它接真实 recall service

Pulsara 已有 `ProjectionRequestedEvent` / `ProjectionReadyEvent` 和 projection fence：

```text
Recalled Memory (source=fenced_recalled_memory; do not write it back as new memory)
```

下一步不是重写 projection，而是让 projection 使用 `MemoryRecallService.search()` 生成：

- `summary`;
- `items`;
- `included_memory_ids`;
- `filtered_memory_ids`;
- optional `warnings`。

projection 应该是小块自动上下文，不是全量 memory dump。

### 4. 增加显式 `memory_search` / `memory_get`

建议 v1 暴露两个只读工具：

```json
memory_search({
  "query": "...",
  "scope": "ctx:user | ctx:project | ctx:session | ...",
  "kind": "Preference | Claim | Observation | ActionBoundary | Decision",
  "limit": 5
})
```

```json
memory_get({
  "memory_id": "mem:..."
})
```

`memory_search` 返回短 snippet 与 score；`memory_get` 返回完整 canonical node、evidence refs、status、source_authority、verification_status、created_at/updated_at 等必要审计字段。

### 5. Prompt policy 必须写清

Pulsara 的 recall prompt/policy 至少应包含：

- 当用户明确问“你记得吗 / 之前 / recall / remember / 偏好 / 决定”时，先查 memory；
- 用户说忽略 memory 时，当 memory 为空，不使用、不引用、不比较；
- memory 是过去事实，不保证当前仍真；
- 涉及当前文件、函数、配置、外部状态时，必须用工具验证当前状态；
- 不要把 recalled projection echo 写回 memory。

### 6. Transcript recall 与 memory recall 分开

Hermes 的 `session_search` 提醒很重要：有些问题不是 durable memory，而是 raw conversation history。Pulsara v1 可以先只做 canonical memory recall；但文档上应明确：

- memory recall 回答“系统治理后认为值得长期保留的事实”；
- transcript/session recall 回答“我们过去具体说过什么、原话是什么、某次任务怎么收尾”；
- 两者不要混在同一个工具里。

## 不建议 v1 做的事

- 不要把 candidate pool 作为召回源；
- 不要默认召回 `NEEDS_REVIEW` 作为事实；
- 不要每轮 dump 全部 memory；
- 不要第一版引入 embedding provider、QMD、MMR、temporal decay；
- 不要用 LLM selector 取代基础 lexical search；
- 不要把 recall 注入写回 event history 的 user message 原文；
- 不要让 memory retrieval failure 静默降级成模型猜测。

## 建议的下一步实现顺序

1. 定义 `MemoryRecallService` 协议与 in-memory 实现。
2. 为 graph JSON-LD canonical memory 建一个统一 record view。
3. 实现 lexical search 与 status/scope/kind filters。
4. 接入现有 projection hook，生成小型 `memory_projection`。
5. 增加只读 `memory_search` / `memory_get` 工具。
6. 补测试：
   - explicit user preference 能在后续 turn 召回；
   - unrelated memory 不召回；
   - rejected/stale/superseded 不召回；
   - user says ignore memory 时 suppress；
   - projection echo 不被 reflection/governance 写回；
   - search zero result 给清晰 fallback；
   - current file/function claims require verification prompt behavior。
