# Execution Evidence Ledger MVP

## 状态标注

```text
Current
  Turn / ToolResult / Artifact / Evidence / Claim 的最小闭环已实现
  EventLog -> ToolResultBlock -> ledger 入库已实现

Next
  output_preview 与 semantic summary 分离
  Decision 作为独立实体或 Claim subtype 明确落地

Target
  更丰富的 execution blackboard
  claim / decision / contradiction / supersede 的更完整生命周期
```

本文档定义 JSON-LD 多层记忆系统中最小可落地的 `Execution Evidence Ledger`。它不是完整的 Task Graph / Blackboard，也不试图理解所有探索性工作。它只解决 MVP 阶段最重要的闭环：

```text
工具执行了什么
结果保存在哪里
哪些结果形成证据
哪些证据支撑或反驳结论
新结论如何替代旧结论
Projection 如何只召回可靠结论
```

MVP 不追求“系统理解一切任务关系”。MVP 只证明：

```text
结论可追溯。
旧结论可被新证据反驳或替代。
Prompt 不被过期/矛盾结论污染。
```

## 1. 为什么不先做完整 Task Graph

用户工作经常是探索性的。早期阶段强行维护完整 task graph 会遇到三个问题：

```text
边界不清：讨论、探索、任务、决策常常交织。
证据难管：证据会过期、被反驳、被新文件状态替代。
关系膨胀：task / plan / question / role / artifact / decision 很快变成一张维护不了的图。
```

因此 MVP 先把 `Task Graph / Blackboard` 收缩成：

```text
Execution Evidence Ledger = append-only execution/evidence graph + claim lifecycle
```

它是一张证据账本，不是全知黑板。

## 2. MVP 最小对象

MVP 只定义 6 个核心对象：

```text
mem:Turn
mem:ToolResult
mem:Artifact
mem:Evidence
mem:Claim
mem:Decision
```

### 2.1 mem:Turn

一次用户/agent 交互回合，或一次 loop iteration。

职责：

```text
记录本轮发生了什么。
连接用户消息、assistant 消息、工具调用和工具结果。
作为 provenance 起点。
```

最小字段：

```text
@id
@type = mem:Turn
mem:session
mem:index
mem:startedAt
mem:endedAt
mem:scope
```

### 2.2 mem:ToolResult

一次工具执行返回的运行时结果。

职责：

```text
记录哪个工具被调用。
记录工具是否成功。
记录输入/输出摘要。
记录输出是否被截断。
连接保存下来的 Artifact。
连接由本次结果产生的 Evidence。
```

`ToolResult` 不是大对象存储。它只保存 metadata 和 summary。完整 stdout/stderr、网页、文件快照、大 JSON 应进入 `ArchiveStore`，并由 `Artifact` 指向。

最小字段：

```text
@id
@type = mem:ToolResult
mem:toolName
mem:status = success | error | cancelled | timeout
mem:inputSummary
mem:outputSummary
mem:startedAt
mem:endedAt
mem:truncated
mem:scope
```

### 2.3 mem:Artifact

被保存下来的原文、大对象或外部对象引用。

职责：

```text
指向 ArchiveStore 中的原始内容。
保存 hash、mime、path、summary。
作为 evidence 的来源之一。
```

最小字段：

```text
@id
@type = mem:Artifact
mem:storedAt
mem:hash
mem:mimeType
mem:summary
mem:createdAt
mem:scope
```

### 2.4 mem:Evidence

可以支撑或反驳某个结论的证据片段。

职责：

```text
把原始来源转成可引用的证据对象。
记录证据来源、状态、有效范围、过期策略。
支撑或反驳 Claim。
```

Evidence 不是“永远正确的事实”。它只是“某个判断当时依赖的一段证据”。

最小字段：

```text
@id
@type = mem:Evidence
mem:statement
mem:sourceType = user_message | assistant_message | tool_result | artifact | file_snapshot | web_page | model_inference
mem:status = active | stale | superseded | contradicted | redacted | deleted
mem:observedAt
mem:scope
mem:staleAfter
mem:createdFrom
```

### 2.5 mem:Claim

系统认为可能有用的结论。

