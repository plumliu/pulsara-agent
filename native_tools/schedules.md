# Pulsara 定时会话工具合同与使用教程

共用规则见[原生工具修订草案索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整保存、原生 12k 分页及 NEXT 沿[统一交付草案](../PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)。

状态：当前生效合同。本族只有 ROOT 原生调用，不提供 JS API。当前记录 query_schedules，以及 manage_schedule 的 create／switch／update／pause／resume／delete、五种时间规则、返回类型和原生示例。TypeScript 只描述合同类型，不表示存在脚本入口。

## 1. 共用时间规则 schedule

query_schedules 展示的时间规则、create 的 values.schedule、switch 新建分支的 replacement.values.schedule，以及 update 的 values.schedule 共用本章定义。规则只有 once、interval、daily、weekly、monthly 五种，不为各 action 分别维护一套规则。

### 1.1 五种规则、格式与创建默认值

下表的可选字段和默认值用于创建新计划，包括 switch 的新建分支；update 提交完整规则，不重新应用创建默认值。timezone 属于计划配置，创建时省略则为 Asia/Shanghai，修改时省略则保持原值。

| kind | 必填字段 | 可选字段与默认 | 行为 |
| --- | --- | --- | --- |
| once | run_at | 无 | 指定当地日期时间执行一次，必须是可用的未来时点 |
| interval | seconds | anchor_at_utc：省略时以创建事务内确定的整秒 UTC 时间为锚点 | 固定锚点、固定间隔重复；seconds 为整数且至少 900 秒，即 15 分钟 |
| daily | time | start_date：默认创建时 timezone 下的当天 | 每天在当地指定时间执行 |
| weekly | time、weekdays | start_date：同上 | 每周在指定星期的当地时间执行 |
| monthly | time、day | start_date：同上 | 每月指定日期的当地时间执行；该月没有此日期则跳过 |

字段格式：

- once.run_at 使用不含 UTC 偏移的当地日期时间 YYYY-MM-DDTHH:mm:ss，例如 2026-11-14T09:00:00；runtime 结合 timezone 转换为内部 UTC instant，不要求模型换算。非法日期、该时区不存在的当地时刻或非未来时点返回 ERROR；重复出现的当地时刻沿现有首次出现规则解释。
- interval.anchor_at_utc 显式提供时为整秒 UTC instant；seconds 不接受小数或布尔值。省略锚点时，第一次在创建锚点的一个间隔后触发，不在创建时立即运行。
- start_date 为有效的 YYYY-MM-DD，time 为当地 HH:mm。首次选择成功保存后的下一个符合规则且不早于 start_date 的时点，不因默认开始日期为当天就补执行当天已过的时点。
- weekdays 为非空、不重复的整数数组，周一为 1，周日为 7；day 为 1～31 的整数。monthly 不因 start_date 所在月或以后某个月缺少此日期而报错、改成月底或暂停；直接跳过该月。day 超出 1～31 才因日期编号无效返回 ERROR。

interval 按固定锚点推进，不按上次执行结束时间重新计时。daily／weekly／monthly 按当地钟表时间计算，不等同于固定秒数间隔。日历规则遇到夏令时不存在的时刻跳过，重复出现的时刻只触发一次。保持现有队列接纳和执行语义，不因省略参数而改变调度行为。

### 1.2 创建输入与完整保存值的类型

ScheduleRuleInput 允许创建时省略锚点或开始日期；ScheduleRule 表达默认值已经确定后的完整规则，供 query 返回和 update 整体替换使用。这是同一套规则在创建前后的必填性区别，不增加规则种类或另一份正文权威。

```typescript
type ScheduleRuleInput =
  | {kind: "once"; run_at: string}
  | {kind: "interval"; seconds: number; anchor_at_utc?: string}
  | {kind: "daily"; time: string; start_date?: string}
  | {kind: "weekly"; time: string; weekdays: number[]; start_date?: string}
  | {kind: "monthly"; time: string; day: number; start_date?: string};

type ScheduleRule =
  | {kind: "once"; run_at: string}
  | {kind: "interval"; anchor_at_utc: string; seconds: number}
  | {kind: "daily"; start_date: string; time: string}
  | {kind: "weekly"; start_date: string; time: string; weekdays: number[]}
  | {kind: "monthly"; start_date: string; time: string; day: number};
```

### 1.3 默认值只确定一次

创建事务内只取一次数据库时间，用它确定省略的 interval 锚点及 timezone 下的“今天”；解析后的默认值、实际 timezone 和冻结权限一起保存。不取模型时间、结果展示时间，也不因等待交付而重算，不新增时间字段。查询返回本章补齐字段的 ScheduleRule：例如省略 interval 锚点，查询仍返回实际 anchor_at_utc；省略 start_date，查询返回实际开始日期。

once 以已经解析的 UTC 时刻作为唯一调度依据，公开 run_at 结合保存的 timezone 投影，不维护另一份可独立修改的当地时刻。查询及原样回填相同规则不重新解释这个时刻；仅实际更改一次性时间或相关解释条件时才解析新值，不增加字段或单独的夏令时处理机制。

## 2. query_schedules：查询当前会话的定时计划

### 2.1 用途与范围

列出当前会话的定时计划，或按 SCHEDULE Target 精确读取一个计划的完整配置。列表和精确查询使用相同的计划 object，列表返回多项，精确查询成功时返回一项。

删除 session 参数。列表固定查询当前会话，精确查询顺手核对计划归属，不接受其他会话的计划。这里收紧的是模型工具范围；前端定时任务页面继续通过用户级服务管理，不依赖当前活动会话。

本工具只读取，不创建、启用或执行计划；已知的 SCHEDULE Target 可以直接查询。计划状态 ACTIVE／PAUSED 是业务属性，与工具调用的 SUCCESS／ERROR／CANCELLED 分开。

### 2.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | SCHEDULE Target | 可选，放在首位 | 精确读取当前会话的一个计划；提供后只填 target，不与 status 混填 |
| status | ALL／ACTIVE／PAUSED | 列表分支可选，默认 ALL | ALL 为全部计划，ACTIVE 为启用计划，PAUSED 为暂停计划 |

只接受上述两个互斥分支：{} 或 {status:ALL／ACTIVE／PAUSED} 查询列表；{target:…} 精确查询。target 和 status 均不接受 null，不接受额外字段。ALL 只用于列表筛选，不成为计划自身的第三种状态。

删除公开 session、cursor 和旧的继续查询调用参数，不新增 limit 或模型可填的并发条件。本次筛选范围内的结果完整保存后，再按统一规则分页。

```typescript
type ScheduleTarget = {kind: "SCHEDULE"; task_id: string};
type ScheduleStatus = "ACTIVE" | "PAUSED";
type ScheduleStatusFilter = "ALL" | ScheduleStatus;

type QuerySchedulesArgs =
  | {target: ScheduleTarget; status?: never}
  | {target?: never; status?: ScheduleStatusFilter};
```

### 2.3 统一返回字段与类型

业务结果只使用 meta/content，没有顶层业务 target；单查也不把 object 内的 Target 再复制到顶层。

| 位置 | 字段 | 含义 |
| --- | --- | --- |
| meta | status | 工具调用的 SUCCESS／ERROR／CANCELLED |
| content 的 object.value | target | 计划的 SCHEDULE Target |
| 同上 | name | 计划名称 |
| 同上 | prompt | 完整执行指令，不缩成固定长度摘录 |
| 同上 | schedule | 保存的时间规则 |
| 同上 | timezone | IANA 时区名称 |
| 同上 | permission_mode | 计划未来执行时使用的权限配置 |
| 同上 | status | 计划自身的 ACTIVE／PAUSED |
| 同上 | next_run_at | 下次自动触发的带时区 UTC 时间字符串；PAUSED 时为 null |

```typescript
interface ScheduleRecord {
  target: ScheduleTarget;
  name: string;
  prompt: string;
  schedule: ScheduleRule;
  timezone: string;
  permission_mode: "read-only" | "ask-permissions" | "accept-edits" | "bypass-permissions";
  status: ScheduleStatus;
  next_run_at: string | null;
}

interface ScheduleTextPart {
  type: "text";
  value: string;
}

interface ScheduleRecordPart {
  type: "object";
  value: ScheduleRecord;
}

interface QuerySchedulesSuccess {
  meta: {status: "SUCCESS"};
  content: (ScheduleTextPart | ScheduleRecordPart)[];
}

interface QuerySchedulesFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: ScheduleTextPart[];
}

type QuerySchedulesResult = QuerySchedulesSuccess | QuerySchedulesFailure;
```

schedule 使用第 1.2 节已保存并补齐默认值的 ScheduleRule。输入规则和默认值统一见第 1 章；查询返回实际保存值，不重新计算默认值。

permission_mode 的初始值取创建 turn 已冻结的实际权限，不是模型创建或修改参数。模型管理动作和输入框的权限选择不改写已有计划权限；用户可以在定时任务页面修改，query 返回本次查询取得的实际保存值。用户实际修改权限时，同一事务内按配置变更规则推进内部 revision，使旧结果不能覆盖或删除这份新配置。

删除 tasks 包装、每项 inspect 调用说明、公开 revision、所属 session、创建／修改时间及旧 next 查询参数，不额外添加命中数或显示数。排队输入和最近一次 turn 等运行信息不放进这份配置记录。

计划存在已知的执行准备错误时，在对应 object 后用相邻 text 项说明，例如“定时任务 schedule:<计划ID> 当前无法准备执行：<实际原因>”。不把诊断伪装成查询失败，不另加 prepare_error 字段或另一组状态；查询本身失败时仍按第 2.5 节处理。

### 2.4 原生调用与正常结果示例

查询当前会话全部计划，以下两种参数等价：

```text
工具：query_schedules
参数：{}
```

```text
工具：query_schedules
参数：
  status: ALL
```

只查询启用计划时填 status:ACTIVE；暂停计划填 status:PAUSED。

假设当前会话有两个计划，且结果可一页完整交付：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际查询耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "SCHEDULE",
      "task_id": "schedule:<A>"
    },
    "name": "每日项目简报",
    "prompt": "检查项目进展，整理完成事项、待办和阻塞。",
    "schedule": {
      "kind": "daily",
      "start_date": "2026-11-01",
      "time": "09:00"
    },
    "timezone": "Asia/Shanghai",
    "permission_mode": "read-only",
    "status": "ACTIVE",
    "next_run_at": "2026-11-14T01:00:00Z"
  }
  {
    "target": {
      "kind": "SCHEDULE",
      "task_id": "schedule:<B>"
    },
    "name": "每周项目回顾",
    "prompt": "总结本周项目变化，并列出下周需要关注的事项。",
    "schedule": {
      "kind": "weekly",
      "start_date": "2026-11-01",
      "time": "18:00",
      "weekdays": [
        5
      ]
    },
    "timezone": "Asia/Shanghai",
    "permission_mode": "read-only",
    "status": "PAUSED",
    "next_run_at": null
  }

