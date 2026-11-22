# Pulsara 能力查询与刷新工具修订与使用教程草案

MCP 在后台连接和发现，首条模型请求不等待其完成。关键词搜索仅在当前 scope／parent 内判断目录覆盖；已启用服务尚无当前配置对应的已接纳目录时，返回 ERROR 并说明未完成范围，即使已有命中也保留已知记录和可用调用引用。ERROR 不代表这些部分无效。已禁用及作用域外服务不影响覆盖；同语义重连保留已接纳目录的可搜索性，物理不可用只影响调用准入。查询不会自动等待或重试。

共用规则见[原生工具修订草案索引](../archived_docs/PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整保存、原生 12k／NEXT、JS 完整返回及 Cell 输出沿[统一交付草案](../archived_docs/PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)，唯一正文及业务字段归属沿[数据库 hardcut 草案](../archived_docs/PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。

状态：当前生效合同。本文件确定 query_capabilities 的 SKILL、HOOK、MCP、PLUGIN 四类必填 kind、最小 Target、参数、返回字段、原生／JS 用法和失败语义、MCP 关键词搜索，以及 reload_capabilities 的无参数原生调用和精简结果。manage_capability 单列在[能力变更工具草案](manage_capability.md)，已确定的管理 action 及参数、结果以该草案为准，其他 action 不由本文件推定。SKILL、HOOK、PLUGIN 沿 ROOT 原生及 Code Mode JS 的来源查询边界，不因分类调整开放子代理的本地来源查询；MCP 保留原调用者的来源及运行目录范围。reload_capabilities 保持仅 ROOT 原生、bypass 权限调用，不提供 JS API。call_mcp_tool 及 read_mcp_resource 的参数、原生／JS 返回和用法见[MCP 接入草案](mcp.md)。

## 1. query_capabilities：SKILL 分支

### 1.1 四类必填 kind

所有 query_capabilities 调用都必须填写顶层 kind，列表和精确查询均不例外。不提供 ALL，不接受空对象作为跨种类目录查询。

| kind | 查询类别 |
| --- | --- |
| SKILL | Skill |
| HOOK | Hook |
| MCP | MCP |
| PLUGIN | 提供 Skill、Hook、MCP 组件的 Plugin 包 |

顶层 kind 选择查询类别，target.kind 标识该类别内的具体对象。PLUGIN 可以组合前三类组件，组件本身仍可在对应类别下查询。四个分支的具体合同见第 1–4 章。

Target 只携带定位所必需的组合，不要求统一成固定两个字段，也不为统一外形新增对象 ID：

| target.kind | 除 kind 外的定位字段 | 明确含义 |
| --- | --- | --- |
| SKILL | skill_path | SKILL.md 的绝对路径 |
| PLUGIN | plugin_path | Plugin 稳定实例目录的绝对路径，即现有存储的 plugin_package_parent；不包含某次 package_install_id |
| HOOK_SOURCE | config_path | 本地或 Plugin Hook 配置声明文件的绝对路径 |
| MCP_CONNECTION | config_path、server_id | 配置文件绝对路径及其中声明该服务的原始键；Plugin 来源使用 local_server_id |
| MCP_TOOL | server_id、remote_tool_name | 调用者当前运行目录中的实际服务标识及服务声明的原始工具名 |

这些字段由 runtime 取得并返回，模型原样复制。作用域、来源类别、Plugin 归属、安装身份及连接到运行实例的关联由原 owner 内部解析和核对，不作为 Target 的额外字段，也不另建已见 Target 注册表。列表的 scope 仍是可选业务筛选，不因 Target 减字段而删除。显式 scope 与 parent 的实际归属是否冲突，由 runtime 解析 plugin_path 后判断，不要求模型填写归属。

路径型 Target 使用非空规范绝对路径。它们只是能力对象的定位，不自动授予任意文件读取或管理能力；查询继续交给原 owner。Plugin 的稳定实例目录在包替换时不变；Hook 及 MCP 配置路径指向具体安装文件，安装替换后旧路径不自动跳转至新包，失效时说明原因，显式重新查询 Plugin 取得当前组件 Target。保留原有内部身份、信任和管理核对，不用路径或短 ID 代替它们。

```typescript
type CapabilityQueryKind = "SKILL" | "HOOK" | "MCP" | "PLUGIN";
```

### 1.2 SKILL 查询范围

列表返回本次调用方在有效 Pulsara home、当前 GUI 项目及 bundled 来源下实际可观察的 Skill，包含独立安装和 Plugin 提供的 Skill。已禁用、被其他来源覆盖或定义存在问题的副本不因未生效而从观察结果中删除。同名副本通过具体 SKILL Target 区分，不合并成一个名称条目。

USER／WORKSPACE 沿原有来源归属，WORKSPACE 指 GUI 会话绑定的项目，不随 terminal 的 cd 或 workdir 改变。不支持项目来源的会话显式查询 WORKSPACE 时按原范围报告错误，不借 cwd 扩大来源范围。bundled Skill 没有 USER／WORKSPACE 安装归属；指定 scope 时不包含 bundled。

精确查询按具体 skill_path 交给原有来源 owner 观察，不扫描一份新目录或建立已见身份白名单。查询不安装、启停、删除或自动读取 Skill 全文，也不改变当前模型前缀。正文继续通过 read_file 的 PATH Target 读取，路径为返回的 skill_path；支持文件和脚本相对其所在目录解析。

### 1.3 参数与分支互斥

列表只接受以下参数：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| kind | SKILL | 必填 | 只查询 Skill |
| scope | USER／WORKSPACE | 可选，默认不过滤 | 按来源安装作用域筛选 |
| parent | PLUGIN Target | 可选，默认不限定 Plugin | 只列指定 Plugin 提供的 Skill |

精确查询只接受以下参数，target 放首位：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | SKILL Target | 必填 | 具体副本的 SKILL.md 绝对路径 |
| kind | SKILL | 必填 | 必须与该具体对象的查询类别一致 |

只要填写 target 就是精确查询，不得同时填写 scope 或 parent。列表的 parent 使用第 1.1 节的最小 PLUGIN Target；与显式 scope 冲突时失败，不暗中改换范围。未填筛选参数表示列出该类当前范围内的全部可观察副本，不表示跨四类查询。

删除 source_kind 输入参数，保留结果中的 source 来源信息。删除 offset、limit 及旧重新查询式 next；不增加关键词、名称搜索、游标或页长参数。不接受 null、额外字段或不完整 Target。

```typescript
type CapabilityScope = "USER" | "WORKSPACE";
type SkillTarget = {kind: "SKILL"; skill_path: string};
type PluginTarget = {
  kind: "PLUGIN";
  plugin_path: string;
};

type QuerySkillArgs =
  | {
      kind: "SKILL";
      scope?: CapabilityScope;
      parent?: PluginTarget;
      target?: never;
    }
  | {
      target: SkillTarget;
      kind: "SKILL";
      scope?: never;
      parent?: never;
    };
```

Skill 的 skill_path 必须是非空绝对路径；该 Target 不是 PATH Target，不将其作为任意文件读取入口。Plugin Target 的 plugin_path 由现有存储布局取得，runtime 解析其实际实例归属，不让模型另传 scope 或 plugin_id。

### 1.4 精简返回值与类型

列表和精确查询均只使用 meta、content，不在顶层重复返回 target。二者使用同一个 SkillRecord，每条记录为 type:object/value，target 放在该记录内；有匹配记录时，列表先说明查询范围，再返回各条记录。精确查询返回目标的一条记录。

共同记录保留：

| 字段 | 明确含义 |
| --- | --- |
| target | 具体 SKILL.md 的 SKILL Target |
| name | 来源实际提供的名称；定义无效时可沿既有观察采用目录名帮助辨认，并明确诊断 |
| description | 完整用途说明；未取得时省略，不伪造 |
| source | LOCAL、PLUGIN 或 BUNDLED，以及实际来源作用域或 Plugin Target |
| status | Skill 的简短业务状态说明，例如当前已选择、已禁用、被覆盖、定义无效或状态未知 |

对象内 status 是 Skill 状态文字，不是另一组工具调用状态。meta.status 才表示本次查询的 SUCCESS／ERROR／CANCELLED；查询到已禁用副本不等于查询失败。

列表和精确查询均不公开 enabled、selected、enable_eligible、remove_eligible 四个布尔字段，也不以其他字段替代管理资格预告。实际 Skill 状态用共同 status 文字说明；“当前已选择”不表示模型已读正文或已将其安装进当前 epoch 前缀。能否启停或删除仍由管理 owner 在实际操作时核对；无法操作时，通过 manage 工具的 meta.status 和 content 告知模型，不因查询字段删除而放宽管理规则。

```typescript
type SkillSource =
  | {kind: "LOCAL"; scope: CapabilityScope}
  | {kind: "PLUGIN"; target: PluginTarget}
  | {kind: "BUNDLED"};

interface SkillTextPart {
  type: "text";
  value: string;
}

interface SkillRecord {
  target: SkillTarget;
  name?: string;
  description?: string;
  source: SkillSource;
  status: string;
}

interface SkillObjectPart {
  type: "object";
  value: SkillRecord;
}

interface QuerySkillSuccess {
  meta: {status: "SUCCESS"};
  content: (SkillTextPart | SkillObjectPart)[];
}

interface QuerySkillFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (SkillTextPart | SkillObjectPart)[];
}

type QuerySkillResult = QuerySkillSuccess | QuerySkillFailure;
```

健康副本返回实际名称和描述；定义损坏或缺失时，无法取得的作者字段不填假值，诊断写 text，保留已核实的目标、来源及状态。无法完成对应观察时按第 1.7 节返回失败及实际已取得记录。

删除每条记录中与 target.kind 重复的 kind、独立 skill_path 和可从目标路径得到的 skill_root。license、compatibility 不作为默认字段，完整作者信息仍在 SKILL.md。不返回 manage_eligible 或另一组可操作性字段。

删除 items、returned_count、total_count、completeness、description_omitted 及旧业务 next 包装，完整描述不因页长被省略。必要诊断进入 text 内容项，不公开第二套错误码、诊断数组或固定 inspect 教学。

### 1.5 原生列表与精确查询示例

列出当前范围内全部 Skill：

```text
工具：query_capabilities
参数：
{
  "kind": "SKILL"
}
```

只列当前工作区的 Skill：

```text
工具：query_capabilities
参数：
{
  "kind": "SKILL",
  "scope": "WORKSPACE"
}
```

只列某个 Plugin 的 Skill：

```text
工具：query_capabilities
参数：
{
  "kind": "SKILL",
  "parent": {"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example-plugin"}
}
```

例如完整目录取得以下两个同名副本且本页可完整交付，原生投影为：

```text
meta:
  status: SUCCESS

content:
  技能列表按来源列出定义，范围包括已禁用和当前未选择的副本。
  {
    "target": {
      "kind": "SKILL",
      "skill_path": "/project/.pulsara/skills/review/SKILL.md"
    },
    "name": "review",
    "description": "检查代码改动并给出审查意见。",
    "source": {
      "kind": "LOCAL",
      "scope": "WORKSPACE"
    },
    "status": "当前已选择。"
  }
  {
    "target": {
      "kind": "SKILL",
      "skill_path": "/home/user/.pulsara/skills/review/SKILL.md"
    },
    "name": "review",
    "description": "检查代码改动。",
    "source": {
      "kind": "LOCAL",
      "scope": "USER"
    },
    "status": "被其他来源覆盖。"
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整查询结果的导出 Target>
next: null
```

精确查询其中一份副本：

```text
工具：query_capabilities
参数：
{
  "target": {"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"},
  "kind": "SKILL"
}
```

假设观察得到与列表中相同的目标事实，原生返回同一记录形状：

```text
meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "SKILL",
      "skill_path": "/project/.pulsara/skills/review/SKILL.md"
    },
    "name": "review",
    "description": "检查代码改动并给出审查意见。",
    "source": {
      "kind": "LOCAL",
      "scope": "WORKSPACE"
    },
    "status": "当前已选择。"
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整详情结果的导出 Target>
next: null
```

路径及状态均为示例，实际必须来自当前来源 owner。观察时间及微秒耗时沿共同原生外壳，未测得耗时不伪造为 0。

### 1.6 JS 完整返回与组合

JS 使用同一参数分支及业务生产者，返回同一次完整查询结果，不返回原生页或展示控制字段。

```javascript
const result = await tools.query_capabilities({kind: "SKILL"});
const skills = result.content
  .filter(part => part.type === "object")
  .map(part => part.value);
console.log({skills});
```

列表值使用 meta/content。下面示意范围说明和一条记录：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "技能列表按来源列出定义，范围包括已禁用和当前未选择的副本。\n"},
    {
      "type": "object",
      "value": {
        "target": {"kind": "SKILL", "skill_path": "/project/.pulsara/skills/review/SKILL.md"},
        "name": "review",
        "description": "检查代码改动并给出审查意见。",
        "source": {"kind": "LOCAL", "scope": "WORKSPACE"},
        "status": "当前已选择。"
      }
    }
  ]
}
```

该例假设完整结果确实只有一项；实际多项时全部返回，不以示例暗示只返回一个对象。精确查询同样返回完整详情对象：

```javascript
const result = await tools.query_capabilities({
  target: {
    kind: "SKILL",
    skill_path: "/project/.pulsara/skills/review/SKILL.md"
  },
  kind: "SKILL"
});
console.log(result);
```

此时 content 的 object.value 与列表使用同一个 SkillRecord，没有精确查询专属的状态或资格字段；不带独立 skill_path／skill_root、原生 tool_result、next 或时间外壳。范围筛选可直接写为 tools.query_capabilities({kind:"SKILL",scope:"WORKSPACE"}) 或填写 parent，精确查询不混填它们。

console.log／显式 return 按脚本选定的完整值沿 Cell steer 整条交付，首版不额外加展示预算或自动分页。查询本身不自动将完整值披露 ROOT，也不自动读取 Skill 正文或触发管理。

### 1.7 空结果与失败

| 情况 | 返回规则 |
| --- | --- |
| 正常列表完成但无匹配 | SUCCESS，content 一条 text：“没有找到符合条件的 Skill。” |
| 精确目标不存在或不在原观察范围 | ERROR，text 说明原因及具体 skill_path，不伪造详情 |
| 来源观察、扫描或选择事实未能完整取得 | ERROR，text 说明实际缺口及必要路径，保留已取得的记录及可明确表达的未知事实 |
| 参数无效、kind 缺失、分支混填或 scope 与 parent 冲突 | ERROR，简短说明，不暗中改变查询范围 |
| 查询取消 | CANCELLED，保留实际已取得的记录及说明 |

Skill 本身已禁用、被覆盖或定义有问题，不直接等于查询失败；能够完整核实的事实仍正常返回，并说明该副本的实际状态。未取得的事实不能伪造，也不能把来源不可读当成“没有匹配”。

列表失败和取消的 content 先给简短原因，随后按实际取得顺序保留完整 object。确实存在的失败业务值完整保存，JS 沿既有异常通道通过 err.result 取得它；没有业务值的早期参数或运行时失败不伪造 err.result。不增加 partial、meta.error、错误枚举或可恢复任务。

```javascript
try {
  const result = await tools.query_capabilities({kind: "SKILL"});
  console.log(result);
} catch (err) {
  console.log(err.result ?? String(err));
}
```

原生错误结果已正常保存并完整交付时仍可返回有效 tool_result 和 next:null；它只表示失败结果已交付完，不表示目录完整。保存及交付本身失败沿共同规则处理，不伪造可读 Target，不自动重做来源查询。

### 1.8 冻结 12k／NEXT

列表和详情均先完成本次业务查询并完整保存，原生再按共同 12k 预算顺序交付。object 的两空格缩进 JSON 文字可以跨页，不能要求每条记录整项装入一页，也不新增结构化分片或 partial 字段。长描述完整保存，不因展示预算变成省略描述的目录行。

首屏和正常续页的 tool_result 指向同一原完整结果；next 为完整 NEXT Target，模型只需原样传给 read_file。内部位置对模型透明。NEXT、导出及重现已接受观察不重新扫描 Skill、读取 Plugin 当前状态、刷新记录中的状态或执行管理；查询后来源变化不改写已保存记录，要取得新事实须显式再次查询。

JS 完整值不按 12k 分页，数组及每个对象字段均可直接访问，不携带 Pulsara 展示 next。业务字段与 content 使用同一保存结果，不另存整份 JSON、副本目录或原生页面。查询和分页都不重建已安装 provider 前缀，SYSTEM/tools 固定、messages 仅后缀追加。

## 2. query_capabilities：HOOK 分支

### 2.1 用途与查询范围

查询本地及 Plugin 提供的 Hook 配置来源、状态和规则。每条记录对应一份配置来源，其中 definitions 包含实际观察到的规则；保留现有来源级信任及管理粒度，不新增逐条 Hook 身份或管理动作。

列表和精确查询都只返回常规查看所需信息：配置位置、简短状态，以及每条规则的事件、匹配条件和执行命令。详细配置需要时由模型沿 config_path 使用 read_file 读取；原生 OpenAI Plugin 来源可能指向 plugin.json 或 .codex-plugin/plugin.json，读取声明后再沿 hooks 路径查看具体文件，不假定必为 hooks.json。不增加详细查询参数，不自动读取或另存整份配置正文。

范围继续覆盖调用者可观察的 USER 和当前 GUI 项目的 WORKSPACE 来源，包括已禁用 Plugin 中可观察的声明。不支持项目来源时显式查询 WORKSPACE 按既有规则失败，不借 terminal cwd 扩大范围。查询不启用、信任或执行任何 Hook。

复用现有 Hook 来源发现、Plugin 定义观察、信任状态及规范定义 owner；能力页是这些事实的另一个消费者。能力页的用户级 HTTP 接口及已启用 Plugin 组合视图不直接作为模型目录，以免漏掉项目来源或已禁用来源；不复制解析器，也不将页面审阅字段全部公开给模型。

### 2.2 参数与现有 Target

列表参数：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| kind | HOOK | 必填 | 只查询 Hook |
| scope | USER／WORKSPACE | 可选，默认不过滤 | 按配置来源的作用域筛选 |
| parent | PLUGIN Target | 可选，默认不限定 Plugin | 只列该 Plugin 提供的 Hook 来源 |

精确查询参数，target 放首位：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | HOOK_SOURCE Target | 必填 | 一份确切的本地或 Plugin Hook 配置来源 |
| kind | HOOK | 必填 | 顶层查询类别 |

精确查询只填 target、kind，不混填 scope 或 parent。列表的 scope 与 parent 的实际归属明确冲突时失败。省略筛选表示列出该类当前范围内的全部可观察来源，不支持全种类 list。

HOOK_SOURCE Target 统一只含 kind、config_path。本地和 Plugin 来源共用这一形状，Plugin Hook 不要求模型重复传 plugin_id；runtime 按配置路径解析原来的来源身份，继续执行内部信任及管理核对。顶层 kind:HOOK 与 target.kind:HOOK_SOURCE 职责不同。删除公开 Target 中的 scope、source_kind、plugin_id，也不保留顶层 source_kind 筛选。

```typescript
type HookSourceTarget = {kind: "HOOK_SOURCE"; config_path: string};

