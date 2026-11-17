# Pulsara 能力变更工具修订与使用教程草案

共用规则见[原生工具修订草案索引](../archived_docs/PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整保存、原生 12k／NEXT 及导出沿[统一交付草案](../archived_docs/PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)，结果存储沿[数据库 hardcut 草案](../archived_docs/PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。能力查询、最小能力 Target 和刷新入口见[能力查询与刷新草案](capabilities.md)。

状态：当前生效合同。逐项记录 manage_capability 的修订，当前包含三个独立 Skill action、三个 Hook action、MCP 共用 config、独立连接新增／删除及独立／Plugin 连接统一修改、独立／Plugin MCP 的两个 OAuth action，以及 INSTALL_PLUGIN、SET_PLUGIN_ENABLED、REMOVE_PLUGIN 的参数、返回值、原生示例及错误语义。安装默认值、启停、信任及审阅流程保持现有产品语义；独立及 Plugin MCP 连接的默认使用范围按第 7 章调整。仅 ROOT 原生调用，不提供 JS API；TypeScript 只描述合同，不表示存在脚本入口。

本文件所有 action 的模型可见 content 都直接说明对象、实际结果及必要失败原因。成功使用“已安装／已更新／已删除”等简短说明；部分失败先说已经完成的操作，再说失败步骤、路径和原因；无法确认时直接说“无法确认……是否已完成”。不向模型解释“采用、结算、状态切点”等内部术语，不重复内部枚举、处理阶段或无助于下一步操作的实现细节。下文实施说明仍可使用内部术语，但不把它们照搬进 content。

## 1. INSTALL_LOOSE_SKILL：安装独立 Skill

### 1.1 用途与安装行为

从一个已准备好的本地目录安装独立 Skill，源目录包含 SKILL.md，随附脚本和资源沿现有导入规则由安装 owner 处理。输入 Target 指向源目录，成功返回的 Target 指向安装后的 SKILL.md，二者不是同一对象。

名称和说明直接取源 SKILL.md 的 name、description；name 决定安装目录名称，不由源目录名代替。删除模型参数 name、description，不重写安装副本的这两个元数据字段，也不由安装工具补写缺失内容。源元数据无效时报告具体原因；需要改名或修改说明时，先用文件工具编辑准备安装的副本。元数据字段本身仍属于 Skill 内容，不因删除工具参数而删除。

scope 选择安装范围：USER 使用有效 Pulsara home 下的 skills 目录，WORKSPACE 使用当前 GUI 项目的 .pulsara/skills 目录。不支持项目来源的会话不能安装 WORKSPACE Skill，不借 terminal cwd 推导项目。目标安装目录由原 owner 根据实际范围和源 name 决定，不接受模型指定任意安装目的地。

保留现有目标已存在时失败的行为，不覆盖已有安装，不增加 replace 参数。安装后的启用设置、同名副本的最终选择及正文采用仍沿现有规则，需要这些事实时模型另用 query_capabilities 查询。后续能力采用沿现有自动路径，不要求模型例行补调 reload_capabilities。

权限、必要用户交互、源读取、安装发布及取消结算继续使用现有 owner，不套用 reload_capabilities 特有的仅 bypass 调用限制，不因参数精简扩大写权限。当前 epoch 的 SYSTEM、provider tools 保持原样，messages 只追加后缀。

### 1.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 必填，放首位 | 包含 SKILL.md 的本地源目录，不是文件本身或安装目的地 |
| action | INSTALL_LOOSE_SKILL | 必填 | 安装独立 Skill |
| scope | USER／WORKSPACE | 必填 | 选择用户级或当前 GUI 项目的安装范围 |

source_path 改为 target，使用文件工具的常规 PATH Target。工作区内可使用相对路径，工作区外使用绝对路径；沿现有文件路径规则解析成实际绝对源目录，不以进程当前目录或 terminal 的 cd 重新解释。这里的 PATH 输入与能力查询中使用绝对 skill_path 的 SKILL Target 各有用途，不混用。

只接受本动作上述字段。不接受 name、description、source_path、任意安装目的地、额外字段或 null。scope 不默认推断，PATH 也不接受远程 URL；下载或准备源目录由模型先完成。

```typescript
type SkillInstallSourceTarget = {kind: "PATH"; path: string};
type InstalledSkillTarget = {kind: "SKILL"; skill_path: string};

interface InstallLooseSkillArgs {
  target: SkillInstallSourceTarget;
  action: "INSTALL_LOOSE_SKILL";
  scope: "USER" | "WORKSPACE";
}
```

### 1.3 精简返回值与状态

成功业务值只保留顶层 target、meta.status 和 text content：

| 字段 | 明确含义 |
| --- | --- |
| target | 原 owner 确认已安装的 SKILL Target，skill_path 为安装后的 SKILL.md 绝对路径 |
| meta.status | SUCCESS／ERROR／CANCELLED |
| content | 有序文字项；成功给简短安装说明，失败或取消给实际原因及已知效果 |

不重复返回源路径、name、description、skill_root、独立 skill_path、字节统计、action、current、operation_status、adoption、清理布尔值或诊断代码。必要的并发、发布及采用事实由内部 owner 保留；有用错误及清理信息写 content，不新增状态矩阵或公开错误字段。

```typescript
interface InstallLooseSkillTextPart {
  type: "text";
  value: string;
}

interface InstallLooseSkillSuccess {
  target: InstalledSkillTarget;
  meta: {status: "SUCCESS"};
  content: InstallLooseSkillTextPart[];
}

interface InstallLooseSkillFailure {
  target?: InstalledSkillTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: InstallLooseSkillTextPart[];
}

type InstallLooseSkillResult = InstallLooseSkillSuccess | InstallLooseSkillFailure;
```

失败值中的 target 只在本次安装已被确认完成时保留，例如安装后能力刷新失败；它不表示整个调用成功。未安装成功或安装效果未确认时不伪造 SKILL Target。目标已存在导致安装被拒绝，也不把旧副本当成本次安装产物返回，具体路径写入 content。

### 1.4 原生调用与成功示例

例如源目录是 /tmp/downloads/review-main，SKILL.md 中声明 name 为 review，安装到当前项目：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"PATH","path":"/tmp/downloads/review-main"},
  "action":"INSTALL_LOOSE_SKILL",
  "scope":"WORKSPACE"
}
```

如果源目录已在当前工作区，也可以传相对路径；以下调用改为安装到用户范围：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"PATH","path":"downloads/review-main"},
  "action":"INSTALL_LOOSE_SKILL",
  "scope":"USER"
}
```

假设第一条调用的工作区为 /project，安装和本次必要的后续处理均成功，完整业务值为：

```json
{
  "target":{"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"},
  "meta":{"status":"SUCCESS"},
  "content":[
    {"type":"text","value":"已安装技能 review。"}
  ]
}
```

原生交付为：

```text
target: {"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"}

meta:
  status: SUCCESS

content:
  已安装技能 review。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整安装结果的导出 Target>
next: null
```

返回的 skill_path 来自实际安装结果；USER 范围的目标使用有效 Pulsara home，不硬编码默认 home。示例路径只用于说明，不由模型自行拼出安装后的 Target。

### 1.5 错误、取消与已发生效果

| 情况 | 状态与说明 |
| --- | --- |
| 参数无效，源目录不存在、不可读或缺少 SKILL.md | ERROR，说明实际涉及的路径和原因；不制造安装产物 |
| 源 name、description 或其他必要内容无效 | ERROR，说明 SKILL.md 路径和实际校验原因，不自动补写 |
| 安装目标已存在 | ERROR，说明目标路径已存在，不覆盖或把旧副本当成新产物 |
| 安装、发布或清理失败 | ERROR，写清已知效果、实际失败位置及具体原因；效果未确认时如实说明 |
| 安装已完成，但后续刷新失败 | ERROR，保留真实 SKILL Target，写清已安装及具体刷新失败，不要求重新安装 |
| 按原 owner 结算为取消 | CANCELLED，说明取消及实际已完成部分，不将已发生效果描述成未发生 |

取消不绕过安装 owner 的必要提交与清理结算；已成功完成的阶段按实际结果表达，不因收到取消信号就覆盖它们。权限与用户交互的失败同样写具体说明，不公开内部权限原因代码或表单控制字段。

例如源文件缺少说明，且尚未发布安装副本：

```text
meta:
  status: ERROR

content:
  无法安装技能：/tmp/downloads/review-main/SKILL.md 缺少必需的 description。
  未创建安装副本。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如安装完成后，来源刷新遇到已知的配置错误：

```text
target: {"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"}

meta:
  status: ERROR

content:
  已安装技能 review。
  后续能力刷新失败：/project/.pulsara/mcp.yaml 存在 YAML 语法错误：<实际解析原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如安装尚未发布时取消，且原 owner 确认没有创建安装副本：

```text
meta:
  status: CANCELLED

content:
  技能安装已取消，未创建安装副本。

tool_result: <本次完整取消结果的导出 Target>
next: null
```

以上错误与效果均为模拟，实际说明必须来自原 owner，不能固定宣称未创建或已经回滚。失败示例省略的观察时间和微秒耗时仍沿共同原生外壳并列呈现，未测得耗时不伪造为 0。具体路径、解析位置及清理问题在确实已知时给出，不只返回“安装失败”。

### 1.6 结果保存、分页与实施核对

完整结果在业务结算后保存，原生再按共同 12k 顺序交付；通常成功说明只有一页，较长错误仍完整保存。删除因结果展示预算不足而在安装前拒绝执行的旧检查。保存或交付本身失败时如实说明安装已知效果，不自动重做安装，不返回假可读结果 Target。

tool_result 指向本次完整冻结结果；next 是可原样交给 read_file 的完整 NEXT Target，内部位置透明。补页及导出只读已保存结果，不重新读取源目录、复制文件、发布安装、刷新能力或执行管理。失败结果正常保存并完整交付时，next:null 只说明结果交付完毕，不表示安装成功。

实施时将该 action 的 source_path 改为 PATH target，在进入既有安装 owner 前完成路径解析；删除模型参数中的 name、description 及对应前端工具说明，不为旧输入保留兼容分支。安装后的 SKILL Target 与能力查询共用同一形状。仅删除本模型动作的覆盖入口，不据此修改用户页面的独立编辑能力或 Skill 元数据本身。

保留源校验、资源导入、目标排他发布、真实权限、取消结算及内部必要并发核对，不因简化返回而删掉它们。内部准备、采用与执行结果转换成公开的 target／meta／text content，不回传整份 current 或 adoption 对象，也不新增代理、注册表或恢复机制。

## 2. SET_LOOSE_SKILL_ENABLED：启停独立 Skill

### 2.1 用途与管理范围

开启或关闭一份独立 Skill 副本，不修改 SKILL.md 正文，不安装或删除该副本。目标沿能力查询的最小 SKILL Target，只用具体 SKILL.md 的绝对路径定位。

runtime 根据路径和原有来源 owner 解析实际管理范围，不要求模型填 scope、source_kind、Plugin ID 或其他来源身份。保持现有独立 Skill 管理范围；Plugin 内的 Skill 和 bundled Skill 不通过本动作单独启停，具体不可操作原因由失败结果说明，不在查询结果里增加管理资格布尔值。

修改启用设置不保证该副本成为同名 Skill 的最终选中项，也不重写当前 epoch 的已安装前缀。正常采用沿既有自动路径，不要求模型例行补调 reload_capabilities。权限、必要用户交互及内部并发核对沿原 owner，不增加查询前置资格或要求模型填写核对条件。

### 2.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | SKILL Target | 必填，放首位 | 具体独立副本的 SKILL.md 绝对路径 |
| action | SET_LOOSE_SKILL_ENABLED | 必填 | 修改这份副本的启用设置 |
| enabled | boolean | 必填，无默认值 | true 开启，false 关闭这份副本 |

target 复用第 1.2 节的 InstalledSkillTarget，只含 kind:SKILL、skill_path。skill_path 是非空绝对路径，不接受 PATH Target、相对能力路径或只填名称。不接受 scope、来源字段、expected_*、额外字段或 null；enabled 只接受布尔值，不以字符串或数字代替。

```typescript
interface SetLooseSkillEnabledArgs {
  target: InstalledSkillTarget;
  action: "SET_LOOSE_SKILL_ENABLED";
  enabled: boolean;
}
```

### 2.3 精简返回值与重复设置

成功时返回与请求相同的 SKILL Target、meta.status:SUCCESS 和简短 text 内容项：“已启用技能 review。”或“已停用技能 review。”，实际名称来自本次观察。

重复设置相同值仍按正常成功结算，返回同一个 Target、SUCCESS 和同样的简短说明。不增加 changed、无变化状态或专门的重复设置字段，不要求模型先查询当前启用值再决定能否调用。

删除结果中的重复 action、current.path、current.enabled 及 current 包装；正常采用结果不另返 adoption 对象，失败及已保存事实直接写 content。不在 meta 中重新放一份 enabled 或内部并发条件。

```typescript
interface SetLooseSkillEnabledSuccess {
  target: InstalledSkillTarget;
  meta: {status: "SUCCESS"};
  content: InstallLooseSkillTextPart[];
}

interface SetLooseSkillEnabledFailure {
  target?: InstalledSkillTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: InstallLooseSkillTextPart[];
}

type SetLooseSkillEnabledResult =
  | SetLooseSkillEnabledSuccess
  | SetLooseSkillEnabledFailure;
```

失败或取消时可以保留格式合法的请求 Target，它只表示操作所指对象，不证明该路径当前存在、可读或可管理。早期参数失败无法形成合法 Target 时不伪造。content 写实际原因及已知效果，不返回文件属性或另一组错误枚举。