tool_result: <本次完整列表结果的导出 Target>
next: null
```

精确读取：

```text
工具：query_schedules
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
```

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际查询耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "SCHEDULE",
      "task_id": "schedule:<A>"
    },
    "name": "每日项目简报",
    "prompt": "检查项目进展，整理完成事项、待办和阻塞。",
    "schedule": {
      "kind": "daily",
      "start_date": "2026-11-01",
      "time": "09:00"
    },
    "timezone": "Asia/Shanghai",
    "permission_mode": "read-only",
    "status": "ACTIVE",
    "next_run_at": "2026-11-14T01:00:00Z"
  }

tool_result: <本次完整精确查询结果的导出 Target>
next: null
```

身份和时间均为示例。实际微秒耗时未测得时不伪造为 0。保存的 content 使用 type:object/value:ScheduleRecord，原生整体投影为固定两空格缩进 JSON，不特判内部 Target，不保存另一份展示正文。

### 2.5 空结果、失败与取消

| 情况 | meta.status | content |
| --- | --- | --- |
| 列表查询正常完成，没有匹配计划 | SUCCESS | 一条 text：“当前会话没有符合条件的定时任务。” |
| 指定计划不存在或不属于当前会话 | ERROR | 一条 text：“该定时任务不存在或不属于当前会话。” |
| 实际查询失败 | ERROR | 简短实际原因，不描述为没有匹配 |
| 调用取消 | CANCELLED | 简短真实取消原因，不声称已正常查完 |