type QueryHookArgs =
  | {
      kind: "HOOK";
      scope?: CapabilityScope;
      parent?: PluginTarget;
      target?: never;
    }
  | {
      target: HookSourceTarget;
      kind: "HOOK";
      scope?: never;
      parent?: never;
    };
```

parent 复用第 1.3 节已定义的完整 PLUGIN Target。只接受上述参数及对应 Target 的实际身份字段，不接受 null、额外字段、offset、limit、游标、详细开关或旧重新查询式 next。

### 2.3 列表与精确查询共用的精简记录

列表和精确查询均使用 meta/content，不重复顶层 target。列表返回多条匹配来源记录，精确查询返回目标的一条记录，object.value 使用同一个 HookRecord。

| 字段 | 明确含义 |
| --- | --- |
| target | 一份确切来源的 HOOK_SOURCE Target，供后续查询及管理 |
| target.config_path | 原 owner 核实的配置文件绝对路径，供需要详细配置时读取；不在记录中重复保存 config_path |
| status | 简短说明开启、信任及异常情况，不要求模型理解内部状态组合 |
| definitions | 按实际定义顺序排列的规则，每条仅 event、matcher、command |

```typescript
interface HookDefinitionSummary {
  event: string;
  matcher: string;
  command: string;
}

interface HookRecord {
  target: HookSourceTarget;
  status: string;
  definitions: HookDefinitionSummary[];
}

