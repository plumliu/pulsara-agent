# Pulsara todo 工具合同与使用教程

共用规则见[原生工具修订草案索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整保存、原生 12k 分页及 NEXT 沿[统一交付草案](../PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)。

状态：当前生效合同。todo 只提供原生调用，不提供 JS API。本文件的 TypeScript 只描述参数和业务返回类型，不表示存在脚本入口。

## 1. todo：替换当前进度清单

### 1.1 用途与行为

维护 ROOT 当前工作的有序进度清单。每次调用提交替换后的完整 items，省略的旧条目被移除；items:[] 清空清单。数组顺序即清单顺序，不增加增删改动作、条目 ID、查询工具或业务 Target。

清单只表示进度，不启动、调度或委派任务，也不等同于 Tasks 页面中的 subagent 任务。保留现有当前工作生命周期：ROOT 执行进入 idle 时清空，等待用户输入期间保留；当前状态由既有进程内 owner 管理，不新增跨重启恢复或持久清单表。工具调用和结果仍沿共用历史记录保存，不用历史结果重建当前清单。

本工具继续作为本地状态操作在只读模式可用，不因此开放文件写入或其他权限。

### 1.2 最简参数表与边界

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| items | 条目对象数组 | 必填 | 完整替换清单，最多 10 项；空数组表示清空 |
| items[].text | string | 每项必填 | 非空单行文字，同一清单内不得重复 |
| items[].status | pending／in_progress／completed | 每项必填 | 待处理、进行中或已完成；整份清单至多一项为 in_progress |

只接受 items，每个条目只接受 text、status；不接受 null、缺失字段或额外字段。条目状态不增加其他枚举。单行文本的 NFC、无首尾空白和控制字符有效性要求沿现有校验保持，不另设模型参数。

条目数量上限为 10。删除原有 16 项边界、每项文字 192 UTF-8 字节上限，以及整份规范 JSON 的 4 KiB 上限；不在 schema、解析、内部候选或当前状态 DTO 的校验中继续保留这些文本大小限制，不换成另一组字符或 token 上限。真实资源无法承载时明确失败，不截短文字或静默丢弃条目。

```typescript
type TodoItemStatus = "pending" | "in_progress" | "completed";

interface TodoItem {
  text: string;
  status: TodoItemStatus;
}

interface TodoArgs {
  items: TodoItem[];
}
```

10 项、文字唯一和至多一个进行中条目的规则由实际参数校验执行。整个候选先验证，验证失败不部分替换旧清单。

### 1.3 返回字段与类型

业务结果不返回顶层 target，只使用 meta/content：

| 字段 | 含义 |
| --- | --- |
| meta.status | SUCCESS、ERROR 或 CANCELLED，沿共用调用状态规则 |
| meta.counts | 成功替换后本次清单的四个计数；失败和取消不返回 |
| content | type:text、value:string 的有序文字项数组，表达简短结果或实际错误原因 |

counts 包含 pending、in_progress、completed、total；前三项相加等于 total，total 为 0～10，in_progress 为 0 或 1。它们是本次成功替换结果的快照，不在补页或导出时重新读取实时清单。

```typescript
interface TodoCounts {
  pending: number;
  in_progress: number;
  completed: number;
  total: number;
}

interface TodoTextPart {
  type: "text";
  value: string;
}

interface TodoSuccess {
  meta: {
    status: "SUCCESS";
    counts: TodoCounts;
  };
  content: TodoTextPart[];
}

interface TodoFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: TodoTextPart[];
}

type TodoResult = TodoSuccess | TodoFailure;
```

非空清单替换成功时 content 为“已更新进度清单。”；清空成功时为“已清空进度清单。”，四个计数均为 0。不回传整份 items，不重复返回 counts，不新增 revision、条目身份、changed 或 cleared 布尔字段。删除公开的 UPDATED／CLEARED 状态，更新与清空由文字说明区分。

### 1.4 原生调用与成功示例

提交完整清单：

```text
工具：todo
参数：
  items:
    - text: "检查当前实现"
      status: completed
    - text: "修改工具合同"
      status: in_progress
    - text: "验证修改"
      status: pending
```

保存的业务值为：

```json
{
  "meta": {
    "status": "SUCCESS",
    "counts": {
      "pending": 1,
      "in_progress": 1,
      "completed": 1,
      "total": 3
    }
  },
  "content": [
    {"type": "text", "value": "已更新进度清单。"}
  ]
}
```

原生交付示例：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际观察耗时>

meta:
  status: SUCCESS
  counts:
    pending: 1
    in_progress: 1
    completed: 1
    total: 3

content:
  已更新进度清单。

tool_result: <本次完整结果的导出 Target>
next: null
```

观察时间与实际微秒耗时在消息顶层；未测得耗时不伪造为 0。示例中的 Target 和观察值为说明占位。下一次更新仍提交完整 items，包括所有要保留的已完成条目。

清空：

```text
工具：todo
参数：
  items: []
```

```text
meta:
  status: SUCCESS
  counts:
    pending: 0
    in_progress: 0
    completed: 0
    total: 0

content:
  已清空进度清单。

tool_result: <本次完整结果的导出 Target>
next: null
```

### 1.5 错误、取消与交付

| 情况 | 状态与说明 |
| --- | --- |
| 超过 10 项、文字为空或非单行、文字重复、多个进行中条目等参数错误 | ERROR，content 简短说明实际原因，清单不更新 |
| 当前工作作用域已失效或实际替换失败 | ERROR，content 说明实际原因及已知效果 |
| 调用取消 | CANCELLED，content 说明真实取消情况及必要的已知效果 |

例如提交两个 in_progress 条目，确实产生并完整保存的错误结果可投影为：

```text
meta:
  status: ERROR

content:
  进度清单最多允许一个 in_progress 条目，本次未更新。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

超过条数上限时，说明可为“进度清单最多允许 10 项，本次未更新。”；执行前取消时，使用 CANCELLED 和“进度清单更新已取消，本次未更新。”。失败及取消不返回 counts，不用全零计数冒充清空成功，不增加 meta.error、_hint 或其他错误枚举。更早的参数或运行时失败若没有业务值，沿既有错误通道报告，不伪造结果。

当前清单替换与工具结果确认仍沿既有本地状态 owner 结算。不能仅因取消或交付失败就声称清单未更新；只有确认未更新时才这样说明。操作效果、结果保存或交付未确认时如实报告，不自动重做替换，不返回假装可读的完整结果 Target。

删除替换前因返回说明不满足展示预算而拒绝操作的检查。业务执行和结果交付分离；完整结果及实际效果按既有 owner 结算，原生按统一 12k 规则分页。tool_result 定位本次完整结果，NEXT 只补读已保存结果，不重新替换清单、不刷新 counts；当前清单之后变化或清空，不改写历史结果。不为 todo 新增独立分页或恢复机制。

## 2. 当前生产实现参考

- [原生工具参数及说明](../src/pulsara_agent/capability/builtin_catalog.py)
- [条目参数解析与校验](../src/pulsara_agent/tools/builtins/todo.py)
- [当前条数和文本大小边界](../src/pulsara_agent/primitives/todo.py)
- [执行与结果结算接点](../src/pulsara_agent/conversation_kernel/tool_runtime_management.py)
- [当前工作及清单生命周期](../src/pulsara_agent/conversation_kernel/current_work_runtime.py)
- [Code Mode 可调用集合](../src/pulsara_agent/conversation_kernel/tool_surface.py)

这些链接用于核对现状与实施接点，不表示当前代码已经符合本草案。