例如列表无匹配：

```text
meta:
  status: SUCCESS

content:
  当前会话没有符合条件的定时任务。

tool_result: <本次完整结果的导出 Target>
next: null
```

指定计划不可查询：

```text
meta:
  status: ERROR

content:
  该定时任务不存在或不属于当前会话。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

示例假设错误业务值确实产生且已完整保存。更早的参数或运行时失败若没有业务值，沿既有错误通道报告，不伪造计划对象或结果 Target。错误只用 meta.status 和文字 content，不增加 meta.error、_hint、revision 冲突码或其他错误枚举。

### 2.6 冻结结果、分页与内部并发依据

本次参数确定查询范围，范围内完整结果保存后，原生按统一 12k 规则顺序分页。object 的 JSON 文字可以跨页，不要求一条计划装入一页，也不重新引入后端旧列表页长作为完整结果上限。按统一 NEXT 续读语义，续读只能读取这次已保存的冻结结果，不重新查询或刷新计划，也不产生新的计划快照；时间规则、状态、诊断、next_run_at 及对应内部执行事实均保持原结果中的值，后续计划修改、触发、暂停或删除不改写它们。

revision 继续承担内部配置并发和调度核对职责，归既有工具结果的必要执行事实，不放进 public_fields、公开 ScheduleRecord、meta、content、原生投影或导出正文。模型不填写 expected_revision；提供给模型的 schema、提示词和教程摘录也不解释这个内部字段，冲突只用文字说明计划已经变化。

查询配置与对应 revision 必须从同一次业务读取一起冻结，不能先读配置再单独读取最新 revision。列表逐项保存对应计划身份和 revision；管理成功结果保存实际变更后的值，switch 保存双方的值。复用现有结果记录中的必要执行事实，不另存配置正文副本，不新增快照表、注册表或公开字段，存储边界见数据库草案第 6.1 节。

runtime 从本 session 已完整保存结果的内部事实取得并发依据，同一计划有多个依据时采用最大的 revision，不因分页展示进度改变其可用性。不能临时读取数据库最新 revision 冒充旧快照，也不能借用其他 session 的结果。无旧快照时先提示查询，switch 的具体核对见第 3.4 节。不新增持久快照登记或阅读资格机制。

正常页面返回完整结果 tool_result 和未读后缀的 NEXT；完整交付后 next:null。失败业务值也按统一规则保存和交付，next:null 不把 ERROR 变成成功。保存失败不提供假结果 Target；保存成功但本页无法交付时，沿统一规则返回实际可读的结果 Target 和本页未交付起点的 NEXT，不重跑查询或把失败页算作已读。

## 3. manage_schedule：创建与切换

### 3.1 共同范围与权限

本章只确定 create／switch，其他 action 不由本章推导。所有计划固定属于当前会话，不接受 session。一个会话最多一个 ACTIVE 计划，可以保存多个 PAUSED 计划。创建或启用只表示开始按时间规则调度，不等于立即执行 prompt。

普通 create 和 switch 中新建计划都由 runtime 保存当前 turn 已冻结的实际权限，不接受 values.permission_mode，也不要求模型复制权限。创建后不随输入框、后续 turn 或查询权限自动变化；用户在定时任务页面实际修改权限时保存新值，并在同一事务内推进内部 revision。switch 启用已有暂停计划时保留其当前保存权限，不顺带改写配置；沿既有权限 owner 检查当前调用是否允许启用该配置。不因参数精简改变工具本身的调用权限。

### 3.2 新计划配置

普通 create 和 replacement.kind:create 共用同一配置：

| values 字段 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| name | string | 必填，非空 | 计划名称 |
| prompt | string | 必填，非空 | 完整未来执行指令，包含任务和输出要求 |
| schedule | 时间规则对象 | 必填 | 第 1 章的五种互斥规则之一 |
| timezone | string | 可选，默认 Asia/Shanghai | IANA 时区，用于当地时间解释和日历计算 |

不接受 null 或额外配置字段；执行权限由 runtime 提供，不新增字符、字节或 token 长度 cap。实际存储、日期及资源边界沿对应 owner 处理，不因原生页预算提前缩短配置。

```typescript
interface ScheduleValuesInput {
  name: string;
  prompt: string;
  schedule: ScheduleRuleInput;
  timezone?: string;
}
```

### 3.3 create 参数与行为

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| action | create | 必填 | 创建一个新计划 |
| values | ScheduleValuesInput | 必填 | 完整新配置 |
| status | ACTIVE／PAUSED | 可选，默认 PAUSED | 创建后是否启用调度；不接受 ALL 或 null |

create 不填 target，不接受 session、expected_revision 或权限参数。创建 PAUSED 计划不替换现有 ACTIVE 计划；当前会话已有 ACTIVE 计划时，再创建 ACTIVE 计划返回 ERROR，不自动暂停或删除旧计划。没有未来可用时点、配置无效或实际创建失败时如实报告，不伪造计划 Target。

### 3.4 switch 参数与行为

| 参数 | 类型 | 必填 | 明确含义 |
| --- | --- | --- | --- |
| target | SCHEDULE Target | 是，放首位 | 要暂停的当前 ACTIVE 计划 A |
| action | switch | 是 | 原子暂停 A，并启用替换计划 B |
| replacement | existing／create 分支对象 | 是 | 启用已有暂停计划，或创建并启用新计划 |

replacement 的两个分支：

| kind | 其余字段 | 含义 |
| --- | --- | --- |
| existing | target | 已有 PAUSED 计划 B 的 SCHEDULE Target |
| create | values | 按第 3.2 节创建新计划 B，并直接启用 |

replacement 不接受 status；切换目标必然为 ACTIVE。A 与已有 B 都必须属于当前会话且不是同一计划；A 必须为 ACTIVE，已有 B 必须为 PAUSED。旧计划 A 暂停后仍保留，不把切换解释为删除。

runtime 按第 2.6 节从本 session 已保存的完整工具结果取得 A 和已有 B 各自最大的已保存 revision；新建 B 时只需 A 的旧依据。缺少依据时不切换，提示先用 query_schedules 查询；任一并发或状态核对失败，整个切换不执行，不自动查询重试。双方核对、暂停 A、启用或创建 B，以及取消 A 尚未消费的排队输入在同一既有事务内结算；已运行的回复继续。

```typescript
type ScheduleReplacementInput =
  | {kind: "existing"; target: ScheduleTarget; values?: never}
  | {kind: "create"; values: ScheduleValuesInput; target?: never};