interface HookTextPart {
  type: "text";
  value: string;
}

interface HookObjectPart {
  type: "object";
  value: HookRecord;
}

interface QueryHookSuccess {
  meta: {status: "SUCCESS"};
  content: (HookTextPart | HookObjectPart)[];
}

interface QueryHookFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (HookTextPart | HookObjectPart)[];
}

type QueryHookResult = QueryHookSuccess | QueryHookFailure;
```

event、matcher、command 来自实际定义，不自行概括命令或把匹配文字改成当前命中的工具名单。status 只描述本次已核实的状态，未知时明确说明；未信任或关闭本身不等于查询失败，也不保证当前 epoch 已采用某次磁盘变更。

不返回重复 source、独立 config_path 或公开来源归属字段；来源身份由内部 owner 按 target.config_path 解析。不公开 enabled、selected、管理资格布尔值、单独授权／来源状态、计数、定义摘要、已信任摘要或内部安装身份；有用诊断用 text 或记录中的 status 简短说明，不再套诊断数组及代码。

definitions 不携带 cwd、声明环境、Windows 命令、超时、异步、上下文限制、状态提示、匹配操作清单或别名映射。它们属于详细配置或审阅辅助信息，不进入这份精简业务值及 JS 返回值。实际解析、执行、信任校验和管理核对保留在原 owner；不因查询减字段而改变执行规则或删除内部必要事实。

### 2.4 原生列表与精确查询示例

列出当前可观察的 Hook 来源：

```text
工具：query_capabilities
参数：
{
  "kind": "HOOK"
}
```

只列当前项目，或指定 Plugin：

```text
工具：query_capabilities
参数：
{"kind":"HOOK","scope":"WORKSPACE"}

工具：query_capabilities
参数：
{
  "kind":"HOOK",
  "parent":{"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example-plugin"}
}
```

假设本次完整列表只有以下一份来源，规则和路径仅为示例，原生投影为：

```text
meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "HOOK_SOURCE",
      "config_path": "/Users/example/.pulsara/hooks.json"
    },
    "status": "已启用，当前定义已信任。",
    "definitions": [
      {
        "event": "PreToolUse",
        "matcher": "Read",
        "command": "python3 /Users/example/scripts/check_read.py"
      }
    ]
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整查询结果的导出 Target>
next: null
```

精确查询该来源：

```text
工具：query_capabilities
参数：
{
  "target":{"kind":"HOOK_SOURCE","config_path":"/Users/example/.pulsara/hooks.json"},
  "kind":"HOOK"
}
```

精确查询取得相同事实时，返回与上述相同的 HookRecord，不多出详情字段。多来源列表按顺序包含多个同形 object；原生 object 使用两空格缩进 JSON，时间外壳沿共同规则，未测得耗时不伪造为 0。

需要详细配置时，由模型显式调用：

```text
工具：read_file
参数：
{
  "target":{"kind":"PATH","path":"/Users/example/.pulsara/hooks.json"}
}
```

实际 path 复制返回记录的 target.config_path，沿文件工具的正常权限和读取规则取得当时文件内容；这是新的文件读取，不自动替换之前冻结的 Hook 查询记录。查询不自动调用它，也不提供 include_details 等额外入口。

### 2.5 JS 完整值与详细配置读取

```javascript
const result = await tools.query_capabilities({kind: "HOOK"});
console.log(result);
```

与上例相同的完整业务值为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {"kind": "HOOK_SOURCE", "config_path": "/Users/example/.pulsara/hooks.json"},
        "status": "已启用，当前定义已信任。",
        "definitions": [
          {
            "event": "PreToolUse",
            "matcher": "Read",
            "command": "python3 /Users/example/scripts/check_read.py"
          }
        ]
      }
    }
  ]
}
```

精确查询和按需读取配置可分别组合：

```javascript
const result = await tools.query_capabilities({
  target: {kind: "HOOK_SOURCE", config_path: "/Users/example/.pulsara/hooks.json"},
  kind: "HOOK"
});
console.log(result);
```

```javascript
const result = await tools.query_capabilities({
  target: {kind: "HOOK_SOURCE", config_path: "/Users/example/.pulsara/hooks.json"},
  kind: "HOOK"
});
const hook = result.content.find(part => part.type === "object").value;
const config = await tools.read_file({
  target: {kind: "PATH", path: hook.target.config_path}
});
console.log({content: config.content});
```

JS 取得的是全部精简 HookRecord，不能额外夹带被默认查询删除的详细执行字段。它不带原生时间外壳、tool_result 或 next；脚本按实际需要选取值，console.log／显式 return 沿 Cell steer 整条交付，不增加展示预算或自动分页。

### 2.6 空结果、失败与冻结分页

正常列表完成且没有匹配来源时为 SUCCESS，content 用一句 text：“没有找到符合条件的 Hook 来源。”已知来源可读但没有规则时可保留实际记录及 definitions:[]，status 说明没有规则，不制造命令。来源读取失败、查询不完整为 ERROR，取消为 CANCELLED；保留简短原因、必要路径及实际已取得的记录，不把不可读当成完整空目录。

精确目标不存在、超出原观察范围或参数混填时报告实际错误，不伪造路径或正常记录。已有完整失败业务值时，JS 沿异常通道通过 err.result 取得；早期失败没有业务值就不伪造。不公开额外错误码、partial 或管理资格字段；保存及交付本身失败沿共同规则，不自动重做来源查询。

列表和精确查询均先完整取得本次精简业务值并保存，原生再按共同 12k 预算顺序分页，object 的 JSON 文字可以跨页，不增加“每份定义必须同页”的限制。tool_result 指向整个保存结果，next 为完整 NEXT Target，原样交给 read_file 即可，内部位置对模型透明。

NEXT 和导出只读冻结记录，不重新发现 Hook、读取当前配置、刷新信任／启用状态或触发管理。查询后文件变化不改写结果；模型要新状态就重新查询，要详细配置就显式读取 config_path。JS 始终取得完整精简业务值，不分页；不另存原生页、完整配置副本或第二份业务 JSON。

## 3. query_capabilities：MCP 分支

合法新上下文在 Skill 目录后提供当前作用域已启用 MCP 服务的轻量名称目录及固定发现指引，见[初始目录规范](../archived_docs/PULSARA_MCP_CATALOG_INCREMENTAL_IMPLEMENTATION_SPEC.zh.md)。名称来自公开连接 Target 的 server_id，Plugin 只补来源名称；启用不等于就绪，同名也不代表同一连接。实际 Target、工具接口和 invocation.target 仍由本查询取得。可直接按任务关键词搜索匹配工具；已有足够接口说明和有效引用时直接调用，不必重新逐层列服务、列工具、查详情。查询参数及返回类型不变。

### 3.1 查询层次、搜索与范围

MCP 分支保留服务、工具目录、工具精确详情三层查询，并支持按关键词搜索，不把协议支持的全部对象都展开为模型目录：

| 本次参数 | 返回内容 |
| --- | --- |
| kind:MCP，不填 target、query | MCP 服务记录列表 |
| kind:MCP，填写 query，不填 target | 当前范围内的工具与服务混合搜索结果，工具在前 |
| kind:MCP，target 为 MCP_CONNECTION | 该服务的工具目录 |
| kind:MCP，target 为 MCP_TOOL | 该工具的完整参数 schema 与实际调用方式 |

