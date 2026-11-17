# Pulsara 展示工具合同与使用教程

共用规则见[原生工具修订草案索引](../archived_docs/PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整结果保存、原生 12k 分页及 NEXT 沿[统一交付草案](../archived_docs/PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)。

状态：当前生效合同。本族只有 open_in_pulsara 一个 ROOT 原生工具，不提供 JS API。当前确定参数、精简返回值、原生示例及错误语义。TypeScript 只描述合同，不表示存在脚本入口。

## 1. open_in_pulsara：向用户展示目标

### 1.1 用途与行为

在当前会话的标签页中请求展示本地文件、HTTP(S) 网页或当前 ROOT 下的 subagent 只读会话。一次可以提交多个目标，按输入顺序处理，最后一个成功接受的目标作为选中目标；实际加载及显示仍由现有前端展示 owner 完成。

成功表示展示请求已接受，不表示文件已成功读取、网页已加载完毕或用户已经看到。该工具不向模型读取文件、网页或 Agent 正文，不启动或继续 Agent 工作，也不把打开网页当作模型已浏览其内容。只读模式沿现有工具权限规则可用。

单项失败不阻止其他合法项继续处理。只要有目标失败，整体 meta.status 就是 ERROR，content 明确已接受项及失败原因；不增加部分成功状态或字段。前端继续处理实际已接受的展示请求，不能因工具整体返回 ERROR 而忽略它们。

### 1.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| targets | Target 数组 | 必填，至少一项 | 按顺序请求展示的目标；不另设数量上限 |

公开 Target 只保留以下三种封闭形状：

| kind | 其余字段 | 明确含义 |
| --- | --- | --- |
| PATH | path:string | 本地文件路径，支持工作区相对路径、绝对路径及 ~；沿现有路径解析与访问规则处理 |
| URL | url:string | 完整的 HTTP(S) 网页地址，不包含用户名或密码 |
| AGENT | agent_id:string | 当前 ROOT 下可读 subagent 的身份，底层为共享 session 身份；只读展示其会话 |

每项只接受 kind 和对应字段；字段为非空文本，AGENT 身份按共享 ID 层校验。PATH 在本工具中解析成本地目标，不以路径被接受证明文件存在或可成功显示；文件实际读取由展示端完成。AGENT 通过既有共同读取 owner 校验归属和可用性，不要求模型填 root_session_id。

外层只接受 targets，不接受 action、标题、显示模式、目标会话、root_session_id 或 host_session_id。空数组、null、非数组或外层字段错误属于整次参数失败；具体目标格式、类型、地址或对象不可用等问题按原数组位置逐项报告，其他合法项继续。

```typescript
type OpenInPulsaraTarget =
  | {kind: "PATH"; path: string}
  | {kind: "URL"; url: string}
  | {kind: "AGENT"; agent_id: string};

interface OpenInPulsaraArgs {
  targets: OpenInPulsaraTarget[];
}
```

### 1.3 精简返回值与状态

业务结果只保留 meta/content，不返回顶层业务 target，也不回显 targets、errors 数组、归属 ID、计数或每项状态。

| 字段 | 含义 |
| --- | --- |
| meta.status | SUCCESS／ERROR／CANCELLED |
| content | type:text、value:string 的有序文字项数组，保存简短结果和实际错误原因 |

| 情况 | 状态 | content |
| --- | --- | --- |
| 所有目标的展示请求均已接受 | SUCCESS | “已提交展示请求。” |
| 有目标接受、有目标失败 | ERROR | 明确哪些原数组项已接受，以及失败项的实际原因 |
| 所有目标失败 | ERROR | 说明没有展示请求被接受，以及各失败项的原因 |
| 整次参数或执行失败 | ERROR | 简短实际原因及必要的已知效果 |
| 调用取消 | CANCELLED | 真实取消情况及已经确定的接受事实 |

```typescript
interface OpenInPulsaraTextPart {
  type: "text";
  value: string;
}

interface OpenInPulsaraResult {
  meta: {status: "SUCCESS" | "ERROR" | "CANCELLED"};
  content: OpenInPulsaraTextPart[];
}
```

项的位置统一写为 targets[n]，n 是从 0 开始的原输入数组下标，不因失败项被跳过而重新编号。需要分行时，文字项自身保存换行，不依赖 runtime 自动插入分隔符。

不使用 PARTIAL 或其他部分成功枚举，不增加 meta.error、_hint、公开错误代码、重试票据或展示完成回执。错误不能声称整批没有执行，除非确实没有任何展示请求被接受；不因 ERROR 自动重做整批请求。

### 1.4 原生示例

展示文件、网页及 Agent 会话：

```text
工具：open_in_pulsara
参数：
  targets:
    - kind: PATH
      path: "output/report.md"
    - kind: URL
      url: "https://example.com"
    - kind: AGENT
      agent_id: "session:<Agent会话ID>"
```

假设所有请求均成功接受，保存的业务值为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "已提交展示请求。"}
  ]
}
```

原生交付：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际调用耗时>

meta:
  status: SUCCESS

content:
  已提交展示请求。

tool_result: <本次完整结果的导出 Target>
next: null
```