### 2.4 原生启停示例

启用指定副本：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"},
  "action":"SET_LOOSE_SKILL_ENABLED",
  "enabled":true
}
```

保存设置且本次必要后续处理成功时，原生结果为：

```text
target: {"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"}

meta:
  status: SUCCESS

content:
  已启用技能 review。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整启用结果的导出 Target>
next: null
```

停用指定副本：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"},
  "action":"SET_LOOSE_SKILL_ENABLED",
  "enabled":false
}
```

成功仍返回同一 Target、meta.status:SUCCESS，content 为“已停用技能 review。”。重复提交相同启停请求，若正常完成仍返回同一 Target、SUCCESS 和对应简短说明，不视为错误。

### 2.5 失败、取消与已保存但刷新失败

| 情况 | 状态与 content |
| --- | --- |
| 目标不存在、不是独立可管理副本或不在原管理范围 | ERROR，说明实际路径与不可操作原因 |
| Skill 或启用配置无法读取、校验失败 | ERROR，写实际失败路径及具体原因，不假装已修改设置 |
| 准备或执行期间实际对象、配置发生变化，内部核对失败 | ERROR，说明内容或配置已变化；核对条件由 runtime 取得，不返回或要求模型补填 |
| 设置写入失败 | ERROR，说明已知效果及实际原因；效果不确定时明确说明未确认 |
| 设置已保存，后续能力刷新失败 | ERROR，写明已保存的启用设置及刷新失败路径和具体原因 |
| 按既有取消规则结算 | CANCELLED，说明取消事实及实际已完成部分，不撤销或掩盖已保存设置 |

例如目标实际属于 Plugin，而不是独立副本：

```text
target: {"kind":"SKILL","skill_path":"/path/to/plugin/skills/review/SKILL.md"}

meta:
  status: ERROR

content:
  无法修改 /path/to/plugin/skills/review/SKILL.md 的启用设置：
  该技能属于 Plugin，本动作只支持独立 Skill 副本。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如启用设置已保存，但后续能力刷新遇到实际配置错误：

```text
target: {"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"}

meta:
  status: ERROR

content:
  技能 review 的启用设置已保存。
  后续能力刷新失败：/project/.pulsara/mcp.yaml 存在 YAML 语法错误：<实际解析原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

以上路径、失败原因及已保存事实均为模拟，实际必须沿原 owner 核实。不能因整体 ERROR 就宣称设置未修改，也不能把旧请求值当成已保存事实。取消仍按现有必要提交、清理和采用结算处理；原生观察时间与微秒耗时沿共同外壳并列呈现，未测得时不伪造。

### 2.6 结果保存、分页与实施核对

执行与原生展示分离，删除结果页预算不足时在修改前拒绝执行的旧检查；完整保存本次结果后再按共同 12k 顺序分页。NEXT 和导出只读取冻结的设置结果，不重新启停、写配置、刷新能力或重新查询实时状态。正常完整交付失败结果的 next:null 不表示操作成功；保存／交付本身失败时如实保留已知效果，不自动重做。

保持当前按 skill_path 解析实际范围，以及准备到提交之间的内部对象／启用配置核对，不因删除公开字段而去掉它们。返回值按第 2.3–2.5 节从真实保存、采用和取消结果产生，只给 Target、meta.status 与 text content；无 JS API，不增加阶段状态矩阵、资格登记或新的持久机制。

## 3. REMOVE_LOOSE_SKILL：删除独立 Skill 副本

### 3.1 删除范围与既有行为

删除一份由 Pulsara 管理的独立 Skill 副本目录，包括 SKILL.md 及随附脚本、资源；不是只删除正文文件。保留源下载目录和其他同名副本，不按技能名称批量删除。

本动作只允许删除以下两个根目录下的直接子目录，以该副本的 SKILL.md 绝对路径定位：

| 位置 | 扫描及启用机制 | 本动作删除 |
| --- | --- | --- |
| 有效 Pulsara home 的 skills/<name> | 沿现有规则 | 支持 |
| 当前 GUI 项目的 .pulsara/skills/<name> | 沿现有规则 | 支持 |
| 用户家目录的 .agents/skills/<name> | 保留扫描和 Pulsara 启停机制 | 不支持 |
| 当前 GUI 项目的 .agents/skills/<name> | 保留扫描和 Pulsara 启停机制 | 不支持 |

bundled Skill 和 Plugin 内的 Skill 均不由本动作删除。两处 .agents/skills 是可发现的共享来源，不因 Pulsara 能扫描它们就授予本管理动作删除源目录的能力；启停仍只修改 Pulsara 自己的配置，不删除或改写共享 Skill 内容。

作用域和来源由 runtime 按路径及原 owner 解析，不要求模型补填。收紧用户级 .agents/skills 的现有删除入口时，模型工具与能力页使用同一删除范围；能力页不能继续按旧规则允许删除。启停、扫描顺序、权限及采用行为保持既有契约，不将该范围规则扩展成通用文件工具或终端的新路径限制。

保留当前可清理损坏副本的能力：只要副本目录仍位于支持的直接子目录位置，即使 SKILL.md 缺失或内容无效，也可以按原目录 owner 观察和删除，不要求先成功解析元数据或通过特定工具读过正文。

### 3.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | SKILL Target | 必填，放首位 | 具体副本的 SKILL.md 绝对路径，用于定位对应目录 |
| action | REMOVE_LOOSE_SKILL | 必填 | 删除这一份受支持的独立副本 |

target 复用第 1.2 节的 InstalledSkillTarget，只含 kind:SKILL、skill_path。skill_path 必须是非空绝对路径并指向副本的 SKILL.md 位置，不接受 PATH Target、仅名称或任意目录 Target。不填写 scope、source_kind、Plugin ID、expected_* 或额外字段，不接受 null。不增加递归范围、全同名删除或清理开关。

```typescript
interface RemoveLooseSkillArgs {
  target: InstalledSkillTarget;
  action: "REMOVE_LOOSE_SKILL";
}
```

### 3.3 启用配置清理与同名副本

随删除清理这一确切 skill_path 在对应 skills.yaml 中的启用／禁用记录：用户级副本使用有效 Pulsara home 的 skills.yaml，项目副本使用该项目 .pulsara/skills.yaml。其他路径的配置记录全部保留；没有该路径的记录时无需制造一条默认记录。

清理成功后，同路径以后重新安装按默认启用规则处理，不继承旧副本残留的禁用设置。这不保证该副本成为最终选中项。删除后若还有同名较低优先级候选，继续由原 resolver 自然选择；结果只说明删除了这个副本，不宣称整个 Skill 名称已不可用。

保留现有准确目录观察、提交前重核对及删除顺序：先将所选副本移出可发现位置，再清理该路径配置和副本文件。目录内的符号链接按原删除规则作为链接处理，不沿链接删除目录外内容。准备后目录被替换时，不删除新出现的对象；同路径出现的新副本也不能被本次删除顺带清理启用配置。无法完整清理时如实报告部分效果，不新增后台清理或恢复任务。

### 3.4 精简返回值与状态

业务结果只保留 meta.status 和 text content。成功时不返回已删除对象的顶层 Target，不回显参数或另列待操作对象；失败、取消的相关路径也直接写入 content。

```typescript
interface RemoveLooseSkillTextPart {
  type: "text";
  value: string;
}

interface RemoveLooseSkillResult {
  meta: {status: "SUCCESS" | "ERROR" | "CANCELLED"};
  content: RemoveLooseSkillTextPart[];
}
```

| 情况 | meta.status 与 content |
| --- | --- |
| 副本删除、对应启用记录清理及本次必要后续处理均完成 | SUCCESS，简短说明“已删除技能副本 review。” |
| 目标不在删除范围或已不存在 | ERROR，说明实际路径及原因，不声称本次已删除 |
| 目录变化、读取或删除前核对失败 | ERROR，说明实际原因及已知效果，不删除替换后的对象 |
| 副本已移出可发现位置，但配置或文件清理失败 | ERROR，具体说明已完成部分、失败路径和实际原因 |
| 副本已删除，但后续能力刷新失败 | ERROR，说明已删除及具体刷新失败，不要求重做删除 |
| 按现有 owner 结算为取消 | CANCELLED，说明取消及实际已发生效果 |

不增加 PARTIAL 状态，不返回 action、current、operation_status、cleanup_attention、adoption、清理路径数组、内部并发身份或诊断代码。有用的清理路径和原因放在文字中；SUCCESS 不能掩盖已知清理失败，ERROR 也不能被解释为整个删除未发生或已回滚。

### 3.5 原生调用及结果示例

删除项目下的副本：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"},
  "action":"REMOVE_LOOSE_SKILL"
}
```

完整成功时：

```text
meta:
  status: SUCCESS

content:
  已删除技能副本 review。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整删除结果的导出 Target>
next: null
```

例如请求删除共享来源时，用户级和项目级 .agents/skills 均拒绝；用户级失败可以是：

```text
meta:
  status: ERROR

content:
  无法删除 /Users/example/.agents/skills/review：
  本动作只支持有效 Pulsara home 和当前项目 .pulsara/skills 中的副本；共享 .agents/skills 不在删除范围。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如副本目录已删除，但启用配置无法写入：

```text
meta:
  status: ERROR

content:
  已删除技能副本 /project/.pulsara/skills/review。
  清理启用配置失败：/project/.pulsara/skills.yaml 无写入权限。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

如果只是移出了可发现位置，文件仍未完全清理，须表达这一实际情况，而非直接宣称目录已删除：

```text
meta:
  status: ERROR

content:
  技能副本 /project/.pulsara/skills/review 已移走，相关启用设置已清理。
  仍有文件未能删除：<实际残留路径>，<具体错误原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如删除完成，但本次后续刷新失败：

```text
meta:
  status: ERROR

content:
  已删除技能副本 review，并清理其启用记录。
  后续能力刷新失败：<实际失败路径和具体原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

以上效果、路径和错误均为模拟，实际由原 owner 确认。取消时同样使用 meta.status:CANCELLED 与具体说明，未发生删除才能写“未删除”；已发生或尚未确认的效果如实表达。后续示例省略的观察时间和微秒耗时沿共同原生外壳并列呈现，不伪造为 0。

### 3.6 结果保存、分页与实施核对

删除结算与展示预算分离。移除因结果页预算不足而在删除前拒绝操作的旧检查，完整保存真实结果后再按共同 12k 顺序交付。tool_result 指向本次冻结结果，NEXT 和导出只读取它，不再次删除、清理配置、刷新能力或查询实时状态。失败结果正常完整交付时的 next:null 不表示删除成功；保存／交付本身失败仍须说明已知删除效果，不自动重做。

实施时同时收紧范围解析、实际删除 owner 和能力页的删除入口，用户级及项目级 .agents/skills 保留扫描和既有启停机制。保留精确目录核对、符号链接处理、先移出可发现位置再清理、只清理确切路径启用记录及同名候选重算等现有契约；不新增资格登记、永久墓碑或恢复机制。

当前 CLEANUP_ATTENTION 不再作为正常成功返回，应映射为 ERROR；内部仍保留真实删除效果，已移除可见副本时沿既有路径采用变化，不能因公开状态为 ERROR 就跳过必要刷新。捕获配置或文件清理异常时保留实际路径及原因用于 content，不只留下 cleanup_attention 布尔值。无 JS API。

## 4. TRUST_HOOK_SOURCE：信任当前 Hook 定义

### 4.1 用途与管理范围

为一份本地或 Plugin Hook 来源发起用户审阅，用户确认后保存对当前定义的信任。管理粒度仍是整份来源，不增加逐条 Hook 的 Target 或信任动作。Hook 定义的创建和修改继续沿现有文件／包管理路径，本动作不修改定义或执行 Hook。

保留当前完整、非空定义及可用信任存储的要求。即便完全权限模式也必须审阅当前命令、环境、事件、匹配及执行配置；模型不能用参数声明用户已经同意。重复请求信任仍沿现有审阅流程，不新增跳过审阅的规则。

信任不自动开启已关闭的 Hook，也不自动开启所属 Plugin。信任与启停分别保留现有 owner；正常可读状态下，写入信任保留原 enabled。定义变化后的信任判断、不可用状态的处理及后续生效时机均保持现有契约。

### 4.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | HOOK_SOURCE Target | 必填，放首位 | 一份确切本地或 Plugin Hook 配置来源 |
| action | TRUST_HOOK_SOURCE | 必填 | 审阅并信任当前定义 |

target 与能力查询共用 kind、config_path，config_path 为配置文件的非空绝对路径。runtime 按路径解析既有来源身份、管理范围及 Plugin 归属；不要求模型填写 scope、source_kind、plugin_id、摘要、expected_* 或同意标志。不接受额外字段、null 或任意路径文件作为可管理来源，不因精简 Target 扩大原管理范围。WORKSPACE 管理仍要求当前 GUI 项目，不从 terminal cwd 推导项目。

```typescript
type HookSourceTarget = {kind: "HOOK_SOURCE"; config_path: string};

interface TrustHookSourceArgs {
  target: HookSourceTarget;
  action: "TRUST_HOOK_SOURCE";
}
```

### 4.3 精简返回值与共同类型

三项 Hook action 共用以下业务结果形状：成功返回同一 HOOK_SOURCE Target、meta.status:SUCCESS 和简短 text content；失败或取消使用 ERROR／CANCELLED，具体原因、路径及已知效果放 content。

```typescript
interface HookManagementTextPart {
  type: "text";
  value: string;
}