顶层 kind 始终必填 MCP，不新增子类别、action 或详细查询开关。未填写 query 的服务列表不展开工具 schema，工具目录不为所有工具提前生成调用引用；关键词搜索命中的工具直接带完整参数 schema、实际声明的返回 schema 和可用调用引用，无须再精确查询一次才能调用。

Prompt 留在用户 slash 链路，资源和资源模板不在本章展开，也不进入关键词搜索。服务记录可返回已知的 resource_count 和 resource_template_count，供模型判断是否按需查看；资源目录与具体读取统一由 read_mcp_resource 承担，传 MCP_CONNECTION 列目录，传 MCP_RESOURCE 读具体 URI。本章不增加资源类别或资源查询分支；相同连接 Target 交给 query_capabilities 仍查询工具目录。ROOT 与子代理通过 call_mcp_tool 使用搜索记录或精确工具详情取得的调用引用，两个 MCP 接入口的参数及返回见[MCP 接入草案](mcp.md)。

复用当前来源观察、MCP 目录、路由及调用引用 owner，查询只读取当前可观察事实，不连接服务、执行远端工具或自动刷新目录。本地／Plugin 配置来源保留原 Host home 和 GUI 项目范围；子代理只见其分配的运行目录，不据此开放 ROOT 的安装来源。独立及 Plugin 连接默认允许 ROOT 和子代理，用户可在能力页关闭子代理使用；二者统一通过 call_mcp_tool 调用各自范围内的工具。JS 沿现有 Code Mode 范围使用，不因查询分类改变原权限。

模型只使用 MCP_CONNECTION 定位服务、MCP_TOOL 定位工具，不公开 MCP_SERVER Target、运行实例 ID 或 connection 到 server 的对应字段。内部运行实例及其生命周期仍由原 owner 管理。子代理的运行目录沿既有范围提供对应连接 Target，不要求其读取本地来源配置，也不因此开放该配置。现有 HOST_OVERRIDE 保留内部入口，当前生产链路继续不启用、不消费；它不作为本次产品要求，不为它扩展公开 Target、来源枚举或兜底分支。invocation 中既有 MCP_TOOL_REF 仍是实际调用引用，不是公开的另一类服务 Target。

### 3.2 参数与现有 Target

服务列表与跨服务搜索参数：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| kind | MCP | 必填 | 只查询 MCP |
| query | string | 可选 | 非空关键词；省略列服务，填写则搜索当前范围内的工具与服务 |
| scope | USER／WORKSPACE | 可选，默认不过滤 | 按服务配置来源的作用域筛选 |
| parent | PLUGIN Target | 可选，默认不限定 Plugin | 只查询该 Plugin 提供的服务及其工具 |

连接和工具精确查询只接受以下两个参数，target 放首位：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MCP_CONNECTION／MCP_TOOL Target | 必填 | 连接 Target 取得工具目录，工具 Target 取得工具精确详情 |
| kind | MCP | 必填 | 顶层查询类别 |

填写 target 后不混填 query、scope 或 parent；列表或搜索的 scope 与 parent 的实际归属冲突时失败。服务内工具目录由连接 target 选择，不再用 parent 传服务。删除顶层 source_kind、offset、limit 和旧重新查询式 next；Target 中不携带 scope、source_kind、plugin_id 或运行实例字段。不接受 null、额外参数、其他种类 Target 或全种类查询。

```typescript
type McpConnectionTarget = {
  kind: "MCP_CONNECTION";
  config_path: string;
  server_id: string;
};
type McpToolTarget = {
  kind: "MCP_TOOL";
  server_id: string;
  remote_tool_name: string;
};

type QueryMcpArgs =
  | {
      kind: "MCP";
      scope?: CapabilityScope;
      parent?: PluginTarget;
      query?: string;
      target?: never;
    }
  | {
      target: McpConnectionTarget | McpToolTarget;
      kind: "MCP";
      query?: never;
      scope?: never;
      parent?: never;
    };
```

parent 复用第 1.3 节的 PLUGIN Target。MCP_CONNECTION 的 config_path 区分来源，server_id 区分同一文件中的多个声明；Plugin 配置使用文件里的 local_server_id，不由模型拼装运行时服务名。Target 由原 owner 解析并关联实际运行实例，不按名称猜关联。模型直接复制连接 Target 取得当前工具目录；没有关联时明确说明目录未知，不另返运行实例 Target。

MCP_TOOL 的 server_id 是当前调用范围中由 runtime 返回的实际服务标识，可能与配置文件内的服务键不同；模型不自行转换。remote_tool_name 是服务声明的原始名字，不使用规范化、截短后的 provider 工具名定位或调用。实际调用统一使用详情中的 MCP_TOOL_REF，不把现有随连接生命周期变化的内部 mcp-tool 身份直接当作新公开 ID。

### 3.3 精简记录与类型

所有结果均使用 meta/content，记录为 type:object/value，不在顶层重复业务 target。

| 记录 | 保留字段 |
| --- | --- |
| 服务 | target、name、status，可选 instructions、resource_count、resource_template_count；配置路径使用 target.config_path |
| 工具目录项 | target、实际 description、简短 status |
| 工具精确详情 | target、实际 description、input_schema、实际声明的 output_schema、invocation |
| 工具搜索记录 | 复用工具精确详情的字段与类型；description 改为本次搜索的原文摘录 |

服务的 target.config_path 来自实际配置来源，不伪造路径，也不在服务记录中重复一个 config_path。instructions 只保留该服务实际提供的使用说明，不自动总结或补写。详细连接配置需要时沿 target.config_path 调用 read_file 或使用终端查看，不默认返回命令、args、cwd、endpoint、认证配置及环境变量；查询不自动读取整份配置。

resource_count 是已发现具体资源数量，resource_template_count 是已发现模板数量，不是模板能展开出的实例数。两项均为非负整数，分别只在有可靠目录依据时提供，未知时省略，确认没有才填 0，不填 null 或用 0 代替未知。计数与本次服务记录所用的已接纳目录快照一起冻结，不为取得数字额外请求 MCP，也不保证查询后远端状态始终不变；NEXT 不重算计数。不增加合计、已读数量或目录就绪布尔值。

服务 status 简短说明连接、禁用或目录尚未发现等实际情况。禁用的配置连接显示「已配置，当前已禁用。」工具目录的 status 说明当前是否可调用；这些是业务状态，不替代 meta.status 的查询状态。来源及运行身份由内部 owner 处理，不再并列返回 source、runtime_target、其他计数、多个状态布尔值或子项查询调用。

```typescript
type McpQueryJsonValue =
  | string | number | boolean | null
  | McpQueryJsonValue[] | McpQueryJsonObject;
type McpQueryJsonObject = {[key: string]: McpQueryJsonValue};

interface McpServiceRecord {
  target: McpConnectionTarget;
  name: string;
  status: string;
  instructions?: string;
  resource_count?: number;
  resource_template_count?: number;
}

interface McpToolListRecord {
  target: McpToolTarget;
  description?: string;
  status: string;
}

type McpToolDetailRecord =
  | {
      target: McpToolTarget;
      description?: string;
      input_schema: McpQueryJsonObject;
      output_schema?: McpQueryJsonObject;
      invocation: {
        mode: "META";
        target: {kind: "MCP_TOOL_REF"; tool_ref: string};
      };
    }
  | {
      target: McpToolTarget;
      description?: string;
      input_schema?: McpQueryJsonObject;
      output_schema?: McpQueryJsonObject;
      invocation: {mode: "UNAVAILABLE"};
    };

interface McpQueryTextPart {
  type: "text";
  value: string;
}

interface McpQueryObjectPart<T> {
  type: "object";
  value: T;
}

interface QueryMcpSuccess<T> {
  meta: {status: "SUCCESS"};
  content: (McpQueryTextPart | McpQueryObjectPart<T>)[];
}

interface QueryMcpFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (
    | McpQueryTextPart
    | McpQueryObjectPart<McpServiceRecord | McpToolListRecord | McpToolDetailRecord>
  )[];
}

type QueryMcpServicesResult = QueryMcpSuccess<McpServiceRecord> | QueryMcpFailure;
type QueryMcpToolsResult = QueryMcpSuccess<McpToolListRecord> | QueryMcpFailure;
type QueryMcpToolResult = QueryMcpSuccess<McpToolDetailRecord> | QueryMcpFailure;
type QueryMcpSearchResult =
  | QueryMcpSuccess<McpToolDetailRecord | McpServiceRecord>
  | QueryMcpFailure;
```

可调用的 META 详情必须保存完整 input_schema，返回类型和固定 SDK 声明均将其设为必填，不为展示页长裁掉或猜测参数；原生投影允许跨页。只有服务实际声明 output_schema 时才返回，不填 null 占位；它描述成功 structuredContent，不描述整个调用结果，也不约束失败业务值。没有输出 schema 时可使用已知共同包装和 content，或输出完整结果，不强制额外探测调用。UNAVAILABLE 可省略无法提供的 schema，content 的 text 简短说明原因，不能伪造可调用性。查询成功取得“当前不可用”这一事实时不把它混同为查询失败。