type ManageScheduleCreateSwitchArgs =
  | {
      action: "create";
      values: ScheduleValuesInput;
      status?: ScheduleStatus;
      target?: never;
      replacement?: never;
    }
  | {
      target: ScheduleTarget;
      action: "switch";
      replacement: ScheduleReplacementInput;
      values?: never;
      status?: never;
    };
```

以上类型只覆盖本章两个 action。各层只接受对应分支声明的字段，不保留 expected_revision、session 或权限的兼容入口。

### 3.5 精简返回字段与类型

create／switch 共用成功形状：

| 字段 | 内容 |
| --- | --- |
| target | 新创建或被启用的计划 B 的 SCHEDULE Target；create 时为新计划 |
| meta.status | SUCCESS |
| meta.schedule_status | 操作后的计划状态；create 为 ACTIVE／PAUSED，switch 固定 ACTIVE |
| meta.next_run_at | 本次操作确定的下次自动触发 UTC 时间；PAUSED 时为 null |
| content | 简短说明的 text 项数组 |

```typescript
interface ManageScheduleCreateSwitchSuccess {
  target: ScheduleTarget;
  meta: {
    status: "SUCCESS";
    schedule_status: ScheduleStatus;
    next_run_at: string | null;
  };
  content: ScheduleTextPart[];
}

interface ManageScheduleCreateSwitchFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: ScheduleTextPart[];
}

type ManageScheduleCreateSwitchResult =
  | ManageScheduleCreateSwitchSuccess
  | ManageScheduleCreateSwitchFailure;
```

create 启用时说明“已创建并启用定时任务。”；暂停保存时为“已创建定时任务，当前为暂停状态。”；switch 为“已暂停原计划并启用替换计划。”。不回传配置全文、action、session、公开 revision、inspect、deleted 标记或 paused／active 两份摘要，不重复同一业务 Target。

成功结果内部保留对应新 revision，供后续管理接续；switch 同时保留双方更新后的内部依据，不因公开返回只给 B 的 Target 而丢失 A 的内部结算事实。页面上的 schedule_status、next_run_at 是冻结业务快照，后续补页或导出不刷新。

### 3.6 原生示例

每日计划使用默认时区、默认开始日期及当前 turn 冻结权限，显式启用：

```text
工具：manage_schedule
参数：
  action: create
  status: ACTIVE
  values:
    name: "每日项目简报"
    prompt: "检查项目进展，整理完成事项、待办和阻塞。"
    schedule:
      kind: daily
      time: "09:00"
```

假设创建时冻结权限为 read-only，创建成功后的下一个当地 09:00 是 2026-11-14，完整结果可一页交付：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际创建耗时>
target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}

meta:
  status: SUCCESS
  schedule_status: ACTIVE
  next_run_at: "2026-11-14T01:00:00Z"

content:
  已创建并启用定时任务。

tool_result: <本次完整创建结果的导出 Target>
next: null
```