职责：

```text
表达一个可被证据支撑或反驳的 statement。
承载 lifecycle status。
作为 Projection 的主要召回对象之一。
```

最小字段：

```text
@id
@type = mem:Claim
mem:statement
mem:scope
mem:status = candidate | active | stale | superseded | contradicted | archived | redacted | deleted
mem:confidenceLevel = low | medium | high | verified
mem:verificationStatus = unverified | inferred | user_confirmed | tool_verified | contradicted | stale
mem:sourceAuthority = model_inference | conversation_evidence | explicit_user_instruction | tool_result | document_source | system_rule
mem:createdAt
mem:updatedAt
```

### 2.6 mem:Decision

特殊类型的 Claim，表示用户、agent 或系统在某个上下文中采纳的设计/行动决策。

职责：

```text
记录为什么选择某条路线。
连接支撑该决策的 Evidence。
控制未来行动和 Projection。
```

`Decision` 可以实现为 `mem:Claim` 的 subtype，但 MVP 可以单独保留，方便查询和 prompt projection。

最小字段：

```text
@id
@type = [mem:Claim, mem:Decision]
mem:statement
mem:why
mem:scope
mem:status
mem:confidenceLevel
mem:verificationStatus
mem:sourceAuthority
mem:createdAt
mem:updatedAt
```

## 3. MVP 最小关系

MVP 只保留 8 条关系：

```text
mem:produced
mem:storedAs
mem:provides
mem:supports
mem:contradicts
mem:supersedes
mem:basedOn
mem:createdFrom
```

### 3.1 turn -> produced -> toolResult

```text
mem:Turn mem:produced mem:ToolResult
```

含义：

```text
这个回合产生了这个工具结果。
```

### 3.2 toolResult -> storedAs -> artifact

```text
mem:ToolResult mem:storedAs mem:Artifact
```

含义：

```text
工具结果的大对象或完整输出被保存成 Artifact。
```

如果工具结果很短，可以没有 Artifact。

### 3.3 toolResult -> provides -> evidence

```text
mem:ToolResult mem:provides mem:Evidence
```

含义：

```text
工具结果摘要本身提供了可引用证据。
```

例如 `pytest` 返回 3 个失败，摘要就足以成为 evidence。

### 3.4 artifact -> provides -> evidence

```text
mem:Artifact mem:provides mem:Evidence
```

含义：

```text
Artifact 中的某段原文、文件快照或日志片段提供了 Evidence。
```

### 3.5 evidence -> supports -> claim

```text
mem:Evidence mem:supports mem:Claim
```

含义：

```text
这条证据支撑某个结论。
```

### 3.6 evidence -> contradicts -> claim

```text
mem:Evidence mem:contradicts mem:Claim
```

含义：

```text
这条证据反驳某个旧结论。
```

### 3.7 claim -> supersedes -> claim

```text
mem:Claim mem:supersedes mem:Claim
```

含义：

```text
新结论替代旧结论。
```

旧结论应变为 `superseded`，而不是被静默覆盖。

### 3.8 decision -> basedOn -> evidence

```text
mem:Decision mem:basedOn mem:Evidence
```

含义：

```text
这个决策基于某条或多条证据。
```

## 4. 不进入 MVP 的关系

以下关系暂不进入 MVP：

```text
task -> hasDecision
task -> hasOpenQuestion
task -> hasArtifact
plan -> hasStep
role -> handoffTo
question -> answeredBy
artifact -> derivedFrom -> artifact
skill -> requires -> skill
memory -> relatedTo -> memory
```

这些关系以后可能有价值，但第一版会让系统过早膨胀。

MVP 的边界是：

```text
先做证据账本，不做完整任务知识图谱。
```

## 5. 写入流程

### 5.1 工具结果写入

当工具执行结束：

```text
ToolExecutor
  -> create mem:ToolResult
  -> if large output: create mem:Artifact
  -> if output can support claims: create mem:Evidence candidate
  -> write JSON-LD documents
  -> expand to RDF quads
  -> write GraphStore
  -> store raw output in ArchiveStore, if needed
```

### 5.2 Claim 写入