所有远端 MCP 工具统一通过 call_mcp_tool 调用，不返回 DIRECT 或独立 tool_name。META 使用原 owner 返回的 MCP_TOOL_REF；UNAVAILABLE 不提供假调用 Target。同一 session 内，原生调用与各个 Cell 共用引用，Cell 结束或模型 epoch 更换本身不使引用失效；ROOT 与子代理各用自己的 session 引用。有效性按该工具实际定义、策略、范围和目标身份核对，权限继续逐次检查。普通同目标重连完成发现且依据未变时可复用原引用；无关工具变化或聚合 runtime 代次更新不使它失效。共同返回沿[MCP 接入草案](mcp.md)，不由查询记录反推资源读取合同。

工具搜索记录和工具精确详情使用同样的两种 Target：记录的 target:MCP_TOOL 用于定位、继续查询工具；invocation.target:MCP_TOOL_REF 是该记录的调用属性，原样交给 call_mcp_tool。可调用记录直接携带 REF，有效绑定重复查询可复用引用，不要求每次查询创建新 REF；不可用记录沿 UNAVAILABLE 分支说明原因。

删除默认详情中的 effect_kind、重复 invocation_notice、内部版本／安装身份、比较链、固定调用教学和独立诊断数组。它们不进入 JS 的精简查询值；实际执行权限、引用有效性、凭据处理及源关联仍由原 owner 承担。有用诊断写 text，不给模型额外管理资格布尔值。

### 3.4 搜索、排序与描述摘录

搜索在当前调用者可见、已接纳的目录中运行 BM25。模型配置中有完整的 Embedding 连接时，工具搜索自动使用该连接。工具公开名、完整描述和查询词会发送到该端点。PostgreSQL 缓存工具文本向量，应用重启后可以复用。冷缓存或 Embedding 暂不可用时，仍可按关键词搜索。

两路工具检索均使用工具名称与完整原始描述。服务标识、名称和用法说明参与服务的词法匹配。scope 和 parent 在检索前限定实际可见来源。服务命中返回连接记录，按连接 Target 可以进一步列出工具。

精确匹配完整工具名的记录居首，其他工具按 BM25 与通过余弦准入的语义命中做等权 RRF 排序，同分沿目录顺序。服务命中按 BM25 顺序跟在工具之后。同一工具两路命中只显示一次，不同连接的同名工具各自保留 Target。记录使用第 3.3 节的 object 形状。

description 只保留一个字段：

| 查询情况 | description |
| --- | --- |
| 搜索命中工具描述 | 完整原始描述中第一处词法命中附近的连续原文摘录 |
| 描述没有词项命中（含仅语义命中） | 描述开头的原文摘录 |
| 描述很短 | 完整原文 |
| 没有描述 | 省略字段 |
| MCP_TOOL Target 精确查询 | 完整原始描述 |

“第一处”指原文中最早出现的匹配查询词项，沿本次分词和归一规则判断，不是 query 中第一个词或得分最高词的位置。省略的首尾用 … 表示，不生成改写摘要，不增加 snippet、截断布尔值或完整描述副本。BM25 始终使用完整描述评分，选中工具后才取摘录。摘录窗口不取决于原生 12k 页长；摘录是本次搜索的业务值，在保存前确定，原生、JS、NEXT 和导出共用它。

搜索记录的 input_schema 及存在时的 output_schema 保持完整，调用引用按同一工具的完整实际定义核对，不按描述摘录建立引用。需要完整说明时，模型可用记录的 MCP_TOOL Target 精确查询；这不是强制读取或调用前置门槛。

例如跨服务搜索，原生调用为：

```text
工具：query_capabilities
参数：
{"kind":"MCP","query":"github pull request"}
```

假设工具名称／描述与服务名称都命中，原生投影为以下混合记录；schema、路径和引用仅为示例：

```text
meta:
  status: SUCCESS

content:
  {
    "target": {"kind":"MCP_TOOL","server_id":"github","remote_tool_name":"search_pull_requests"},
    "description": "…Search pull requests in GitHub repositories using a query.…",
    "input_schema": {
      "type":"object",
      "properties":{"query":{"type":"string"}},
      "required":["query"],
      "additionalProperties":false
    },
    "invocation": {
      "mode":"META",
      "target":{"kind":"MCP_TOOL_REF","tool_ref":"mcpref:<实际引用ID>"}
    }
  }
  {
    "target": {"kind":"MCP_CONNECTION","config_path":"/Users/example/.pulsara/mcp.yaml","server_id":"github"},
    "name":"GitHub",
    "status":"已连接，工具目录已发现。"
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整搜索结果的导出 Target>
next: null
```

JS 发起相同查询，完整结果仍为 meta/content，工具和服务各占一个 object 项：

```javascript
const matches = await tools.query_capabilities({
  kind: "MCP",
  query: "github pull request"
});
console.log(matches);
```

对应的完整业务值为：

```json
{
  "meta": {"status":"SUCCESS"},
  "content": [
    {
      "type":"object",
      "value": {
        "target":{"kind":"MCP_TOOL","server_id":"github","remote_tool_name":"search_pull_requests"},
        "description":"…Search pull requests in GitHub repositories using a query.…",
        "input_schema": {
          "type":"object",
          "properties":{"query":{"type":"string"}},
          "required":["query"],
          "additionalProperties":false
        },
        "invocation": {
          "mode":"META",
          "target":{"kind":"MCP_TOOL_REF","tool_ref":"mcpref:<实际引用ID>"}
        }
      }
    },
    {
      "type":"object",
      "value": {
        "target":{"kind":"MCP_CONNECTION","config_path":"/Users/example/.pulsara/mcp.yaml","server_id":"github"},
        "name":"GitHub",
        "status":"已连接，工具目录已发现。"
      }
    }
  ]
}
```

模型从原生结果或准备 Cell 的输出取得所需接口说明后，可以直接原生调用 call_mcp_tool，或编写后续业务 Cell 复用同 session 的 REF；不要求先用 MCP_TOOL 再查一次，也不把 JS 内存中存在 matches 当作模型已看到接口。

### 3.5 原生三层调用与结果

第一层，列 MCP 服务：

```text
工具：query_capabilities
参数：
{"kind":"MCP"}
```

也可显式筛选 scope，或填写 PLUGIN parent；以下假设完整结果只有一项，路径及内容为示例：

```text
meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "MCP_CONNECTION",
      "config_path": "/Users/example/.pulsara/mcp.yaml",
      "server_id": "docs"
    },
    "name": "文档服务",
    "status": "已连接，目录已发现。",
    "instructions": "搜索文档后，根据返回的文档标识取得正文。",
    "resource_count": 3,
    "resource_template_count": 1
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整服务列表的导出 Target>
next: null
```

第二层，原样使用服务 Target 取得工具目录：

```text
工具：query_capabilities
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/Users/example/.pulsara/mcp.yaml","server_id":"docs"},
  "kind":"MCP"
}
```

例如本次完整目录只有一个工具，结果为：

```text
meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "MCP_TOOL",
      "server_id": "docs",
      "remote_tool_name": "search"
    },
    "description": "搜索文档。",
    "status": "当前可调用。"
  }

tool_result: <本次完整工具目录的导出 Target>
next: null
```

第三层，取得该工具的精确详情：

```text
工具：query_capabilities
参数：
{
  "target":{"kind":"MCP_TOOL","server_id":"docs","remote_tool_name":"search"},
  "kind":"MCP"
}
```

假设工具当前可调用，且服务未声明 output_schema，原生返回：

```text
meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "MCP_TOOL",
      "server_id": "docs",
      "remote_tool_name": "search"
    },
    "description": "搜索文档。",
    "input_schema": {
      "type": "object",
      "properties": {
        "query": {"type": "string"}
      },
      "required": ["query"],
      "additionalProperties": false
    },
    "invocation": {
      "mode": "META",
      "target": {
        "kind": "MCP_TOOL_REF",
        "tool_ref": "mcpref:<引用ID>"
      }
    }
  }

tool_result: <本次完整工具详情的导出 Target>
next: null
```

后两层示例省略的观察时间外壳仍沿共同原生规则；示例 ID 为说明占位，实际身份由既有 owner 和共享 ID 层产生。工具目录不提前带所有工具的 schema 或引用，精确详情不额外展开服务配置、Prompt 或资源。

### 3.6 JS 完整返回与三层组合

JS 按相同业务层次查询，取得各层完整的精简业务值：

```javascript
const services = await tools.query_capabilities({kind: "MCP"});
console.log(services);
```