interface HookManagementSuccess {
  target: HookSourceTarget;
  meta: {status: "SUCCESS"};
  content: HookManagementTextPart[];
}

interface HookManagementFailure {
  target?: HookSourceTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: HookManagementTextPart[];
}

type HookManagementResult = HookManagementSuccess | HookManagementFailure;
type TrustHookSourceResult = HookManagementResult;
```

信任成功 content 为“已信任该来源的当前 Hook 定义。”。删除公开 action、current.path、current.enabled、current.trust_revoked、current 和 adoption 包装，不在 meta 重建信任／启停状态矩阵，不向模型返回定义摘要、内部时间或审阅接受标志。

失败可保留格式合法的请求 Target，它只定位本次请求对象，不证明来源存在、可读或可管理；无法形成合法 Target 时不伪造。观察时间和微秒耗时沿共用原生外壳顶层并列，不进入业务 meta。成功与失败的保存、分页及导出均沿统一规则。

### 4.4 原生调用与结果示例

```text
工具：manage_capability
参数：
{
  "target":{"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"},
  "action":"TRUST_HOOK_SOURCE"
}
```

用户审阅确认、信任保存及本次必要后续处理成功后：

```text
target: {"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"}

meta:
  status: SUCCESS

content:
  已信任该来源的当前 Hook 定义。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整信任结果的导出 Target>
next: null
```

用户取消审阅、信任尚未保存时：

```text
target: {"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"}

meta:
  status: CANCELLED

content:
  已取消 /project/.pulsara/hooks.json 的信任审阅，未保存本次信任。

tool_result: <本次完整取消结果的导出 Target>
next: null
```

### 4.5 错误、取消与已发生效果

| 情况 | 状态与 content |
| --- | --- |
| 目标缺失、不可管理、定义无效、不完整或为空，或信任存储不可用 | ERROR，给出实际配置路径及具体原因，不声称已经信任 |
| 用户取消审阅，尚未保存信任 | CANCELLED，说明未完成本次信任 |
| 审阅／准备后来源、定义、信任状态或相关 Plugin 状态变化，内部核对失败 | ERROR，说明当前内容或状态已经变化，不将旧审阅用于另一份定义 |
| 保存失败 | ERROR，说明实际原因及已知效果；保存结果不确定时说明未确认，不自动重做 |
| 信任已保存，后续能力刷新失败 | ERROR，写明信任已保存，以及刷新失败路径和具体原因 |
| 其他取消 | 按现有结算规则使用 CANCELLED，说明实际完成部分，不掩盖已保存效果 |

例如信任已经保存，但后续刷新失败：

```text
target: {"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"}

meta:
  status: ERROR

content:
  已信任 /project/.pulsara/hooks.json 的当前 Hook 定义。
  后续能力刷新失败：<实际失败路径>，<具体原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

所有示例中的路径、原因和效果仅为模拟，实际必须由原 owner 确认。不能因整体 ERROR 宣称信任未保存，也不能仅凭请求值宣称保存成功。其他 Hook action 沿相同的实际效果与错误表达规则，不增加公开错误代码或部分成功状态。

### 4.6 结果保存、分页与实施核对

操作与展示分离，删除因结果展示预算不足而在信任前拒绝执行的旧检查；结算后完整保存结果，原生按共同 12k／NEXT 分页。NEXT 和导出只读冻结结果，不重新弹审阅、授予信任、执行 Hook 或刷新能力。正常完整交付失败结果的 next:null 不表示信任成功；保存或交付本身失败时如实报告已知效果，不返回假可读结果 Target，不自动重做业务。

在原来源 owner 内解析最小 Target，保留当前用户审阅、提交前来源及信任状态重核对、Plugin 安装身份核对、信任写入和副作用结算。必要信任材料留在内部，不因删除公开字段而删除。管理调用本身继续使用捕获的前置 Hook 视图，变化按现有后续事件规则生效，不重建当前 epoch 的 provider 前缀。无 JS API，不新增资格登记、信任注册表或恢复任务。

## 5. REVOKE_HOOK_TRUST：撤销 Hook 信任

### 5.1 用途与既有行为

撤销一份本地或 Plugin Hook 来源的信任，清除当前信任记录，不删除定义、脚本或安装包。正常可读状态下保留 enabled，不把撤销信任改成关闭来源；之后重新信任仍须沿 TRUST_HOOK_SOURCE 的用户审阅。

撤销不要求再次进行完整的信任审阅，沿原权限路径及内部对象核对处理，不扩大权限。来源缺失、损坏或状态不可用时，能否操作由现有 owner 判断，不新增必须先读取正文或先查询的资格要求。

### 5.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | HOOK_SOURCE Target | 必填，放首位 | 要撤销信任的确切来源 |
| action | REVOKE_HOOK_TRUST | 必填 | 清除该来源的信任 |

Target 及来源解析沿第 4.2 节，不填写 scope、来源身份、Plugin ID、摘要、expected_*、enabled 或额外字段。

```typescript
interface RevokeHookTrustArgs {
  target: HookSourceTarget;
  action: "REVOKE_HOOK_TRUST";
}

type RevokeHookTrustResult = HookManagementResult;
```

### 5.3 精简返回值

复用第 4.3 节的共同结果类型。成功只返回同一 Target、SUCCESS 和“已撤销该来源的 Hook 信任。”。删除 current.trust_revoked 等重复事实及 adoption 包装，不增加新的未信任状态枚举。

已经没有信任记录时，若原 owner 正常完成撤销，仍返回 SUCCESS 和同样说明；不新增 changed 或重复撤销状态。内部核对或写入失败仍按实际错误报告。

### 5.4 原生调用与结果示例

```text
工具：manage_capability
参数：
{
  "target":{"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"},
  "action":"REVOKE_HOOK_TRUST"
}
```

```text
target: {"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"}

meta:
  status: SUCCESS

content:
  已撤销该来源的 Hook 信任。

tool_result: <本次完整撤销结果的导出 Target>
next: null
```

示例省略的观察时间与微秒耗时沿共同外壳并列呈现，不伪造数值。

### 5.5 失败、取消与已保存效果

沿第 4.5 节的共同规则：目标或状态无法核实、内部冲突、写入失败用 ERROR；取消沿原结算；实际原因和路径放 content。撤销已保存但刷新失败时，明确“该来源的 Hook 信任已撤销”，再说明刷新失败原因。不能因为刷新失败宣称旧信任仍存在，也不自动重新信任或重做撤销。

### 5.6 结果保存、分页与实施核对

沿第 4.6 节完整保存与冻结分页，删除旧展示预算前置拒绝；NEXT／导出不再次撤销信任、写配置或刷新。保留现有来源、Plugin 及信任状态核对、必要提交与取消结算、自动采用和后续事件生效行为。已保存的实际效果不因公开 ERROR 而丢失必要后续处理；不增加确认回执或后台恢复路径。仅 ROOT 原生，无 JS API。

## 6. SET_HOOK_SOURCE_ENABLED：启停 Hook 来源

### 6.1 用途与既有行为

开启或关闭一份本地或 Plugin Hook 来源。正常可读状态下只修改 enabled，保留原有信任材料；同一安装及相关定义未变时，关闭再开启不要求重新信任。定义变化后的信任判断仍沿原契约，不能用开启动作跳过审阅。

开启不建立信任，也不自动开启所属 Plugin；执行继续受现有定义有效性、信任和 Plugin 启用等条件约束。关闭不删除配置或脚本，不撤销信任。启停本身不要求再次完整审阅定义，沿现有权限及内部核对路径处理。

### 6.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | HOOK_SOURCE Target | 必填，放首位 | 要启停的确切来源 |
| action | SET_HOOK_SOURCE_ENABLED | 必填 | 修改来源的启用设置 |
| enabled | boolean | 必填，无默认值 | true 开启，false 关闭 |

Target 及来源解析沿第 4.2 节。enabled 只接受布尔值，不接受字符串、数字或 null；不填写 scope、source_kind、plugin_id、expected_*、同意标志或额外字段。

```typescript
interface SetHookSourceEnabledArgs {
  target: HookSourceTarget;
  action: "SET_HOOK_SOURCE_ENABLED";
  enabled: boolean;
}

type SetHookSourceEnabledResult = HookManagementResult;
```

### 6.3 精简返回值与重复设置

复用第 4.3 节的共同结果类型。成功返回同一 Target、SUCCESS，以及“已启用该来源的 Hook。”或“已停用该来源的 Hook。”。重复设置相同值仍按正常成功结算，不增加 changed、无变化状态或查询前置条件。

删除 current.enabled、current.path、重复 action 和 adoption 包装，不另给信任与启停的组合字段。说明仅表示本次设置完成，不将开启解释为已经建立信任或实际执行成功。

### 6.4 原生调用与结果示例

停用来源：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"},
  "action":"SET_HOOK_SOURCE_ENABLED",
  "enabled":false
}
```

```text
target: {"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"}

meta:
  status: SUCCESS

content:
  已停用该来源的 Hook。

tool_result: <本次完整启停结果的导出 Target>
next: null
```

重新开启：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"HOOK_SOURCE","config_path":"/project/.pulsara/hooks.json"},
  "action":"SET_HOOK_SOURCE_ENABLED",
  "enabled":true
}
```

成功返回同一 Target、SUCCESS，content 为“已启用该来源的 Hook。”。Plugin 来源同样复制其查询返回的 HOOK_SOURCE Target，不添加 plugin_id。观察时间和微秒耗时沿共同外壳并列。

### 6.5 失败、取消与已保存效果

沿第 4.5 节的共同规则：不可管理、无法核实当前状态、内部冲突及保存失败返回 ERROR，原因与路径放 content；取消保留实际效果。不因启停入口存在而绕过原 owner 对缺失、损坏或不可用来源的处理。

设置已保存但刷新失败时，写明“该来源的 Hook 停用设置已保存。”或对应开启说明，再给出实际刷新失败原因。保存结果不确定时说明未确认，不把请求值冒充当前状态，不自动重做。

### 6.6 结果保存、分页与实施核对

沿第 4.6 节完整保存与冻结分页，删除旧展示预算前置拒绝；NEXT／导出不再次启停、写配置、刷新或查询实时状态。保留原启停与信任的独立存储语义、内部状态重核对、不可用信任存储的既有处理及必要取消结算，不改为统一 trust 系统。仅 ROOT 原生，无 JS API。

## 7. MCP 连接的共用 config：ADD／UPDATE

### 7.1 用途与模型字段

本章确定 ADD_LOCAL_MCP、UPDATE_MCP_CONNECTION 共用的模型 config 字段。ADD 只新增独立连接；UPDATE 同时修改独立和 Plugin 连接，具体可修改字段由实际连接归属决定。两个 action 的完整合同分别见第 8、9 章。

config 是模型提供的公开配置候选，不再等同于底层完整 native entry。runtime 补齐新增默认值，必要的秘密输入、私密 Header 及特殊 OAuth 注册信息沿现有用户编辑器补齐，再由原 owner 校验并保存完整配置。不让模型构造内部凭据引用，不新增另一套配置存储或凭据 owner。

| 字段 | ADD 必填与默认 | 明确含义 |
| --- | --- | --- |
| enabled | 可选，默认 true | 是否启用，继续支持 true／false |
| transport.type | 提交 config 时必填 | stdio／streamable_http／sse，明确选择传输方式 |
| transport.command | stdio 必填 | 可执行程序或启动器，不是 shell 命令串 |
| transport.args | stdio 可选，默认 [] | 有序字符串参数数组，保留每个参数的边界 |
| transport.env | stdio 可选，默认 {} | 非秘密环境变量，不假设子进程继承完整 shell 环境 |
| transport.cwd | stdio 可选，默认工作区根 | 沿现有规则使用工作区相对路径，不从 terminal 的 cd 重新解释 |
| transport.endpoint | HTTP／SSE 必填 | 实际服务地址，保留所选路径及非秘密 query |
| public_headers | HTTP／SSE 可选，默认 {} | 服务需要的非秘密请求头，不放进 transport 对象 |
| auth.type | 可选，默认 none | none／oauth／bearer／static_headers；只表达认证方式 |

不公开 display_name；新增连接直接以 server_id 作为显示名称。stdio 不接受 endpoint 或 HTTP public_headers，HTTP／SSE 不接受 command、args、env、cwd；使用传输分支校验，不根据服务名或 URL 外形猜测类型，不自动将一种传输失败改成另一种传输重试。

模型只提交上述字段。不接受 scope_policy、effect_policy 等高级策略、内部凭据引用、明文秘密或额外字段。参数类型及实际值沿原 owner 校验。audience 是 MCP 返回内容的受众标注，按统一交付草案接入过滤，不属于本 config。

### 7.2 公开类型

```typescript
interface McpLocalStdioConfig {
  type: "stdio";
  command: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  endpoint?: never;
}

interface McpLocalHttpConfig {
  type: "streamable_http" | "sse";
  endpoint: string;
  command?: never;
  args?: never;
  env?: never;
  cwd?: never;
}

interface McpLocalAuthSelection {
  type?: "none" | "oauth" | "bearer" | "static_headers";
}

interface McpLocalPublicSettings {
  enabled?: boolean;
  auth?: McpLocalAuthSelection;
}

type McpLocalConfigForAdd = McpLocalPublicSettings & (
  | {transport: McpLocalStdioConfig; public_headers?: never}
  | {transport: McpLocalHttpConfig; public_headers?: Record<string, string>}
);

interface McpStdioTransportPatch {
  type?: "stdio";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  endpoint?: never;
}

interface McpHttpTransportPatch {
  type?: "streamable_http" | "sse";
  endpoint?: string;
  command?: never;
  args?: never;
  env?: never;
  cwd?: never;
}

interface McpConnectionConfigPatch {
  enabled?: boolean;
  transport?: McpStdioTransportPatch | McpHttpTransportPatch;
  public_headers?: Record<string, string>;
  auth?: McpLocalAuthSelection;
}
```