该计划之后的 query_schedules object 中，schedule 为 {"kind":"daily","start_date":"<创建时确定的当地日期>","time":"09:00"}，timezone 为 Asia/Shanghai，permission_mode 为 read-only；不因查询 turn 的权限不同而变化。

以下四种创建调用示意省略 status，均默认保存为 PAUSED，便于之后明确启用：

一次性，直接填写当地日期时间，示例假设保存时该时点仍在未来：

```text
工具：manage_schedule
参数：
  action: create
  values:
    name: "检查发布结果"
    prompt: "检查项目发布结果，报告失败或异常。"
    schedule:
      kind: once
      run_at: "2026-11-14T09:00:00"
```

固定间隔为 15 分钟，省略锚点，创建事务内由 runtime 补齐实际锚点；本例默认 PAUSED，不自动触发，恢复行为见第 5.2 节：

```text
工具：manage_schedule
参数：
  action: create
  values:
    name: "定期检查进展"
    prompt: "检查项目进展并汇报新增阻塞。"
    schedule:
      kind: interval
      seconds: 900
```

每周一、三、五，当地 09:00：

```text
工具：manage_schedule
参数：
  action: create
  values:
    name: "工作日项目简报"
    prompt: "汇总项目进展和当前待办。"
    schedule:
      kind: weekly
      time: "09:00"
      weekdays: [1, 3, 5]
```

每月 31 日，当地 09:00；没有 31 日的月份直接跳过：

```text
工具：manage_schedule
参数：
  action: create
  values:
    name: "月度项目回顾"
    prompt: "总结项目本月变化及后续重点。"
    schedule:
      kind: monthly
      time: "09:00"
      day: 31
```

上述暂停创建的成功结果使用同一形状：

```text
target: {"kind":"SCHEDULE","task_id":"schedule:<新计划ID>"}

meta:
  status: SUCCESS
  schedule_status: PAUSED
  next_run_at: null

content:
  已创建定时任务，当前为暂停状态。

tool_result: <本次完整创建结果的导出 Target>
next: null
```

切换到已有暂停计划 B：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: switch
  replacement:
    kind: existing
    target: {"kind":"SCHEDULE","task_id":"schedule:<B>"}
```

或者暂停 A，创建并启用一个每天 10:00 的新计划 B：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: switch
  replacement:
    kind: create
    values:
      name: "每日项目简报"
      prompt: "检查项目进展，整理完成事项、待办和阻塞。"
      schedule:
        kind: daily
        time: "10:00"
```

假设启用 B 后下一次运行是上海时间 2026-11-14 10:00，两种切换成功均可投影为：

```text
target: {"kind":"SCHEDULE","task_id":"schedule:<B>"}

meta:
  status: SUCCESS
  schedule_status: ACTIVE
  next_run_at: "2026-11-14T02:00:00Z"

content:
  已暂停原计划并启用替换计划。

tool_result: <本次完整切换结果的导出 Target>
next: null
```

身份与时间均为说明示例。实际观察时间和微秒耗时沿共用规则在消息顶层，未测得耗时不伪造为 0。

### 3.7 失败、取消与结果结算

| 情况 | 状态与说明 |
| --- | --- |
| interval.seconds 小于 900、非法日期／时区、字段无效等 | ERROR，content 简短说明实际原因 |
| 已有 ACTIVE 计划时又要求 create ACTIVE | ERROR，说明当前已有启用计划；不自动切换 |
| 缺少 A 或已有 B 的旧并发依据 | ERROR，提示先用 query_schedules 查询；切换不执行 |
| 并发冲突、计划归属或状态不符合 switch 要求 | ERROR，说明实际原因；切换不执行 |
| 实际创建／切换失败或调用取消 | ERROR／CANCELLED，保留实际已知效果或不确定性 |

例如间隔小于 15 分钟时，确实产生并保存的失败业务值可投影为：

```text
meta:
  status: ERROR

content:
  固定间隔至少为 15 分钟（900 秒），本次未创建计划。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

失败和取消不伪造成功 Target、schedule_status 或 next_run_at，不增加 meta.error、_hint 或其他分类码。只有确认操作没有发生时才说明“未创建／未切换”；提交结果未确认时如实说明，不自动重做创建或切换。进入不可安全中断的提交后沿既有 owner 等待实际结算，不把取消到达解释为事务未执行。更早的失败没有业务值时，沿既有运行时错误通道报告。

创建、切换和完整结果保存沿既有 owner 结算，删除因成功说明放不进展示预算而提前拒绝操作的检查。原生页在完整保存后按统一 12k 交付；NEXT 只补读固定结果，不创建新计划、不再次切换、不重算默认时间或推进调度。完整保存失败或未确认时不提供假结果引用；保存成功但当前页交付失败时，保留已知业务效果及实际可读的完整结果 Target，NEXT 指向本页未交付起点，不重执行业务。

## 4. manage_schedule：局部修改配置

### 4.1 update 参数

修改当前会话的现有计划，保留同一个 SCHEDULE Target。values 中未填写的字段保持原值；提供的字段替换对应值，不要求模型重填整份配置。

| 参数 | 类型 | 必填 | 明确含义 |
| --- | --- | --- | --- |
| target | SCHEDULE Target | 是，放首位 | 当前会话中要修改的计划 |
| action | update | 是 | 局部修改配置 |
| values | 配置修改对象 | 是，至少填写一项 | 仅包含下面需要修改的字段 |

| values 字段 | 类型 | 未填时 | 填写时 |
| --- | --- | --- | --- |
| name | string，非空 | 保持名称 | 替换名称 |
| prompt | string，非空 | 保持执行指令 | 替换完整执行指令 |
| schedule | 完整 ScheduleRule | 保持整份时间规则 | 整体替换时间规则，可更换 kind |
| timezone | IANA 时区字符串 | 保持原时区 | 使用指定时区，不套用创建默认值 |

只接受上述字段，不接受 null、空 values 或额外字段。参数与类型不提供 permission_mode，也不提供 session 或 expected_revision。模型工具的 schema 和用法不增加权限修改教程；已有计划的执行权限保持原保存值，不从当前 turn 重新取得。用户页面继续承担权限修改，query_schedules 仍返回实际保存的权限。

```typescript
interface ScheduleUpdateValues {
  name?: string;
  prompt?: string;
  schedule?: ScheduleRule;
  timezone?: string;
}