例如对应第一层的程序值为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {"kind": "MCP_CONNECTION", "config_path": "/Users/example/.pulsara/mcp.yaml", "server_id": "docs"},
        "name": "文档服务",
        "status": "已连接，目录已发现。",
        "instructions": "搜索文档后，根据返回的文档标识取得正文。",
        "resource_count": 3,
        "resource_template_count": 1
      }
    }
  ]
}
```

后两层可以按 Target 继续查询，在脚本中直接使用对象字段：

```javascript
const directory = await tools.query_capabilities({
  target: {kind: "MCP_CONNECTION", config_path: "/Users/example/.pulsara/mcp.yaml", server_id: "docs"},
  kind: "MCP"
});
console.log(directory);
```

```javascript
const inspected = await tools.query_capabilities({
  target: {kind: "MCP_TOOL", server_id: "docs", remote_tool_name: "search"},
  kind: "MCP"
});
console.log(inspected);
```

directory 的 object.value 为 McpToolListRecord，inspected 的 object.value 为 McpToolDetailRecord。schema 为完整 JSON 对象，可直接访问及组合；服务记录的两个可选资源计数也保留 number 类型，未知字段省略。JS 不携带原生 tool_result、next 或时间外壳，不隐含返回完整配置、资源或 Prompt。console.log／显式 return 只交付脚本选择的结果，沿 Cell steer 整条处理，不额外加展示预算或分页。

精确详情示例属于接口准备：输出 description、input_schema、存在时的 output_schema 及 invocation，让模型收到足以编写后续代码的信息。模型随后可原生调用 call_mcp_tool，也可编写业务 Cell 使用同一个引用，不要求重新查询。也允许直接原生调用 query_capabilities 取得详情；不强制先运行准备 Cell。查询结果只在 JS 内存中存在不等于模型已经看到。实际调用与图文组合示例见[MCP 接入草案](mcp.md)；资源目录与读取见其[第 4 章](mcp.md#4-read_mcp_resource按需列目录与读取资源)。

### 3.7 空结果、错误与冻结分页

正常服务列表为空时为 SUCCESS 和一句“没有找到符合条件的 MCP 服务。”。已完整观察的服务工具目录为空时为 SUCCESS 和一句“该服务当前没有可见工具。”。未关联运行目录、尚未发现目录或目录不可读取时如实说明未知／失败及已知服务状态，不用空列表断言远端没有工具。

当前 scope／parent 中已启用连接的目录均已接纳且没有命中时，返回 SUCCESS 和一句“没有找到匹配的 MCP 工具或服务。”。未命中不表示服务未安装或工具目录为空。范围内任一已启用连接尚无当前配置对应的完整目录时，无论零命中还是已有命中均返回 ERROR，并以 text 列出未完成服务，说明搜索不完整；保留本次已知命中及有效 schema／REF，结果接受后这些引用仍可调用。禁用及范围外连接不影响覆盖；同名来源必须与实际配置来源相接，不能借用另一来源的目录。相同语义重连保留已接纳目录的可搜索性，物理不可用仅影响调用准入。

新查询和新调用引用解析使用当前已接纳、当前范围内的目录，不依附 provider 冻结工具视图。继续复用现有 SDK 通知、完整刷新、已配置周期刷新及重连发现；收到变动通知先按原 owner 暂挡新准入，完整刷新后才确认具体增删。刷新失败或部分目录不可读不确认删除，也不要求每次查询或调用都重新 listing。

某工具的完整原始 description、input_schema、output_schema、实际执行策略或目标身份变化时，其旧引用按实际依据失效，模型重新查询；不计算原生 schema 兼容、降级或恢复。不同 query 或摘录位置不改变工具定义，也不使引用失效。其他工具未变则保留其引用。换 endpoint、账户或来源不能仅凭同名同 schema 继承旧调用资格；规则见[架构草案第 4 章](../archived_docs/PULSARA_MCP_META_ONLY_CALLING_ARCHITECTURE_DRAFT.zh.md#4-能力发现与模型上下文)。

服务记录显示关闭、连接失败等可核实状态，不直接表示查询失败。精确目标不存在、无法完整取得请求的目录或参数分支错误时为 ERROR；取消为 CANCELLED。content 保留原因及实际已取得的业务记录，JS 通过实际存在的 err.result 取得完整失败值；不伪造结果、调用引用或 schema，不新增 partial、错误枚举或恢复任务。

ERROR 写明已知原因及是否派发，不把所有失败都归为引用过期。已知失效引用的本地拒绝说明本次未发送；连接暂不可用、远端业务失败和已发送但结果未确认分别如实报告，不靠重新查询自动重做。服务未通知的变化不保证能提前识别，实际参数或业务错误本身不证明 schema 已变。

三层查询与关键词搜索都先取得本次完整业务值并保存，再由原生按 12k 顺序交付。完整服务 instructions、非搜索查询的完整描述及所有 schema 不为展示页长裁掉；搜索 description 按第 3.4 节确定摘录后完整保存，不在展示时另行裁短。object 的确定 JSON 文字允许跨页，不能为“schema 必须一页显示”恢复旧拒绝上限。JS 返回完整对象，不套用 12k，也不向程序交付结构化分片。

首屏及正常续页的 tool_result 指向同一冻结结果，next 为完整 NEXT Target，原样交给 read_file，不暴露内部位置。NEXT 和导出不扫描配置、重查工具目录、重新搜索、重算排序或摘录、刷新路由、重新生成工具引用或执行远端调用；已接受查询不会因后续连接变化改写。引用按同 session 的进程内 owner 管理；保存、补读或导出引用文字不提供跨重启恢复资格。引用实际失效时由执行 owner 报告，需要新调用事实时显式重新查询，不让 NEXT 刷新。

删除当前整页 schema 预算拒绝及 FULL 完整交付后激活的门槛。精确详情或搜索结果完整保存、接纳后，其中的引用即可调用；模型即使只读到部分记录，也自行决定是否调用或用 NEXT 续读。runtime 不累计检查 schema 是否读完，原生与 JS 的实际交付回调不决定引用生效。权限、参数和执行器有效性仍在调用时检查。

必须删除旧每个 epoch 1,024 个调用引用上限及其专属拒绝分支和测试，不改成 session 总上限，也不另设替代数量；继续复用有效引用并清理失效引用。上述变化复用现有进程内 owner，不新增数据库引用表、已读资格、交付回执或执行机制。实际保存或交付失败沿共同规则如实结算，不重执行业务，不提供假可读 Target。

## 4. query_capabilities：PLUGIN 分支

### 4.1 用途、范围与稳定实例定位

PLUGIN 负责包的概览及组件导航，不复制 SKILL、HOOK、MCP 的详细查询。列表只给包的概览；精确查询用 text 说明包及组件类别，穿插各组件的 Target，模型按需交给前三个分支继续查询。

列表覆盖调用者在有效 Pulsara home 和当前 GUI 项目下可观察的 USER／WORKSPACE Plugin，包括已禁用、空包及已知但定义存在问题的安装。列表不因包没有可用组件就隐藏该包。不支持项目来源时显式查询 WORKSPACE 报告实际错误；不借 terminal cwd 更换项目，不扩展子代理的本地来源权限。

PLUGIN Target 只含 kind、plugin_path。plugin_path 是现有存储的稳定实例目录 plugin_package_parent，例如 /Users/example/.pulsara/plugins/packages/user/example-plugin，不是带某次 package_install_id 的包目录。由 Plugin owner 按存储布局及当前状态解析实际实例，模型不填写 scope、plugin_id 或安装身份，不新造独立 ID、映射表或正文实体。包更新后仍用同一实例目录查询当前包；已删除或不可观察的实例按实际事实报告。

组件 Target 复制实际观察值：SKILL 使用 SKILL.md 路径，HOOK_SOURCE 使用实际 Hook 声明路径（也可能是原生 OpenAI manifest，详见第 2.1 节），MCP_CONNECTION 使用 MCP 配置路径及其中的服务键。Hook 配置是一份来源，即使包含多条规则也只返回一个来源 Target；MCP 配置中的每个服务分别返回一个连接 Target。不递归展开工具 schema、Hook 全量配置或 Skill 正文，也不自动调用这些组件。

### 4.2 参数

列表只接受：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| kind | PLUGIN | 必填 | 只列 Plugin 包 |
| scope | USER／WORKSPACE | 可选，默认不过滤 | 按实际安装作用域筛选 |

精确查询只接受，target 放首位：

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PLUGIN Target | 必填 | 以稳定实例目录定位一个 Plugin |
| kind | PLUGIN | 必填 | 顶层查询类别 |

精确查询不混填 scope。PLUGIN 自身不接受 parent；它的 Target 可以作为前三个分支的 parent。不提供 source_kind、名称搜索、offset、limit、游标或旧重新查询式 next，不接受 null、额外字段或不完整 Target。

```typescript
// PluginTarget 复用第 1.3 节定义：{kind: "PLUGIN"; plugin_path: string}。
type QueryPluginArgs =
  | {
      kind: "PLUGIN";
      scope?: CapabilityScope;
      target?: never;
    }
  | {
      target: PluginTarget;
      kind: "PLUGIN";
      scope?: never;
    };
```

### 4.3 列表概览、精确查询与返回类型

两种查询都只使用 meta/content，不在顶层重复 target。

列表中的每个包使用一个 object 项：

| 字段 | 明确含义 |
| --- | --- |
| target | 该包的 PLUGIN Target |
| name | 实际取得的包名称；未取得时省略 |
| description | 实际取得的完整用途说明；未提供或未取得时省略 |
| status | 已启用、已禁用或定义异常等简短状态文字 |
| components | skills、hooks、mcp_servers 的声明数量，各值为非负整数；无法确认的值为 null |

hooks 数量指 Hook 规则数，精确查询交出的 HOOK_SOURCE Target 数量则按配置来源计算，二者不是同一计数。数量只表达包声明的内容，不保证当前组件全部生效或可调用。确认没有才填 0；未知不写成 0，无法完成观察时保留已知概览并报告失败。

精确查询的 content 只使用 text 与组件 target 项：

- text 简短说明当前包的名称、用途、状态及后续组件类别，必要分隔由文字自身提供。
- target 项的 value 直接为 SkillTarget、HookSourceTarget 或 McpConnectionTarget；不把它们嵌入 components 对象，不重复列一份 Target 清单或数量。
- 不再返回包自身的 object 或重复的 PLUGIN Target；该包已经由本次参数指定。组件类别按 SKILL、HOOK、MCP 展示，类别内沿原 owner 的确定顺序排列；原生与 JS 保留同一顺序。
- 没有组件的类别不制造 Target；全部为空时以 text 说明“该插件没有可查询的组件。”。

```typescript
interface PluginTextPart {
  type: "text";
  value: string;
}