ADD 提交 config 时必须有完整 transport；UPDATE 可以只提供本次要改的嵌套字段，例如 transport.env 或 transport.endpoint。UPDATE 类型描述共同字段，runtime 结合现有传输和独立／Plugin 归属校验实际可修改部分；不能因类型中存在某字段就允许修改包定义。除 Plugin 的整个 config:null 外，字段不接受 null。空映射、空数组和字段省略具有不同含义。

### 7.3 ADD 默认值与 UPDATE 保留规则

| 输入情况 | ADD | UPDATE |
| --- | --- | --- |
| 未填 enabled | true | 保持原值 |
| 未填 auth | none | 保持原认证配置及凭据 |
| 提供 auth，但未填 type | none | 保持原值；只有明确填写 type 才修改认证方式 |
| 未填 public_headers | {} | 保持原值；切换传输时校验适用性 |
| 未填 transport | 提交 config 时不成立 | 保持原值 |
| 提供 transport | 校验所选分支 | 按明确提供的字段局部更新，未填的嵌套字段保持 |

UPDATE 在同一传输分支内不为省略的字段重新填 ADD 默认值。独立连接显式改变 transport.type 时，必须提供新分支的必填字段；其余按新分支默认值和现有适用性规则处理，不继承不适用的字段。

env／public_headers 映射仍整体替换本次可编辑的公开映射，不逐键合并；{} 表示清空这份映射，args:[] 表示空参数数组。Plugin 清空的是用户覆盖映射，包及输入默认值仍按现有覆盖逻辑参与合成，不表示删除包默认值。省略映射则保持原覆盖。

config 对象局部更新只修改模型明确提供的公开设置，不能因为未填字段就把 enabled 恢复为 true、认证恢复为 none，或把用户高级设置重置。未提供 display_name 也不成为重置原用户显示设置的理由。Plugin 的可修改边界及 config:null 见第 9.1 节；第 7.1 节的 ADD 默认值不适用于 Plugin 修改。

config 对象局部更新时，用户保存的高级策略、私密环境变量、私密 Header、OAuth 注册信息等不属于这份模型对象；必要且仍适用的值由原 owner 保留，不用模型对象替换整份底层 entry。连接目的地或认证方式变化时，继续沿现有凭据目的地确认、适用性校验及清理流程处理；不将不适用的旧配置硬塞到新传输，也不静默携带秘密到另一目的地。

### 7.4 认证、用户高级设置与子代理范围

auth 只让模型选择方式，不要求模型填写密钥、私密 Header 名称和值、client secret、特殊注册和回调参数。需要这些材料时由现有编辑器补齐，沿原凭据存储及 OAuth 登录流程处理。重复选择同一认证方式不因只提供 type 就清空原凭据；显式切换方式仍按原 owner 处理旧凭据，不创建临时凭据拼装接口。

stdio 的凭据仍由现有私密环境变量路径处理，不能伪装成 HTTP auth；公开 env 和 public_headers 不得夹带秘密。auth 缺省 none 只表示配置默认，不表示自动识别 OAuth、已经登录或服务无需认证。服务把秘密放在 URL query 或命令行参数中的示例不能直接作为公开 endpoint／args 接受；当前支持边界如实报告，不在本章新增秘密 URL／argv 机制。

以下设置留给用户，不进入模型 config：子代理范围、工具筛选与效果分类、并发及无状态声明、目录刷新、默认和逐工具超时、本机明文 HTTP 许可，以及复杂认证设置。它们继续由既有配置及用户编辑器持有，不另存一份高级配置副本。

独立及 Plugin MCP 连接默认都允许 ROOT 和子代理使用，即 scope_policy:ROOT_AND_SUBAGENTS。该默认统一用于新增独立连接、Plugin 连接没有显式范围覆盖时，以及创建连接覆盖但未指定范围时；用户可在能力页关闭子代理使用，保存为 ROOT_ONLY。模型没有 scope_policy 参数，config 对象局部更新保留用户已保存的范围；Plugin 的 config:null 则移除整份连接覆盖，其中的范围覆盖一并移除，恢复 ROOT_AND_SUBAGENTS 默认值。

ROOT 与子代理均通过 call_mcp_tool 调用其范围内的远端工具，不向任一方安装 MCP direct 工具。引用在各自 session 内供原生与适用的 Cell 共用，不跨 ROOT／子代理 session 借用；这不向子代理开放 Code Mode 或本管理工具。

默认使用范围不代表连接已启用、Plugin 已开启、Hook 已信任或 OAuth 已授权，具体调用仍沿现有权限及来源准入。显式保存的 ROOT_ONLY 不因普通局部更新、查询或刷新被重置。默认值在配置、Plugin 合成及用户编辑器中保持一致，不新增范围字段或并列状态。

配置采用和重连后，调用引用按实际目标与该工具依据核对：换 endpoint、执行账户、来源或改变该工具策略时，受影响旧引用不能沿用；同目标普通重连完成发现且定义、策略未变时可复用。修改显示名称、其他工具的设置或刷新间隔，不仅因整份配置标识变化就使未变工具引用失效；复用配置和连接 owner 的实际事实，不新增身份体系。原生兼容、降级及恢复规则删除，现有发现与刷新保留，完整规则见[架构草案第 4 章](../archived_docs/PULSARA_MCP_META_ONLY_CALLING_ARCHITECTURE_DRAFT.zh.md#4-能力发现与模型上下文)。

### 7.5 config 片段示例与实施核对

以下仅展示 action 参数中的 config 片段，不是完整工具调用，也不是 JS API。完整 action 参数与结果见第 8、9 章。

新增 stdio 配置：

```text
"config": {
  "transport": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/project"]
  }
}
```

新增远程 OAuth 配置；账户授权仍沿既有流程完成：

```text
"config": {
  "transport": {
    "type": "streamable_http",
    "endpoint": "https://api.greptile.com/mcp"
  },
  "auth": {"type": "oauth"}
}
```

UPDATE 只停用独立连接，保留原 transport、认证、公开 Header 及用户高级设置：

```text
"config": {
  "enabled": false
}
```

UPDATE 只修改 HTTP／SSE 连接入口，保持原传输类型及其他未填字段；原有用户配置仍按适用性及原凭据确认规则处理：

```text
"config": {
  "transport": {
    "endpoint": "https://example.org/mcp"
  }
}
```

UPDATE 清空 HTTP／SSE 连接的公开 Header，不清空私密 Header：

```text
"config": {
  "public_headers": {}
}
```

实施时模型候选适配与完整配置存储分别承担上述职责，复用现有 parser、配置 owner、用户编辑器和凭据边界。不能继续把精简候选直接作为 UPDATE 的完整 native entry 保存，也不保留旧模型完整配置参数的兼容旁路。只修改公开字段与连接范围默认值，不据此删除用户高级能力、改变当前 epoch 的 SYSTEM／provider tools 或重写已安装消息。

## 8. ADD_LOCAL_MCP：添加独立 MCP 连接

### 8.1 用途与新增行为

在选定的独立 MCP 配置位置新增一个 server_id。已有同键连接时失败，不覆盖；更改已有连接使用 UPDATE_MCP_CONNECTION。只管理原 owner 支持的用户配置及当前 GUI 项目配置，不添加或修改 Plugin 包中的连接。

提供 config 时按第 7 章接纳模型候选并补齐默认值；需要秘密、特殊认证配置或必要确认时仍进入现有用户编辑器。省略 config 则打开新增连接编辑器，由用户填写。打开编辑器不等于添加完成；最终结果在实际提交或取消结算后产生。账户 OAuth 登录沿现有授权流程，不将添加配置写成已经登录或调用成功。

沿现有权限、来源准入及副作用判断处理，不将本动作限制为 reload_capabilities 的仅 bypass 模式，也不因为使用最小 Target 扩大文件写入范围。正常采用继续自动进行，不要求固定后继调用 reload_capabilities。

### 8.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MCP_CONNECTION Target | 必填，放首位 | 准备新增的独立连接位置：config_path＋server_id |
| action | ADD_LOCAL_MCP | 必填 | 新增连接，不覆盖已有同键配置 |
| config | 第 7 章的 ADD 公开配置候选 | 可选，无默认对象 | 提供时必须包含 transport；省略打开新增编辑器 |

Target 复用能力查询的 kind、config_path、server_id。config_path 为原 owner 支持的配置文件绝对路径，server_id 为文件内的非空连接键，不是运行实例 ID。新增前可以尚无该键或配置文件；模型指定的是新增位置，不需先伪造已有连接。

runtime 按实际配置路径解析 USER 或当前 GUI 项目范围，不要求 scope、source_kind 或 Plugin ID。不接受任意配置文件位置，不从 terminal cwd 改换项目；项目配置仍须有实际 GUI 项目。config 省略与 null 不同，null 不接受。不填写 expected_identity、其他 expected_*、display_name、额外参数或内部凭据值。

```typescript
type McpConnectionTarget = {
  kind: "MCP_CONNECTION";
  config_path: string;
  server_id: string;
};

interface AddLocalMcpArgs {
  target: McpConnectionTarget;
  action: "ADD_LOCAL_MCP";
  config?: McpLocalConfigForAdd;
}
```

### 8.3 精简返回值与共同类型

ADD／UPDATE 成功均只返回同一 MCP_CONNECTION Target、meta.status:SUCCESS 和简短 text content，不回传完整配置。新增成功说明为“已添加 MCP 连接 docs。”，连接键使用实际 server_id。

```typescript
interface McpConnectionManagementTextPart {
  type: "text";
  value: string;
}

interface McpConnectionManagementSuccess {
  target: McpConnectionTarget;
  meta: {status: "SUCCESS"};
  content: McpConnectionManagementTextPart[];
}

interface McpConnectionManagementFailure {
  target?: McpConnectionTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: McpConnectionManagementTextPart[];
}

type McpConnectionManagementResult =
  | McpConnectionManagementSuccess
  | McpConnectionManagementFailure;
type AddLocalMcpResult = McpConnectionManagementResult;
```

删除公开的重复 action、current.config_path、cleanup_attention、current 及 adoption 包装；状态统一为 SUCCESS／ERROR／CANCELLED，不再返回 APPLIED／CONFLICT／REJECTED 等另一套公开状态。具体失败、路径、清理问题和已保存效果直接放 content，不另加连接可用、登录完成或高级配置统计字段。

失败可以保留格式合法的请求 Target，它表示本次操作所指位置，不证明连接已经创建或仍然存在；无法形成合法 Target 时不伪造。成功仅在新增配置及本次必要处理实际完成后返回，不以弹出编辑器或收到提交作为成功依据。

### 8.4 原生调用与结果示例

提供已知公开配置；以下地址为模拟：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"},
  "action":"ADD_LOCAL_MCP",
  "config":{
    "transport":{
      "type":"streamable_http",
      "endpoint":"https://example.org/mcp"
    }
  }
}
```

省略 config，由用户在编辑器填写：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"},
  "action":"ADD_LOCAL_MCP"
}
```

两条路径正常完成后的结果形状相同：

```text
target: {"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"}

meta:
  status: SUCCESS

content:
  已添加 MCP 连接 docs。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整新增结果的导出 Target>
next: null
```

### 8.5 错误、取消与已保存效果

| 情况 | 状态与 content |
| --- | --- |
| 同一配置位置已有该 server_id | ERROR，说明实际文件与连接键已经存在，不覆盖，不自动改用 UPDATE |
| 配置位置不可管理、配置无效或无法读取／保存 | ERROR，说明实际路径及具体原因，不假装已添加 |
| 准备到提交之间状态变化，内部核对失败 | ERROR，说明配置已变化或同键连接已出现；不要求模型填写内部条件 |
| 用户取消编辑器，尚未保存 | CANCELLED，说明未添加 |
| 新增已保存，必要的后续采用或清理失败 | ERROR，写明已保存的事实，再给失败路径及实际原因 |
| 保存结果不确定 | ERROR，说明新增状态未确认，不直接宣称已添加或未添加，不自动重做 |
| 其他取消 | 沿现有提交与取消结算，保留实际完成效果 |

例如已保存但后续处理失败：