Claim 不由工具直接产生。Claim 来自：

```text
用户明确表达
主 Agent 提议
Background Memory Curator 提议
Consolidation 提议
```

所有 Claim 必须经过 `MemoryWriteGate`。

```text
ClaimCandidate
  -> check scope
  -> attach evidence
  -> check duplicate
  -> check contradiction
  -> assign confidenceLevel / verificationStatus / sourceAuthority
  -> accepted / rejected / needs_review
```

### 5.3 新证据到来

新证据不覆盖旧证据。新证据进入图后，系统再判断它影响哪些 Claim。

```text
new Evidence
  -> search affected Claims
  -> supports existing Claim, or
  -> contradicts existing Claim, or
  -> supports new Claim
```

如果新证据反驳旧结论：

```text
newEvidence mem:contradicts oldClaim
oldClaim mem:status "contradicted"
```

如果新结论替代旧结论：

```text
newClaim mem:supersedes oldClaim
oldClaim mem:status "superseded"
```

## 6. Evidence 过期与失效

MVP 不做复杂证据时效系统，只做简单规则。

```text
user_message evidence
  默认长期有效，除非用户纠正或要求删除。

tool_result evidence
  默认与本次工具调用绑定；如果依赖文件快照，则文件 hash 变化后 stale。

artifact evidence
  由 hash 保证可追溯；外部网页快照默认 staleAfter 较短。

file_snapshot evidence
  文件内容 hash 变化后 stale。

web_page evidence
  默认 staleAfter = 7-30 days，视配置而定。

model_inference evidence
  默认 low confidence，不能单独支撑高影响 Claim。
```

过期 evidence 不删除，默认改为：

```text
mem:status = stale
```

Projection 可以在高相关时召回 stale evidence，但必须标注“需验证”。

## 7. Projection 规则

Projection 不直接召回所有 Evidence。默认召回对象是 Claim / Decision。

规则：

```text
active + high/verified Claim
  可以进入 prompt。

active + medium Claim
  可以低权重进入 prompt。

stale Claim
  默认不进 prompt；高相关时标注需验证。

contradicted Claim
  不进执行 prompt，只进审计/追溯视图。

superseded Claim
  默认不进 prompt，只在需要解释演化链时进入。
```

Claim 进入 prompt 时必须带：

```text
@id
statement / summary
confidenceLevel
verificationStatus
supporting evidence @id
staleness warning, if any
```

## 8. SPARQL MVP 查询

### 8.1 查询当前可用结论

```sparql
PREFIX mem: <https://agent.example/memory#>

SELECT ?claim ?statement ?confidenceLevel ?verificationStatus WHERE {
  ?claim a mem:Claim ;
         mem:statement ?statement ;
         mem:status "active" ;
         mem:confidenceLevel ?confidenceLevel ;
         mem:verificationStatus ?verificationStatus .
  FILTER(?confidenceLevel IN ("high", "verified"))
}
LIMIT 20
```

### 8.2 查询某结论的证据

```sparql
PREFIX mem: <https://agent.example/memory#>

SELECT ?evidence ?statement ?sourceType ?status WHERE {
  ?evidence a mem:Evidence ;
            mem:supports <mem:claim/current> ;
            mem:statement ?statement ;
            mem:sourceType ?sourceType ;
            mem:status ?status .
}
```

### 8.3 查询被新证据反驳的旧结论

```sparql
PREFIX mem: <https://agent.example/memory#>

SELECT ?claim ?claimText ?evidence ?evidenceText WHERE {
  ?evidence a mem:Evidence ;
            mem:contradicts ?claim ;
            mem:statement ?evidenceText .
  ?claim a mem:Claim ;
         mem:statement ?claimText ;
         mem:status "contradicted" .
}
```

### 8.4 查询工具结果到证据链

```sparql
PREFIX mem: <https://agent.example/memory#>

SELECT ?turn ?toolResult ?artifact ?evidence WHERE {
  ?turn a mem:Turn ;
        mem:produced ?toolResult .
  ?toolResult a mem:ToolResult .
  OPTIONAL { ?toolResult mem:storedAs ?artifact . }
  OPTIONAL { ?toolResult mem:provides ?evidence . }
  OPTIONAL { ?artifact mem:provides ?evidence . }
}
```