interface PluginListRecord {
  target: PluginTarget;
  name?: string;
  description?: string;
  status: string;
  components: {
    skills: number | null;
    hooks: number | null;
    mcp_servers: number | null;
  };
}

interface PluginObjectPart {
  type: "object";
  value: PluginListRecord;
}

type PluginComponentTarget = SkillTarget | HookSourceTarget | McpConnectionTarget;
interface PluginComponentTargetPart {
  type: "target";
  value: PluginComponentTarget;
}

interface QueryPluginsSuccess {
  meta: {status: "SUCCESS"};
  content: (PluginTextPart | PluginObjectPart)[];
}

interface QueryPluginDetailSuccess {
  meta: {status: "SUCCESS"};
  content: (PluginTextPart | PluginComponentTargetPart)[];
}

interface QueryPluginFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (PluginTextPart | PluginObjectPart | PluginComponentTargetPart)[];
}

type QueryPluginsResult = QueryPluginsSuccess | QueryPluginFailure;
type QueryPluginDetailResult = QueryPluginDetailSuccess | QueryPluginFailure;
```

不返回 enabled、管理资格布尔值、独立诊断数组、package_install_id、data_root、安装包目录或版本字段；实际包版本仍属于包元数据，不因默认查询省略就删除它。能够正常查询已禁用的包时为 SUCCESS，实际能否修改或删除由后续管理入口结算，不在查询中预告资格。内部安装、来源与信任事实继续保留在原 owner。

### 4.4 原生列表和精确查询示例

列当前范围内的包，或只列项目来源：

```text
工具：query_capabilities
参数：
{"kind":"PLUGIN"}

工具：query_capabilities
参数：
{"kind":"PLUGIN","scope":"WORKSPACE"}
```

例如完整列表只有一个包，原生投影为：

```text
meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "PLUGIN",
      "plugin_path": "/Users/example/.pulsara/plugins/packages/user/example-plugin"
    },
    "name": "example-plugin",
    "description": "提供文档检索和项目检查能力。",
    "status": "已启用。",
    "components": {
      "skills": 1,
      "hooks": 2,
      "mcp_servers": 1
    }
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整 Plugin 列表的导出 Target>
next: null
```

复制该包 Target 精确查询：

```text
工具：query_capabilities
参数：
{
  "target":{"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example-plugin"},
  "kind":"PLUGIN"
}
```

假设包里有一份 Skill、一份含两条规则的 Hook 配置和一个 MCP 服务，原生结果为：

```text
meta:
  status: SUCCESS

content:
  example-plugin：提供文档检索和项目检查能力。已启用。
  Skill：
  {"kind":"SKILL","skill_path":"/Users/example/.pulsara/plugins/packages/user/example-plugin/package:<安装ID>/skills/review/SKILL.md"}
  Hook：
  {"kind":"HOOK_SOURCE","config_path":"/Users/example/.pulsara/plugins/packages/user/example-plugin/package:<安装ID>/dev.pulsara/hooks/hooks.json"}
  MCP：
  {"kind":"MCP_CONNECTION","config_path":"/Users/example/.pulsara/plugins/packages/user/example-plugin/package:<安装ID>/mcp.json","server_id":"docs"}

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整 Plugin 详情的导出 Target>
next: null
```

上述包路径、定义路径及安装 ID 均为示意，真实值由原 owner 返回，模型不按示例拼路径。文字来自 text 项，引用来自 target 项的紧凑 JSON 投影，不将原生纯文本误当作 JS 返回对象。复制其中任一组件 Target，再以 kind:SKILL、HOOK 或 MCP 精确查询即可；MCP 连接查询给出工具目录，之后按 MCP_TOOL Target 取得工具详情。

### 4.5 JS 完整返回与组件组合

列表直接取得完整概览对象数组，不带原生分页外壳：

```javascript
const result = await tools.query_capabilities({kind: "PLUGIN"});
const plugins = result.content
  .filter(part => part.type === "object")
  .map(part => part.value);
console.log({plugins});
```

精确查询返回有序 text／target 内容项：

```javascript
const detail = await tools.query_capabilities({
  target: {
    kind: "PLUGIN",
    plugin_path: "/Users/example/.pulsara/plugins/packages/user/example-plugin"
  },
  kind: "PLUGIN"
});
console.log(detail);
```

与原生精确查询示例对应的完整业务值为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "example-plugin：提供文档检索和项目检查能力。已启用。\nSkill：\n"},
    {
      "type": "target",
      "value": {
        "kind": "SKILL",
        "skill_path": "/Users/example/.pulsara/plugins/packages/user/example-plugin/package:<安装ID>/skills/review/SKILL.md"
      }
    },
    {"type": "text", "value": "Hook：\n"},
    {
      "type": "target",
      "value": {
        "kind": "HOOK_SOURCE",
        "config_path": "/Users/example/.pulsara/plugins/packages/user/example-plugin/package:<安装ID>/dev.pulsara/hooks/hooks.json"
      }
    },
    {"type": "text", "value": "MCP：\n"},
    {
      "type": "target",
      "value": {
        "kind": "MCP_CONNECTION",
        "config_path": "/Users/example/.pulsara/plugins/packages/user/example-plugin/package:<安装ID>/mcp.json",
        "server_id": "docs"
      }
    }
  ]
}
```

脚本可以直接筛选并组合已有 Target，不解析文字、不重新构造内部身份。例如接着查询这个包提供的 MCP 工具目录：

```javascript
const connection = detail.content.find(
  part => part.type === "target" && part.value.kind === "MCP_CONNECTION"
);
if (connection) {
  const directory = await tools.query_capabilities({
    target: connection.value,
    kind: "MCP"
  });
  console.log(directory);
}
```

JS 的列表与详情均完整返回，数组、计数字段及 Target 字段可直接访问。不带 tool_result、next 或时间外壳，console.log／显式 return 按脚本实际选择沿 Cell steer 整条交付，不新增输出预算或分页；返回组件引用本身不自动读取、执行或管理它们。

### 4.6 空结果、失败与冻结分页

正常列表完成但没有包时为 SUCCESS，content 一条 text：“没有找到符合条件的插件。”精确查询已知空包时为 SUCCESS，以 text 说明实际状态及没有可查询组件，不返回假的子 Target。已禁用不等于查询失败，当前可确认的定义异常也用实际文字表达。

精确实例不存在或不在当前观察范围、无法完整取得包或请求的组件清单、参数不合法时为 ERROR；取消为 CANCELLED。content 先说明原因及必要路径，再保留本次实际取得的概览或组件 Target；不把未知组件当成空清单，不伪造名称、数量或组件。JS 通过实际存在的 err.result 取得完整失败业务值，不新增 partial、公开错误码或管理资格字段。

列表与精确查询都先完成本次业务读取并完整保存，原生再按共同 12k 顺序分页。列表 object 的 JSON 投影可以跨页，精确详情的文字与组件引用按有序内容交付；next 只作为完整 NEXT Target 交给 read_file，内部位置对模型透明。tool_result 指向该次完整冻结结果，正常结束 next:null 不替代业务状态。

NEXT 和导出只读取冻结结果，不重查 Plugin 状态、解析新安装包、刷新组件路径或取得实时目录。补页取得的组件 Target 仍指向结果中保存的对象；实际查询或管理时沿当前 owner 核对。需要更新后的组件就显式重新查询 Plugin。保存／交付失败沿共用规则处理，不重做查询或自动执行业务，不另存原生页或第二份完整业务值。

## 5. reload_capabilities：全量刷新

### 5.1 用途与调用范围

重新读取并刷新当前 Host 的能力来源：本地 MCP 配置、Plugin 及其 Skill／Hook，以及 USER 和当前 GUI 项目的本地 Hook 定义与信任状态。继续使用既有刷新及采用 owner，不增加新的扫描器或跨来源原子事务。

本工具用于手工修改文件、CLI 或外部程序造成的来源变化，以及已明确报告的采用失败后的刷新补救。正常能力管理变更沿既有自动采用路径，不要求模型例行补调本工具，不重做已经完成的安装或修改。

保持当前仅 ROOT 原生、bypass 权限调用的边界，不提供 JS API，不向子代理开放。刷新成功表示本次来源刷新完成，不保证远端 MCP 已连接、目录已发现或工具可调用；需要这些事实时由模型另用 query_capabilities 查询。

当前 epoch 的 SYSTEM、provider tools 保持原样，messages 只追加后缀；刷新不能重建已安装前缀。该行为由原 owner 保证，不在每次结果中重复返回 same_epoch_prefix。HOST_OVERRIDE 仍按第 3.1 节保留内部未消费入口，本工具不因刷新而启用它。

### 5.2 无参数调用

参数固定为 {}，每次全量刷新当前 Host 范围内的能力来源，不提供 kind、target、scope、阶段选择或页长参数。不接受额外字段或 null。

```text
工具：reload_capabilities
参数：{}
```

### 5.3 精简返回值与状态

业务结果只保留 meta.status 和 text 内容项数组，不返回顶层业务 target。下列类型用于明确原生业务值，不表示开放 JS API：