```text
target: {"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"}

meta:
  status: ERROR

content:
  MCP 连接 docs 的配置已保存到 /project/.pulsara/mcp.yaml。
  后续能力刷新失败：<实际失败路径>，<具体原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

配置添加、远端连接、目录发现、OAuth 登录及实际调用是不同事实。添加不强制执行探测调用，也不因当前尚未登录或连接尚未就绪而谎报保存失败；本次必要处理确实失败时按上述 ERROR 表达。以上路径、原因及效果为模拟，实际由原 owner 确认。

### 8.6 结果保存、分页与实施核对

业务执行与展示分离，删除结果页预算不足时在添加前拒绝执行的旧检查。结算后完整保存结果，原生按 12k／NEXT 分页；通常简短成功结果一页即可。NEXT 和导出只读冻结结果，不重新打开编辑器、添加配置、保存凭据、发起授权或刷新能力。失败结果正常完整交付时 next:null 不表示新增成功；保存或交付本身失败仍须说明已知效果，不返回假可读结果 Target，不自动重做。

在既有 owner 内解析最小 Target，保留同键排他新增、权限效果、来源确认、秘密输入、内部提交核对及副作用结算。按第 7 章保存完整业务配置，新增子代理范围默认值与用户能力页一致，不新增独立配置实体、资格登记或恢复任务。仅 ROOT 原生，无 JS API；当前 epoch 的 provider 前缀保持既有连续性。

## 9. UPDATE_MCP_CONNECTION：修改独立或 Plugin MCP 连接

### 9.1 用途、局部更新与修改边界

修改已存在的独立或 Plugin MCP 连接，不新增缺失连接，不移动配置文件或修改 server_id。提供 config 对象时按第 7 章局部修改；省略 config 打开该连接原有的预填用户编辑器，沿原流程提交或取消，不把打开编辑器当作修改完成。

未填字段保持原值，包括 transport 内未填的字段；env／public_headers 仍按第 7.3 节整体替换对应公开映射。两类连接共用字段名称，但修改边界如下：

| 字段 | 独立连接 | Plugin 连接 |
| --- | --- | --- |
| enabled | 可修改 | 不提供此入口；整个 Plugin 的启停沿 SET_PLUGIN_ENABLED |
| transport.type | 可修改，切换时提供新分支必填字段 | 包定义，不可修改 |
| transport.command／args／cwd | stdio 可修改 | 包定义，不可修改 |
| transport.env | stdio 可修改公开环境变量 | stdio 可修改公开环境变量覆盖，保留包的保留变量约束 |
| transport.endpoint | HTTP／SSE 可修改 | HTTP／SSE 可修改连接地址覆盖 |
| public_headers | HTTP／SSE 可修改 | HTTP／SSE 可修改公开 Header 覆盖 |
| auth.type | 沿实际传输支持的认证方式修改 | 沿原连接覆盖与输入默认值规则修改，不新增认证能力 |

Plugin 的 config:null 表示移除这条连接的整份用户覆盖，恢复包及输入默认值；范围覆盖也一并移除，按第 7.4 节恢复为允许 ROOT 和子代理使用的 ROOT_AND_SUBAGENTS。不是删除连接、关闭 Plugin 或清除全部凭据。独立连接不接受 config:null，不另存一份“初始配置”来提供恢复默认能力。Plugin 显式 auth.type:none 继续按原覆盖 owner 的含义合成；存在包输入默认认证时，不把它改成强制关闭默认认证。仅更新 env 或 endpoint 不要求重填包的传输类型。

config 对象局部更新时，用户高级策略及适用私密配置仍由原 owner 保留，精简 config 不作为完整底层 entry 或 overlay 直接覆盖保存。Plugin 仍通过现有包定义、输入默认值及实例覆盖合成有效连接；局部候选只适配到该 owner 已有的完整覆盖值。config:null 不适用局部更新的覆盖保留承诺，凭据清理只按恢复后实际引用处理，不增加清空全部秘密的语义。目的地、认证或秘密变化继续沿原凭据确认、OAuth 失效与清理流程处理。

### 9.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MCP_CONNECTION Target | 必填，放首位 | 已存在的独立或 Plugin 连接：config_path＋server_id |
| action | UPDATE_MCP_CONNECTION | 必填 | 修改该连接 |
| config | 第 7 章的局部公开配置候选，或 null | 可选，无默认对象 | 对象只修改明确提供的字段；省略打开预填编辑器；null 仅用于 Plugin 恢复默认 |

两类连接的 Target 形状相同：config_path 是实际配置文件的绝对路径，server_id 是文件内连接键。独立连接指向原 owner 支持的用户／项目配置文件，Plugin 连接指向当前安装包的实际 MCP 配置文件；runtime 解析归属，不增加 scope、source_kind 或 Plugin ID。Plugin 替换后应使用查询返回的当前 Target，不把旧包路径自动重定向到新包。

删除模型填写的 expected_identity、expected_overlay、expected_package_install_id 等条件；runtime 在本次准备时取得真实配置、包及覆盖核对条件，提交时由原 owner 核对。不新增必须先查询的资格，不要求模型记忆内部值，也不开放任意配置路径。

```typescript
interface UpdateMcpConnectionArgs {
  target: McpConnectionTarget;
  action: "UPDATE_MCP_CONNECTION";
  config?: McpConnectionConfigPatch | null;
}

type UpdateMcpConnectionResult = McpConnectionManagementResult;
```

config 对象的类型表示共同字段；具体传输、包归属及 null 的适用性由 runtime 校验，非法字段组合直接返回 ERROR，不静默忽略。

### 9.3 精简返回值与重复提交

复用第 8.3 节的共同结果类型。成功只返回同一 Target、meta.status:SUCCESS 和“已更新 MCP 连接 docs。”；Plugin 恢复默认时说明“已恢复 MCP 连接 docs 的默认配置。”。不回传配置、enabled、认证、内部核对值、current 或 adoption。

重复提交相同配置，若原 owner 正常完成，仍返回 SUCCESS、同一 Target 及对应简短说明，不增加 changed、无变化状态或额外查询步骤。不为结果文案另建无变化检测机制。

### 9.4 原生调用与结果示例

只停用独立连接，其他配置不变：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"},
  "action":"UPDATE_MCP_CONNECTION",
  "config":{"enabled":false}
}
```

只修改 Plugin 的 stdio 公开环境变量覆盖，不重填包内命令、参数和传输类型。下列路径为模拟的当前安装包配置路径：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/plugins/example/current-package/mcp.json","server_id":"docs"},
  "action":"UPDATE_MCP_CONNECTION",
  "config":{"transport":{"env":{"LOG_LEVEL":"info"}}}
}
```

省略 config，打开对应连接的预填编辑器：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"},
  "action":"UPDATE_MCP_CONNECTION"
}
```

正常完成后的结果：

```text
target: {"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"}

meta:
  status: SUCCESS

content:
  已更新 MCP 连接 docs。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整修改结果的导出 Target>
next: null
```

恢复 Plugin 连接默认配置：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/plugins/example/current-package/mcp.json","server_id":"docs"},
  "action":"UPDATE_MCP_CONNECTION",
  "config":null
}
```

```text
target: {"kind":"MCP_CONNECTION","config_path":"/plugins/example/current-package/mcp.json","server_id":"docs"}

meta:
  status: SUCCESS

content:
  已恢复 MCP 连接 docs 的默认配置。

tool_result: <本次完整修改结果的导出 Target>
next: null
```

### 9.5 错误、取消与已保存效果

目标不存在、位置不可管理、候选无效、修改包定义字段、独立连接传 config:null、内部并发冲突，以及写入或必要清理／刷新失败均为 ERROR；取消沿原结算。连接不存在时说明实际路径和 server_id，不自动改为 ADD。

例如准备后配置发生变化，本次修改未提交：

```text
target: {"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"}

meta:
  status: ERROR

content:
  /project/.pulsara/mcp.yaml 中 MCP 连接 docs 的配置已变化，本次修改未提交。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

用户取消编辑器且未保存时使用 CANCELLED，说明未修改。修改已保存但后续失败时，先说明“连接配置已更新”或“连接默认配置已恢复”，再给实际失败原因；保存状态不确定时说明未确认，不把旧值或请求值当作已保存事实，不自动重新修改。未提交结论仅在原 owner 已确认时使用。

### 9.6 结果保存、分页与实施核对

完整保存本次结果，再按冻结 12k／NEXT 分页；删除展示预算前置拒绝。NEXT／导出只读取这份冻结结果，不重新打开编辑器、查询实时配置、修改连接、恢复默认、写凭据、授权或刷新。实际业务效果不因后续公开 ERROR 而失去必要的内部采用与结算。

统一模型 action 和 config 参数，独立配置与 Plugin 实例覆盖仍由各自现有 owner 提交。保留原覆盖合成、包身份及旧覆盖核对、凭据目的地确认、秘密保存与清理、OAuth 登录／刷新失效和等待、运行连接采用及引用退出语义，不合并底层存储或凭据归属。删除被统一的旧模型修改入口，不保留别名或兼容分支；不新增永久快照表、模型已读资格或恢复任务。仅 ROOT 原生，无 JS API；不重写当前 epoch 的 provider 前缀。

## 10. REMOVE_LOCAL_MCP：删除独立 MCP 连接

### 10.1 用途与删除范围

删除指定 config_path 中的一个独立 server_id，保留其他连接配置，不删除整个配置文件、MCP 程序、下载源或远端服务。不通过本动作删除 Plugin 提供的连接。目标不存在或无法按原 owner 核实当前配置时返回错误，不凭空构造旧配置或扫描删除其他凭据。

配置、凭据、OAuth 及运行连接的清理完全保持现有底层语义。本章只精简模型参数和公开结果，将实际部分清理失败映射为 ERROR；不改变清理范围、顺序、取消结算、连接退出方式或已经接纳操作的生命周期。

### 10.2 最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MCP_CONNECTION Target | 必填，放首位 | 要删除的独立连接：config_path＋server_id |
| action | REMOVE_LOCAL_MCP | 必填 | 删除连接及其既有关联清理 |

Target 与位置解析沿第 8.2 节，只接受原 owner 支持的用户配置或当前 GUI 项目配置位置。模型不填写 scope、source_kind、Plugin ID、config、expected_identity、其他 expected_* 或额外字段，不接受 null。内部核对条件由 runtime 在本次准备时取得，并在提交时沿原 owner 核对；不要求模型先查询、保存内部值或取得已读资格。

```typescript
interface RemoveLocalMcpArgs {
  target: McpConnectionTarget;
  action: "REMOVE_LOCAL_MCP";
}
```

### 10.3 凭据、OAuth 与运行连接：保留当前底层语义

本文件所述“引用退出／释放”指运行连接、包及已接纳操作持有的物理使用引用。模型持有 MCP_TOOL_REF 不会让已停用连接继续接受新调用，也不会单独阻止旧连接关闭；已接纳操作仍按原规则结算。同 session 共用调用引用不改变这些生命周期。

已有调用持有的原执行器、冻结参数和执行许可不随引用解析迁往新目标。目录变更先沿现有暂挡与排空路径处理；删除或停用后拒绝新准入，已经接纳的操作继续按原效果结算。管理或调用结果中的 ERROR 均说明实际原因及已知效果，不因重新查询或重新连接自动重做已经派发的操作。

| 部分 | 保留的现有行为 |
| --- | --- |
| 配置提交 | 在既有串行管理路径内核对当前配置，只删除所选文件内的 server_id，保留其他项 |
| OAuth 登录与刷新 | 按原顺序使该连接 owner 的进行中登录／令牌刷新任务失效；沿既有结算等待相应任务，不留下晚到写入重新恢复已删除授权的路径 |
| 本机凭据与 OAuth 授权 | 清理该连接 owner 下本机托管的秘密及本地 OAuth 授权记录，不清理其他连接 owner |
| 项目来源批准 | 项目连接继续清理对应的项目来源批准记录 |
| 能力采用 | 沿现有自动采用与通知路径处理配置变化，不要求模型固定补调 reload_capabilities，不改变其他会话的既有刷新时机 |
| 运行连接 | 在采用变化时阻止旧连接接纳新调用，取消相应连接、重试和目录刷新等任务；旧连接按现有退出路径关闭 |
| 已接纳操作 | 不因删除统一强杀已经接纳的工具调用；按原结算规则结束，相关操作与引用释放后再完成旧连接的物理关闭或本地子进程清理 |
| 取消与提交 | 沿原不可安全中断提交及清理结算，不因调用方取消就放弃已经开始的写入或抹去实际效果 |

清理的是本机托管凭据和本地授权记录，不修改外部环境变量、用户自行持有的密钥文件，也不承诺撤销远端账户上的 token。删除连接不额外调用远端业务工具，不安装或卸载程序。

配置删除和运行连接物理退出不是同一时刻。不能将 SUCCESS 文案扩写成所有在途调用都已结束、所有进程已经立即退出；也不为等待全部旧引用释放新增阻塞条件。底层已有的停止接纳、操作结算及关闭机制继续承担这些职责，不新增清理任务、持久回执或恢复流程。

### 10.4 精简返回值与原生示例

成功仅返回 meta.status:SUCCESS 和“已删除 MCP 连接 docs。”。业务结果不再返回已删除对象的顶层 target；失败和取消同样只用 meta／text content，路径及 server_id 放在说明中。原生外壳仍按共同规则返回 tool_result 和 next，不能将这两个结果读取引用与已删除业务对象混淆。

```typescript
interface RemoveLocalMcpResult {
  meta: {status: "SUCCESS" | "ERROR" | "CANCELLED"};
  content: McpConnectionManagementTextPart[];
}
```

删除 action、current.config_path、cleanup_attention、current 和 adoption 等公开重复字段，不增加清理计数、部分成功枚举、凭据清单或内部配置核对值。

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"},
  "action":"REMOVE_LOCAL_MCP"
}
```

本次删除及必要清理／采用正常结算后：

```text
meta:
  status: SUCCESS

content:
  已删除 MCP 连接 docs。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整删除结果的导出 Target>
next: null
```

### 10.5 错误、取消与部分清理失败

| 情况 | 状态与 content |
| --- | --- |
| 连接不存在、配置位置不可管理、文件／条目无法解析或核实 | ERROR，说明具体路径、server_id 及原因，不冒充删除成功 |
| 准备到提交之间配置发生变化，内部核对失败 | ERROR，说明变化及本次已知效果，不要求模型补填内部条件 |
| 删除写入失败 | ERROR，说明实际原因；只在已确认时声称未删除 |
| 配置已删除，但凭据、OAuth 授权、项目批准等清理未完成 | ERROR，说明配置已删除，以及已核实的失败步骤和具体原因 |
| 配置已删除，但本次必要能力采用失败 | ERROR，说明已完成删除及实际采用失败，不声称所有运行连接已经退出 |
| 删除状态未确认 | ERROR，说明未确认，不自动重做，不断言已删除或未删除 |
| 按既有规则结算为取消 | CANCELLED，说明实际已完成部分；不能把已经删除写成未删除 |

例如配置已删除，但本机凭据清理失败：

```text
meta:
  status: ERROR