interface ManageScheduleUpdateArgs {
  target: ScheduleTarget;
  action: "update";
  values: ScheduleUpdateValues;
}
```

values 至少一项及各字段有效性由实际校验执行。整次修改先校验并通过并发核对，再在既有事务内提交，不部分应用。

### 4.2 schedule 整体替换与时间行为

update.schedule 使用第 1.2 节完整的 ScheduleRule，不使用创建时允许省略默认字段的 ScheduleRuleInput。不提供 schedule 时原规则完全不动；提供时必须填写所选 kind 的完整对象，不做内部字段合并，也不重新套用创建默认值。

各 kind 的完整字段统一见第 1.2 节 ScheduleRule，不在 update 下另列一套规则定义。

例如修改 interval.seconds 时，显式保留 query 返回的 anchor_at_utc；不因省略锚点而按本次修改时间重新起算。修改日历时间时，提供明确 start_date，不把开始日期自动重置为今天。各规则沿第 1.1 节的格式和有效性要求，包括间隔至少 900 秒、monthly 缺日跳过；实际改变一次性执行时刻时，新时刻必须在未来，不因仅修改名称或指令而把原时间当成新提交时刻。

ACTIVE 计划的时间安排实际改变后，按新规则确定下一次符合要求的未来触发时点；PAUSED 仍保持暂停，next_run_at 为 null。不因修改名称、指令或省略 schedule 而重置时间规则。已经排队的旧输入保持原样，不改写其指令，也不因 update 取消；已运行回复继续。

仅修改 timezone 时，daily／weekly／monthly 保留当地钟表规则，按新时区计算；once 保留已经确定的实际时刻，查询的当地 run_at 相应换算；interval 保留 UTC 锚点。once 的 schedule 与 timezone 都与当前公开值相同时，保留原 UTC 时刻并按无变化处理，不因重新解析同一个当地时间而改变实际时刻；其他字段的修改仍正常处理。显式提供变化的 once.schedule，或同时提供 once.schedule 和变化的 timezone 时，按有效时区解析新 run_at。内部解析与调度事实沿同一业务 owner 处理，不另存一份展示正文。

### 4.3 内部并发与返回类型

runtime 按第 2.6 节从本 session 已保存的完整工具结果自动取得目标计划最大的已保存 revision，与当前业务行在事务内核对，不要求模型填写。不以分页进度授予操作资格。没有旧快照先提示用 query_schedules 查询；冲突时不更新，不自动查询后重做，也不把数据库当前 revision 临时当作旧快照。

成功实际修改后内部 revision 按现有配置变更规则推进，完整成功结果保留该内部依据以便接续后续操作。提交值与当前值相同也必须先通过相同并发核对；无实际变化时 SUCCESS，保留原 revision，不因重复调用推进配置版本。

| 情况 | 返回字段 |
| --- | --- |
| 修改成功 | 同一顶层 target、meta.status:SUCCESS、content：“已更新定时任务。” |
| 无变化 | 同一顶层 target、meta.status:SUCCESS、content：“定时任务未发生变化。” |
| 失败或取消 | meta.status:ERROR／CANCELLED，以及 content 中的实际原因和必要效果说明 |

```typescript
interface ManageScheduleUpdateSuccess {
  target: ScheduleTarget;
  meta: {status: "SUCCESS"};
  content: ScheduleTextPart[];
}

interface ManageScheduleUpdateFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: ScheduleTextPart[];
}

type ManageScheduleUpdateResult =
  | ManageScheduleUpdateSuccess
  | ManageScheduleUpdateFailure;
```

成功不回传 schedule_status、next_run_at、配置全文或改动字段清单，不增加 changed 布尔字段；需要当前完整配置时由 query_schedules 查询。内部 revision 不进入公开返回值、投影或导出。

### 4.4 原生修改示例

仅修改执行指令，其他字段保持原值：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: update
  values:
    prompt: "只汇报新增阻塞，正常进展无需逐项复述。"
```

每天 09:00 改为 10:00，使用完整 schedule，明确保留开始日期：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: update
  values:
    schedule:
      kind: daily
      start_date: "2026-11-14"
      time: "10:00"
```

将固定间隔从 15 分钟改成 30 分钟，保留原锚点：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: update
  values:
    schedule:
      kind: interval
      anchor_at_utc: "2026-11-13T01:00:00Z"
      seconds: 1800
```

改为每周一、三、五，直接整体替换为 weekly 规则：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: update
  values:
    schedule:
      kind: weekly
      start_date: "2026-11-14"
      time: "09:00"
      weekdays: [1, 3, 5]