```typescript
type ReloadCapabilitiesArgs = Record<string, never>;

interface ReloadCapabilitiesTextPart {
  type: "text";
  value: string;
}

interface ReloadCapabilitiesResult {
  meta: {status: "SUCCESS" | "ERROR" | "CANCELLED"};
  content: ReloadCapabilitiesTextPart[];
}
```

| meta.status | 明确含义 | content |
| --- | --- | --- |
| SUCCESS | 本次请求的来源刷新完成 | 简短说明“已刷新当前能力来源。” |
| ERROR | 刷新失败、部分失败，或超时后仍无法确认完成 | 写清实际已完成部分、未完成或未确认部分、已知失败路径和具体原因 |
| CANCELLED | 调用按既有取消规则结算 | 说明取消事实及实际已完成部分；已发生的刷新不因此撤销 |

部分成功不另设 PARTIAL 状态，也不能因已经刷新一部分而统一返回 SUCCESS。删除正文 status、各阶段的公开状态字段、Hook 来源数量和详细清单、mcp_changed_server_ids、diagnostics、interruption、same_epoch_prefix 等包装。模型需要能力详情时另用 query_capabilities；内部阶段结果、错误事实、取消及采用状态仍由原 owner 保留并参与实际结算。

失败和取消的 content 至少给出一条有用说明，按实际情况表达：

- 具体哪些来源已经刷新，哪些失败或尚未完成，不能只写“部分配置未能刷新”，也不能暗示全量回滚。
- 配置读取或解析失败时，给出实际文件路径、底层错误原因；底层提供行列位置时一并保留，不猜测缺失的位置。
- 超时或其他失败导致最终采用状态不能确认时，明确写“刷新状态未确认”，不直接宣称未刷新。
- 旧定义是否仍在使用、是否采用了新定义，仅在原 owner 已确认时说明。错误文案不得伪造已完成部分、失败路径或已发生效果。

### 5.4 原生结果示例

以下路径、错误位置和已完成阶段均为模拟；实际结果使用 owner 返回的事实。完整成功时：

```text
meta:
  status: SUCCESS

content:
  已刷新当前能力来源。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整刷新结果的导出 Target>
next: null
```

例如其他阶段完成，但本地 Hook 的新定义未被采用：

```text
meta:
  status: ERROR

content:
  Plugin 和 Skill 来源已刷新，MCP 配置已刷新。
  本地 Hook 刷新失败：
  /Users/example/.pulsara/hooks.json 第 12 行第 5 列存在 JSON 语法错误：缺少逗号。
  本次未采用该文件中的新定义。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如 MCP 刷新等待超时，实际采用状态仍未确认：

```text
meta:
  status: ERROR

content:
  Plugin 和 Skill 来源已刷新。
  MCP 配置刷新等待超时，涉及 /Users/example/.pulsara/mcp.yaml，最终采用状态未确认。
  本地 Hook 刷新尚未开始。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如取消时已有部分阶段完成：

```text
meta:
  status: CANCELLED

content:
  刷新已取消。Plugin 和 Skill 来源已刷新，本地 Hook 刷新尚未开始。

tool_result: <本次完整取消结果的导出 Target>
next: null
```

后面三个示例省略的观察时间和微秒耗时，实际仍按共同原生外壳并列呈现，不伪造为 0。没有业务对象 Target，不因此省略完整结果的 tool_result 或正常分页所需的 next；失败结果正常保存且完整交付时，next:null 只表示该结果已交付完，不表示刷新成功。

### 5.5 结果保存、分页与实施核对

刷新及副作用结算遵守现有 deadline、取消和采用生命周期。已经完成的阶段不能因后续失败被描述成未发生；进入不可安全中断的处理后，沿既有 owner 等待必要结算，不增加回滚、自动重做或恢复任务。

删除执行前因原生结果展示预算不足而拒绝刷新的检查。完整结果先保存，再沿共同 12k 顺序分页；通常简短结果只有一页，较长失败说明仍完整保存，不为简短文案要求裁掉真实原因。NEXT 和导出只读取本次冻结结果，不重新刷新能力、不查询实时阶段状态或重放管理操作。保存／交付本身失败时按共同规则如实说明已知效果，不伪造成功或可读 Target。

实施时不能沿用当前适配器无条件 SUCCESS 的结果包装：应将内部真实完成、失败或部分失败、取消分别映射到 SUCCESS、ERROR、CANCELLED。捕获来源异常时保留实际路径、解析位置及具体原因用于 content，不吞掉原因后只给通用失败文案；不为此增加公开错误码、阶段状态矩阵或新的结果字段。

## 6. 当前生产实现参考

- [当前 query_capabilities 参数与说明](../src/pulsara_agent/capability/builtin_catalog.py)
- [查询分类与分支参数校验](../src/pulsara_agent/capability/source_query.py)
- [来源观察、Skill 列表和精确详情](../src/pulsara_agent/conversation_kernel/capability_query.py)
- [独立 Skill 观察及管理 owner](../src/pulsara_agent/capability/local_skills.py)
- [Plugin 观察及来源 owner](../src/pulsara_agent/plugins/management.py)
- [查询调度与当前结果适配](../src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [当前 JS 工具目录及调用范围](../src/pulsara_agent/conversation_kernel/tool_surface.py)
- [本地 Hook 来源发现](../src/pulsara_agent/hooks/source.py)
- [Hook 状态与公共定义投影](../src/pulsara_agent/hooks/presentation.py)
- [能力页用户级 Hook 后端](../src/pulsara_agent/desktop_app/session_capabilities.py)
- [能力页常规规则及审阅展示](../frontend/components/hooks-panel.tsx)
- [MCP 目录接纳与作用域快照](../src/pulsara_agent/conversation_kernel/mcp/supervisor.py)
- [MCP 工具详情及调用引用](../src/pulsara_agent/conversation_kernel/mcp/meta.py)
- [能力页 MCP 服务与工具展示](../frontend/components/capability-view.tsx)
- [Plugin 稳定实例及安装目录布局](../src/pulsara_agent/plugins/package_store.py)
- [Hook 来源身份与配置路径](../src/pulsara_agent/hooks/contracts.py)
- [MCP 原始工具名与 provider 名称转换](../src/pulsara_agent/conversation_kernel/mcp/naming.py)
- [现有内部 Host 配置覆盖入口](../src/pulsara_agent/mcp_config.py)
- [能力全量刷新及已有采用生命周期](../src/pulsara_agent/conversation_kernel/host.py)
- [当前刷新调用及结果适配](../src/pulsara_agent/conversation_kernel/tool_runtime_management.py)
- [能力管理执行与结果结算](../src/pulsara_agent/conversation_kernel/capability_management_execution.py)
- [ROOT 与 bypass 刷新授权](../src/pulsara_agent/conversation_kernel/tool_runtime_authorization.py)

这些接点用于核对现状，不表示生产代码已实现本草案。实施时接通四个 query_capabilities 分支及各自最小 Target，删除旧复合 Target 参数及其兼容入口、顶层 source_kind 输入和旧展示分页。MCP 搜索按第 3 章使用内存 bm25s、复用详情和引用 owner，并核对工具／服务混合命中、工具去重、描述原文摘录与完整 schema；同一搜索结果的原生、JS、NEXT 和导出保持一致。现有 Skill 页面 ID 不当作通用 Skill 身份；Plugin 稳定实例路径、Hook 配置路径、MCP 配置键及原始工具名由原 owner 解析，不新建 Target 登记表。公开 MCP 查询只接受连接及工具 Target，移除运行实例 Target 的公开 schema、返回字段与教学，不删除底层实例或生命周期。HOST_OVERRIDE 保留现有内部入口且不启用、不消费，不为其增加公开支持。

查询值和对应 JS 声明以本文件为准；reload_capabilities 按第 5 章收敛原生结果，保留既有内部来源、安装、信任、权限、取消和 provider 前缀边界。查询与搜索保留原有来源及执行边界，不能顺带开放管理权限。三个独立 Skill 动作见[能力变更工具草案](manage_capability.md)第 1–3 章，Hook 信任、撤销与启停见该草案第 4–6 章，MCP 共用 config 见第 7 章，ADD_LOCAL_MCP 的独立连接新增见第 8 章，UPDATE_MCP_CONNECTION 的独立／Plugin 局部更新与恢复默认见第 9 章，REMOVE_LOCAL_MCP 及现有清理语义见第 10 章，独立／Plugin MCP 的 OAuth 登录与本地授权清除见第 11、12 章，INSTALL_PLUGIN、SET_PLUGIN_ENABLED、REMOVE_PLUGIN 的统一参数及精简结果见第 13–15 章；独立及 Plugin MCP 连接默认允许 ROOT 和子代理使用，用户可在能力页关闭；config 对象局部更新保留显式范围，Plugin config:null 移除范围覆盖并恢复此默认，模型不填范围。其余安装默认值、启停、信任及审阅流程保持现有产品语义。两处 .agents/skills 保留发现和启停机制，且不由 REMOVE_LOOSE_SKILL 删除，bundled 和 Plugin 内的 Skill 同样不由该动作删除。已有能力对象的管理沿同一最小 Target 在原 owner 内解析，不按当前参考猜测新合同。call_mcp_tool 的共同结果、参数获取和原生／JS 调用示例，以及 read_mcp_resource 的两种 Target 分支与返回合同，见[MCP 接入草案](mcp.md)。