## 9. JSON-LD 示例

```json
{
  "@context": {
    "mem": "https://agent.example/memory#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "produced": { "@id": "mem:produced", "@type": "@id" },
    "storedAs": { "@id": "mem:storedAs", "@type": "@id" },
    "provides": { "@id": "mem:provides", "@type": "@id" },
    "supports": { "@id": "mem:supports", "@type": "@id" },
    "basedOn": { "@id": "mem:basedOn", "@type": "@id" },
    "createdAt": { "@id": "mem:createdAt", "@type": "xsd:dateTime" }
  },
  "@graph": [
    {
      "@id": "turn:session-001/turn-004",
      "@type": "mem:Turn",
      "produced": "tool-result:rg-jsonld-001",
      "mem:index": 4,
      "createdAt": "2026-06-06T01:00:00Z"
    },
    {
      "@id": "tool-result:rg-jsonld-001",
      "@type": "mem:ToolResult",
      "mem:toolName": "rg",
      "mem:status": "success",
      "mem:outputSummary": "找到 l2_blackboard.rs 中 build_triples 对 JSON 字段拍扁处理。",
      "storedAs": "artifact:rg-jsonld-001",
      "provides": "evidence:jsonld-flattening-code"
    },
    {
      "@id": "artifact:rg-jsonld-001",
      "@type": "mem:Artifact",
      "mem:storedAt": "archive://session-001/tool/rg-jsonld-001.txt",
      "mem:hash": "sha256:...",
      "mem:summary": "rg 搜索结果，指向 JSON-LD flattening 实现。"
    },
    {
      "@id": "evidence:jsonld-flattening-code",
      "@type": "mem:Evidence",
      "mem:statement": "当前实现中 JSON 字段被拍扁为伪 triple，未保留完整 JSON-LD 语义。",
      "mem:sourceType": "tool_result",
      "mem:status": "active",
      "mem:observedAt": "2026-06-06T01:00:00Z",
      "supports": "claim:jsonld-currently-wrapper"
    },
    {
      "@id": "claim:jsonld-currently-wrapper",
      "@type": "mem:Claim",
      "mem:statement": "当前实现里的 JSON-LD 更接近语义包装层，还没有充分用作 RDF/SPARQL 事实层。",
      "mem:status": "active",
      "mem:confidenceLevel": "high",
      "mem:verificationStatus": "tool_verified",
      "mem:sourceAuthority": "tool_result"
    },
    {
      "@id": "decision:mvp-evidence-ledger-first",
      "@type": ["mem:Claim", "mem:Decision"],
      "mem:statement": "MVP 先实现 Execution Evidence Ledger，而不是完整 Task Graph。",
      "mem:why": "探索性工作边界难定，先证明证据、结论、反驳、替代、projection 过滤闭环。",
      "basedOn": "evidence:jsonld-flattening-code",
      "mem:status": "active",
      "mem:confidenceLevel": "verified",
      "mem:verificationStatus": "user_confirmed",
      "mem:sourceAuthority": "explicit_user_instruction"
    }
  ]
}
```

## 10. 验收标准

MVP 完成时必须能通过这些测试：

```text
test_tool_result_creates_ledger_nodes
  工具执行后生成 ToolResult；大输出生成 Artifact。

test_evidence_supports_claim
  Evidence 能通过 supports 连接 Claim。

test_evidence_contradicts_claim
  新 Evidence 能反驳旧 Claim，并将旧 Claim 标为 contradicted。

test_claim_supersedes_claim
  新 Claim 能 supersede 旧 Claim。

test_projection_filters_contradicted_claim
  contradicted Claim 不进入执行 prompt。

test_projection_keeps_evidence_ids
  Claim 进入 Projection 时保留 supporting evidence @id。

test_archive_not_graph_body
  大工具输出全文进入 ArchiveStore，GraphStore 只保存 metadata。
```

如果这些测试通过，说明 MVP 已经证明 JSON-LD / GraphStore 的核心价值，而不需要先实现完整 Task Graph。
