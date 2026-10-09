# 记忆提示与重排投影问题记录

日期：2026-10-10。

状态：用户已明确授权修复，两项均已修复。保留问题证据并记录当前实现与验收；没有修改生产设置、数据库、记忆正文或已有长度边界。未引入 when_to_use、对话上下文或 query 改写。

## 1. remember 的正文限制说明遗漏 RESPONSE_PREFERENCE 的 2048 字节上限

### 修复前行为

- [remember.statement 工具说明](../src/pulsara_agent/capability/builtin_catalog.py:236) 统一写为 `at most 8192 UTF-8 bytes`。
- [现有产品常量](../src/pulsara_agent/conversation_kernel/memory/contracts.py:21) 分别规定普通记忆 8192 字节、RESPONSE_PREFERENCE 2048 字节。
- [写入校验](../src/pulsara_agent/conversation_kernel/memory/writes.py:58) 按最终 kind 选择上限，并校验规范化正文的 UTF-8 字节长度。超过相应上限会报错。
- JSON Schema 的 `maxLength: 8192` 是字符数边界，不能替代上述按类别执行的 UTF-8 字节校验。

### 问题与影响

对 RESPONSE_PREFERENCE，模型可见说明与实际写入约束不一致。例如一条 3000 字节的 ASCII 偏好正文满足工具说明宣称的 8192 字节限制，但仍会被实际写入校验拒绝。不是数据库应扩容，也不是已有 2048 字节上限本身错误。

### 已实施修复

工具说明已准确写成普通记忆最多 8192 UTF-8 字节，RESPONSE_PREFERENCE 最多 2048 UTF-8 字节；保留运行时和数据库已有边界。

当前文案：

```text
One self-contained, source-faithful memory, at most 8192 UTF-8 bytes
(2048 for RESPONSE_PREFERENCE).
```

模型可见说明已通过[提示契约测试](../tests/test_direct_memory_prompt_contract.py)。工具描述变化仅在新 cold epoch 或显式采用的 compaction successor 安装；不得热改现有 epoch 已安装的 provider tools。

## 2. 重排对完整 JSON 文本做 head/tail 截断，破坏候选投影结构

### 修复前行为

[候选投影](../src/pulsara_agent/conversation_kernel/memory_tools.py:1460) 先把以下字段整体编码为 JSON 文本：

```text
kind / context_product_label / statement / recorded_at
```

旧实现随后对完整 JSON 字节执行 head/tail，单条投影上限 8192 字节。超限时保留头尾，并在中间插入：

```text
...[rerank projection omitted]...
```

普通 rerank 将这个文本放入 documents；Decision 将其放入候选的 memory 字符串。正文即使满足普通记忆的 8192 字节写入边界，额外元数据与 JSON 编码开销也可能使完整投影超限。

### 确定性复现

使用根 `.venv` 执行以下纯函数检查；不读取生产设置、不访问网络或数据库：

```python
import json
from types import SimpleNamespace
from pulsara_agent.conversation_kernel.memory_tools import _prepare_rerank_projection

fact = SimpleNamespace(
    fact_kind="FACT",
    context_id="ctx:global",
    statement="汉" * 2700,
    recorded_at="2026-10-10T00:00:00Z",
)
result = _prepare_rerank_projection("测试查询", [fact])
text = result[1][0]
print(len(fact.statement.encode("utf-8")))  # 8100
print(len(text.encode("utf-8")))            # 8192
document = json.loads(text)  # 修复前此处抛出 JSONDecodeError；修复后可解析
print(document["statement_truncated"])  # True
```

修复前实际检查结果：

```json
{
  "statement_bytes": 8100,
  "projection_admitted": true,
  "projection_bytes": 8192,
  "has_omission_marker": true,
  "inner_json_valid": false
}
```

### 问题与影响

- 被截断的候选投影不再保证是合法 JSON；UTF-8 解码成功不等于结构仍完整。
- 正文中间的重要条件、否定或范围可能丢失，进而影响模型理解。这一准确性影响尚未经过真实模型对比评测。
- 外层 HTTP 请求仍可为合法 JSON，因为这里的投影是一个字符串；不能把此问题描述为整个请求必然非法或 API 必然拒绝。
- 更长的提示词无法恢复已经删去的内容，不应要求 Decision 严格解析所有候选为完整 JSON。

### 已实施修复与验收

当前 [_rerank_document_projection](../src/pulsara_agent/conversation_kernel/memory_tools.py:1494) 先保留原完整对象。超限时只对 statement 的 Unicode 字符保留头尾，在正文内加入 `...[memory statement truncated]...`，并添加 `statement_truncated=true`。随后使用现有 canonical JSON encoder 序列化，按完整序列化字节数（包括元数据、转义、标记）确定可以保留的正文长度。未截断条目保持原字节投影，不增加标记。若完整元数据和省略标记本身超过既有边界，沿用 NOT_APPLICABLE / 原序降级。

原 8100 字节中文正文复现样例现在的实际结果：

```json
{
  "statement_bytes": 8100,
  "projection_admitted": true,
  "projection_bytes": 8192,
  "inner_json_valid": true,
  "statement_truncated": true,
  "metadata_preserved": true,
  "original_statement_unchanged": true
}
```

[投影及 SDK 测试](../tests/test_memory_retrieval_adapters.py) 覆盖：短条目原字节保持、接近正文上限的中文 / emoji、引号 / 反斜杠 / 换行 / 制表符的 JSON 转义开销、完整元数据、正文头尾与截断标记，以及 20 条长候选在 flat_rerank、nested_rerank、System One、native Decisions 四种实际 SDK wire 上均可解析。

本轮相关 74 项测试通过，包含提示契约、投影 / adapter、候选选择 / 降级及 provider prefix 连续性。验证记录：[测试日志](../output/memory_retrieval_implementation_20261010/prompt-projection-fix-tests.log)。代码静态检查通过。

修复仍遵守原单条、整请求、token 与候选数量边界；正文中间的信息在必须截断时仍会省略，标记不代表内容完整，不宣称已证明记忆准确性提升。