某项无效，其他请求仍被接受：

```text
工具：open_in_pulsara
参数：
  targets:
    - kind: PATH
      path: "output/report.md"
    - kind: URL
      url: "file:///tmp/report.html"
    - kind: AGENT
      agent_id: "session:<Agent会话ID>"
```

假设文件与 Agent 请求均被接受：

```text
meta:
  status: ERROR

content:
  已接受 targets[0]、targets[2] 的展示请求。
  targets[1] 未接受：网页地址只支持 HTTP(S)。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

此时前端仍处理 targets[0] 和 targets[2]，不等待模型处理或补读错误说明才展示。全部目标失败时，例如一次只提交了一个非 HTTP(S) URL，结果可为：

```text
meta:
  status: ERROR

content:
  没有展示请求被接受。
  targets[0] 未接受：网页地址只支持 HTTP(S)。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

上述结果均假设实际业务值已完整保存并一页交付。身份及观察值为说明占位，实际观察时间及已测得微秒耗时在消息顶层；未测得耗时不伪造为 0。实际网页或文件稍后加载失败不反向改写已接受的工具结果，仍由现有展示端表达。

### 1.5 内部展示数据、错误与分页

展示端实际需要的规范化目标、所属 ROOT 等上下文沿既有展示／执行 owner 保留，作为必要内部事实，不作为公开工具返回值，不另存一份模型正文。前端从实际已接受目标处理展示，不能继续依赖模型可见正文中的 targets 数组，也不能以整体 SUCCESS 为有合法目标的唯一条件。保留现有可见连接、会话归属及对象访问检查，不从当前界面猜测或替换原目标归属。

删除调用前因旧控制载荷或结果说明不满足展示页预算而整批拒绝的检查。请求和结果按既有 owner 接受与保存，原生结果按统一 12k 规则分页；页面边界不决定接受哪些目标。实际资源或执行失败仍如实报告，不能截掉部分请求后称整批成功。

NEXT 只读取已保存的冻结工具结果，不重新展示目标、重新加载网页或再次打开标签页；导出结果也不触发展示。展示动作不依赖模型是否读完结果说明，不增加独立分页协议、后台恢复任务、展示完成等待或 ACK。

输入被拒绝时只说明实际原因；可能包含凭据的无效 URL 不原样回显。调用取消或保存／交付失败时，保留已经确定的接受事实；只有确认没有接受任何请求时才这样说明，不能自动重新提交。更早失败没有业务值时沿既有错误通道报告，不伪造结果。

完整结果 Target 只在实际结果已成功保存且可读时提供；保存失败或未确认时不提供假 tool_result 或 next。保存成功但本页无法交付时，沿共用规则保留实际结果 Target 和本页未交付起点的 NEXT。错误结果正常交付完的 next:null 只表示结果读完，不表示所有展示请求成功。

## 2. 当前生产实现参考

- [工具参数及公开 Target](../src/pulsara_agent/capability/builtin_catalog.py)
- [目标规范化和逐项处理](../src/pulsara_agent/tools/builtins/presentation.py)
- [逐项目标校验入口](../src/pulsara_agent/conversation_kernel/tool_runtime_schema.py)
- [Agent 读取资格与归属检查](../src/pulsara_agent/conversation_kernel/presentation_targets.py)
- [原生调用及结果接入](../src/pulsara_agent/conversation_kernel/tool_runtime_management.py)
- [前端展示结果消费](../frontend/lib/runtime-adapter.ts)
- [前端目标解析](../frontend/lib/presentation-targets.ts)
- [前端标签页展示](../frontend/components/presentation-provider.tsx)

这些链接用于核对实施接点，不表示当前代码已经符合本草案。