```

修改成功的原生结果：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际修改耗时>
target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}

meta:
  status: SUCCESS

content:
  已更新定时任务。

tool_result: <本次完整修改结果的导出 Target>
next: null
```

无实际变化时，target 保持相同，结果为：

```text
target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}

meta:
  status: SUCCESS

content:
  定时任务未发生变化。

tool_result: <本次完整结果的导出 Target>
next: null
```

身份、锚点和日期均为示例；保留原字段时使用实际查询值。观察信息仍顶层并列，未测得耗时不伪造为 0。

### 4.5 失败、取消与交付

| 情况 | 状态与 content |
| --- | --- |
| values 为空、字段无效或 schedule 不完整 | ERROR，简短说明参数问题，确认未更新时说明未更新 |
| 目标不存在或不属于当前会话 | ERROR，说明该计划不存在或不属于当前会话 |
| 没有旧快照 | ERROR，说明本次未更新，提示先用 query_schedules 查询 |
| 并发冲突 | ERROR，说明计划已变化、本次未更新，提示重新查询后决定 |
| 实际执行失败或调用取消 | ERROR／CANCELLED，说明实际原因及已知效果或不确定性 |

例如只提交 schedule.time，缺少完整规则字段时，确实产生并保存的失败结果可以是：

```text
meta:
  status: ERROR

content:
  schedule 需要提供完整时间规则，本次未更新。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

并发冲突可说明“该定时任务在上次查询或操作后已发生变化，本次未更新。请先用 query_schedules 查询，再决定是否修改。”；没有快照时可说明“尚无该定时任务的可用旧快照，本次未更新。请先用 query_schedules 查询。”。其他未声明字段统一按参数规则处理，不为特定字段另设模型教程或错误枚举。

失败或取消不伪造业务 Target、状态、时间或完整计划，不增加 meta.error、_hint 或第二组状态。只有确认未执行时才说未更新；进入不可安全中断的提交后沿既有 owner 等待实际结算，实际效果或提交状态不确定时如实说明，不自动重做。更早的失败没有业务值时沿既有运行时错误通道报告。

完整结果保存和原生交付沿共用规则；正常页返回实际可读的 tool_result，next 只读本次固定结果，不重新修改计划或刷新并发依据。保存失败不返回假结果引用，保存成功但当前页交付失败时保留已知业务效果及指向未交付位置的 NEXT。交付完一个错误结果的 next:null 不表示修改成功。成功返回不以展示预算提前限制实际修改范围。

## 5. manage_schedule：暂停、恢复与删除

### 5.1 最简参数与内部并发

三个 action 只接收 target、action，固定操作当前会话的计划，不接受 values、session、expected_revision 或其他字段。

| 参数 | 类型 | 必填 | 明确含义 |
| --- | --- | --- | --- |
| target | SCHEDULE Target | 是，放首位 | 当前会话的目标计划 |
| action | pause／resume／delete | 是 | 暂停、恢复调度或删除计划 |

```typescript
interface ManageScheduleControlArgs {
  target: ScheduleTarget;
  action: "pause" | "resume" | "delete";
}
```

runtime 按第 2.6 节从本 session 已保存的完整工具结果取得目标计划最大的已保存 revision，在既有事务内核对，不由模型填写，不要求分页读完。没有依据先提示用 query_schedules 查询，发生冲突不执行、不自动查询重试。配置和保存权限不因控制操作被重新填写或替换。

### 5.2 队列与运行行为：沿用当前语义

本章不改变现有暂停、恢复、删除对调度、队列和运行中回复的语义，继续复用现有业务事务及队列 owner。

| action | 后续自动触发 | 该计划已经排队、尚未消费的输入 | 已经开始的回复 |
| --- | --- | --- | --- |
| pause | 暂停 | 取消 | 继续 |
| resume | 从下一个符合规则的未来时点恢复 | 不恢复此前取消的输入；其他现存输入不动 | 继续 |
| delete | 删除计划，停止后续自动触发 | 取消 | 继续，聊天记录保留 |

暂停或删除仅取消该计划产生且仍为待消费状态的输入，不取消普通用户消息或其他计划的输入。计划变更与对应待执行输入的取消在同一事务内结算。与输入消费竞争时沿既有事务顺序处理：输入先被消费并开始执行后，pause／delete 不再把它当作待执行输入，也不停止已开始的回复。执行中回复继续的规则写在本节，不作为每次工具结果的固定后缀。

重复 pause 正常成功，仍取消该计划实际存在的待执行输入；不能仅因状态已是 PAUSED 而跳过队列处理。重复 resume 正常成功，不重新安排已有 ACTIVE 计划的下次触发时间。无实际配置或状态变化时不无故推进内部 revision，队列实际发生的取消仍按原 owner 记录。

resume 保持原时间规则和保存权限：周期计划从下一个未来时点开始，不补跑暂停期间遗漏的时点；interval 保留原锚点，不从恢复时刻重新起算；此前取消的输入不复活。当前会话已有另一个 ACTIVE 计划时返回 ERROR，不隐式替换。不存在未来可用时点时沿实际错误处理，不返回恢复成功。

例如锚点为 09:00、每 15 分钟触发的计划：09:00 的回复仍在运行，09:15、09:30 的输入已排队；09:35 暂停时取消后两条输入，已开始的回复继续。09:40 恢复后，下次仍按原锚点为 09:45，不补回 09:15、09:30。若 09:35 删除，则取消待执行输入并移除计划，后续不再自动触发。

保留当前一次性计划生命周期：once 输入成功入队后，计划自动移除，已接纳的输入及其冻结来源继续存在。此后计划 Target 已不存在，pause／delete 不能通过不存在的计划取消该条输入；取消待执行输入沿现有队列操作。已过期的 PAUSED once 也沿现有生命周期清理，不通过 resume 补执行，不另建计划墓碑、恢复或特殊队列入口。

### 5.3 精简返回值与类型

| 情况 | 顶层业务 target | meta | content |
| --- | --- | --- | --- |
| pause 成功 | 同一计划 Target | status:SUCCESS | “已暂停定时任务。”；实际取消了待执行输入时，使用下述完整说明 |
| resume 成功 | 同一计划 Target | status:SUCCESS | “已恢复定时任务。”；重复恢复可说明“定时任务已处于启用状态。” |
| delete 成功 | 不返回 | status:SUCCESS | “已删除定时任务。”；实际取消了待执行输入时，使用下述完整说明 |
| 失败或取消 | 不返回 | status:ERROR／CANCELLED | 简短实际原因和必要的已知效果说明 |

实际取消了待执行输入时，pause 的说明为“已暂停定时任务，并取消其待执行输入。”，delete 为“已删除定时任务，并取消其待执行输入。”；不追加“已开始的回复继续”这类固定后缀。没有待执行输入时直接使用短句，不声称取消了不存在的输入。是否取消仅据既有事务实际结果表达，不增加公开计数。

```typescript
interface ManageSchedulePauseResumeSuccess {
  target: ScheduleTarget;
  meta: {status: "SUCCESS"};
  content: ScheduleTextPart[];
}