content:
  已从 /project/.pulsara/mcp.yaml 删除 MCP 连接 docs。
  本地凭据清理失败：<实际原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如目标连接已不存在：

```text
meta:
  status: ERROR

content:
  /project/.pulsara/mcp.yaml 中不存在 MCP 连接 docs。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

取消请求不将已经完成的删除强行改成取消，最终状态沿原结算。以上仅模拟已知效果。清理失败可能使后续清理步骤尚未执行，不能仅凭一个布尔标志猜测所有其他步骤均已完成。捕获清理异常时保留实际步骤、路径及原因供 content 使用，并沿原凭据边界排除真实秘密；不公开密钥、令牌或原始秘密配置。

当前底层 cleanup_attention 所代表的实际部分清理失败不再公开映射为正常 APPLIED／SUCCESS，应映射为 ERROR。内部仍保留配置已经删除的事实，并沿现有路径完成应有的采用及结算；不能因为公开状态为 ERROR 就跳过它们。这里只改变公开状态及错误信息，不重排或重做清理，不增加自动重试或后台补偿。

### 10.6 结果保存、分页与实施核对

业务与展示分离，删除结果页预算不足时在删除前拒绝执行的旧检查；业务结算后完整保存真实结果，原生按共同 12k／NEXT 分页。NEXT 和导出只读取冻结结果，不再次删除配置、清理秘密或 OAuth 授权、取消任务、关闭连接或刷新能力。失败结果正常完整交付时 next:null 不表示删除成功；保存或交付本身失败仍须说明已知效果，不返回假可读结果 Target，不自动重做。

实施时按第 10.3 节逐项对照当前配置 owner、秘密存储、OAuth owner、能力采用和连接 supervisor，完整保留底层清理语义；只删除模型 expected_identity 参数及重复公开字段，由原 owner 继续取得并核对必要条件。仅 ROOT 原生，无 JS API，不新增已读资格、清理登记表、持久任务或旧接口兼容路径，不重写当前 epoch 的 provider 前缀。

## 11. AUTHORIZE_MCP：发起 MCP OAuth 登录

### 11.1 用途与底层复用

对已保存的独立或 Plugin MCP 连接发起 OAuth 登录。本次只精简公开参数及结果投影，完整复用现有产品语义和底层授权实现，不重写 OAuth 发现、注册、浏览器跳转、回调、令牌交换与保存、取消、任务复用或前端关联路径。

保留连接已经存在且配置为 OAuth 的要求，不顺带修改认证方式、启用连接或开启所属 Plugin。用户沿现有浏览器流程授权，模型不填写授权码、令牌或同意标志；等待登录及保存按原流程结算后返回最终结果，不以打开浏览器作为成功。符合现有条件的同目标进行中登录继续复用，配置及 Plugin 变化后的核对继续由原 owner 负责，不增加新的登录任务 Target、轮询协议或等待期限。

### 11.2 最简参数与类型

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MCP_CONNECTION Target | 必填，放首位 | 要登录的独立或 Plugin 连接：config_path＋server_id |
| action | AUTHORIZE_MCP | 必填 | 发起该连接的 OAuth 登录 |

复用第 8.2 节的 MCP_CONNECTION 形状；Plugin 连接同样复制查询返回的 config_path、文件内 server_id，不添加 plugin_id 或运行实例字段。原 owner 从实际路径解析来源及归属，保持各来源的既有可操作范围。删除公开 expected_identity、expected_package_install_id；核对条件由 runtime 在本次准备时取得，独立配置、Plugin 安装及连接变化仍按现有规则核实。不接受 scope、source_kind、config、额外字段或 null，不要求先查询或记忆内部身份。

```typescript
interface AuthorizeMcpArgs {
  target: McpConnectionTarget;
  action: "AUTHORIZE_MCP";
}

type AuthorizeMcpResult = McpConnectionManagementResult;
```

### 11.3 精简返回值与原生示例

复用第 8.3 节的共同结果：成功返回同一 Target、meta.status:SUCCESS 和“已完成 MCP 连接 docs 的 OAuth 登录授权。”。不额外返回 authorized、current、action、adoption 或给模型使用的 authorization 对象，不声称远端工具已经调用成功。

当前 authorization 中供前端识别登录结果的 owner、配置及安装身份等必要事实继续沿既有工具执行记录和前端关联路径持有；只移出模型公开正文，不删除前端登录结果功能，也不新增登记表、持久事件或独立授权结果实体。

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"},
  "action":"AUTHORIZE_MCP"
}
```

登录及本次必要处理正常结算后：

```text
target: {"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"}

meta:
  status: SUCCESS

content:
  已完成 MCP 连接 docs 的 OAuth 登录授权。

tool_result: <本次完整授权结果的导出 Target>
next: null
```

观察时间与微秒耗时沿共同外壳顶层并列，示例省略时不伪造数值。Plugin 连接使用其实际查询 Target，参数和结果不另设一套类型。

### 11.4 错误、取消、分页与实施边界

目标不存在或不可管理、连接未配置 OAuth、内部核对冲突、登录或保存失败使用 ERROR，content 写可公开的实际原因与配置路径。现有流程结算为取消时使用 CANCELLED，说明已知效果；取消请求不抹去已保存授权，也不将已完成操作强行改成取消。授权已经保存但必要后续处理失败时，明确已保存及失败原因；状态未确认时如实说明，不自动重新登录。

完整结果按共同规则保存，原生 12k／NEXT；NEXT 和导出只读取冻结结果，不重开浏览器、发起登录、交换或刷新令牌、写授权记录或刷新能力。正常完整交付失败结果的 next:null 不代表授权成功。结果保存或交付失败时沿共用规则说明已知效果，不返回假可读结果 Target。仅 ROOT 原生，无 JS API；权限、用户交互、凭据边界、底层结算及 provider 前缀连续性全部沿原实现。

## 12. CLEAR_MCP_AUTHORIZATION：清除本地 OAuth 授权

### 12.1 用途与底层复用

清除指定独立或 Plugin MCP 连接的本地 OAuth 授权。完整复用现有 logout 及其 owner、内部核对、任务失效与结算流程，不改变实际清理范围：

| 对象 | 保留的现有行为 |
| --- | --- |
| 本地 OAuth 授权记录 | 清除该连接 owner 的记录 |
| 进行中的 OAuth 登录／令牌刷新 | 按原流程使任务失效并等待相应结算 |
| 连接配置、认证配置、enabled 及 Plugin 启用状态 | 保留，不删除连接或改变启停 |
| 单独配置的 API key、私密 Header、secret env 等非 OAuth 凭据 | 不由本动作清除 |
| 远端账户上的授权及 token | 不承诺撤销 |

不将清除授权改为删除连接、清空所有秘密或强杀已接纳调用，不新增强制关闭运行连接的行为。当前没有本地授权记录时，只要原 owner 正常完成清除仍返回 SUCCESS；不要求为了清理旧 OAuth 记录先将当前连接改成 OAuth。

### 12.2 最简参数与类型

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MCP_CONNECTION Target | 必填，放首位 | 要清除本地授权的独立或 Plugin 连接 |
| action | CLEAR_MCP_AUTHORIZATION | 必填 | 清除该连接的本地 OAuth 授权 |

Target、参数删减及内部核对沿第 11.2 节。模型不提供 expected_identity、expected_package_install_id、scope、source_kind、Plugin ID 或额外字段；不增加查询前置资格。

```typescript
interface ClearMcpAuthorizationArgs {
  target: McpConnectionTarget;
  action: "CLEAR_MCP_AUTHORIZATION";
}

type ClearMcpAuthorizationResult = McpConnectionManagementResult;
```

### 12.3 精简返回值与原生示例

成功返回同一连接 Target、meta.status:SUCCESS 和“已清除 MCP 连接 docs 的本地 OAuth 授权。”。连接仍存在，保留 Target 供后续操作；不返回 current.authorized:false、清理统计、action 或 adoption。重复清除正常完成仍使用同一成功形状，不增加无需清除状态。

```text
工具：manage_capability
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"},
  "action":"CLEAR_MCP_AUTHORIZATION"
}
```

```text
target: {"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"}

meta:
  status: SUCCESS

content:
  已清除 MCP 连接 docs 的本地 OAuth 授权。

tool_result: <本次完整清除结果的导出 Target>
next: null
```

观察时间和微秒耗时沿共同外壳顶层并列，示例省略；不存在授权记录时不额外构造另一份结果类型。

### 12.4 错误、取消、分页与实施边界

目标缺失或不可管理、内部核对冲突、清除或必要后续处理失败使用 ERROR，content 说明原因及路径。已经清除但后续失败时明确本地授权已清除；未确认时说明未确认，不宣称已清除或仍然存在，不自动重做。取消完全沿当前提交及任务结算，按实际最终状态及效果返回，不因参数精简改变取消作用范围。

完整失败值、保存及原生 12k／NEXT 沿共同规则；NEXT／导出不再次清除授权、取消任务、关闭连接或刷新能力。仅 ROOT 原生，无 JS API。实施只适配最小 Target、移除模型需要填写的内部核对参数及精简公开结果；底层 logout、凭据／OAuth owner、运行连接和前端产品语义均复用现有完整路径，不增加新的清理或授权机制。

## 13. INSTALL_PLUGIN：安装或替换 Plugin

### 13.1 用途与底层语义

从本地已准备好的插件分发根目录安装 Plugin，或在允许替换时替换同一安装范围、同一插件名称的实例。名称与其他包元数据仍由选定源格式的声明及现有导入器取得，不要求模型填写 plugin_id，不新增名称覆盖或任意安装目的地参数。

安装、替换、格式导入、源校验、并发核对、发布、凭据及 OAuth 清理、取消结算和运行生命周期复用现有 owner。导入按 best effort 保留独立有效组件；模型不自行转换支持的分发格式，不修改供应商脚本、搬目录或改托管状态来绕过诊断。需要公开参数或凭据分类的 MCP 服务可由用户导入流程补齐；本次未能导入的服务不阻止其他组件安装，不为本动作增加输入字段。

| 情况 | 保留的现有行为 |
| --- | --- |
| 所选范围内没有同名安装 | 安装新实例；replace:true 只是允许替换，不要求原实例一定存在 |
| 已有同名安装，replace=false | 失败，不覆盖 |
| 已有同名安装，replace=true | 由原 owner 替换该实例，保留源目录和其他范围的实例 |
| 新安装或替换成功 | 均保持关闭；旧实例原先已开启也不使替换后的实例自动开启 |
| 后续启用 | 继续使用 SET_PLUGIN_ENABLED 及其现有审阅，不将安装与启用合并 |
| Plugin Hook | 按现有安装身份、定义与信任规则判断，不因安装或替换自动信任 |
| 连接配置及私密材料 | 继续保留适用的 MCP 覆盖配置，按原规则清理不再适用的凭据和 OAuth 授权、使相关任务失效并结算 |
| 旧组件及旧包 | 按原调用接纳、引用释放和回收生命周期处理，不强行扩大停止范围或要求返回前立即退出全部旧组件 |

安装成功指本次可导入的支持子集已发布，不承诺原宿主全部行为。原生包完整遵循 Agent Plugins 1.0.0：根 manifest 除规范明确允许忽略的项外，违反 schema 的字段拒绝整包；未知根字段、未实现扩展及独立组件失败按规范处理，不将局部诊断升级为整包失败。外部格式在导入时归一化后进入同一原生包 owner，不新增外部运行时。外部来源的可选元数据只映射可表达的源事实，无效可选项说明后忽略；原生声明仍按规范验证，不凭空生成名称或默认值。

| 局部问题 | 安装行为 |
| --- | --- |
| 单个 Skill 无效或不可读取 | 跳过该 Skill，保留其他 Skill 及组件 |
| 单个 MCP 服务不支持、显式关闭、缺必要公开输入或尚未完成凭据分类 | 不导入该服务，说明具体条目及原因，保留其他组件；纯说明 note 说明后忽略，oauth_resource 等无等价授权不推测；不代填值或擅自开启 |
| Hook 文件、组或 handler 无效，或属于未支持事件／类型 | 只影响对应独立单元，其他有效定义继续导入；不扩展现有 11 事件和 command 子集 |
| 路径问题 | 按解析后的包含关系及最窄失败边界处理：核心 manifest、组件类型、单个 Skill／服务或普通资源；包内合法链接可用，普通坏资源不拖垮独立组件 |
| 没有当前可用组件，但存在有效插件身份且可正常发布 | 允许安装为关闭的包，content 明确当前没有可用组件 |

Hook 声明可为单路径、路径数组、内联对象或对象数组，按所选声明顺序处理；显式空声明覆盖默认发现，不跨发行版合并。

外部格式导入始终写固定 dev.pulsara/hooks/hooks.json（包括空选择），保留的 vendor manifest／.mcp.json 等只作惰性资源；原生包自身的该文件优先，否则 extensions.com.openai 对象提供 Hook 设置并整体取代 .codex-plugin overlay，无该对象时才采用 overlay。选定设置未填 hooks 才使用默认 hooks/hooks.json，显式空值不回退；没有选定扩展时普通 hooks 目录仍是资源。原生 OpenAI 声明保持文件不变，用实际 manifest config_path 定位一个 Hook 来源，继续共用原 parser／executor，不为 OpenAI 声明伪造不存在的固定配置文件。

