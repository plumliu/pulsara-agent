# Pulsara 语义判断工具修订与使用教程草案

共用规则见[工具索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整保存、JS 返回及 Cell 输出沿[统一交付草案](../PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)，存储沿[数据库 hardcut 草案](../PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。

状态：合同已确定，未实施。tools.decide 仅供 ROOT 在 bypass-permissions 的 Code Mode 中调用，不提供原生入口或 Subagent JS API。本文件只规定参数与结果修订，其余底层产品语义保持现状。

## 1. tools.decide：逐项判断文本是否满足条件

### 1.1 参数与判断语义

| 参数 | 类型 | 明确含义 |
| --- | --- | --- |
| condition | string，必填 | 一个明确的自然语言真假条件，不允许纯空白，保留原文 |
| inputs | string[]，必填 | 按原顺序独立判断的文本；每项所需背景由调用者放入该项 |

只接受这两个字段，不接受 null、额外字段或非字符串元素。inputs:[] 合法，不读取 Decision 配置或请求服务；空字符串与重复文本均保留各自位置。主会话、记忆、Skill、工具目录和其他输入项不自动成为该项的背景；输入中的指令也只作为待判断材料。精确比较和计数直接使用 JS。

每项结果严格为 boolean，数量与 inputs 相同、逐项对应。底层概率固定以 >= 0.5 转为 true，不公开概率、阈值、理由或模型选择参数。任一子批失败，整次调用失败，不返回成功前缀、不补 false、不跳过输入。

### 1.2 返回类型与 available()

业务值只保留 meta.status 和 content。成功时 content 固定包含一个 object，其 value 只有 decisions；空输入返回同一形状，decisions 为 []。失败或取消使用文字 content，不携带 decisions，不返回业务 Target、输入副本或统计。

```typescript
interface DecideArgs {
  condition: string;
  inputs: string[];
}

interface DecideSuccess {
  meta: {status: "SUCCESS"};
  content: [{type: "object"; value: {decisions: boolean[]}}];
}

interface DecideFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: {type: "text"; value: string}[];
}

type DecideResult = DecideSuccess | DecideFailure;

interface DecideApi {
  (args: DecideArgs): Promise<DecideSuccess>;
  available(): boolean;
}
```

DecideResult 描述保存的完整业务值；JS 正常返回只使用 DecideSuccess，失败或取消沿既有异常通道提供实际存在的 err.result。参数或运行时更早失败尚无业务值时，不伪造 err.result。

tools.decide.available() 是无参、本地同步方法，返回 Cell 启动时冻结的配置完整性 boolean；不联网、不调用模型、不产生工具记录，也不保证服务健康。缺少配置、所需认证材料或设置无法读取时为 false；设置读取错误保留在现有诊断中。新 Cell 读取最新状态；实际非空 decide 调用读取当时的连接一次，各子批使用同一连接。Decision 连接仍独立于记忆重排开关。

### 1.3 JS 示例

```javascript
const condition = "这段反馈是否说明导出失败？";
const inputs = ["点击导出后没有生成文件。", "导出的文件内容正确。"];

if (tools.decide.available()) {
  try {
    const result = await tools.decide({condition, inputs});
    const {decisions} = result.content[0].value;
    console.log(inputs.filter((_, i) => decisions[i]));
  } catch (err) {
    console.log(err.result !== undefined ? err.result : String(err.message ?? err));
  }
} else {
  console.log({condition, inputs});
}
```

未配置时将条件和材料交给 ROOT 继续处理，不自动把未判断项视为 false，也不自动借用主模型或其他连接。上例成功调用的完整业务值可为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "object", "value": {"decisions": [true, false]}}
  ]
}
```

### 1.4 完整保存、错误与底层复用

结果完整保存后才返回完整 JS 值，不使用原生观察时间外壳、12k／NEXT 或 tool_result。decisions 只保存为上述 object 内容项，不另存顶层 decisions、原始返回 JSON 或文字副本；meta.status 从实际结算事实投影。console.log／非 undefined 的显式 return 沿既有 Cell 输出链路整条交付，普通中间值不自动进入 ROOT 上下文，异常及晚到结果沿共同规则。

删除按原生展示页预算拒绝完整判断结果的旧检查；旧 JS 固定单值 cap 按 unified 删除，不改成条数上限。真实桥接、内存及服务资源失败沿现有 owner 明确报告，不截短成功结果。继续复用现有 Decision 配置、协议适配、内部批处理、并发、请求边界、超时、取消与 SDK 清理；不新增判断任务、恢复游标或重放机制。

未配置、服务拒绝、请求或响应非法等实际失败为 ERROR；只有实际取消才为 CANCELLED，超时不当成语义 false。原因及必要的输入位置、限制写进文字 content，删除公开 error／message／input_index／limit 字段。保存或交付失败说明已知事实与可用性，不伪造成功值或自动重做判断。

## 2. 当前生产实现参考

- [参数校验、配置、结果与旧预算检查](../src/pulsara_agent/conversation_kernel/decision_tools.py)
- [共用 Decision 协议、分批、取消及关闭](../src/pulsara_agent/retrieval/decision.py)
- [脚本 API 目录与说明](../src/pulsara_agent/capability/builtin_catalog.py)
- [Cell 中冻结的 available()](../src/pulsara_agent/code_mode/node/guest.js)

这些链接用于核对实施接点，不表示当前代码已经符合本草案。