interface ManageScheduleDeleteSuccess {
  meta: {status: "SUCCESS"};
  content: ScheduleTextPart[];
}

interface ManageScheduleControlFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: ScheduleTextPart[];
}

type ManageScheduleControlResult =
  | ManageSchedulePauseResumeSuccess
  | ManageScheduleDeleteSuccess
  | ManageScheduleControlFailure;
```

不回传配置全文、公开 revision、schedule_status、next_run_at、取消数量、action 回显或 deleted 布尔字段。pause／resume 的实际内部 revision 继续随完整成功结果保存，供后续操作接续。模型需要当前完整配置和下次运行时间时可使用 query_schedules。

### 5.4 原生示例

暂停：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: pause
```

假设本次确实取消了该计划的待执行输入：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际操作耗时>
target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}

meta:
  status: SUCCESS

content:
  已暂停定时任务，并取消其待执行输入。

tool_result: <本次完整暂停结果的导出 Target>
next: null
```

没有待执行输入时，content 为“已暂停定时任务。”。

恢复：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: resume
```

```text
target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}

meta:
  status: SUCCESS

content:
  已恢复定时任务。

tool_result: <本次完整恢复结果的导出 Target>
next: null
```

删除：

```text
工具：manage_schedule
参数：
  target: {"kind":"SCHEDULE","task_id":"schedule:<A>"}
  action: delete
```

假设本次确实取消了该计划的待执行输入：

```text
meta:
  status: SUCCESS

content:
  已删除定时任务，并取消其待执行输入。

tool_result: <本次完整删除结果的导出 Target>
next: null
```

没有待执行输入时，content 为“已删除定时任务。”。示例身份与观察值均为占位，实际观察时间和已测得微秒耗时在消息顶层，未测得耗时不伪造为 0。

### 5.5 失败、取消与实施边界

目标不存在或不属于当前会话、没有旧快照、并发冲突、恢复时已有另一 ACTIVE 计划或实际操作失败，使用 ERROR 和简短实际原因；取消使用 CANCELLED。重复 pause／resume 可以成功，重复 delete 时目标已经不存在则返回 ERROR，不伪造再次删除。已自动移除或清理的 once 返回实际不可用事实，不能宣称已取消其仍存在的排队输入。

失败不伪造业务 Target、计划状态或运行时间，不增加 meta.error、_hint、错误枚举或队列控制参数。确认未执行时才说明未暂停／未恢复／未删除；进入不可安全中断的提交后等待实际结算，效果不确定时如实说明，不自动重做。结果保存与交付沿共用规则，NEXT 只补读冻结结果，不再次执行控制动作；错误结果交付完的 next:null 不表示业务成功。

hardcut 保持本节既有调度、取消待执行输入、保留运行中回复及 once 清理的产品语义，不重写为另一套调度或队列机制。仍需按新合同修改工具参数、当前会话归属接入、自动取得旧 revision 的调用路径和结果投影；内部函数签名或必要返回事实可随接入调整，不能把“沿用语义”理解为所有底层代码都无需修改。

## 6. 当前生产实现参考

- [工具目录与 ROOT 原生边界](../src/pulsara_agent/capability/builtin_catalog.py)
- [参数 schema 与校验](../src/pulsara_agent/scheduling/requests.py)
- [查询、模型结果投影与管理服务](../src/pulsara_agent/scheduling/service.py)
- [时间规则与触发语义](../src/pulsara_agent/scheduling/contracts.py)
- [定时任务业务行与事务](../src/pulsara_agent/conversation_kernel/_repository/scheduling.py)
- [原生调用结算](../src/pulsara_agent/conversation_kernel/tool_runtime_management.py)
- [用户级定时任务页面接口](../src/pulsara_agent/desktop_app/scheduled_controller.py)

这些链接用于核对现状与实施接点，不表示当前代码已经符合本草案。