已选 Pulsara 声明因坏路径等被排除时，托管包用带诊断的空配置保留该选择，不意外启用 OpenAI 候补。

脚本和普通资源保留原字节与包内关系；合法包内链接保留为规范化相对链接，外部 Skill 保留原目录、正文及资源，按有效 frontmatter.name 建 skills/<name> 入口，预览只列实际有效名称；无效或冲突逐 Skill 说明，原生目录约束不放宽。不能保持资源关系时只影响对应组件。生成的根配置和 Pulsara 扩展按选定发行版规范化，不将它们声称为原始资源副本。

导入只归一化配置，不执行 Hook 或重写脚本；阻止、权限裁决、续接、原生工具输入、正则匹配和输出控制保持现状，PreToolUse 不改参数。

安装后的诊断用直白 content 说明已导入与未导入内容、对应文件／条目和实际原因。不支持的功能保持不启用，不将正常支持子集安装一律判为 ERROR；真实源不可用、发布、必要清理或取消继续按原 owner 结算。

### 13.2 统一参数与类型

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 必填，放首位 | 包含选定分发格式的本地插件根目录，不是安装目的地或某个清单文件 |
| action | INSTALL_PLUGIN | 必填 | 安装，或在允许时替换 Plugin |
| scope | USER／WORKSPACE | 必填 | 安装到用户范围或当前 GUI 项目范围 |
| replace | boolean | 可选，默认 false | 允许替换同范围、同插件名称的已有实例，沿现有用户意图和权限路径判断 |
| source_format | native／claude／codex／cursor | 可选，默认 native | 明确选择原分发格式，交现有导入器处理 |

仅将 source_path 改为首位 PATH target，其余参数、默认值及业务含义保持。路径沿文件工具常规规则解析：工作区内可用相对路径，工作区外使用绝对路径；进入原 owner 前取得实际绝对源路径，不从 terminal cwd 推导项目。scope 仍须显式提供，WORKSPACE 仍要求当前 GUI 项目。

不接受远程 URL、任意目的地、Plugin Target 输入、name、description、plugin_id、enabled、expected_*、原 source_path、额外字段或 null。源准备、下载与必要内容修改在安装前完成。source_format 省略只表示 native，不增加自动猜测、跨格式合并或失败后依次尝试格式的路径。

```typescript
type PluginInstallSourceTarget = {kind: "PATH"; path: string};
type PluginTarget = {kind: "PLUGIN"; plugin_path: string};

interface InstallPluginArgs {
  target: PluginInstallSourceTarget;
  action: "INSTALL_PLUGIN";
  scope: "USER" | "WORKSPACE";
  replace?: boolean;
  source_format?: "native" | "claude" | "codex" | "cursor";
}
```

### 13.3 精简返回值

成功返回最小 PLUGIN Target、meta.status:SUCCESS 和简短 text content。新安装为“已安装插件 example，当前未启用。”；实际替换为“已替换插件 example，当前未启用。”。安装／替换的区别来自原 owner 的真实结果，不仅根据请求里的 replace 值推断。

Target 与能力查询共用 kind、plugin_path，plugin_path 为原存储布局的稳定实例目录 plugin_package_parent，使用绝对路径，不是本次带 package_install_id 的具体包目录。输入 PATH 指向源目录，输出 PLUGIN 指向安装实例，两者不是同一对象。

```typescript
interface PluginManagementTextPart {
  type: "text";
  value: string;
}

interface InstallPluginSuccess {
  target: PluginTarget;
  meta: {status: "SUCCESS"};
  content: PluginManagementTextPart[];
}

interface InstallPluginFailure {
  target?: PluginTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: PluginManagementTextPart[];
}

type InstallPluginResult = InstallPluginSuccess | InstallPluginFailure;
```

删除模型公开的重复 action、operation_status、scope／plugin_id 复合 Target、package_install_id 及尝试／前序安装身份、package_root、内部提交阶段、current、cleanup_attention 和 adoption。安装身份、发布位置及原并发条件留在原 owner 内，不因字段减法删除底层核对、清理或生命周期事实。

原 diagnostics 中有用的源校验原因、兼容性提示、未支持内容及实际清理问题转入 content，保留具体含义及适用路径，不默认返回另一份诊断数组或诊断代码。失败仅在实例身份已核实时可携带真实 PLUGIN Target；它不代替 content 对实际安装状态的说明，尚未安装或状态未确认时不得伪造成功实例。

### 13.4 原生安装与替换示例

从已准备好的 Claude 格式源目录安装到用户范围：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"PATH","path":"/tmp/downloads/example-plugin"},
  "action":"INSTALL_PLUGIN",
  "scope":"USER",
  "source_format":"claude"
}
```

假设源声明名称为 example，本次实际完成新安装及必要后续处理，默认 Pulsara home 位于示例路径：

```text
target: {"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example"}

meta:
  status: SUCCESS

content:
  已安装插件 example，当前未启用。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整安装结果的导出 Target>
next: null
```

支持子集安装沿同一成功形状，例如 content 可为：

```text
已安装插件 ponytail，当前未启用。
已导入可用 Skill 和 command Hook。
未启用 hooks/claude-band.json 中的 modules 扩展：当前不支持。
```

说明只采用本次实际解析和发布结果，不因文件存在就断言脚本能正常运行。

允许替换同范围内的同名安装：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"PATH","path":"/tmp/downloads/example-plugin"},
  "action":"INSTALL_PLUGIN",
  "scope":"USER",
  "replace":true,
  "source_format":"claude"
}
```

本次实际完成替换时，结果保留同一稳定 PLUGIN Target，content 为“已替换插件 example，当前未启用。”。若实际是新安装，则使用新安装说明，不因 replace:true 谎报替换。native 格式可省 source_format；其余源路径和范围规则不变。

### 13.5 失败、取消与已发生效果

| 情况 | 状态与 content |
| --- | --- |
| 核心 manifest 无效、源确实不可用或无法形成可发布包 | ERROR，给出具体文件／条目、原因及源路径；配置错误不笼统改写为 source unavailable |
| 已有同名安装但未允许替换 | ERROR，说明安装范围和已存在实例，不自动开启 replace |
| 源、当前实例或配置在准备后变化，原内部核对失败 | ERROR，说明实际变化和已知效果，不要求模型填内部身份 |
| 实际安装或替换已经完成，但清理或本次必要采用失败 | ERROR，说明已安装／已替换及未启用状态，再说明失败路径和原因；可返回已核实的实例 Target |
| 发布或状态切换结果未确认 | ERROR，说明安装状态未确认，不断言旧实例仍在或新实例已经生效，不自动重试 |
| 现有流程结算为取消 | CANCELLED，保留实际已完成部分；取消请求不抹去已经发布的结果 |
| 独立组件无效、未支持或正常支持子集安装 | 可发布子集正常安装为 SUCCESS；content 说明未导入项及具体原因，不自动启用、不重复安装 |

例如替换已完成，但关联清理失败：

```text
target: {"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example"}

meta:
  status: ERROR

content:
  已替换插件 example，当前未启用。
  关联凭据清理失败：<实际原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

示例只表示已确认的效果，不将请求意图或一个清理布尔值当成完整事实。实际清理失败按 ERROR 展示，捕获的有用步骤、路径和原因沿原秘密边界进入 content，不泄露密钥或令牌；内部仍保留已经完成的发布效果，并沿原路径执行必要采用及结算，不因为公开 ERROR 跳过它们。不新增回滚、自动重新安装、后台清理或恢复任务。

### 13.6 结果保存、分页与实施核对

安装／替换结算与展示预算分离，删除因结果页放不下而在安装前拒绝执行的旧检查；完整保存结果后再按原生 12k／NEXT 交付。NEXT 和导出只读冻结结果，不重新观察源、导入、发布、替换、清理凭据、启用或刷新能力。失败结果正常完整交付时 next:null 不表示安装成功；结果保存或交付本身失败时说明已知效果，不返回假可读结果 Target，不自动重做业务。

实施只统一 source_path 的 PATH 参数形状及公开结果投影，复用完整现有导入、源观察、真实权限、实例锁及并发核对、包发布、MCP 覆盖保留、秘密／OAuth 结算、取消与旧运行组件生命周期。新安装及替换均保持关闭，启用审阅及 Hook 信任不合并、不跳过。仅 ROOT 原生，无 JS API，不增加新版本标签、Target 登记表或旧输入兼容入口，不重建当前 epoch 的 provider 前缀。

## 14. SET_PLUGIN_ENABLED：启停 Plugin

### 14.1 用途与底层复用

开启或关闭一个已安装的 Plugin 实例，不安装、替换或删除插件，不修改包内容。本章只统一最小 Target、删除模型填写的安装核对条件并精简公开结果；启用审阅、状态保存、权限、副作用、取消、自动采用及运行组件生命周期完全保持现有底层语义。

enabled:true 继续触发现有 Plugin 开启审阅，展示实际组件及 MCP 命令、连接目的地和凭据使用等必要事实，由用户确认。完全权限模式也不跳过该审阅，模型不能填写同意标志。提交前保留当前安装、连接配置及凭据存在情况等既有审阅核对，不将审阅内容移到模型参数，也不因参数精简删除内部接受事实。

enabled:false 不新增完整开启审阅，仍沿正常权限路径及原内部核对处理。安装后以及替换后的插件继续保持关闭，后续启用仍使用本动作；不合并安装与启用流程。

### 14.2 最简参数与类型

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PLUGIN Target | 必填，放首位 | 已安装实例的稳定 plugin_path |
| action | SET_PLUGIN_ENABLED | 必填 | 修改该实例的启用设置 |
| enabled | boolean | 必填，无默认值 | true 开启，false 关闭 |

target 复用第 13.2 节的 PluginTarget，仅含 kind、plugin_path；使用原存储布局中的稳定实例目录绝对路径，不使用本次安装包目录或仅名称。runtime 在原 owner 内解析来源、scope 及实例身份，模型不填写 scope、plugin_id、expected_package_install_id、其他 expected_*、config、审阅接受标志或额外字段，不接受 null。enabled 不接受字符串或数字代替。

内部安装身份及准备到提交之间的对象、配置与审阅核对由 runtime 取得并保留，不新增先查询或已读资格，也不要求模型记忆内部安装值。

```typescript
interface SetPluginEnabledArgs {
  target: PluginTarget;
  action: "SET_PLUGIN_ENABLED";
  enabled: boolean;
}

type SetPluginEnabledResult =
  | {
      target: PluginTarget;
      meta: {status: "SUCCESS"};
      content: PluginManagementTextPart[];
    }
  | {
      target?: PluginTarget;
      meta: {status: "ERROR" | "CANCELLED"};
      content: PluginManagementTextPart[];
    };
```

### 14.3 关闭对组件的影响及 Hook 信任

| 对象 | 完整保留的现有行为 |
| --- | --- |
| Skill | 后续采用的 Plugin 视图不再纳入该包；已交付给模型的历史内容不回撤，不改写已安装前缀 |
| MCP | 采用关闭变化后阻止旧连接接纳新调用，相应连接／重试／目录刷新按原路径处理；已接纳操作继续沿原规则结算，相关操作和引用释放后再关闭旧连接或本地子进程 |
| Hook | 后续采用的新事件视图不再纳入该包；已捕获视图和正在执行的操作按原生命周期处理，不新增统一强杀或追溯撤回 |
| 包文件、数据和连接覆盖配置 | 保留，关闭不是删除或重新安装 |
| 已保存凭据与 OAuth 授权记录 | 不因关闭而删除；其他进行中活动继续遵守原 owner 的检查和结算 |
| Hook 信任 | 开启不自动授予信任，关闭不撤销信任；同一安装及相关定义未变时重新开启可沿用原信任，仍须满足 Hook 自己的启用及信任条件 |

启停不生成新安装身份；定义变化或包替换后的信任判断继续由原机制负责，不能借开关跳过核对。不增加等待全部运行组件退出才返回的条件，也不能把关闭成功写成所有在途操作已经结束或所有进程已经立即退出。

当前 epoch 的 SYSTEM 和 provider tools 保持 byte-identical，messages 只追加后缀；运行授权及组件视图变化不成为重建前缀的边界。

### 14.4 精简返回值、重复设置与原生示例

成功只返回同一 PLUGIN Target、meta.status:SUCCESS 和“已启用插件 example。”或“已停用插件 example。”。删除公开 action、current、adoption、cleanup_attention、内部安装身份及提交阶段，不另返 enabled、changed 或 ENABLED／DISABLED／ALREADY_* 等公开枚举。

重复设置同一值，原流程正常完成仍使用相同成功形状及对应简短说明。重复 enabled:true 继续走当前开启审阅，不能因为底层已有 ALREADY_ENABLED 结果就自动免审阅；重复 false 同样沿原权限和内部核对结算。不新增无变化检测、查询前置条件或特殊返回字段。

开启调用：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example"},
  "action":"SET_PLUGIN_ENABLED",
  "enabled":true
}
```

用户审阅和本次必要处理正常结算后：

```text
target: {"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example"}

meta:
  status: SUCCESS

content:
  已启用插件 example。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整启停结果的导出 Target>
next: null
```

关闭调用：

```text
工具：manage_capability
参数：
{
  "target":{"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example"},
  "action":"SET_PLUGIN_ENABLED",
  "enabled":false
}
```

成功返回同一 Target、SUCCESS，content 为“已停用插件 example。”。文案不附加运行组件已全部退出或 Hook 已信任的承诺。

### 14.5 错误、取消与已保存效果

| 情况 | 状态与 content |
| --- | --- |
| 实例不存在、位置不可管理、当前包或配置不能满足原操作要求 | ERROR，给实际路径及原因，不伪造启用／关闭结果 |
| 安装、连接配置或其他审阅事实变化，内部核对失败 | ERROR，说明当前对象或配置已经变化，不让旧审阅应用到另一份内容 |
| 用户取消开启审阅，设置尚未保存 | CANCELLED，说明本次未启用 |
| 状态写入失败 | ERROR，说明实际原因及已知效果；未确认时明确说明设置状态未确认 |
| 设置已保存，但本次必要能力采用失败 | ERROR，说明开启／关闭设置已保存，再给具体失败原因，不声称原状态仍然有效 |
| 其他取消 | 完全沿现有不可安全中断提交及任务结算，根据实际最终状态和效果返回，不把已完成设置强行改成取消 |

例如停用设置已经保存，但后续刷新失败：

```text
target: {"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example"}

meta:
  status: ERROR

content:
  插件 example 的停用设置已保存。
  后续能力刷新失败：<实际失败路径>，<具体原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

示例只表达已核实效果，不能根据请求 enabled 值猜测保存结果。失败可保留格式合法的请求 Target，它不证明实例存在或可操作；无法形成合法 Target 时不伪造。错误内容沿原凭据边界处理，不泄露秘密。不自动重新启停、跳过审阅、回滚设置或发起后台恢复任务。

### 14.6 结果保存、分页与实施核对

启停结算与展示分离，删除因结果页预算不足而在修改前拒绝执行的旧检查；完整保存结果后原生按共同 12k／NEXT 分页。NEXT 和导出只读冻结结果，不再次审阅、保存状态、启停组件、修改信任或刷新能力。正常完整交付失败结果的 next:null 不代表设置成功；保存或交付本身失败时保留已知效果，不返回假可读结果 Target，不重做业务。

实施时逐项对照当前管理准备、用户开启确认、实例锁、连接审阅重核对、状态提交及自动采用，复用完整底层路径。只适配最小 Target、移除模型填写的 expected_package_install_id 和重复公开字段；不删除内部安装身份或用户确认事实，不改变关闭对运行组件的作用范围，不改变 Hook 信任、凭据、OAuth 及 provider 前缀语义。仅 ROOT 原生，无 JS API。

## 15. REMOVE_PLUGIN：删除 Plugin

### 15.1 用途与底层复用

删除指定范围内的一个 Plugin 安装实例。保持当前先核对实际实例、移除安装状态记录，再沿原路径清理相应私密材料和更新运行状态的完整语义。只统一模型参数及公开结果，不把删除扩成立即递归删包、清空数据或强杀全部组件。

保留损坏插件的删除能力：当前包内容无法完整解析，不等于不能删除；只要原 owner 能核实实例身份，仍沿现有删除路径处理。实例不存在或身份无法核实时明确报错，不猜测包位置、构造旧记录或直接扫描清空其他对象。

### 15.2 最简参数与类型

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PLUGIN Target | 必填，放首位 | 要删除实例的稳定 plugin_path |
| action | REMOVE_PLUGIN | 必填 | 删除该 Plugin 安装实例 |

target 复用第 13.2 节的 PluginTarget，仅含 kind、plugin_path。plugin_path 是原 owner 支持的稳定实例目录绝对路径，不是任意文件目录、具体安装包目录或仅插件名称。runtime 解析原来源与范围，保留现有管理权限和内部实例核对。

删除公开 expected_package_install_id；runtime 在本次准备时取得原安装及状态条件，提交时继续按原 owner 核对。不接受 scope、plugin_id、expected_*、config、额外字段或 null，不新增清数据、强制删包、是否清凭据等开关，也不要求先查询或取得已读资格。

```typescript
interface RemovePluginArgs {
  target: PluginTarget;
  action: "REMOVE_PLUGIN";
}

interface RemovePluginResult {
  meta: {status: "SUCCESS" | "ERROR" | "CANCELLED"};
  content: PluginManagementTextPart[];
}
```

### 15.3 包、数据、凭据与运行组件

| 对象 | 完整保留的现有行为 |
| --- | --- |
| 安装实例及连接覆盖设置 | 核对后移除实例状态记录；其中的连接覆盖设置不再作为该实例的配置生效，其他范围实例不受本次删除影响 |
| 托管插件包 | REMOVE 本身不调用 GC、不立即递归删除包；现有 GC 仅在包不再被实例引用且无使用中的锁时清理 |
| 数据目录 | 保留，不清空；不把数据目录加入包 GC 范围 |
| 原始安装源目录 | 保留，不因删除安装实例而删除下载或源文件 |
| 本机凭据及本地 OAuth 授权 | 按现有 owner 和范围清理属于该插件连接的托管秘密与本地授权，不影响其他插件或独立 MCP |
| OAuth 登录／刷新任务 | 按原 owner 范围、失效及结算路径处理，保留授权保存前对当前实例的核对，不新增统一登录取消或恢复机制 |
| 远端账户授权 | 不承诺撤销远端 token 或账户授权，不额外调用远端业务工具 |
| Skill | 后续采用的插件视图不再纳入该实例，已经交付给模型的正文不回撤 |
| MCP | 运行时处理删除变化后停止旧连接接纳新调用；已接纳操作沿原规则结束，相关操作与引用释放后再关闭连接或本地子进程 |
| Hook | 后续采用的新事件视图不再纳入该实例；已捕获视图和正在执行操作沿原生命周期处理，不新增信任撤销链路或统一强杀 |

删除安装实例不等于包文件已物理清空；正常等待原 GC 处理的包文件不算部分清理失败。既有包锁、在用引用和运行组件退出机制保持，不新增后台 GC 任务、等待全部包文件消失的返回条件或数据清理流程。

本次删除仍沿现有自动通知和运行状态更新路径传播。当前 epoch 的 SYSTEM、provider tools 不重建，messages 只追加后缀；不为删除旧组件而改写历史正文或已安装前缀。

### 15.4 精简返回值与原生示例

成功仅返回 meta.status:SUCCESS 和“已删除插件 example。”。不再返回已删除实例的顶层业务 target；失败和取消也只使用 meta／text content，必要路径写在说明里。原生外壳仍返回本次结果的 tool_result 与 next，不将其与插件 Target 混淆。

删除公开 action、current、operation_status、prior_package_install_id、cleanup_attention、adoption、内部提交阶段和重复诊断包装；内部身份及已发生效果留给原 owner。有用错误直接说明哪个操作已经完成、哪一步失败，不要求模型理解内部状态组合。

```text
工具：manage_capability
参数：
{
  "target":{"kind":"PLUGIN","plugin_path":"/Users/example/.pulsara/plugins/packages/user/example"},
  "action":"REMOVE_PLUGIN"
}
```

本次删除及必要处理正常完成后：

```text
meta:
  status: SUCCESS

content:
  已删除插件 example。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整删除结果的导出 Target>
next: null
```

包回收和数据保留属于本动作固定语义，不在每次成功 content 重复说明，也不额外返回清理统计、目录清单或待回收状态。

### 15.5 错误、取消与已发生的删除

| 情况 | 状态与直接说明 |
| --- | --- |
| 实例不存在或位置不可管理 | ERROR，说明插件名称／路径和实际原因，不冒充删除成功 |
| 当前实例或状态无法核实 | ERROR，说明无法读取或确认的位置及原因，不把包损坏等同于实例不存在 |
| 准备后状态变化，内部核对失败 | ERROR，直接说明插件已变化及本次已知效果，不要求模型补内部安装身份 |
| 实例移除已完成，凭据或本地授权清理失败 | ERROR，先说明插件已删除，再说明失败步骤及原因 |
| 实例移除已完成，运行时更新失败 | ERROR，先说明插件已删除，再说明运行状态更新失败及已知原因，不宣称所有旧组件已经退出 |
| 删除状态未确认 | ERROR，使用“无法确认插件 example 是否已删除：<原因>。”，不自动重做 |
| 取消 | 按原提交与任务处理后的实际结果返回；结算为 CANCELLED 时说明已完成部分，不抹去已经发生的删除 |

例如插件已删除，但本地凭据清理失败：

```text
meta:
  status: ERROR

content:
  已删除插件 example。
  本地凭据清理失败：<实际原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

例如插件已删除，但运行时未能正常更新：

```text
meta:
  status: ERROR

content:
  已删除插件 example。
  运行状态更新失败：<实际原因>。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

所有说明必须来自已核实的实际效果；如果无法确认删除，就不能使用“已删除”。捕获清理异常时保留实际步骤、适用路径和原因供 content 使用，沿原凭据边界排除真实秘密，不只返回一个清理失败布尔值，也不猜测其他步骤已经全部成功。

当前 cleanup_attention 所代表的实际部分清理失败，公开结果改为 ERROR。但只要删除已经发生，runtime 仍须按原流程处理这次删除，停止相应的新操作并让旧组件沿原规则退出；不能因为给模型返回 ERROR，就当作插件仍安装着而跳过这些处理。这里不是重新删除或自动重试清理，不新增回滚、后台补偿或恢复任务。

### 15.6 结果保存、分页与实施核对

删除与展示分离，移除结果展示预算不足时在删除前拒绝执行的旧检查；完整保存本次实际结果后再按原生 12k／NEXT 交付。NEXT 和导出只读取冻结结果，不再次移除实例、清凭据或 OAuth、关闭组件、执行 GC 或清理数据。失败结果正常完整交付时 next:null 不表示删除成功；结果保存／交付本身失败时仍说明已知效果，不返回假可读结果 Target，不自动重做。

实施逐项复用现有实例观察与核对、状态记录移除、包锁及 GC、数据目录保留、凭据／OAuth 清理和运行组件退出代码。只统一最小 Target、删除模型填写的 expected_package_install_id，并精简公开结果及部分失败状态；已发生的删除继续驱动原运行时处理路径，不将公开状态作为是否已经删除的唯一依据。仅 ROOT 原生，无 JS API，不新增持久记录、清理任务或旧接口兼容路径。

## 16. 当前生产实现参考

- [当前 action 参数及旧 source_path、name、description](../src/pulsara_agent/capability/management_intent.py)
- [管理准备、源验证及权限效果](../src/pulsara_agent/conversation_kernel/capability_management.py)
- [安装执行、当前结果字段及展示预算前置检查](../src/pulsara_agent/conversation_kernel/capability_management_execution.py)
- [管理结果适配及自动采用](../src/pulsara_agent/conversation_kernel/tool_runtime_management.py)
- [独立 Skill 安装请求及 owner](../src/pulsara_agent/capability/local_skill_management.py)
- [现有安装目录选择、复制和发布](../src/pulsara_agent/capability/local_skill_publisher.py)
- [源元数据覆盖的现有实现](../src/pulsara_agent/capability/skill_import.py)
- [现有 ROOT 范围及权限／用户表单分流](../src/pulsara_agent/conversation_kernel/tool_runtime_authorization.py)
- [当前 Skill 启用配置读取、写入和内部快照核对](../src/pulsara_agent/capability/user_skill_config.py)
- [当前独立 Skill 目录移除及配置／文件清理](../src/pulsara_agent/capability/local_skill_removal.py)
- [当前 Hook 信任、撤销、启停及写入前重核对](../src/pulsara_agent/hooks/trust.py)
- [当前 Hook 来源、信任及执行合同](../src/pulsara_agent/hooks/contracts.py)
- [当前 MCP 完整配置、解析及默认值](../src/pulsara_agent/mcp_config.py)
- [当前独立 MCP 配置及凭据结算 owner](../src/pulsara_agent/capability/mcp_management.py)
- [现有用户连接编辑器与高级设置](../frontend/components/mcp-editor.tsx)
- [本机 MCP 秘密与 OAuth 记录清理](../src/pulsara_agent/settings.py)
- [OAuth 登录／刷新失效与结算](../src/pulsara_agent/conversation_kernel/mcp/oauth.py)
- [运行连接采用变化、停止接纳与退出](../src/pulsara_agent/conversation_kernel/mcp/supervisor.py)
- [运行连接的既有调用接纳与引用生命周期](../src/pulsara_agent/conversation_kernel/mcp/slots.py)
- [前端既有 OAuth 登录结果关联](../frontend/components/mcp-authorization.tsx)
- [Plugin 安装、替换及当前结果转换](../src/pulsara_agent/plugins/management.py)
- [Plugin 包发布、关闭状态及稳定实例目录](../src/pulsara_agent/plugins/package_store.py)
- [Plugin 连接覆盖合成与原有编辑器](../src/pulsara_agent/plugins/mcp_connection.py)
- [Plugin 安装／替换及连接覆盖的凭据结算](../src/pulsara_agent/plugins/connection_management.py)
- [Plugin 请求及结果类型](../src/pulsara_agent/plugins/contracts.py)
- [现有分发格式导入 owner](../src/pulsara_agent/plugins/source_import.py)

这些接点用于核对现状，不表示生产代码已实现本草案。本文件确定 INSTALL_LOOSE_SKILL、SET_LOOSE_SKILL_ENABLED、REMOVE_LOOSE_SKILL、TRUST_HOOK_SOURCE、REVOKE_HOOK_TRUST、SET_HOOK_SOURCE_ENABLED、ADD_LOCAL_MCP、UPDATE_MCP_CONNECTION、REMOVE_LOCAL_MCP、AUTHORIZE_MCP、CLEAR_MCP_AUTHORIZATION、INSTALL_PLUGIN、SET_PLUGIN_ENABLED、REMOVE_PLUGIN 及相关共用 config，其他管理动作不由这些参考反推。
