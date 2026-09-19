# Pulsara 能力页、通用 MCP 认证与插件导入、可安装能力删除 Hard-cut 实施规范

> 状态：**IN PROGRESS / NOT ACTIVATED — 单一路径已实施，验收收尾中**。
> 2026-09-06：SDK HTTP/SSE 与 httpx2 OAuth 接线、管理面、凭据及 GUI 导入已落地；
> 此前 Python 全量 1575 passed；随后目录自动识别修订 focused 42 passed、前端 120 passed，类型检查与构建通过。
> Firecrawl HTTP/stdio、Notion 完整目录/安全调用/注销重新授权与真实模型调用已通过；
> 完整场景矩阵仍逐项验收，不能把这些结果等同于整篇激活。
>
> 初稿日期：2026-09-03。代码真源复核：2026-09-05，基线 `a147ec39`。
> 本次结合 24 个市场/维护方仓库与本地 OpenCode `f12e14cf` 修订：新增确定性导入、
> 通用 OAuth、显式 legacy SSE，以及 Plugin instance 的有限连接参数覆盖。
> “最大兼容”指通用协议与有明确定义的格式覆盖，不承诺执行任意宿主专属扩展。
>
> 当前工作区已采用 SDK HTTP/SSE transport；完整激活仍取决于第 14 节验证。凭据持久化服从当前
> [`PULSARA_ROUTE_WIRE_API_MODEL_UNIVERSE_AND_ADAPTER_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_ROUTE_WIRE_API_MODEL_UNIVERSE_AND_ADAPTER_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)
> 的单一 local-settings 文件契约；不恢复已经删除的 Keychain owner。
>
> 本文是能力管理产品面的前向实施权威。代码激活前，现有 production 行为仍以
> [`PULSARA_FRONTEND_APPLICATION_SPEC.zh.md`](PULSARA_FRONTEND_APPLICATION_SPEC.zh.md)、
> [`PULSARA_LOCAL_SKILL_INSTALLATION_PRODUCT_COMPLETION_SPEC.zh.md`](PULSARA_LOCAL_SKILL_INSTALLATION_PRODUCT_COMPLETION_SPEC.zh.md)、
> [`ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md`](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md) 与
> [`ROUND_9_3_AGENT_PLUGIN_BUNDLE_AND_HOOK_ADAPTER_IMPLEMENTATION_SPEC.zh.md`](ROUND_9_3_AGENT_PLUGIN_BUNDLE_AND_HOOK_ADAPTER_IMPLEMENTATION_SPEC.zh.md)
> 为准；实施完成后必须同步这些 active specs，只保留本文规定的单一路径。
>
> Fingerprint subtraction 继续服从
> [`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。

---

## 0. 一句话产品结论

Pulsara 的“能力”页成为用户拥有的可安装能力控制面：不依赖模型即可手动配置或导入 MCP、安装原生/可确定性转换的 Plugin；通用 stdio、Streamable HTTP、legacy SSE 与静态认证/OAuth 按形状接线，不按服务名字维护适配器。Plugin package definition 保持不可变，instance 可以管理有限的连接参数与凭据覆盖；主模型仍只通过 typed `manage_capability` 协作，不持有秘密。所有可安装的 loose Skill、local MCP 与 Plugin 均提供真实删除。

GUI 是完整入口，模型是可选协作者。导入格式、认证方式与运行状态分层；缺少凭据不等于格式不兼容，
包可安装不等于已登录，连接成功不等于所有工具/Hook 可运行。不能用删除活跃组件、猜测 token 或
改写宿主行为来制造“兼容成功”。

最终优先级明确为 **MCP + 可移植 Skills 的高质量兼容**，不是 OpenCode 插件宿主兼容。
不实现其 npm/local JS/TS Plugin API，也不为了市场整包覆盖率扩展 Pulsara Hook/agent 执行语义。
这不禁止通过 stdio 运行以 JS/TS 编写的标准 MCP server，或在用户正常授权后使用 Skill 附带的脚本；
排除的是宿主扩展协议，不是编程语言。

这是一条产品承诺，而不是建立抽象的 `GenericCapability`：

```text
统一用户体验
  installable => inspectable => disableable（适用时）=> deletable

仍由三个现有 owner 分别实现
  Loose Skill filesystem owner
  Native MCP config/supervisor owner
  Managed Plugin package/state owner
```

不得新增 capability 数据库、统一 registry、万能删除器、durable operation job、receipt、tombstone、repair graph 或后台 reconciliation。

实现职责先冻结：**SDK 管 MCP 协议和标准传输；Pulsara 管配置、凭据、权限、必要的资源边界和运行时采用。**
遵循 AGENTS.md §7；不能因为已有自研 transport 或 SDK 默认配置不适用，就继续复制标准协议。
§6.3 为本轮 MCP 集成的前向实施边界；历史 Round 6 中指定自研 transport 的实现选择不再约束本轮，
但其保留的用户可见行为、网络/资源保护与工具结算语义不得静默丢失。

---

## 1. 已冻结的产品决策

### 1.1 能力页是用户拥有的控制面；模型可以协作但不持有秘密

1. 用户可以在能力页添加、查看、启停、编辑local MCP，为Plugin-provided MCP管理凭据，并删除可安装能力。
2. Local MCP 与 Plugin-provided MCP 的手填 secret 均由用户直接交给本地控制面；OAuth token
   仅由用户启动的授权/其后合法 refresh 取得。两者均不得进入模型、terminal command、transcript、
   tool arguments/result、provider input、memory、Hook stdin/context 或普通诊断。
3. 模型侧管理投影只包含认证种类、需要哪个非秘密 binding 名称，以及“未配置 / 已配置 / 需要更换”，不提供 secret value。它不是针对同一 OS 用户、具有文件读取权限的 terminal 或恶意外部程序的隔离承诺，见 §5.2。
4. MCP installer Skill可以完成公开endpoint、transport、scope与普通非秘密配置；遇到需要用户凭据或其他用户独占选择时，必须让runtime唤起能力页同款本地表单，而不是要求用户自行找入口或在对话中粘贴key。
5. “用户拥有”不等于“模型只能给教程”。主模型可以通过一个ROOT-only typed tool提交MCP/Plugin安装、编辑、启停或删除意图；当信息完整且当前permission允许时，operation可以直接完成。
6. 当需要secret、缺少用户独占选择、existing product trust review或当前permission要求确认时，runtime必须自动打开与能力页相同的表单；模型不能通过参数强制跳过，也不能把“是否简单”作为自报字段。
7. 用户在表单中修改并提交后的exact值才是最终mutation request。模型的草稿只是prefill，不是authority；secret从browser-local form直接进入local controller/credential owner，不回流给模型。

### 1.2 MCP 可以直接编辑

MCP config 是用户配置，不是不可变软件包。添加与编辑共用一套 structured editor；编辑保存是同一 `server_id`、同一配置来源内的 whole-entry replacement，不是字段级 merge。

- `server_id` 是 native routing identity，不可原地修改；变更 ID 等价于删除后重新添加。
- `USER / WORKSPACE` 配置来源不可在编辑时偷偷迁移；移动作用域等价于在目标作用域新建，再由用户单独删除原项。
- endpoint、stdio command/args/cwd、认证、启用状态、subagent 可见性、exposure、effect 与物理运行参数均可编辑。
- Skill 不新增网页代码编辑器；用户仍可直接编辑其文件目录。
- Plugin package 仍是 immutable managed bundle；不允许在能力页修改包内 Skill/MCP/Hook 定义，更新继续走 exact replace/install。
- Plugin-provided MCP 的实例连接配置不是 package definition。用户可覆盖 HTTP endpoint、普通
  Header、普通 env、认证和 secret-env，并补齐导入时声明的连接参数。command、ordered args、cwd、
  transport family、Skill/Hook/策略定义仍只读。需要改 executable 或 HTTP/stdio 家族时走 package replace，
  不把实例覆盖变成代码编辑器。有效目的地变化与凭据重新使用遵守 §6.4。

### 1.3 所有“可安装对象”必须可删除

本文中的安装对象只有：

```text
LOOSE_SKILL
  USER      ${PULSARA_HOME}/skills/<name>
  WORKSPACE <workspace>/.pulsara/skills/<name>

LOCAL_MCP_SERVER
  USER      ~/.pulsara/mcp.yaml 中一个 native entry
  WORKSPACE <workspace>/.pulsara/mcp.yaml 中一个 native entry

CAPABILITY_BUNDLE_PLUGIN
  USER / WORKSPACE 中一个 managed Plugin instance state
```

以下不是独立可删除安装对象：

- Pulsara 随发行版提供的 bundled Skill 或 builtin tool；
- Plugin 内部贡献的某个 Skill、MCP server 或 Hook；删除它们的产品单位是整个 Plugin。Plugin MCP 的instance connection可以单独配置或清除，但这不等于删除component；
- 从其他作用域继承而来、当前页面不拥有的能力；
- process-local Host override；
- 已经进入 canonical transcript、provider input 或历史 ToolResult 的内容。

这些行不渲染删除按钮，不能显示一个无后端动作的“不可用”按钮。

### 1.4 删除不是关闭，也不是历史抹除

```text
关闭 / disable
  保留安装与配置，只阻止未来采用或执行

删除 / remove / uninstall
  从该作用域的 canonical local source 移除安装对象
  清理只属于该对象的附属管理状态
  阻止 future admission
  允许已经取得 authority 的旧 physical consumer 安全 drain
```

删除不会：

- 修改或抹除已有会话记录、provider replay、ToolResult、memory 或 compaction summary；
- 撤销远端已经发生的 MCP tool effect；
- 删除远端账户、吊销供应商 API key 或修改供应商账单；
- 删除安装来源目录；
- 因为 UI 需要而重建同一 epoch 的 provider input prefix。

### 1.5 Plugin data 不与 capability 删除混为一谈

删除 Plugin instance 必须移除其 future Skill/MCP/Hook contribution，删除只属于该instance的managed MCP credential bindings，并使 immutable package root 在旧 consumer drain 后可由现有 GC 回收。现有 `PLUGIN_DATA` 可能包含用户工作数据，默认继续保留；本轮不增加“同时删除数据”复选框，也不把普通 Plugin 删除冒充数据擦除。

Plugin MCP credential是可重新取得的连接认证附属状态，不是Plugin工作数据：disable时保留，remove时删除。Environment reference由用户/launcher拥有，remove只删除引用，不修改环境变量本身。

如果未来需要删除 `PLUGIN_DATA`，必须作为单独、明确、不可误触的数据删除产品动作规范；不能暗中级联进本轮统一 capability 删除。

### 1.6 一个模型工具，两种合法完成路径

本轮新增且只新增一个模型侧管理入口：`manage_capability`。它是现有MCP config、Plugin management、credential与live-adoption owners的窄facade，不是`GenericCapability`、第二套mutation service或万能文件写入工具。

同一tool call只有两种合法完成路径：

```text
DIRECT
  closed candidate完整
  不需要secret或其他用户独占输入
  existing product review已满足
  当前FrozenRunPermissionSnapshot允许本次exact effect
  => typed owner mutation => runtime automatic adoption => sanitized ToolResult

USER_FORM
  需要secret / 缺少用户选择 / 需要existing product review
  或当前permission要求用户确认
  => 打开shared capability editor/review
  => 用户可检查、补齐或修改
  => 用户提交exact request
  => typed owner mutation => runtime automatic adoption => sanitized ToolResult
```

二者必须汇入同一个typed operation与同一个live adoption owner。不得让DIRECT shell out到CLI、让USER_FORM拼YAML，或为模型、Web页面各实现一条写入路径。

`READ_ONLY`下模型调用不得直接修改能力；runtime可以打开一个由用户拥有的prefilled editor。该表单提交是新的显式用户控制面动作，模型tool call只等待并观察其non-secret settlement，不能把它伪装成模型获得了write permission。`ASK_PERMISSIONS`、`ACCEPT_EDITS`与`BYPASS_PERMISSIONS`继续完全服从现有permission preset；本文不发明第五种mode或capability-specific bypass。

Plugin仍保持install-disabled、optional connection overlay、enable review三个独立settle。`manage_capability`可以编排连续UI，但不能把三种authority折叠为一个“安装并信任”布尔值。一个公开配置完整的Plugin可以在permission允许时直接安装为disabled；`SET_PLUGIN_ENABLED(enabled=true)`始终进入USER_FORM，由用户review current exact package后产生call-local acceptance。Disable仍按实际effect与permission正常分流。

---

## 2. 当前代码真源与具体缺口

### 2.1 用户能力页

当前 [`frontend/components/capability-view.tsx`](frontend/components/capability-view.tsx)：

- 有 Plugin、MCP、Skill 三个 tab；
- MCP 只可填写基本 HTTP/stdio 字段并添加，不能编辑，也不能删除；
- Skill 可安装、启停，不能从 UI 删除；
- Plugin 已有启停与移除确认。

当前 [`src/pulsara_agent/web_app/http_server.py`](src/pulsara_agent/web_app/http_server.py) 与
[`src/pulsara_agent/web_app/session_controller.py`](src/pulsara_agent/web_app/session_controller.py)：

- 有 user Skill install/enable；无 typed removal；
- 有 user MCP create/enable；无 update/remove；
- 有 user Plugin install/enable/remove。

项目能力面已有 workspace MCP removal，但 workspace Skill 仍只有安装/启停；因此“能安装就能删除”尚未成立。

### 2.2 Loose Skill owner

当前 `LocalSkillManagementService` 只有：

```text
validate_local_skill_source
install_loose_local_skill
inspect_effective_skill_catalog
```

直接从文件系统 copy/edit/delete/rename 本来就是合法真相，但 Web/CLI 尚无 descriptor-safe typed delete operation。能力页不能自行调用 `rm` 或实现第二套路径校验。

当前 Web install 仅接收 source path 等既有字段，直接调用 service/publisher；没有来源集合预览、
缺 description 补填或安装副本归一化入口。因此现有“能装合规目录”不能当作 §3.4 的外部 Skill
兼容已经完成；需补入口和有限归一化，不重建 Skill runtime。

### 2.3 MCP auth 与管理面

当前 native MCP config 已支持：

```text
NoAuth
BearerEnvironmentRef
StaticHeaderEnvironmentRefs
StdioTransportConfig.secret_environment_refs
```

HTTP transport 会在 physical open/request 时解析 secret reference；stdio transport 会把引用解析为 child environment。Raw value 不进入 config repr 或普通 fingerprint。

但 `pulsara mcp add`、用户能力页、项目能力面都没有表达 auth/header/secret-env 的字段，因此当前状态是：**Kernel transport 能力存在，正式安装产品链路不完整。**

`UnsupportedOAuth` 当前仍是明确的 unsupported branch；本轮将其 hard-cut 为真实的通用 OAuth
配置、用户登录/回调、token refresh 与注销。新增范围见 §5.7，不能只删掉 unsupported 校验。

### 2.4 Plugin removal

现有 `remove_local_plugin` 已以 instance state unlink 作为 future contribution authority cut；旧 package root 由 physical lifetime anchor 保持到 consumer drain，再由现有 GC 回收。本文复用并补齐 UI stale guard，不新建第二套 Plugin uninstaller。

`PluginStoreLayout`把USER与WORKSPACE两种namespace的package/state/lock都放在`PULSARA_HOME/plugins/...`；WORKSPACE只决定identity与可见范围，不表示物理写入落在workspace目录。Permission classification必须按这个physical truth判定为outside-workspace write。

现有`SetLocalPluginEnabledRequest`要求`enabled=true`与`ExternalProcessAcceptance.ACCEPTED`exact同行；CLI只在展示current package summary并收到用户确认后构造该call-local value。模型参数不能成为这项acceptance，因此model-assisted enable必须落到用户review form，而不是由permission的DIRECT verdict单独替代。

当前 Agent Plugins 1.0 `mcp.json` 没有 credential slot schema；现有 `PluginMcpAdapter` 把 Plugin MCP
归一化为 `NoAuth`，且仅展开 `PLUGIN_ROOT/PLUGIN_DATA`。本轮新增的本地真相是现有 instance state
中的 non-secret connection overlay 与 local-settings 中的 private values/OAuth grants。
导入所需参数声明使用窄的 Pulsara client extension，不篡改 Agent Plugins 官方 schema，见 §3.3。

### 2.5 当前 permission、interaction 与 reload 真源

当前代码已经有且只应保留一套run permission authority：

```text
FrozenRunPermissionSnapshot
  -> BuiltinToolCallClassifier
  -> PolicyPermissionGate
  -> ToolDispatchAuthorizationPolicy
  -> ALLOW | DENY | REQUIRE_CONFIRMATION
```

[`src/pulsara_agent/primitives/permission.py`](src/pulsara_agent/primitives/permission.py)定义四种preset，普通run默认`BYPASS_PERMISSIONS`；`READ_ONLY`禁止write/terminal，`ASK_PERMISSIONS`逐次询问，`ACCEPT_EDITS`允许workspace write但对outside-workspace write与terminal询问，`BYPASS_PERMISSIONS`允许普通host-local write/terminal。本文只把capability action的exact effect接入这张现有表，不改变preset语义。

当前`DefaultBuiltinToolCallClassifier`只读取raw model arguments与static action override，`PolicyPermissionGate._filesystem_write_access()`只从`arguments["path"]`推导inside/outside；READ_ONLY的non-read-only call会在`DirectKernelToolPort.authorize()`成为终局DENY，而只有REQUIRE_CONFIRMATION才进入interaction。由于`manage_capability`禁止模型提供mutation destination path，实施必须先由trusted preparation owner解析target/effect，再窄扩展这条现有链；不能靠descriptor标成read-only或把DENY粗暴改成ALLOW来弹表单。

当前[`src/pulsara_agent/conversation_kernel/interaction.py`](src/pulsara_agent/conversation_kernel/interaction.py)与live-control/protocol只拥有process-local单一current tool confirmation，resolution仍是通用allow/deny；它尚不能承载editable capability form或secret-safe submission。这是需要扩展的真实缺口，但不构成新增durable interaction subsystem的理由。

当前`Host.reload_capabilities`已经明确same-epoch no-rebase，Web的若干user mutation也会通知live sessions；`reload_capabilities` builtin则是ROOT/bypass的显式process-control adapter。缺口不是再造refresh，而是让所有first-party typed mutation统一自动进入已有refresh owner，并把model tool保留为out-of-band/partial recovery。

### 2.6 2026-09-05 基线记录与 2026-09-06 工作区复核

以下“当前”描述属于 2026-09-05 基线，不能作为未提交工作区的完成状态；新的 ownership 决策见 §6.3。

- `settings.py` 的 `LocalSettingsStore` 已统一保存模型 key、DashScope key 与 PostgreSQL 配置；
  `write_local_settings` 使用 no-follow、父目录 0700、文件 0600、fsync/atomic replace。
  Keychain 与 `.env` 产品配置路径已删除。当前还没有 MCP managed credential collection，不能写成已支持。
- `PluginInstanceState` 当前没有 connection overlay；`RemoveLocalPluginRequest` 当前没有
  expected package install id。两者均是待实施改动，不是现成 API。
- `InstallLocalPluginRequest` 当前只接受绝对本地 `source_path`。本轮不凭空增加 remote package
  downloader；从远端获取候选仍先走既有 installer/source acquisition，再调用本地 typed install。
- `KernelHostSession.reload_capabilities → DirectKernelToolPort.reload_mcp_configs` 已能发布
  future config cut，而不等待所有旧 borrow drain。优先复用该事实；无需为了新表单重造调度器。
- 最近 runtime 修复已明确：tool attempt 存在与 result_state 必须匹配；前端已用 canonical
  `tool_results` 精确关联调用，不能重新按 FIFO 或 body 文案猜成功。新增管理工具必须遵守 §6.5。
- 工作量分为管理面（typed CRUD/表单）、凭据接线（本地保存/physical resolution）、模型协作
  （permission/interaction/adoption）三部分。后两部分涉及真实 execution boundary，不能按“小 UI
  修复”估算；但均可落在既有 owner 内，不需要数据库 schema/event 增长。

本次兼容性复核还确认：

- 基线 `package_core._normalize_portable_server` 拒绝标准 `sse`；工作区已加入 SSE variant，
  但自写 `_BoundedLegacySseTransport` 不是本次修订认可的最终实现。
- 首次复核的 MCP SDK `2.0.0`（现已按用户要求升级为 `2.1.0`，结果见 §6.3）已提供 `ClientSession`、`streamable_http_client(http_client=...)`、
  `sse_client(httpx_client_factory=...)`、`OAuthClientProvider` 和 `TokenStorage`。
  现有 `_BoundedHttpTransport`、SSE parser 与 OAuth 的 `httpx2 → httpx` 桥接应做减法，
  不能从“不能直接用默认 client”推导出“必须自研 MCP transport”。
- 本机 `httpx2.EventSource` 已有事件与 pending line 大小检查，默认 1 MiB；Pulsara 当前 SSE data
  上限为 16 MiB，两者口径也不同。因此“SDK 默认无限 SSE reader”不属实，但也不能宣称限制已等价。
  SDK POST SSE 路径直接构造 `EventSource(response)`，仅改 client.sse 配置不足以覆盖全部路径。
- 现有 discover → 有证据的 legacy initialize 协商已通过 typed 路径实现；必须保留，
  不把协议年代协商与新增的 HTTP/SSE transport 选择混在一起。
- OpenCode 的 plugin loader 加载 npm/local JS/TS 模块，不是通用 Agent Plugins/Claude/Codex
  package importer；它对 `.claude/.agents` 的 Skill 扫描不证明整个外部 Plugin 兼容。
  本轮只借鉴其 generic local/remote MCP config、OAuth user action 与认证状态分层。
- OpenCode 的 HTTP→SSE 广泛失败重试、全 process env 继承、固定 list-page cap、持久化 pending
  OAuth state/code verifier，不作为 Pulsara 新契约。已有物理边界与 AGENTS.md 优先。

现有workspace路径只由`LocalSessionController._mark_workspace_capability_refresh()`登记请求，并在`_workspace_adoption_payload()`公开为`next_user_turn`；`KernelHostSession._adopt_project_capabilities_if_requested()`也只在prompt delivery取得下一条root prompt后执行。这个时点不足以支持模型在同一turn完成“安装 → 验证”，因此本规范明确把它hard-cut为“下一次provider dispatch前的合法safe point”，同时保留old surface borrow drain与same-epoch prefix continuity。

---

## 3. 范围与非目标

### 3.1 本轮必须完成

1. 用户能力页 MCP add/edit 共用完整 structured form。
2. 当前 native MCP 能力中适合用户配置的绝大多数选项进入常用或高级区。
3. 用户独占的 managed MCP secret 输入与环境变量引用两条路径。
4. Plugin-provided MCP 共用实例 connection editor；有限覆盖字段与认证可编辑，package definition 不变。
5. User 与已有 project management surface 的 Skill/MCP 删除闭合。
6. User Plugin 删除补齐 exact stale guard 与统一文案；workspace Plugin CLI 继续使用现有 remove owner。
7. 新增唯一ROOT-only `manage_capability` builtin tool，让主模型以closed actions参与local MCP与Plugin管理；它不得接收secret或任意YAML/shell payload。
8. `manage_capability`接入现有frozen run permission、call classifier、permission gate与same-Host interaction owner，自动决定DIRECT或USER_FORM，不新增permission mode或旁路。
9. 能力页与模型唤起的表单复用同一component、schema projection、typed controller operation与stale guard；用户提交可覆盖模型prefill。
10. First-party Web/tool mutation成功后由runtime直接调用existing live capability refresh/safe-point adoption owner；不得要求模型例行补调`reload_capabilities`。
11. MCP/Plugin installer Skill明确direct/form分流、用户凭据边界与automatic adoption；loose Skill installer同步独立导入、资源保留与既有CLI边界。`reload_capabilities`仅作为out-of-band变更或明确adoption attention的补救措施。
12. GUI 粘贴/导入 MCP JSON/JSONC，以及本地 Plugin 发行目录的确定性格式转换；无需模型。
13. 通用 OAuth 用户登录、刷新、注销；授权不等于安装/启用，不绕过 existing review。
14. SDK-owned Streamable HTTP、stdio 与显式 legacy SSE 集成；替换重复 transport 实现，保留已验证的 product/resource/tool outcome 语义。
15. 独立 Skill 来源发现/导入与完整资源保留，不要求先转换或安装其所属 JS/TS Plugin；见 §3.4。

### 3.2 本轮明确不做

- raw YAML editor；
- OpenCode npm/local JS/TS Plugin 的加载、API shim、回调执行与宿主运行环境仿真；
- MCP elicitation、sampling、roots、Apps/Tasks 或 server logging 新产品面；
- 在能力页直接执行 MCP tool/resource/prompt；
- Skill 文本/资源在线编辑器；
- Plugin 包内 component definition editor；
- Plugin data deletion；
- 供应商 API key 创建/吊销、账号管理、爬取网页登录 cookie；正常 OAuth 授权与 refresh 不是此非目标；
- capability history、undo log、trash timeline、soft-delete rows；
- PostgreSQL schema/event/subject/guard/relation/job 增长；
- provider、compaction、memory、task、Hook execution或terminal语义变化；
- 新permission mode、修改现有四种permission preset含义、capability专用permission gate或模型自报“simple/trusted”绕过；
- 通过解析terminal command、watch filesystem或轮询来猜测能力配置是否变化；
- 任意 provider-specific MCP auth branch。

### 3.3 GUI 确定性导入：格式适配一次，runtime 只留原生路径

#### 入口和产物

1. 独立 MCP 支持“手动填写 / 粘贴配置 / 读取用户选定的配置文件”。JSON/JSONC 是一次性导入格式，
   不是 raw config editor 或第二套 canonical config。用户不必安装包含它的 Plugin。
2. Plugin GUI 只要求选择本地 source directory，读取预览时自动枚举该根目录已支持的 manifest。
   只有一种时自动选定并预览（包括原生 Agent Plugins 1.0）；有多种时才询问，并并列展示每种
   发行版的 Skills/MCP/Hooks 数量和名称，不默认偏向原生、不合并相邻组件。无法解析的发行版
   仍展示具体问题，不因此隐藏其他合法版本；无 manifest 时明确提示，不猜测或拼装。
   修改源目录清空识别结果与参数；改选发行版清空该版本的参数输入。远端获取仍走现有 source acquisition，
   本轮不增加市场同步器、任意安装脚本执行或 npm JS 插件 runtime。
3. 一个只读 importer 形成 call-local typed draft、组件清单、需填写参数和不能映射的具体字段。
   手动 GUI 与模型 INSTALL_PLUGIN 共用该 importer；后者只传 source identity/format，不接收 raw config。
4. 无障碍候选转成现有原生 package/candidate 后校验并安装。转换不是仅在失败后 runtime fallback；
   唯一格式自动确定或用户从多种发行版中选择后，在安装前执行；source 不变，临时候选在已有 source lifetime 内释放。
   GUI 的只读预览 API 仅接收 source_path 并返回各候选的独立预览/问题；安装仍传确切 source_format
   并复用现有 fresh observation/validator，不将识别结果升级为安装 authority，不增加检测 registry。
5. 不创建 import registry、receipt、checkpoint、manifest fingerprint 或 DB 行。缺少 URL/command
   等公开必填定义时保持本地表单 draft，补齐后才生成合法 native config/package；不得用假 URL 过校验。
   公开定义完整、仅缺认证时可以保存连接或安装 disabled Plugin，缺认证只阻止 exact 连接执行。

#### 本轮实现的格式边界

| 来源 | 确定性读取范围 | 归一化目标 |
|---|---|---|
| 原生 Agent Plugins 1.0 | root manifest/MCP、既有 Skill 与 Pulsara extension | 现有唯一 validator/loader |
| Claude / Cowork | `.claude-plugin/plugin.json`，引用路径或内联 MCP、该格式的默认组件目录 | 完整可等价组件进入原生包；Cowork 不是另一套 runtime |
| Codex | `.codex-plugin/plugin.json`，显式组件路径/对象与已定义的默认发现 | 不把 App ID 改成 MCP URL，不跨宿主补齐组件 |
| Cursor | `.cursor-plugin/plugin.json`、MCP、variables、明确的 Skill 路径 | 变量描述转为连接输入；rules/agents/特殊 hooks 不擅改为 Skill |
| 独立 MCP JSON | `mcpServers` map、明确的裸 server map、单个 server、OpenCode `mcp` map | 同一 native local MCP candidate；歧义时用户选择，不盲猜 |

每种外部格式的 selected/default/empty 语义必须用本轮市场快照与该格式的官方定义形成 fixture。
缺省、`{}`、`[]`、显式路径、内联对象不可当成同一意思。特别保留 Slack Codex 空 MCP、
PostHog/Superpowers 空 Hooks。只激活所选发行版声明/按其契约默认发现的组件，不扫描整个仓库求并集。
目录里有多个 manifest 时 GUI 让用户选择；marketplace 只是 source 定位信息，不授权自动安装所有 entries。

普通 MCP 字段映射：

- `command` string + `args` array；OpenCode command array拆为首项与有序参数；不 shell tokenize、
  不把 command string 当一段 shell 自动执行。`env/environment`、cwd 保持其 source-relative 语义。
  `CLAUDE_PLUGIN_ROOT/CURSOR_PLUGIN_ROOT` 等只有在源格式确切定义为所选 package root 时，
  才在声明中转换为 `PLUGIN_ROOT`；不改脚本字节来伪造原宿主环境，也不把外部绝对路径冒充包内路径。
- `http/streamable-http` → Streamable HTTP；`sse` → legacy SSE；有 URL 而类型缺失或 OpenCode
  `remote` 无法唯一决定 transport 时默认在表单推荐 Streamable HTTP，允许用户改 SSE，不后台逐个试。
- 普通 headers 保留；`bearer_token_env_var`、明确 env/header 引用、scope、OAuth 的
  `clientId/client_id/CLIENT_ID`、clientSecret、callback port/redirect URI、resource 等已知形状
  转为同一 closed fields；重复来源互相矛盾时标出冲突，不任选其一。
- 未知纯展示字段可保留为 inert metadata 并在预览说明；未知行为字段必须定位到字段，不能默默忽略。
  不把源 timeout 单位猜成目标单位，不截断超过既有物理边界的值。

#### 参数、秘密与 portable package

外部 `${VAR}`、`${VAR:-default}`、OpenCode `{env:VAR}` 只作为有限字符串模板语法读取，不执行表达式。
literal + named input 的有限拼接可以表达 Header 前后缀或 endpoint 普通参数；不支持嵌套表达式、
命令替换或任意模板代码。缺省规则保留原意，必填缺失显示“待配置”，不能发出 literal placeholder。
`{file:path}` 只在独立配置导入时由用户显式选择读取该具体文件；不因下载包内一个引用自动读本机文件。

导入预览中的输入分成普通参数与私有输入。已声明用途明确的 bearer/client secret/secret refs 自动标私有；
不按变量名 regex 猜其他值。无法判断的 literal header/env 让用户在本地预览确认，确认前不发布到模型、
公共 inspection 或生成包；用户可以选择保存为普通值或私有值。GUI 粘贴的原始内容不进日志/聊天/持久草稿。
下载包发现嵌入真实凭据不能当成用户已授权的 key；不自动激活，走既有 source admission 与用户处理。

原生 Agent Plugins 对未知 `${...}` 的语义不被改变。转换需要的非秘密输入 schema/target binding 写到
窄 client extension `dev.pulsara/mcp/connection-inputs.json`，按 local_server_id 声明：输入名称、
标题、普通/私有种类、是否必填、非秘密 default、有限模板 parts 与目标字段。该 extension 仅为
HTTP endpoint/header、stdio env、OAuth config 的实例补参，不允许决定 executable、Hook 或任意文件写入。
values 仍只写 instance overlay / private settings。它是唯一 immutable 输入定义，不复制进第二套 registry；
未使用导入输入的原生包不必新增这个文件。

Native composition 必须先 exact join 该 extension、portable component 与 instance overlay；
extension 声明的私有 target 即使用户尚未配置 overlay，也生成明确 missing reference，不能沿用
包中 `${TOKEN}` literal 发请求或忽略必填而以 NoAuth 连接。公开定义预先补齐后，缺认证仍可通过
package 结构校验；“可检查的 installed component”和“可执行连接”不混为一个 verdict。

私有模板只允许落入 secret header/env；`BoundSecretValue` 是 literal/reference parts 的闭合值，
runtime 仅为 exact target 求值，parts 中不得含 secret literal。现有 Bearer 是同一 lowering 的简写，
不是另一套 resolver。用于 endpoint 的输入必须明确为非秘密；不把 key query 自动转 Header 或伪造认证等价性。

#### 整包兼容与连接可用分开

脚本/资源保持字节不变；普通包装字段可转换。缺依赖或未登录可展示并让用户之后配置，不要求预览执行代码。
声明的可执行依赖不得伪装成已安装；启用仍走既有 review。`headersHelper` 是执行行为，不能在导入预览运行；
没有现成等价契约时报告组件缺口，不创造 helper executor。Hooks 只映射既有精确子集；agents/commands/rules
中依赖宿主调度、权限、工具形状的部分不能统一塞进 Skill 冒充等价。不能普遍承诺静态证明任意脚本可移植。

不支持的活跃 App/channel/Hook/host tool 使“整包等价导入”未通过；GUI 列出具体原因，允许用户另外选择
其独立 MCP 或可移植 Skill 做一次独立安装，不宣称该 Plugin 已安装。只有用户另行要求作者改写时才进入可选模型流程，
不静默制作裁剪版。供应商/模型名字不参与 importer 或 runtime 分支。

### 3.4 MCP + Skills 的兼容目标与独立导入

验收目标是：**支持的标准 MCP 与可移植 Skill，不因来自 OpenCode 或另一宿主而要求用户重写。**
MCP 的完整配置导入、普通 Header/env、多凭据、OAuth、SSE、工具发现/调用与错误结算是第一优先级；
不能把“JSON 读进来了”当兼容已完成。仍不承诺支持依赖本轮非目标（如 sampling）的服务器全部功能。

Skill 复用既有 `LocalSkillManagementService`、publisher 与唯一 `parse_skill_document`：

- 用户可以选择含 `SKILL.md` 的目录，或从选定来源根枚举多个候选再分别安装；支持作为来源的
  `.opencode/skill(s)`、`.claude/skills`、`.agents/skills` 和普通 `skills` 目录布局。
  不要求把来源目录手工改名，也不强迫安装其宿主 Plugin。
- 来源选择只是 import，不把这些目录全部加入新的隐式 runtime discovery roots。目标仍是现有
  USER/WORKSPACE loose Skill root；保留现有名字冲突、启停、删除、source observation 与 prefix 规则。
- 保留正文、相对引用、`references/`、`scripts/`、`assets/` 等完整资源与许可；不只抽取一个 Markdown。
  安装不执行脚本、不自动安装依赖、不读取外部 CLI 登录缓存。普通脚本存在不构成 Skill 不可移植的理由。
- frontmatter 的 name/description/license/compatibility/metadata 按当前 native parser 归一化。
  OpenCode 当前允许 description 缺省，而 Pulsara 要求非空；遇到这一具体差异，GUI 允许用户补充
  描述后生成安装副本，不把它升级为“整个 Skill 不支持”，不要求模型参与或凭空生成业务说明。
  只需格式/目录名调整时预览修改，不改 source；脚本字节与正文语义保持。
- 无执行语义的附加元数据可保留为 inert source information，不因陌生展示字段拒绝整个 Skill。
  `allowed-tools`、专属 agent 调度等若承担真实权限/激活语义，不能默默丢弃后声称等价；显示具体差异，
  不按正文里出现 OpenCode/Claude 字样就一律阻断。普通 prose/tool 示例与必需宿主依赖应区别判断，
  不增加无法证明的“静态验证所有自然语言行为”门禁。
- 一个仓库同时包含不支持的 JS Plugin、标准 MCP config 和独立 Skill 时，GUI 允许用户明确选择
  后两者分别导入；不执行前者，也不以“整个 Plugin 不支持”为由挡住独立来源。界面如实显示安装对象，
  不称为完整安装该 JS Plugin。
- OpenCode 的远程 skill index/cache 是来源获取方式，不是 Skill 执行协议。本轮复用既有 source
  acquisition 后的本地导入，不照抄其远程同步、cache version 或改变当前 Skill 召回/执行机制。

MCP 与 Skill 可以分别安装，也可作为原生声明式 Plugin 一起分发。已有 Agent Plugins 1.0 与精确
Hook 子集继续保留；优先级调整不意味着删除它们，只是不再扩大到另一套 JS/TS 插件宿主。

---

## 4. 能力页信息架构

### 4.1 保持轻量列表

三个 tab 继续使用当前产品名称：

```text
插件 | MCP | 技能
```

列表只显示用户决策所需信息。高级 config、完整 tool schema、internal provider name、tool ref、fingerprint、generation、package path 和 secret reference carrier 不常驻列表。

### 4.2 MCP add/edit 共用 editor

点击“添加 MCP”打开空 editor；点击一个 owned MCP 的“编辑”打开同一 editor，并填入完整非秘密配置。

`manage_capability`需要用户参与时也打开这个editor，而不是另做一张简化确认卡：

- model给出的公开配置只作为prefill；字段仍然可编辑；
- 因permission而需要确认、但candidate已经完整时，editor以compact review状态打开，仍允许用户展开完整配置；
- 需要secret或缺少用户独占选择时，直接聚焦相应字段并解释缺少什么，不要求用户重新填写模型已经可靠提供的公开字段；
- 用户提交走与能力页手动添加/编辑完全相同的controller operation；取消则产生typed `CANCELLED` ToolResult，让模型继续自然收尾；
- 表单不得包含“允许模型以后都这样做”“将此MCP视为安全”或其他新的持久授权控件。

Editor 分为两层：

#### 常用配置

- 显示名称；
- 连接方式：Streamable HTTP / 本地命令；legacy SSE 在高级区可选；
- HTTP endpoint，或 stdio command 与 ordered args；
- 认证：无需认证 / Bearer Token / 自定义秘密 Header / OAuth 登录；
- 是否启用；
- 是否向 subagent 提供。

#### 高级配置

- 使用 Pulsara 本地凭据或引用环境变量；
- HTTP 普通 headers；OAuth client ID、可选 client secret、scope、loopback callback port、
  redirect URI、可选明确 resource 与 client metadata URL；按需展示，不要求所有用户填写；
- stdio working directory；
- stdio ordinary environment 与 secret environment bindings；
- tool include/exclude；
- invalid tool policy；
- server default effect 与 per-tool effect overrides；
- server timeout 与 per-tool timeout；
- catalog refresh interval；
- private network / localhost HTTP 明确允许；
- `required`；
- parallel tool calls 与 proved-stateless HTTP declaration；
- stateless maximum in-flight。

高级区默认折叠。所有数值范围、组合限制继续由唯一 native parser 校验；前端可提供即时提示，但不得成为第二套 authority。

### 4.3 Plugin-provided MCP instance connection editor

Plugin-provided MCP与local MCP出现在同一MCP列表，并明确标注来源Plugin。它不进入local MCP完整编辑模式：

```text
只读的 package definition
  display name / Plugin name
  原始 endpoint 或 command/args/cwd
  package public headers / ordinary environment
  exposure/effect/timeout/concurrency等package-derived配置

可编辑
  HTTP：instance endpoint / ordinary headers / auth / OAuth settings
  stdio：ordinary environment / secret environment bindings
  importer 明确声明的有限 connection inputs
```

Agent Plugins 1.0 没有 credential-slot declaration；原生无 extension 的包允许用户按文档直接配置
上述通用字段，不能因“没有 slot”拒绝。带 §3.3 extension 的包生成具体参数表单；不根据变量名或
错误字符串猜 key。原始值与实例覆盖分开显示，可显式恢复默认值。command/args/cwd、transport family、
policies 不可通过此 editor 修改；secret value 仍只进入 local-settings。

当overlay target与package public Header/ordinary env同名时，connection overlay只在effective runtime覆盖该值；editor必须展示“将覆盖插件默认值：<field name>”。Package原文、inspection summary与managed package bytes保持不变。

该配置是以`scope + plugin_id + local_server_id`定位的Plugin instance connection overlay，不修改`mcp.json`、managed package root或portable component summary。Plugin详情中的“配置连接”和MCP列表中的“配置凭据”必须打开同一个editor、调用同一个operation，不能形成两份truth。

这些可管理行来自installed Plugin inspection，不以当前live MCP catalog为唯一来源：Plugin默认disabled或暂时连接失败时仍能先配置credential，并标注“插件已关闭”或真实连接状态；它不会因此被发布给模型。Invalid/skipped component没有可执行definition，不渲染伪connection editor，只在所属Plugin诊断中说明。

Plugin UI可以把下列过程呈现为一个连续向导，但Kernel/API必须保持三个有序且独立settle的operation：

```text
1. validate/copy/install Plugin，创建exact disabled instance
   - install request schema不接受任何secret
2. 配置Plugin MCP连接（可跳过）
   - 对已存在的instance调用connection-overlay operation
3. review并enable exact Plugin instance
   - 显示endpoint/command与non-secret credential presence，不显示value
```

因此用户可以在视觉上的“安装过程”中紧接着填写key，但secret永远不是package validation/copy/install transaction的输入，也不是安装成功的前提。Install未成功或instance尚不存在时，UI不得提前保存Plugin credential。用户跳过credential步骤后仍可选择enable；缺少认证只使exact MCP显示“需要凭据”或真实connection failure，Plugin的其他Skill/Hook不因此被伪装成未安装。用户也可以暂不enable，之后随时从能力页继续配置。

这条分阶段边界只适用于Plugin。Standalone local MCP创建的就是其canonical config entry，“添加MCP”可以在同一create form中接收credential mutation，但仍遵守secret不进入MCP definition YAML/model/log的边界；value 仅存入 §5.2 的 private local-settings。

### 4.4 不向用户展示内核字段

以下字段只可作为 opaque request guard 或内部诊断使用，不显示为表单项：

```text
semantic_config_fingerprint
runtime_config_fingerprint
resolved_config_identity
workspace_approval_identity
physical_lifetime_anchor
provider-mangled tool name
opaque tool_ref
connection generation / slot identity
```

### 4.5 认证状态文案

列表只使用产品文案：

```text
无需认证
需要凭据
已配置凭据
需要登录
等待浏览器授权
登录已过期
需要配置客户端
环境变量不可用
正在连接
已连接
连接失败
已关闭
```

不得显示 secret slot、环境变量值、秘密 Header 值、凭据存储内部定位、内部异常 class 或 request dump。公开 Header 配置可在高级编辑区按原值展示。

### 4.6 删除入口与确认

每个 owned installable row 在展开详情或 overflow menu 中提供一个明确动作：

```text
Skill   删除技能
MCP     删除连接
Plugin  卸载插件
```

第一次点击进入同一行/面板内确认；确认期间明确列出影响，不使用浏览器原生 `confirm()`。

- Skill：说明将删除 Pulsara 管理根中的完整 Skill 目录，不影响最初的安装来源；若位于 `~/.agents`，额外说明其他兼容 agent 也可能不再看到它。
- MCP：说明当前作用域中的连接配置及由 Pulsara 保存的对应凭据将被删除，远端 key 不会被供应商吊销。
- Plugin：说明该 Plugin 提供的 Skill、MCP 与 Hook 会一并从 future capability view 消失；Plugin data 默认保留。

禁用、loading、stale 或失败状态不能悄悄把删除变成无动作按钮。可重试的失败必须显示原因并保留用户重新确认/刷新路径。

### 4.7 独立 Skill 导入与管理

“技能”tab 直接提供“导入技能”，不隐藏在 Plugin 安装或模型对话内。用户选择本地 Skill 目录，
或选择来源集合后勾选候选；预览显示名称、描述、目标 USER/WORKSPACE、资源目录与具体兼容提示。
缺描述时可补描述，目录名不匹配时显示安装副本的目标名；不是任意正文编辑器。

每个候选分别安装、分别显示成功/待补充/失败；一个无效候选不阻塞其他独立候选，也不宣称整批原子。
重名沿用现有不覆盖规则。预览、取消与安装均不执行来源脚本，不加载同仓库 JS/TS Plugin。
成功后进入现有 Skill 列表、启停、详情与删除流程，并展示真实 adoption 状态；无需创建 Plugin instance。

预览只是 call-local 读取与表单草稿，不签发安装 authority。安装请求携带所选 source path 和用户确认的
有限 name/description 归一化值；service 在安装时重新读取并验证 source，沿用既有 source observation、
publisher stage 与 exclusive no-replace publish。仅在安装副本中归一化 frontmatter/目录名，正文和资源
不改写；最终副本仍由唯一 native parser 校验。不得新增 preview fingerprint、导入 registry、持久任务，
也不得为了归一化而先修改用户 source 或另写一套复制/发布实现。

---

## 5. MCP secret 产品边界

### 5.1 Closed secret reference

Native local MCP config与Plugin instance connection overlay都只持有引用，不持有可打印 secret。实现 hard-cut 到以下 closed source union；命名可以按现有模块风格微调，但语义不得改变：

```text
McpSecretReference
  EnvironmentVariableReference(name)
  ManagedLocalCredentialReference(binding)
```

HTTP auth 使用：

```text
NoAuth
BearerSecret(reference)
StaticHeaderSecretReferences(
  case-insensitive unique header name -> reference or BoundSecretValue
)
OAuthAuthorization(non-secret client/resource configuration,
                   optional client_secret reference)
```

stdio transport 使用：

```text
ordinary_environment:
  child env name -> non-secret literal

secret_environment:
  child env name -> McpSecretReference or BoundSecretValue
```

不得支持 raw secret string 进入 `McpServerConfig`、MCP definition YAML（`mcp.yaml`）、Plugin instance state、fingerprint payload 或 public DTO。唯一允许持久化 value 的位置是 §5.2 的 private local-settings credential collection，不把该文件作为 public projection。

### 5.2 Managed local credential

默认“Bearer Token / 自定义秘密 Header / stdio secret env”的输入由本地 Web controller 交给
现有 `LocalSettingsStore`，在 `${PULSARA_HOME}/local-settings.yaml` 中增加窄的 MCP credential
collection。沿用现有 no-follow、0700/0600、物理字节边界与 atomic document replace；值为本机
可直接读取的明文，不承诺文件加密。不恢复 Keychain/keyring、跨平台 OS vault adapter、独立
secret sidecar 或 `.env` fallback，也不写 PostgreSQL、`mcp.yaml`、Plugin state 或 Skill 文件。

这份 collection 是凭据值的唯一来源，不是新 capability registry。完整 document 的读取、更新、
删除都通过同一个注入的 `LocalSettingsStore`；扩展 closed codec 及所有重建 `LocalSettings` 的
mutation 时，必须保留其他模型/DashScope/PostgreSQL/MCP 字段。普通 settings GET 仍只投影 presence，
不能直接返回 `local_settings_to_dict`。不为新增凭据类型创建第二个 settings writer 或 version 双路径。

Credential binding 由以下非秘密 identity 唯一确定：

```text
credential owner
  LocalMcpOwner(configuration scope identity, server_id)
  PluginMcpOwner(plugin scope identity, plugin_id, local_server_id)
binding kind (bearer | header:<normalized-name> | env:<target-name> | oauth-client-secret)
OAuth grant / dynamic client info：同一 owner 下的唯一 private auth record（见 §5.7）
```

- Local `server_id`、Plugin `plugin_id + local_server_id`与scope均不可直接编辑，因此binding不随显示名称变化；
- Plugin binding不包含`package_install_id`：同一Plugin component的合法package replace可以保留凭据，但replacement必须在采用前重新验证transport kind与reserved names；
- workspace binding 使用现有 stable workspace identity，而不是把任意绝对路径当 secret key；
- 不增加 credential registry 或数据库映射表；
- 对 UI/model 的 credential inspection 只返回 present/missing；仅 physical owner 的窄 resolver
  可取得该目标的 call-local secret value，不向它返回完整 LocalSettings；
- Python/runtime 不虚构内存零化保证，但必须缩短值的可达 lifetime，禁止进入 repr/log/event/trace。

本地文件存储不因非 macOS 而隐藏“由 Pulsara 保存”。不增加平台专属 credential negotiation。
文件权限仅限制其他 OS 用户；不能阻止同一 OS 用户或已获相应文件权限的进程读取它。本文保证
正式 API/模型管理投影不主动泄漏，不另建文件沙箱，也不声称恶意 MCP 无法把收到的 key 转发到远端。

### 5.3 Environment reference

高级用户可选择“引用环境变量”，只填写变量名。能力页可以显示当前 Host 观察到“可用 / 不可用”，但不得返回或显示值。

- `reload_capabilities` 只能重新解析当前 Host 已拥有的环境；child terminal 中的 `export` 不能修改 parent Host。
- 如果变量是通过新的 env file 或外部 launcher 配置，用户需要重启 Host；UI 必须诚实说明，不能承诺 reload 会摄取另一个进程的新环境。
- `.env` 不是 Pulsara 配置加载入口；environment reference 仅指 launcher 实际传入当前 Host 的环境。
- `PULSARA_API_KEY` 仍不得被引用为 MCP credential，但不能只依靠该旧环境变量名保护模型 key。
  Provider/DashScope/MCP 的本地存储字段与 resolver 保持分离，不把完整 settings 或其他连接的 key
  交给 MCP；既有 `ProcessCredentialBoundary` 对当前 caller credential 的保护继续保留。

### 5.4 Secret mutation

现有 secret 永不回显。Editor 只呈现：

```text
未配置
已配置
更换
清除
```

- 未触碰 secret field 的普通编辑必须保留原 binding/value；
- 输入新值并保存才替换；空白 placeholder 不代表清除；
- “清除已保存的值”保留认证种类和 reference，enabled config 因缺值成为“需要凭据”，不匿名 fallback。
  “改为无需认证 / 移除 Plugin override”是不同的显式配置动作；后者恢复 package-derived 配置，
  不是同一个 clear 按钮的隐含效果；
- custom Header name 大小写归一后必须唯一；禁止用户覆盖 MCP protocol-owned `Host`、`Content-Type`、`Accept`、`Mcp-Session-Id` 等 transport headers；
- Plugin MCP connection overlay可以显式覆盖package已经声明的同名public Header或ordinary stdio env，这正是为immutable package补充instance secret的必要运行时语义。优先级固定为`package public/ordinary value < user connection overlay < MCP protocol-owned fields`；UI必须明确显示被覆盖的non-secret field name，不能静默覆盖。Protocol-owned Header仍不可配置；
- secret 不得放入 endpoint path/query。Pulsara 不按字符串形状猜测 key，但 UI、Skill 与文档必须只引导 Header/secret-env 方案。

### 5.5 Credential failure 与删除

Credential lookup missing/denied/unavailable 必须在 physical connect 前形成 typed, non-secret failure；不得发起匿名请求或把 value 填入错误文本。

删除 local MCP 先原子移除 exact config entry；Plugin 先移除 exact instance state，再由同一 request
owner 从 LocalSettingsStore 移除只属于它的 credential entries。当前 Host 登记/发布 removal 后
阻止 future admission，旧已取得 physical execution authority 的请求正常 drain；其他 Host/CLI
的外部文件修改仍需显式 reload，不承诺跨进程瞬间撤权。

Credential cleanup 失败不回滚已删除的能力；返回“能力已删除，凭据清理需要重试”，而非普通成功。
当前结果面板可重试，**不能仅凭旧 binding identity 盲删**：在同一 canonical owner mutation lane
中重新确认该 owner/component 尚未重建、binding 未被新配置使用，再由 LocalSettingsStore 删除。
发现重新安装/配置就停止旧 cleanup 并返回 stale，不删除新凭据；不需要 generation 表或 receipt。
若进程崩溃后遗留 inert entry，不自动恢复执行，也不增加后台 repair。高级用户可在停止 Host 后
从本地配置文件清理，不能继续指引其使用 OS credential manager。

Environment variable 本身由用户/launcher 拥有，删除 MCP 只删除引用，不修改 process environment 或 `.env` 文件。

### 5.6 Runtime secret egress boundary

Secret-safe不能只检查request side。每个physical MCP client/process在解析其credential refs时，
在已有 client owner 上保留 private exact-value scrub set；不新增 lease class、registry 或引用计数
体系。只覆盖该 client 实际注入的每个 non-empty credential value，并满足：

- HTTP value只进入exact auth/custom Header；stdio value只进入exact child environment target；
- server返回的tool result、resource、prompt、catalog metadata、protocol error、exception与已有
  UI/trace diagnostic 在离开 MCP transport owner 前检查该 scrub set。当前 `_StdioSessionTransport._stderr`
  直接丢弃 stderr，继续丢弃；不为了脱敏新增日志采集或 server logging 产品面；
- 在完整、已解码的协议字符串上做 exact-value 处理，命中时不把原文交给下游。普通文本可以替换
  为固定 marker；若命中 object key、tool name/schema 或路由身份，不可改写后继续发布为另一项能力，
  使用既有 invalid/unavailable/error 路径。不新增专用 live event 或诊断流水线；
- 不按`key/token/password`字符串形状广泛猜测或清洗普通内容；authority仅来自本次实际解析的exact values；
- old client在rotation/reconfigure后继续持有old scrubber直到old request/stream drain，new client只持new values；close后释放value references；
- scrub set 使用`repr=False, compare=False`或等价private carrier，不进入config identity、event、database、provider replay或durable registry。

不得把既有 caller `ProcessCredentialBoundary` 扩展成全局secret service locator。MCP scrub set
由 native MCP physical owner 局部持有，与其 lifetime 一起释放。它不是任意编码、加密、拆分后
转述或恶意外传的 DLP 保证；不得用“所有恶意 server 均绝不泄漏”作为无法证明的验收条件。

当前 HTTP `start()` 和每次 `_send` 都调用 `resolved_headers`；新增 managed resolution 必须改为
同一个 physical client 使用一致的 frozen credential snapshot，避免 header 已换新值而 scrub set
仍是旧值。这里指用户提供的静态凭据；OAuth refresh 的 request snapshot/scrub-set 规则见 §5.7。
环境/managed secret rotation 使用现有 runtime-config generation commitment/reconnect
边界，stable semantic tool identity 不变；不另添 credential fingerprint 字段或值到对象 registry。

### 5.7 通用 MCP OAuth：真实登录，不维护逐服务代码

#### 配置与用户动作

HTTP/SSE 的 closed auth union 新增 OAuth；stdio 仍使用 secret env，不给子进程偷偷代理网页登录。
支持 server metadata discovery、预注册 client、SDK 支持的 Client ID Metadata Document、DCR、
Authorization Code + PKCE、refresh 与本地注销。HTTP Header auth 与 OAuth 是明确选择，
不得把无 token 的 OAuth 配置降为 NoAuth，或把任意 401 都变成登录网页。

NoAuth 连接遇到可解析的认证 challenge 可以显示“需要登录/凭据”及预填建议；只有用户切换并保存
OAuth 后才能建立授权。已有 OAuth 配置可探测公开 metadata/使用已保存 grant；后台 connect/reload
不弹浏览器、不等待用户。用户点击“登录”才开始交互式流程，模型发起则必须经过同一 USER_FORM。
缺预注册 client 且服务不接受 DCR/CIMD 时显示需填写 client 信息，不伪造其他宿主的客户端注册。

GUI 高级字段保留 client ID、private client-secret reference、scope、loopback redirect/callback port、
可选 resource 与 HTTPS client metadata URL。缺省 scope 由 SDK 按标准 metadata/challenge 处理；
scope 升级显示给用户，不默默扩大授权。导入其他宿主的 client ID/redirect 是待确认配置，不能
宣称注册在其他产品名下就可合法复用；允许用户改为自己的注册。CIMD 没有真实可用的 HTTPS 文档时
不发明 Pulsara 官方 URL，也不为本轮部署一个公共服务。

“保存配置”“登录”“测试连接”“注销”是不同动作。登录针对已保存的 exact owner，不偷偷保存未提交
editor draft；成功也不自动 enable disabled Plugin。Test 可消费已存在的适用 grant，若需登录就返回
该状态，不把 disposable test 变成长期授权 owner。取消登录不删除安装或旧的有效授权。

#### 最小存储与 ownership

唯一 LocalSettingsStore 的 MCP private collection 存储已取得的 access/refresh token、到期信息及
必要注册 client information。这些字段用于下次正常连接/刷新，不是执行恢复记录。复用同一
LocalMcpOwner/PluginMcpOwner，grant 携带 exact resource server URL、经校验 issuer、client ID、
scope 等实际绑定值；不加 OAuth fingerprint、global token registry、账号表或第二个 auth 文件。

pending state、PKCE verifier、回调 listener、一次性 code 与尚未接受的 token 仅 process-local。
每个 exact owner 的交互登录串行：重复点击复用当前 UI，用户显式重新开始时旧 flow 取消。
使用独立的短期 OAuth flow nonce 是防 CSRF/回调关联的真实协议用途，不是 DTO fingerprint。
等待用户不持 settings/instance/mutation lock；不继承 provider planning deadline，不加 turn 总时长上限。
浏览器交互取消、Host 退出或现有用户交互 expiry 后关闭 listener；网络交换使用局部 connect/write/read
watchdog，用户可重新登录，不做 pending flow 的跨重启恢复。

回调使用 loopback listener 和用户配置的精确 redirect path/port；校验 state、PKCE、OAuth error，
只接受当前 flow 一次。不同 owner 需要同一 callback port 时共享受控 listener/路由或报告端口冲突，
不能偷偷改已注册 redirect URI。浏览器 callback 是独立端点，不要求供应商具有主应用 same-origin
header；它只凭当前 state 进入 exact flow，不接受 arbitrary capability mutation，也不把 code/token
记录到 access log、Referer、HTML、toast 或模型。主 Web mutation 仍保留现有 Origin/Host 防护。

完成交换后，在短的 owner lane 内重读 exact config/instance：删除、改 URL/issuer/client/scope、
package replace 或 logout 后到达的旧 callback 不可再写 grant。比较完整已冻结目标值与当前 flow
对象身份即可；不新增 generation/receipt 表。认证 draft/token 只交唯一 local-settings writer。
Remote OAuth 的第三方授权页当然接收协议所需参数；这不是把 access token 交给模型。

同一 owner 的 refresh 使用 process-local single-flight。提交 refresh 结果前同样复核目标与当前 grant；
已 logout/重配则丢弃，不让晚到 refresh 恢复登录。logout 清除本地 grant/动态注册信息并取消 pending
auth/refresh，保留用户配置的认证种类及手填 client reference；需要清除 client secret 用现有清除动作。
删除 owner 则清理所有专属 values/registration/grant，复用 §5.5 的 stale cleanup 边界。不自动向远端
发 revoke，不声称注销或删除已经吊销供应商 token。disable 保留授权但不维持后台 refresh worker。

#### SDK、网络与 replay 边界

OAuth discovery、PKCE、注册、交换与 refresh 的协议算法由 SDK 实现；Pulsara 仅接用户动作、
TokenStorage、回调生命周期及 exact owner 提交。MCP 网络层统一使用 SDK 所需 `httpx2` 与同一
网络策略接线，删除工作区的 `httpx2 → httpx → httpx2` 请求/响应桥接；不连带改造 LLM provider HTTP 层。
SDK 的 auth-flow 会重新 yield 原请求；必须用真实 fixture 验证它不会自动重新执行 `tools/call`。
可将 SDK auth-flow 用于单独的非工具授权握手并在工具发送前取得授权，不自行实现 OAuth discovery
或 token exchange，也不以大量私有 SDK context 覆盖代替受支持的配置入口。具体缺口按 §6.3 处理。

- 初次公开 metadata/discovery、明确尚未进入 MCP 应用执行的 HTTP 认证拒绝，可在授权后重新连接。
- 工具请求若可能已发送或结算未知，认证变化不允许自动 replay；OAuth error 只改变 future availability，
  不吞掉原 tool outcome。正常 token 到期刷新优先在未来 request admission 前完成；不能中断健康流。
- 普通 API-key/client-secret rotation 用新 physical config；OAuth refresh 是同一 grant 的局部认证续期，
  不改变 semantic config/工具 identity 或 provider prefix。每个请求持自己的 exact header snapshot；
  physical client 在新 token 发出前同步扩大 private scrub set，保留仍可能回显的旧 token 到 client drain。
  不为轮换制造凭据值的公开 hash，也不要求每次 refresh 重做整个 session/discovery。
- 复用既有 DNS/IP/network-policy 保护 metadata/token/registration 请求及 redirects；授权服务器可以
  合法不同源，但源于已验证 metadata，不能把 MCP 的 API key/Authorization 转发到其他 origin。
  issuer/resource binding 由 SDK 标准校验负责；Pulsara 校验配置 owner 与网络目的地。
  配置映射不足时指出具体 API 缺口，不预先授权覆盖 SDK 私有校验器，不按供应商名绕过。
- HTTP 保持 HTTPS，loopback callback 与显式允许的本地测试按已有例外；无任意 open redirect。
  OAuth response body/metadata 同样执行已有单响应大小保护；优先在 HTTP 响应流边界接入，不另写 JSON/OAuth 协议栈。

以上是客户端协议安全边界，不新增产品耐久性。标准依据：
[MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)。

---

## 6. MCP typed management operations

Web controller 与 CLI-independent service 使用四个 typed operations；不得让 React 拼 YAML，也不得让 Web service shell out 到 CLI：

```text
create_local_mcp_server(
  scope,
  server_id,
  complete_candidate,
  optional_secret_mutations
)

update_local_mcp_server(
  scope,
  server_id,
  expected_current_identity,
  complete_replacement,
  optional_secret_mutations
)

remove_local_mcp_server(
  scope,
  server_id,
  expected_current_identity
)

test_local_mcp_server(
  complete_candidate,
  call_local_secret_inputs_or_references
)
```

### 6.1 Whole-entry update

Update 必须：

1. 从 canonical scope 重新读取 current entry；
2. exact compare browser inspection 携带的 existing config identity；
3. 用唯一 parser 解析完整 replacement；
4. 在现有 capability mutation lane 内 atomic replace YAML；
5. 触发现有 live capability refresh；
6. 返回新 inspection 与逐 session adoption/attention。

不得 partial merge auth、transport 或 policy；不得在 stale 时覆盖用户/其他进程刚保存的新值。

- USER mutation 使用 existing `resolved_config_identity` 作为同一运行内 opaque stale guard。
- WORKSPACE mutation继续使用 existing `workspace_approval_identity`，并在变更后只批准用户刚保存的完整新值。
- 不为 update/delete 新增 quote fingerprint、revision table 或 config registry。

现有 config write 只有 read/parse/atomic replace，不提供跨文件事务，controller 的
`_capability_mutation_lock` 也只是同一 Host 的 lane。把 lane 下移/注入唯一 typed mutation owner，
让 Web 和 model 两条入口共享它；不要各自创建同名但互不排他的锁。CLI 复用同一 parser/写入算法，
但不同进程与手工 editor 的无锁写入不能被宣称为已解决的 CAS。本轮至少保证 fresh read 检测到的
drift 返回 conflict，不为任意外部文件写入再造 durable transaction/receipt。

Local MCP 的 credential 写入也必须明确顺序：先完整验证 candidate/current guard，在 owner lane
内写 secret，再发布 YAML（以及既有 workspace approval），发布成功后清理不再引用的旧 binding。
切换到 NoAuth、移除 Header/env target、managed 改 environment 时也要清理 obsolete managed value。
YAML publish 失败时仅由当前 owner 撤销本次 secret 写入；若 publish 已 FULL，则不能伪称零变更或
盲目恢复旧 key。按 exact 已观察 settlement 返回 partial/attention，参照既有 LocalSettingsStore
的 indeterminate-readback 处理，不新增跨文件事务日志。不要让 immutable parse/inspection 顺手写凭据。

### 6.2 Save 与 test 的关系

“测试连接”使用 disposable candidate/client，只提供 connectivity/catalog 证据：

- 不写 config；
- 不发布 provider tool surface；
- 不取得 future dispatch authority；
- 不要求成功后才能保存；
- 使用已有 connect/read/write/catalog-operation/settlement physical watchdog。测试只做当前
  bounded discovery/readiness 检查，不能等整个健康 stream 的 lifetime；用户填表时间不计入该 deadline；
- test 完成后关闭所有 client/process 并释放 private credential values。

用户仍可保存当前暂时不可达的服务；保存后列表显示真实失败并允许再次编辑/重连。不得因为 test 失败偷偷保留旧 config，也不得把 test 成功伪装成已经安装。

### 6.3 SDK ownership 与 MCP 网络减法（先于剩余功能实施）

能力页尽量投影 current native MCP config 与 SDK 已被 Pulsara 正式采用的能力，不直接暴露 SDK 的开放对象：

- 支持 Streamable HTTP、stdio 与显式 legacy HTTP+SSE；
- 支持 tools/resources/resource templates/prompts 的发现计数与非秘密说明；
- 不因 SDK 新增 experimental field 自动生成 UI；
- OAuth 按 §5.7 的 closed contract 实现；elicitation/sampling/roots 等仍不进入本轮产品。

#### 职责分配

| 机制 | 唯一职责归属 | Pulsara 的必要接线 |
|---|---|---|
| MCP JSON-RPC 编解码、请求关联、标准 session/notifications | MCP SDK | 将 SDK 的 typed result/error 映射到既有工具结算；不复制 session 状态机 |
| Streamable HTTP 的 GET/POST/DELETE、session header、SSE event/id/retry | SDK transport + httpx2 | 注入同一受配置约束的 HTTP client，管理它与 SDK context 的唯一生命周期 |
| legacy SSE endpoint/message 处理与 framing | SDK sse_client + httpx2 | URL/network policy 与凭据目的地检查，不写第二个 SSE parser |
| stdio JSON-RPC 通道、标准子进程连接与关闭 | SDK stdio transport 优先 | 精确 command/args/cwd/env、进程凭据隔离与物理资源限制；具体 SDK 缺口见下文 |
| OAuth metadata/issuer/resource/PKCE/DCR/CIMD/token 协议 | SDK OAuthClientProvider | 用户确认、浏览器和 loopback callback、TokenStorage、取消与失效后的提交检查 |
| HTTP 网络准入与凭据发送边界 | Pulsara 既有 policy/credential owner | 在真实请求的 client/transport hook 处验证 URL、DNS/IP、TLS/Host 与 credential destination |
| 单帧/单响应/缓冲资源保护 | 依赖已有能力优先，Pulsara 补实际缺口 | 保护发生在分配/累积前；不把整条健康流累计字节当单次资源上限 |
| 配置、permission、secret settings、采用与旧 borrow drain | Pulsara 现有 owner | SDK 不写 Pulsara DB、不决定模型权限、不改 provider prefix |

“薄接线”不是换一个类名：标准消息解析、协议协商和传输调度不能继续藏在包装类中自研。
也不引入统一网络治理服务、MCP 第二套连接池/重试调度器、网络 operation registry 或 per-service adapter。
SDK 类型仅在集成边界转换，GUI 和模型仍消费本规范的产品 DTO。

#### 目标调用链和生命周期

```text
native config + exact credential snapshot / existing OAuth grant
    → Pulsara 网络策略配置的 httpx2 client
    → SDK streamable_http_client / sse_client
    → official ClientSession
    → Pulsara typed tool outcome / existing supervisor
```

客户端在进入 SDK transport 前构造，不能一半请求经 SDK 默认 client、另一半经自研桥接。
HTTP policy 必须覆盖 SDK 发出的后续 GET/POST/DELETE、SSE message endpoint、重连和合法 redirect；
OAuth metadata/token/registration 使用同一网络策略实现，但分开认证上下文，不把 MCP API key 默认
附到授权服务器。跨源 OAuth metadata 合法性由标准 discovery 决定，不把“所有不同源均拒绝”当安全。
每个网络目的地按既有 DNS/IP/TLS/Host 规则验证；不允许仅验证第一次 URL、随后跟随任意 redirect。

复用 existing slot/borrow：一个 physical slot 拥有一个 SDK transport context 和所注入 client；
按依赖的 ownership contract 关闭，避免 SDK 和 Pulsara 同时关闭同一对象。新配置建立 future slot，
旧 slot drain，不让凭据修改/能力管理反向改写在途请求。注销、删除、late callback 检查仍由既有 owner 做。

#### 必须删除／收缩的工作区路径

1. 删除 `_BoundedHttpTransport` 的 MCP writer、listener、session header 与 response 分流实现，
   以及 `_BoundedLegacySseTransport` 的 endpoint/message 状态机；由上述两个官方 transport 取代。
2. 删除 `_consume_sse` 与 `_sse_event` 自有解析路径；SSE 的标准换行、注释、data 拼接、event/id/retry
   由依赖负责。不能把它们搬到另一个模块后继续称为 SDK 接入。
3. 删除 OAuth `_request` 的双 HTTP 库桥接；收缩 `_ConfiguredOAuthProvider` / `_ConfiguredResourceContext`
   对 SDK 内部状态的介入。TokenStorage、用户 callback 和 local-settings owner 提交不删除。
4. `BoundedMcpSdkClient` 收缩为 SDK 集成 façade；`_BoundedTransport` 中原始 JSON carrier 解码等重复路径
   随 transport 替换移除。保留必要的 typed result 语义检查、凭据防回显与异常映射，但不从 SDK 验证失败
   反向重建第二个 parser。`wire.py` 的 schema/discovery 产品资源限制不因 transport 减法整体删除。
5. stdio 同样先核对官方入口。不得因原有 `_BoundedStdioTransport` 已通过测试就默认保留全部实现；
   也不得为“全 SDK”名义删除精确环境、pre-frame bound 或进程清理。只有下面明确的局部 API 缺口
   才能提议窄 process/byte bridge，不能再包含另一套 MCP session、协商或 JSON-RPC correlation。

这些是目标改动，不是已删除清单；本次规范修订不修改或丢弃未提交代码。

#### 资源与 SDK 扩展口的具体核验项

本机源码已确认以下差异，不能用“SDK 不安全／SDK 什么都支持”概括：

- `httpx2.EventSource` 默认 1 MiB，检查 event 与未完成 line；Pulsara 原 SSE data 限制是 16 MiB。
  `sse_client` 的 client factory 可以接配置，但 `StreamableHTTPTransport._handle_sse_response`
  直接创建 `EventSource(response)`。必须覆盖 POST response 与 GET 两条路径；仅 override `client.sse`
  不足以证明全部生效。默认限额若收紧了旧契约，应在此明确修订并说明实际工具结果影响，不能静默采用。
- 普通 JSON response、解压后的数据大小、JSON shape、slot 总在途缓冲与 SSE 单事件不是同一限制。
  先检查 HTTP/SDK 已有能力，再在实际缺口加 response byte stream 包装；禁止复制整套 SSE/JSON
  grammar 来“顺便”实现计数。只在 SDK 完整解析后检查不能证明 pre-allocation 等价；需要明确该差异。
- 本机 `stdio_client` 有进程关闭机制和默认 env allowlist，但 `stdout_reader` 的未完成行缓冲未见
  Pulsara 的 frame limit 参数，env 合并规则也不等于 Pulsara 的精确 spawn contract。先验证可用
  扩展口；不能伪装成传 `env` 就已经满足全部边界。

实施时依次选择：现有公开参数/注入 → 可核验的受维护依赖版本或小范围 upstream 扩展 →
只弥补已证实缺口的局部 adapter。若都不可行，提交具体 API、行为和最小取舍，在本节修订后再实施；
不得默认复制依赖内部 parser、运行时 monkey patch 私有类、长期 fork 整个 SDK，或恢复旧 transport fallback。
这是工程范围确认，不新增 runtime gate、持久化证明或依赖能力 registry，也不要求静态证明一切。

#### 2026-09-06 隔离实验结果（原暂停点，已由后文资源取舍解除）

`tests/test_mcp_sdk_transport_probe.py` 先使用仓库 `.venv` 的 MCP 2.0.0 / HTTPX2 2.10.0，
随后按用户要求升级到 MCP / mcp-types 2.1.0（HTTPX2 仍为 2.10.0）重新验证，
实际进入官方 `streamable_http_client` / `sse_client`，以 `MockTransport` 提供可控 HTTP 响应。
仅 DNS 查询与远端响应为 fixture；不替换 SDK parser、session transport 或 auth generator。
本节是集成实验，不是生产接线或真实外部服务验收。

执行 `.venv/bin/pytest -q tests/test_mcp_sdk_transport_probe.py --tb=short`：**14 passed**。
其中 `test_gap_*` 是明确复现缺口的测试；绿色意味着缺口被稳定复现，**不意味着可以激活**。

| 已执行场景 | 实验观察与接线结论 |
|---|---|
| 自定义 transport + 既有 DNS/IP policy | 请求使用解析出的 IP，保留 Host/SNI；public → private redirect 在实际第二次发送前拒绝。只是 HTTP policy 接线证据，不替代完整凭据边界测试 |
| 明文 JSON response byte stream 上限 | 超限在完整 body 交给 SDK 前中止；已打开 stream 关闭，注入 client 仍归调用方所有 |
| 原始字节上限 + gzip | 压缩后不足 256 bytes、解压后超过 4 KiB 的响应仍被 SDK 接受；只包 raw stream 不等于解压后上限，不能照搬实验 helper 到生产 |
| JSON byte 上限 + node 上限 | body 小于 16 MiB、节点超过 65,536 的合法响应仍被 SDK 解析。现有 pre-parse node/depth 契约不能用事后检查冒充 |
| 取消正在等待数据的 POST-SSE | SDK task 被取消、stream/client 释放，只有一次工具 POST；尚不等于已验证配置替换/旧 borrow drain |
| 无自动 auth 的 401/429/500 | 工具 POST 各一次，返回 SDK error，不自行重发工具 |
| SSE 断流并已收到 event ID | 一次 POST 后用 GET + Last-Event-ID 取得原结果，没有第二次工具 POST |
| 直接把 SDK OAuth auth 挂到工具 client | 401 授权完成后重复发送同一工具 POST；普通 403 也重复发送。因此生产继续用独立授权握手，工具 client 只附已有 token，不挂自动 auth flow |
| 开启 HTTP 自动 redirect 的工具 client | 307 会把相同工具 POST 发给第二个 URL。OAuth discovery 合法 redirect 与应用工具 POST 不能共用无条件自动跟随策略 |
| legacy SSE client factory | `/sse` GET 与相对 `/messages` POST 都经过注入 client，正确返回结果并关闭 |
| 自定义 client 的 SSE 上限设为 16 MiB | 单个略大于 1 MiB 的事件直接经 client.sse 成功，但 SDK POST-SSE 返回 `SSE stream ended without a response`；POST 路径没有使用该设置 |

首轮实验中的 redirect 测试曾错误预期 SDK 返回 error item；实际 policy ValueError 经 SDK task group
抛出 ExceptionGroup。已将断言改为精确异常类型、消息与仅一次实际请求，并保留关闭检查；
没有更改生产代码或放宽网络策略来使测试通过。

已核对上游 [issue #3332](https://github.com/modelcontextprotocol/python-sdk/issues/3332)、
[PR #3338](https://github.com/modelcontextprotocol/python-sdk/pull/3338) 和
[v2.1.0 client 源码](https://github.com/modelcontextprotocol/python-sdk/blob/v2.1.0/src/mcp/client/streamable_http.py)。
截至本次核验，PR 仍为 draft，v2.1.0 POST 路径仍直接构造 `EventSource(response)`；
不能把未发布补丁当受维护版本已经解决。该 PR 提议取消事件上限，也不等于保留 Pulsara 的有限资源边界。
后续已按用户要求实际升级 `pyproject.toml`、`uv.lock` 与仓库 `.venv` 至 2.1.0，并更新
既有 supervisor conformance identity 中的依赖版本值（没有新增 fingerprint 或 registry）。
上述 14 项实验在 2.1.0 同样通过，包括明确复现的缺口；不能再把这些问题仅归因于旧 2.0.0。

升级 focused 命令与结果：

```text
uv sync --group dev
# mcp / mcp-types 2.0.0 → 2.1.0；锁文件其他依赖版本未变
uv sync --locked --check
# Would make no changes
.venv/bin/pytest -q tests/test_mcp_sdk_transport_probe.py tests/test_round6_mcp_production.py tests/test_mcp_sse.py tests/test_mcp_oauth.py --tb=short
# 94 passed
.venv/bin/pytest -q tests/test_mcp_credentials.py tests/test_mcp_management.py tests/test_mcp_import.py tests/test_plugin_mcp_connections.py tests/test_plugin_source_import.py tests/test_plugin_connection_inputs.py --tb=short
# 65 passed
```

这不是全量测试或真实 OAuth/Firecrawl 验收；未重启运行中的应用、改生产数据库或提交代码。

上述是隔离实验时的暂停点。用户随后明确授权以 SDK 为主体做减法，不因局部差异保留整套旧传输。
本轮后继契约（取代本节前文要求逐项保持旧 parser 资源口径的实施门禁）：

- HTTP/SSE 一律采用 MCP 2.1.0 官方 transport，不维护补丁 SDK、私有 monkey patch 或自写 SSE parser。
- SSE 采用 HTTPX2 的 1 MiB 单事件/pending-line 限制，GET、POST、legacy SSE 不另设不同上限。
  超大单事件可能失败，这是明确接受的依赖限制，不是工具成功或客户端截断后的成功。
- 普通 HTTP JSON 在交给 SDK 前限制解码后的 body 为原有 16 MiB；HTTP 解压本身由 HTTPX2 承担。
  不再声称控制解压器内部单次分配。SSE 不设置整流累计字节上限。
- HTTP/SSE JSON 结构在 SDK carrier 解析后的 Pulsara typed 接线检查；删除其 pre-parse grammar 与
  自研 slot 原始缓冲配额的等价要求。保留既有 schema/discovery、工具结果和并发准入限制，
  但删除 schema 独立 4096-node 配额：schema 共享现有 wire JSON 65536-node 边界，
  仍受 256 KiB 单 schema 字节与 depth 64 限制。2026-09-06 真实 Notion 工具定义
  `notion-query-data-sources` 为 5770 nodes / 紧凑 64019 bytes / depth 33，合法校验
  耗时 76–93 ms；节点还包含字段名，旧子配额不能直接代表计算复杂度。
  此修改是用户授权的通用资源边界减法，不按服务名提额、不裁剪 schema 或自动隐藏工具。
  超限连接测试返回 `schema_bound_exceeded`，UI 明确区分目录资源问题与鉴权失败。
- SDK transport/context 由单一 process-local owner 进入和退出。接线只过滤 typed messages 与异常，
  不重做 HTTP writer/listener、session headers、SSE framing、事件续读或 RPC correlation。
- 工具 client 不挂 SDK 自动 OAuth auth，不自动跟随 redirect；OAuth 握手 HTTP client 使用同一
  网络/credential 接线，并只在其独立认证上下文内处理合法 redirect。二者都使用 HTTPX2。
- stdio 的精确环境、受限未结束行和进程清理由现有窄 process bridge 暂承担；这是已验证的官方
  stdio 注入口缺口，不是保留自写 HTTP/SSE 的理由，也不扩张为第二套 session 或协商实现。

这不是放宽工具权限、凭据目的地、prefix continuity 或结果结算，也不增加数据库、fingerprint、job 或新事件。
全矩阵与真实服务验收仍必须继续，不能把以上取舍当作已完成的生产验证。

#### 协商、重连与工具执行必须分开

Legacy SSE 是外部 MCP transport 兼容，不是内部旧/新双路径：native union 增加明确 SSE variant，
同一 façade/supervisor 消费；不按 server 名判断，不在任意连接错误后 HTTP→SSE 试错。导入不确定
类型时用户可在表单修改，错误如实报告；现有 discover/initialize 协商保留。

保留 discover → 有证据的 legacy initialize 协商，但先核验 SDK 当前已有协商能力，仅在它未覆盖的
精确外部错误形状保留小范围映射；不把已验证 fixture 当成永久拥有整套自研 transport 的理由。
Agent Plugins `type: sse` 使用同一 SDK-backed native variant，不恢复 unconditional unsupported。

SDK 根据 event ID 用 GET/Last-Event-ID 续读已存在响应，不等于重新 POST 一个 `tools/call`。
允许经过 fixture 证明不重新执行工具、不重复交付结果的标准续读；不另外实现续传状态机、跨重启恢复
或 event ID 数据库。未知结果仍按现有 outcome 结算，不能通过重连把它改写成“肯定没有执行”。
OAuth 401 后重发原应用请求是另一种行为，必须验证并约束；禁止 blanket 禁用所有 SDK reconnect，
也禁止直接把自动 auth replay 接在有副作用工具上。SDK 的局部连接重试不得被提升为 task/turn 总次数上限。

SSE 一律 sessionful，不因用户勾选 stateless 获取并行承诺。超时使用现有 connect/write/read-idle、
单工具操作期限和关闭等待边界；没有整个 stream/turn/task 的 wall-clock lifetime cap。

参考 [MCP transports](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)；
这不是改变 provider Chat/Responses adapter 的授权。

### 6.4 Plugin MCP connection operation

Plugin MCP不调用local config update，因为package definition不是local entry。Plugin management owner增加一个窄operation：

```text
replace_plugin_mcp_connection_overlay(
  scope,
  plugin_id,
  local_server_id,
  expected_current_package_install_id,
  expected_current_non_secret_overlay,
  complete_non_secret_overlay,
  optional_secret_mutations
)
```

`complete_non_secret_overlay` 只允许 §4.3 的 HTTP endpoint/普通 Header/env、closed auth/secret-env
refs 与导入 connection inputs；不能改变 command/args/cwd、transport family 或 Hook。空 overlay
表示恢复 package default，且必须先解析、展示恢复后的目的地。Operation 在 exact instance lock 内完成：

1. 重读current instance并exact compare package install id 与完整旧 non-secret overlay；仅比较
   package id 检测不到同一包上的并发 credential 配置，不可因此覆盖另一窗口刚保存的 overlay；
2. exact join current portable MCP component，拒绝不存在、invalid或transport kind不匹配；
3. 使用唯一 native connection/auth validator 验证完整 overlay，求值导入参数并形成 effective fields；
   优先级固定为 package defaults → ordinary instance overrides → private bindings → protocol-owned fields；
4. settlement secret mutation并保留本次call-local rollback material；
5. atomic replace existing Plugin instance state中的该component overlay；
6. 通过existing Plugin composition/native MCP supervisor refresh发布new physical config。

Non-secret overlay是current Plugin instance state的一部分，不新增sidecar YAML、credential registry、数据库row或第二个Plugin state owner。Managed value 只在 local-settings。两份文件没有原子事务，顺序同 §6.1；整个 owner mutation 结束前不释放该 instance lock/共享 lane，不允许旧 rollback/cleanup 覆盖同一 binding 后来的新值。完整旧 overlay 已携带，不再 hash 一份。纯 key rotation 的 whole-secret replacement 在 lane 内顺序生效；不为不可回显的值新建 browser credential revision 或 CAS token。

Package bytes 与原始 inspection 不变；effective connection inspection 单独显示覆盖项。普通 env
仍不能覆盖 reserved/credential-boundary 字段，HTTP headers 仍不能覆盖 protocol-owned 字段。
原生固定包不要求有 input schema 才能覆盖合法连接字段；parameterized extension 也不是修改代码的后门。

本地 MCP 和 Plugin endpoint/resource/client 变化均须先切断旧 OAuth grant 的适用性、取消 pending auth；
旧 grant 不能移用到新 resource/issuer。保留静态 key 发向改变后的目的地时，用户 submit 必须明确
展示并确认“新目的地 + 继续使用哪些已配置凭据”；模型自行改变目的地且携带旧 secret 时必须进 USER_FORM。
复用完整 expected config/overlay 与现有 lane，不新增 trust token 或 durable consent。没有秘密携带的普通
参数编辑照常按 permission 分流；不能把所有 header/env 变更一律提高为额外强门禁。

Package replace 对同一 `plugin_id + local_server_id` 保留 overlay，但发布新 state 前按新 definition/
input schema 重验；component 消失、transport family 改变、target/input 不再合法时不采用旧 overlay，
列出需重配字段并清理 obsolete private values/grants。仍然合法的显式 endpoint override 不被新默认值
静默覆盖；enable review 显示新 defaults 与实际 effective destination。OAuth 只有完整 grant binding
仍适用时才能保留；不是相同 component 名就继续使用。

Replace继续遵守existing“new package默认disabled”：保留的credential在用户重新enable前只是suspended local binding，不能连接或spawn。Enable review必须exact join new package MCP endpoint/command、credential auth kind、non-secret target names与configured/present状态并显示给用户；用户接受new exact package后才允许把旧value用于new physical destination。SUBMIT 时在同一 mutation lane 内重读并比较所展示的完整 non-secret overlay 与 presence；发生变化就刷新 review，不凭相同 package id 接受过时表单。纯 secret value rotation 不增加可回显 revision。Review不显示secret，也不新增consent receipt。Ordinary disable保留overlay；remove删除overlay及managed values。

### 6.5 唯一模型入口：`manage_capability`

Builtin catalog新增一个ROOT-only tool，名称固定为`manage_capability`。它在四种permission mode中均可被模型调用，不得照搬`reload_capabilities`当前“仅ROOT bypass可调用”的特殊授权。第一版closed action union只覆盖本轮确有共享typed owner的MCP与Plugin动作：

```text
ADD_LOCAL_MCP
UPDATE_LOCAL_MCP
REMOVE_LOCAL_MCP

INSTALL_PLUGIN
SET_PLUGIN_ENABLED
REMOVE_PLUGIN
CONFIGURE_PLUGIN_MCP_CONNECTION
AUTHORIZE_MCP
CLEAR_MCP_AUTHORIZATION
```

`UPDATE_LOCAL_MCP`包含启停与完整whole-entry replacement；Plugin definition不可编辑，因此没有`UPDATE_PLUGIN`。
Plugin connection replace/clear 共用 `CONFIGURE_PLUGIN_MCP_CONNECTION`。`AUTHORIZE_MCP` 恒 USER_FORM，
`CLEAR_MCP_AUTHORIZATION` 按既有删除 effect 分流；target 为 owned local server 或 Plugin component，
不接受 token/authorization URL，复用 §5.7 auth owner，不新建第二个 builtin。
Loose Skill继续由既有installer/filesystem与本轮Web delete owner管理。

Tool input只能包含：

- closed action；
- `USER | WORKSPACE`与当前workspace context可exact resolve的scope；
- server/plugin/component identity与相应expected-current guard；
- typed public source/candidate、非秘密credential kind/target names，以及模型已知的用户意图。

Expected-current facts 必须来自真实 inspection：浏览器携带打开表单时的值；模型若未取得这些值，由 prepare owner 读取当前目标并冻结为本次 operation 的事实，再按权限进入 DIRECT 或 USER_FORM。不得要求模型猜测 package install id/approval identity，也不为取得 guard 新增第二套管理 registry。已经明确携带的旧值若 stale，不能由 prepare 偷换成新值后继续执行。

Tool input不得包含：

- raw YAML/JSON config blob、shell command wrapper或作为mutation destination的任意filesystem path；Plugin使用现有 typed absolute local source_path，目标root由scope owner导出；远端获取不是这个 install operation 的新职责；
- Bearer value、秘密 Header value、secret env value、OAuth token或“请用户把key贴到这里”的字段；公开 Header/ordinary env 仍是 native candidate 的非秘密配置，不因字段叫 Header 就全部禁止；
- `simple`、`trusted`、`skip_confirmation`、`force`、`interactive=false`等由模型自报的授权结论；
- 另一个tool call、CLI invocation或UI submit token。

`INSTALL_PLUGIN`只接收existing Plugin installer已经支持的typed source identity与验证参数，并继续落为disabled exact instance。`SET_PLUGIN_ENABLED(enabled=true)`必须复用existing Plugin review/enable owner且恒走USER_FORM：表单exact join current package install id，展示normalized Skill/MCP/Hook与credential-presence摘要，只有用户SUBMIT时才生成call-local `ExternalProcessAcceptance.ACCEPTED`并交给existing owner。Acceptance不出现在模型参数，也不持久化为receipt。`enabled=false`按实际write/process effect与permission正常分流。`manage_capability`不执行包内代码来判断Plugin是否“简单”，也不根据名称、README或模型描述放宽permission。

配置完整的environment reference是非秘密candidate，可按permission进入DIRECT；新增或更换managed credential value必然进入USER_FORM。清除existing managed credential不需要读取value，可按exact destructive effect与当前permission分流，不能因为“曾经含secret”而永远强制表单。

Tool最终只返回sanitized closed outcome：

```text
APPLIED | CANCELLED | REJECTED | CONFLICT | PARTIAL
object kind / action / scope / stable public identity
current non-secret inspection
automatic adoption: RELOADED | PENDING_SAFE_POINT | PARTIAL | NOT_APPLICABLE
reloaded / pending / attention session counts（如有）
```

USER_FORM是tool执行期间的process-local interaction状态，不是提前返回的伪成功ToolResult。用户取消、拒绝或关闭表单后必须让原tool call以`CANCELLED`/`REJECTED`settle，模型取得下一次推理机会并自然收尾；不得把一轮永远停在未完成tool card。Secret、opaque form carrier与private rollback material均不得进入最终ToolResult。

上面的 `APPLIED/CANCELLED/REJECTED/CONFLICT/PARTIAL` 是 **body 中的管理业务结果**，不是新增
canonical `result_state`。严格使用当前 `PreparedToolResultAcceptance` 的 attempt/state 联合：

| 结算位置 | 现有 canonical result_state |
|---|---|
| attempt 前 schema/target 无效 | `INVALID_ARGUMENTS`，不造 attempt |
| attempt 前 permission DENY | `PERMISSION_DENIED`，不造 attempt |
| 需要 USER_FORM 但 controller 不存在 | `TOOL_UNAVAILABLE`，不造 attempt |
| admission 前用户取消 | `CANCELLED_BEFORE_DISPATCH`，不造 attempt |
| 已有 attempt，保存完成（含 `APPLIED + adoption PARTIAL`） | `SUCCESS`，body 明确哪些事实已变更、哪些采用尚未完成 |
| 已有 attempt，已知业务拒绝或 stale | `APPLICATION_ERROR`，body 为 `REJECTED` 或 `CONFLICT` |
| 已有 attempt，内部执行失败或已取得确定结果的取消 | `SYSTEM_ERROR` / `CANCELLED`；不能把未知 mutation outcome 冒充“未修改” |

Capability form 采用 **先等待用户、后 admission**：pending 中仅持有 non-secret prepared facts；
在表单 SUBMIT 被 owner 接受后，按实际用户输入生成 execution candidate，再通过既有 attempt/result
写入纪律执行，READ_ONLY run 不因此得到 model write permit。普通 Tool confirmation 现有
`accept_tool_confirmation` 同时持久化 permission decision 与 attempt，不能直接塞入 secret 或借它
伪造 READ_ONLY ALLOW；新 variant 复用 same-Host slot/lifecycle，但走窄的 user-control-plane
submission handler。无 controller、取消、替换与关闭均只产生一次正确的 no-attempt 结果。

`AUTHORIZE_MCP` 同样先等 shared form 的用户确认，再 admission 并启动 OAuth browser flow。
确认后的 OAuth 等待属于该 action 的在途执行，由同一 owner 等待授权成功/取消/失败并只结算一次；
不能在 grant 尚未写入前返回 APPLIED，也不能把 admission 后的授权取消写成 no-attempt。
不持 mutation lane 等浏览器，不因等待授权给整个 turn/task 添加总时长限制。

用户实际提交的 non-secret candidate 与原 model prefill 若不同，ToolResult 明确说明最终保存的公开
值；不改写原 assistant tool arguments，不把 secret 写入 attempt/interaction decision。Hook 只能审阅
公开字段及权限相关的动作，不能修改或替代 secret submission。

### 6.6 Permission与表单分流算法

当前generic authorization先按raw tool arguments分类，READ_ONLY的non-read-only call会直接得到终局DENY，filesystem policy也只认识模型参数中的`path`。`manage_capability`不得假设只加descriptor/action override就能得到USER_FORM；它必须是一个窄的prepare-first binding。

Preparation owner先形成一个process-local、不可由模型伪造的typed value：

```text
PreparedCapabilityManagementInvocation
  normalized closed action and public candidate
  exact resolved target/current inspection/expected guard
  user-only input requirements
  existing product review requirements
  ResolvedCapabilityEffectProjection
```

该对象不持久化、不加fingerprint，也不进入provider input。其唯一用途是把已经由canonical owner解析的事实交给现有permission与execution owner。“是否直接执行”按以下顺序机械计算：

1. resolve exact target owner与current inspection，拒绝foreign/inherited/bundled/component-only target；
2. 用对应唯一parser/preflight解析public candidate，列出缺失或必须由用户提供的字段；
3. 根据真实physical destination与adoption行为导出effect projection，而不是把逻辑USER/WORKSPACE scope直接当成filesystem边界；
4. 将prepared projection与当前`FrozenRunPermissionSnapshot`交给existing `BuiltinToolCallClassifier -> PolicyPermissionGate -> ToolDispatchAuthorizationPolicy`的窄扩展接口；permission gate消费resolved facts，不从模型参数读取mutation target path；
5. `ALLOW`只有在无user-only input且无pending product review时才进入DIRECT；`REQUIRE_CONFIRMATION`、user-only input或product review进入process-local `CAPABILITY_FORM_REQUIRED`；
6. READ_ONLY的write `DENY`绝不转换为模型执行permit。Tool execution adapter只允许展示process-local form；用户SUBMIT以独立user-control-plane actor调用同一mutation owner。其他policy DENY仍立即settle为no-attempt ToolResult；
7. `CAPABILITY_FORM_REQUIRED`必须在generic“DENY立即返回”和普通boolean confirmation execution之前由exact prepared owner消费；用户取消或controller缺失则不执行mutation，但仍给原tool call一个最终ToolResult；
8. 表单提交后重新读取current target、重新parse完整submitted candidate并exact compare stale guard；不得执行最初草稿、把用户submit伪装成model permission，或last-write-wins；
9. 调用第6节已有typed owner，等待mutation与automatic adoption settlement，再生成一个ToolResult。

`CAPABILITY_FORM_REQUIRED`只是existing `KernelToolAuthorization`/same-Host interaction flow新增的process-local closed disposition，不是permission mode、durable event或通用表单框架。普通tools的DENY/REQUIRE_CONFIRMATION控制流保持不变；同一`PreparedCapabilityManagementInvocation`只能被DIRECT execution或一次form submission线性消费。

Physical effect mapping固定为：

| Exact operation part | 交给existing permission gate的事实 |
|---|---|
| USER local MCP YAML create/update/remove | `outside_workspace_write` |
| WORKSPACE local MCP YAML create/update/remove | `workspace_write` |
| 任意scope Plugin package/state install/enable/disable/remove | physical target位于`PULSARA_HOME/plugins/...`，一律`outside_workspace_write` |
| 任意scope Plugin MCP non-secret overlay | Plugin instance state位于`PULSARA_HOME`，一律`outside_workspace_write` |
| 任意managed credential create/replace/clear/cleanup | local credential store不在workspace，一律追加`outside_workspace_write` |
| Environment reference only | 不产生credential-store write；仍保留其config/state physical write |
| Adoption会start/stop stdio process或activate/deactivate local Hook/executable | 同时追加existing terminal/process effect |
| HTTP reconnect/catalog refresh | 继续服从existing `network_isolated=false`，不凭“网络”新增确认 |

一个operation可同时携带多个effect，使用现有规则中更严格的实际verdict。`BuiltinToolCallClassifier`可以按closed action与prepared projection生成不同effective category/metadata，但不得信任模型提供的path或风险标签；不要把所有action永久写死为bypass-only，也不要为此新建capability permission service。

四种mode的产品矩阵固定为：

| 当前 effective permission | `manage_capability`可否调用 | 完整、无secret、无需额外product review | 缺secret/用户选择或需existing review |
|---|---|---|---|
| READ_ONLY | 可以 | 不允许模型DIRECT；打开prefilled USER_FORM，由用户控制面提交独立显式mutation | 打开USER_FORM；模型只观察脱敏settlement |
| ASK_PERMISSIONS | 可以 | 进入existing permission confirmation；Hook未替用户合法settle时打开shared review/editor | 打开同一表单补齐并确认；Hook不能代填secret或product review |
| ACCEPT_EDITS | 可以 | 只有pure WORKSPACE local-MCP YAML write可DIRECT；USER MCP、所有Plugin state、managed credential、stdio/local process或Hook effect均按现有outside-write/terminal规则打开表单 | 打开同一表单补齐并确认 |
| BYPASS_PERMISSIONS | 可以 | DIRECT；但Plugin `enabled=true`仍需existing product review | 打开表单；bypass不能替用户发明secret、缺失值或existing product trust决定 |

HTTP/network本身继续服从现有`network_isolated=false`preset，不因“网络”二字发明新approval；stdio process与Plugin executable/Hook activation映射到existing terminal/executable effect。一个operation若同时含多种effect，采用现有规则中更严格的实际边界，但不能把“未来也许有副作用”泛化成所有MCP/Plugin都询问。

表单不是permission旁路：

- permission为ASK且existing PermissionRequest Hook没有合法settle时，它承载本次exact确认；Hook decision只能处理permission，不能代替secret input或Plugin product review；
- READ_ONLY时，表单提交的actor是用户控制面，而非把模型run提升到write mode；
- secret/缺失选择触发表单时，即使permission为BYPASS，最终authority也只覆盖用户实际提交的exact candidate；
- current controller不存在、interaction被替换、用户取消或stale时不执行mutation；
- PreToolUse/PermissionRequest Hook最多看到sanitized public candidate与effect projection，永远看不到form secret。

现有same-Host interaction owner只需增加一种process-local typed capability form payload与对应submission resolution。它复用现有单一current interaction、controller、replacement与settlement纪律；不得增加数据库interaction row、committed event kind、durable form job、receipt或跨重启恢复。前端复用第4.2节editor component，不让模型操纵DOM。

### 6.7 Automatic adoption 与 `reload_capabilities`补救边界

全文“下一次 provider dispatch 前采用”指尚未冻结的下一份 dispatch 的 preparation safe point；已冻结的 executable successor plan 按本节下述例外处理，不构成重新规划或重建 prefix 的许可。

First-party能力变更——无论来自能力页、`manage_capability` DIRECT还是其USER_FORM submit——在typed mutation成功后，都由当前request owner直接调用existing Host/live-session refresh与safe-point adoption。Runtime调用的是`reload_capabilities`背后的同一个内部owner，不是伪造一次模型tool call：

- 不新增assistant tool request、ToolResult、provider roundtrip或canonical transcript项；
- USER mutation继续传播给所有live sessions，WORKSPACE mutation只传播给matching sessions；每个target取得一个requested adoption revision；
- healthy provider stream/tool attempt不被中断。每个session在第一个合法safe point采用：若mutation来自`manage_capability`，发起它的当前session必须在同一turn的下一次provider call前settle该revision；其他busy/idle matching sessions最迟在各自下一次provider dispatch前采用，不要求等到“下一条user message”；
- old tool-surface borrow可继续drain，new surface只供后续dispatch。Adoption不得修改same-epoch provider SYSTEM/tools/messages prefix；新/变化MCP继续走existing DIRECT/META exposure规则；
- mutation settlement与adoption settlement明确分层，adoption失败不回滚已经保存的canonical capability truth；
- `manage_capability`的ToolResult等待origin-session requested revision：成功才返回`APPLIED + RELOADED`，失败返回`APPLIED + PARTIAL`并禁止假装可立即验证。普通Web API可对尚未到safe point的session返回`PENDING_SAFE_POINT`；正常pending不是attention；
- response列出reloaded/pending/attention session counts。模型不得在`RELOADED`后再例行reload。

这里的 origin adoption 必须在调用栈内执行现有 `reload_capabilities` 的 **future publication**，
不能仅登记 revision 后等待“下一次 provider dispatch”才唤醒自己：runner 正在等待本次 tool return，
那样会构成循环等待。发布时不持有 config/settings/instance mutation 锁，不等待当前 tool surface
borrow 释放，不关闭已取得 authority 的旧 physical client；只等待这次 publication 的局部 settlement。
能力 source 已保存后先释放 mutation lane，再调用 adoption，避免 model form/其他 session 反向等待。

其他 session 的 pending revision 在 **准备下一份 dispatch 之前**消费，而非 executable dispatch
已经冻结之后又偷偷重规划。已有 model-switch/compaction successor 持有唯一 executable plan 时先
尊重该 plan 的线性 ownership，不可为了 adoption 重复 hydrate/materialize/install；新 revision
继续 pending 到下一合法 preparation safe point。采用失败记录 attention 并允许后续回复，不让一个
advisory capability refresh 变成全 turn 永久拒绝门禁。

`RELOADED` 仅证明 local source 已被当前 Host 采用，不证明 MCP 远端连接/完整 catalog/tool call
成功；当前 supervisor 的 reload 本就不等于 discovery。工具返回保留 connector 真实状态，后续
list/inspect/safe call 才验证可用性；不得显示“已连接”或承诺模型下一次必能调用所有新工具。

保留现有ROOT-only `reload_capabilities` builtin tool，但把它定位为显式补救入口，而非正常安装步骤。它只用于：

1. terminal/CLI、手工编辑YAML/Skill目录、git checkout或其他进程installer造成的out-of-band source change；
2. first-party mutation已经返回明确`PARTIAL`/adoption attention，需要用户或模型在问题解除后重试；
3. 诊断时需要重新读取canonical sources以确认外部修改是否可采用。

`reload_capabilities`继续服从它已有的ROOT/bypass授权与same-epoch no-rebase契约；其他mode下的用户可从能力页发起同一底层刷新补救，不因此扩大模型run permission。Reload不能安装、启用、信任、删除或修复invalid config，不能摄取另一个进程后来新增的environment value，也不能代替MCP reconnect/connection editor。

不得通过解析terminal command来猜测“刚才可能改了能力”并自动reload，也不新增filesystem watcher。Out-of-band caller负责在确知source settled后显式调用补救入口。

---

## 7. 删除代数

### 7.1 通用规则

任何删除操作都必须满足：

```text
删除对象 = 当前页面拥有的 exact source item
不是按 display name 搜索
不是删除所有同名候选
不是删除 inherited/bundled/Plugin child contribution
```

删除通过原类型 owner 完成。UI 共享的只是下列产品状态，并不要求三个 subsystem 发明同一组底层 outcome：

```text
REMOVED
NOT_FOUND
STALE
CLEANUP_ATTENTION（canonical source 已切除，但附属物理清理未完成）
```

Plugin owner 继续保留其已有 `UNAVAILABLE / CANCELLED / TIMED_OUT / ACK_UNKNOWN` physical settlement；Skill 或 MCP 不得仅为视觉对称新增这些状态。不得为了统一 UI 强迫三个 subsystem 使用一个 DTO；UI adapter exhaustive 映射各自既有或本轮确有必要的 closed outcome 到共同产品文案。

### 7.2 Loose Skill 删除

新增第四个 local Skill management operation：

```text
remove_loose_local_skill(
  exact installed SKILL.md/path,
  scope,
  workspace_root when WORKSPACE
) -> LocalSkillRemovalOutcome
```

行为：

1. 目标必须 exact 位于受支持 loose root 的 immediate child：
   - `${PULSARA_HOME}/skills/<name>`；
   - `~/.agents/skills/<name>`（只对用户能力页已枚举的 exact row）；
   - `<workspace>/.pulsara/skills/<name>`。
2. Root、target 与 descendants 使用 descriptor-relative no-follow 观察/删除；symlink 作为 leaf unlink，绝不沿链接删除 root 外内容。目标在确认期间被替换成另一个安装目录时返回 stale；可以复用 held directory 的 device/inode 等文件身份观察，不计算整树/逐文件 SHA、不新增删除 receipt。
3. 先在 held root 下把 exact visible child 原子 unbind 到 scanner 忽略的 private hidden delete stage，再递归清理 stage；不得逐文件删除 visible tree 后才让 catalog 消失。
4. 同时从 `skills.yaml` 中移除该 exact path 的 enable/disable override，使同路径未来重新安装时回到默认 enabled，而不是继承幽灵关闭状态。
5. 删除的是安装副本；原始 `source_path` 不受影响。
6. 若同名较低优先级 candidate 存在，它按现有 resolver 自然成为 winner；UI 不得声称整个 Skill name 已消失。
7. Plugin/bundled Skill 不进入该 operation；Plugin Skill 必须删除 parent Plugin。

Hidden delete stage 只是 descriptor-safe physical cleanup，不是 tombstone、receipt 或恢复 authority。Ordinary caught failure必须尝试清理；进程 crash 后残留仍由 scanner 忽略。本轮不增加后台 janitor；doctor 可以报告 exact Pulsara-owned residue，但不按 mtime 猜测删除未知目录。

### 7.3 Local MCP 删除

USER 与 WORKSPACE local entry 使用同一 native config removal owner：

1. exact source kind + `server_id` 查找；
2. optimistic compare current config identity；
3. atomic whole-entry removal；
4. workspace entry 同时撤销 existing local approval；
5. future supervisor publication 不再包含该 source；
6. already-borrowed old request/client/process按 existing owner drain/close；
7. 清理该 entry 的 managed credentials；environment refs只删除引用；
8. 刷新页面与所有相关 live sessions。

删除 user entry 后，某个 workspace 或 Plugin 中同名/相关 server 可能在其合法作用域继续存在；UI 必须重新 inspection，不得把“删除这一项”翻译成“所有会话均不存在同名 MCP”。

Plugin-produced MCP row不可单独删除；其详情提供credential管理与“管理所属插件”，删除入口只存在于parent Plugin。

### 7.4 Capability bundle/Plugin 删除

复用 `remove_local_plugin`，并收紧 stale input：

```text
scope
plugin_id
expected_current_package_install_id
workspace_root when WORKSPACE
```

在 instance lock 内重读 current state；package install id 不一致返回 `STALE`，不得删除 replacement 后的新包。

State unlink FULL 后：

- future enabled Plugin view 不再包含该 instance；
- 该 bundle 的 Skill、MCP、Hook 一起从 future capability composition 消失；
- instance state中的Plugin MCP connection overlays失去authority，同一个remove owner继续删除其managed credential bindings；environment refs只随state消失，不修改Host environment；
- already-retained Skill body、running Hook、MCP slot/request/process继续持有原 physical anchor直到 settlement；
- package root只在 unreferenced 且取得 existing exclusive package lock时由 existing GC删除；
- GC未立即回收不影响“Plugin 已卸载”的产品成功，因为 instance state已是唯一 current installation authority；
- `PLUGIN_DATA` 保留并在确认文案中明示。

不得逐个删除 bundle 内 component、修改 package root、绕过 state owner，或在 UI 成功前强制杀死 healthy old consumer。

### 7.5 Delete effect matrix

| 用户删除 | Canonical source cut | 附属清理 | 不受影响 |
|---|---|---|---|
| User loose Skill | exact user-root Skill child | exact `skills.yaml` path override、private delete stage | 原始安装来源、同名其他 root candidate、历史 transcript |
| Workspace loose Skill | exact workspace-root Skill child | exact workspace enablement override、private delete stage | user/inherited/Plugin/bundled candidate、其他目录 |
| User MCP | user `mcp.yaml` exact entry | managed credential bindings、old slot drain | workspace/Plugin server、environment variable、远端账户 |
| Workspace MCP | workspace `mcp.yaml` exact entry | workspace approval、managed credential bindings、old slot drain | user/Plugin server、其他 workspace |
| User Plugin | exact USER instance state | Plugin MCP managed credentials、future Skill/MCP/Hook contributions；package later GC | Plugin data、history、workspace instance、Host env values |
| Workspace Plugin | exact WORKSPACE instance state | Plugin MCP managed credentials、future Skill/MCP/Hook contributions；package later GC | Plugin data、history、USER instance、其他 workspace、Host env values |

---

## 8. Live adoption、continuity 与并发

### 8.1 Provider-input prefix continuity

Capability edit/delete不是新的 provider rebase boundary。

- same epoch 中 SYSTEM 与 provider tools byte-identical，messages append-only；
- Skill 删除只改变下一次 legal Skill observation；已经提供给模型的 Skill body不回写历史；
- MCP/Plugin edit/delete通过既有 supervisor/Plugin safe-point publication改变future availability；
- Plugin MCP credential变化只改变该native config的runtime/resolved physical identity并触发必要reconnect，不改变portable package bytes或stable semantic tool identity；
- 已安装的旧 DIRECT descriptor如果仍存在于 immutable tools prefix，availability gate必须返回 typed unavailable，不能调用已删除 binding；
- 新增或变化后的工具继续服从现有 DIRECT/META 与 cold epoch/compaction successor规则；
- 只有 new cold epoch 与 adopted compaction successor可重建 provider input roots。

### 8.2 Linear ownership

- 用户点击保存/删除只产生一个 capability mutation owner；double click在前端和controller lane均不得形成重复 physical mutation。
- Old MCP client/process、Plugin package anchor、Hook attempt与Skill retained body继续由其既有唯一 owner持有。
- Delete不夺取已借出的 handle，不 double-close，也不通过路径重新打开以假装原 object。
- UI request cancellation后，已经进入 filesystem/local-settings/config cut 的worker必须 shield+join 并取得 exact settlement；不得留下 detached mutation，也不能保证已断开的 browser 一定收到 response。

### 8.3 Live sessions

User capability mutation继续并行通知所有已打开sessions；一个session adoption失败不回滚已保存的用户真相，也不阻塞其他session。Workspace mutation继续只影响同一resolved workspace的sessions，但采用边界统一为“该session下一次provider dispatch之前的第一个合法safe point”，不再限定到下一次root user turn。这个传播由Web与`manage_capability`共用的mutation owner自动触发，而不是由发起变更的模型session单独调用`reload_capabilities`。

若mutation由当前root turn中的`manage_capability`发起，当前session是origin target：tool owner必须在允许旧execution borrow安全存活的前提下等待requested revision adoption，然后才完成ToolResult；这样模型能在同一turn的后续provider call中使用list/inspect/meta route验证。若adoption失败，ToolResult明确为PARTIAL，模型可以自然说明，但不能调用尚未证明available的新能力。其他session可以保持`PENDING_SAFE_POINT`直到自身下一次dispatch；这不是失败，也不需要固定轮询或唤醒一个空turn。

页面展示：

```text
已保存 / 已删除
已更新 N 个会话
等待安全采用 P 个会话
M 个会话需要重试
```

不得用固定轮询制造执行恢复；用户刷新、后续明确 mutation 或既有连接生命周期可以再次观察。

`manage_capability`的USER_FORM仍占用当前session唯一same-Host interaction slot；它可以按existing arbiter被更新或替换，但不能与同一tool call的另一份表单alias。表单关闭后，无论mutation成功、取消、拒绝、stale或partial，原tool execution owner都必须settle并把一个最终ToolResult送回模型。

---

## 9. MCP、Skill 与 Plugin installer Skill 契约

### 9.1 MCP installer

Bundled `pulsara-mcp-installer`必须以`manage_capability`作为模型参与正式安装/编辑的首选路径，同时保留production CLI作为用户明确要求或tool不可用时的out-of-band路径，并新增以下指导：

```text
Pulsara 的能力页支持 Streamable HTTP、stdio、显式 legacy SSE、Bearer、自定义秘密 Header、
OAuth 登录、stdio secret environment、配置导入及 tool exposure/effect/timeout 等高级 MCP 配置。

不要要求用户在对话、terminal command 或可见配置文本中粘贴 secret。
1. 查明公开 endpoint、transport、auth shape 与所需非秘密 Header/env 名；
2. 用 manage_capability 提交typed public candidate；不要先自行判断要不要弹窗；
3. runtime会在信息完整且permission允许时直接保存，否则自动打开“能力 → MCP”同款表单；
4. 用户若需要输入secret，只在该表单中输入；model与Skill均不得索取或复述；
5. manage_capability返回APPLIED + RELOADED后，直接使用list_mcp_servers /
   inspect_new_mcp_tool / use_new_mcp_tool或doctor验证exact server与representative
   safe operation，不再例行调用reload_capabilities；
6. 只有走CLI/手工文件等out-of-band路径，或结果明确为PARTIAL/adoption attention时，
   才使用reload_capabilities补救；
7. 只报告connector state、tool count、使用的工具与non-secret result。
```

推荐用户文案：

> 该 MCP 需要凭据。我会打开本地配置表单；请只在那里填写密钥，不要发送到对话里。保存后我会继续检查连接并实际验证工具。

Skill不应把所有高级配置都推给用户。对于有权且可由official management surface表达的非秘密配置，模型应形成尽可能完整的prefill；是否DIRECT由runtime和当前permission决定。只有secret、缺失的用户独占选择、existing trust/review或当前surface不支持的设置需要用户补充。

对于 OAuth，Skill 配置公开认证形状后使用 `AUTHORIZE_MCP` 唤起用户登录，不索取 token、cookie
或照搬其他宿主的私有 client secret。服务不接受现有客户端注册/授权失败时报告具体原因，不能声称
所有 OAuth 服务都已保证可用。GUI 手动添加/登录不需要模型先调用任何工具。

### 9.2 Plugin installer

Bundled `pulsara-plugin-installer`不得要求用户把Plugin MCP API key发进对话，不得把secret写进source package、转换candidate、`mcp.json`、terminal command或Plugin data。它必须遵循：

1. 用`manage_capability(INSTALL_PLUGIN)`进入existing exact validate/install workflow；secret不是package合法性或安装成功的条件，成功后仍先得到disabled instance。
2. 从inspection列出Plugin提供的MCP component及其公开endpoint/transport，不猜测credential slot。
3. 若Plugin文档或连接结果表明需要认证，用`CONFIGURE_PLUGIN_MCP_CONNECTION`提交non-secret binding shape，由runtime自动打开shared credential form；不让用户把secret发给模型。
4. `SET_PLUGIN_ENABLED(enabled=true)`始终复用existing exact-package review UI；只有用户SUBMIT产生call-local external-process acceptance，不能把install、credential、enable压成一次隐式信任。
5. 每个first-party mutation返回`RELOADED`后直接通过existing list/inspect/use路径验证；不要求重装Plugin，也不例行调用`reload_capabilities`。
6. 只有CLI/手工source等out-of-band变更或明确`PARTIAL`/adoption attention才调用reload补救。
7. 只报告configured/present、connection状态、tool catalog与non-secret call结果。

推荐用户文案：

> 插件已经安装，但其中的这个 MCP 还需要凭据。我会打开它的本地配置表单；请只在那里填写密钥，不要发送到对话里。保存后我会继续验证。

### 9.3 Loose Skill installer

同步 `pulsara-skill-installer` 与其 directory-contract reference：独立 Skill 不要求先转换成 Plugin，
也不要求模型代写。GUI 导入复用 §4.7；模型仍可使用现有正式 Skill CLI/service，不为本轮新增第二个
管理工具或 `manage_capability` 的 Skill action。CLI 与 GUI 共用来源验证、归一化与发布语义，
需要用户补描述等选择时如实说明，不私自生成来源未表达的说明。

明确完整资源随安装副本保留，支持来源布局不等于新增 runtime roots；无需用户删掉 Node/JS/TS 脚本。
只有具体必需的宿主行为不能表达时才解释缺口，不把“来源是 OpenCode”作为拒绝理由。
GUI first-party mutation 自动通知 adoption；CLI/out-of-band 沿用既有 reload 边界，不能把文件安装成功
表述为当前模型已经读到 Skill。最终通过现有 list/doctor、Skill 正文及相对资源读取验证实际可用性。

---

## 10. Web/API projection

具体 URL 可按现有 server routing风格调整，但必须只有一组 user operations 与一组 workspace operations。推荐形状：

```text
POST   /api/capabilities/mcp
PUT    /api/capabilities/mcp/{server_id}
DELETE /api/capabilities/mcp/{server_id}
POST   /api/capabilities/mcp/test
POST   /api/capabilities/mcp/import-preview
POST   /api/capabilities/plugins/import-preview
POST   /api/capabilities/mcp/{server_id}/authorize
POST   /api/capabilities/mcp/{server_id}/authorization/cancel
DELETE /api/capabilities/mcp/{server_id}/authorization

POST   /api/capabilities/skills/import-preview
POST   /api/capabilities/skills/install
POST   /api/capabilities/skills/remove
DELETE /api/capabilities/plugins/{plugin_id}
PUT    /api/capabilities/plugins/{plugin_id}/mcp/{local_server_id}/connection
POST   /api/capabilities/plugins/{plugin_id}/mcp/{local_server_id}/authorize
DELETE /api/capabilities/plugins/{plugin_id}/mcp/{local_server_id}/authorization

PUT    /api/sessions/{session_id}/capabilities/mcp/{server_id}
DELETE /api/sessions/{session_id}/capabilities/mcp/{server_id}
POST   /api/sessions/{session_id}/capabilities/skills/import-preview
POST   /api/sessions/{session_id}/capabilities/skills/install
POST   /api/sessions/{session_id}/capabilities/skills/remove
PUT    /api/sessions/{session_id}/capabilities/plugins/{plugin_id}/mcp/{local_server_id}/connection
```

要求：

- Skill `install` 是当前已存在的 endpoint，扩展有限归一化输入即可，不新增平行的外部 Skill 安装接口；
  `import-preview` 返回候选与兼容提示，复用同一 service。多选由 UI 逐候选调用同一 install operation，
  成功项即时显示，失败项可单独重试，不引入批处理 durable job 或整批回滚。
- create/update request 可一次性携带 secret mutation，但 request body不得被普通 logger/trace保存；
- inspection response只返回 editable non-secret config、credential presence与opaque current identity；
- update/delete携带 expected current identity；stale返回 conflict并附fresh inspection，不接受last-write-wins；
- Plugin delete增加 expected package install id；
- Plugin MCP credential update同样携带expected package install id，只能whole-replace该component的connection overlay；
- 同一 package 上的 overlay edit 还比较完整 expected old overlay，见 §6.4，不新增 overlay fingerprint；
- Skill delete只接受当前 inspection中exact owned path/row，不接受任意绝对路径作为通用recursive delete API；
- UI adapter不解析 CLI JSON，不操作 YAML，也不直接读写本地 settings 文件；
- server/controller不把 secret复制到 capability operation details。
- `manage_capability`不得绕过这些controller/service contracts直接写source；DIRECT与USER_FORM submit只是在actor/admission上不同。

管理操作的可复用 owner 必须位于 kernel 可依赖的 typed service/port 边界，不能让 builtin 导入
`web_app.session_controller` 或调用自身 HTTP。LocalSessionController 与 CLI 是同一 owner 的调用者，
不是 model runtime 的反向依赖。头部 Host/Origin/Fetch-Site、DELETE 支持与 body 物理边界复用
当前 HTTP middleware。沿用现有就绪划分：用户级 local capability 配置不依赖 PostgreSQL；带
session 的 workspace/model form 路径需要当前数据面 ready，不复刻记忆页 gate 到全部用户能力配置。

实现路由时保留已有 create/reconnect 的产品能力，并将旧 `/enabled` 写入入口收敛到同一 complete
update owner；不能留下旧简化入口绕过 auth/stale/cleanup 校验。`/mcp/test` 在动态 `{server_id}`
路由前注册或采用明确静态子路径，避免把 test 当成 server id。

### 10.1 MCP editable projection

上表为 user scope 示例；workspace auth/import/connection 操作复用同一 typed owner，按已有
workspace surface 传 exact scope，不重复实现。Plugin auth cancel 同 local cancel。OAuth loopback
callback 独立于这些 mutation URL；单次 flow 可观察状态通过现有 request/live projection 返回，
不新增 live event kind。Import preview 不保存 raw source/secret，不建立 durable preview ID；
提交时复用仍存活的原生 source observation，或 fresh read 后比较完整候选，不增加内容 fingerprint。
AUTHORIZE_MCP 的 USER_FORM 先确认目标，再由用户登录；尚无成功保存的目标时先完成普通保存操作，
不能把 install、enable、login 三种授权合成一个 token。

非秘密 inspection至少包括：

```text
server_id
display_name
source scope
enabled / required
transport kind and complete non-secret fields
auth kind
for each auth/secret binding:
  binding name
  source kind (managed | environment)
  configured/present boolean
exposure/scope/effect policy
parallel/stateless/refresh/timeout settings
opaque current config identity
current connector/catalog projection（exact-join后）
```

Secret value字段在任何 response schema中都不存在，而不是返回 `***` 伪值。

Plugin-provided MCP 使用同一 presence projection，并携带 source_plugin/local_server_id、current
package identity、完整 current connection overlay、package defaults 与 effective fields 的区别。
它不返回 local MCP update identity，不接受 package bytes 写入；仅接受 §4.3 的 instance 覆盖。

### 10.2 Model-triggered capability form projection

Current same-Host interaction projection增加一个closed `CAPABILITY_FORM` variant，至少携带：

```text
existing interaction identity / revision / existing expiry semantics
requested closed action
object kind / USER-or-WORKSPACE scope / public target identity
shared editor mode (create | edit | review | credential)
sanitized prefill
editable field schema/profile
public effect summary
permission reason（产品文案）
opaque expected-current guard
```

Projection不得携带existing secret value、credential-store binding internals、model/provider payload、raw Plugin package bytes或让frontend选择backend operation的自由字符串。Frontend按variant加载与能力页相同的form component；submission返回完整public candidate、closed secret mutation与opaque guard，直接交给唯一pending interaction owner。

Secret-bearing submission只可通过本地controller transport进入credential owner；request/response middleware、WebSocket/protobuf debug logging、error serialization与browser persistence均须显式排除其value。Live snapshot/reconnect最多重新投影non-secret form与`configured/present`，不得重放用户已经输入但尚未成功settle的secret。Controller断开后保留现有process-local interaction行为，不增加durable草稿或跨重启恢复。

同一interaction resolution必须原子选择`SUBMIT | CANCEL`之一并exact compare interaction identity/revision。普通tool confirmation的`ALLOW | DENY`继续原样存在；不得把capability form的完整candidate塞进通用allow boolean，也不得用先调用Web API、再单独点击ALLOW的两阶段split-brain流程。

Secret submission 使用窄 HTTP request body 直接交给 pending owner，不增加包含 secret 的 live event、
protobuf broadcast 或 reconnect state。表单草稿的公开字段可由 existing interaction 投影恢复；未提交
secret 只在当前表单输入状态中存在，关闭后清除。等待用户不占 mutation/settings/instance 锁，不消耗
provider/planning/physical mutation deadline；用户 SUBMIT 后才签发局部写入 deadline，之后另签发
adoption deadline。继承 existing interaction expiry 行为，不新增整个 turn/task/form-workflow 总时长上限。

---

## 11. 文件与 owner 变更上限

实施优先修改现有 owner；以下是上限，不是要求创建空壳模块：

```text
src/pulsara_agent/mcp_config.py
  closed secret refs/BoundSecretValue/OAuth、SSE variant、完整 local entry create/update/remove

src/pulsara_agent/settings.py
  复用唯一 LocalSettingsStore，增加 MCP credential collection 与 exact-owner 窄接口；
  更新 closed codec/所有 document mutation，保留其他设置；禁止复活 Keychain/keyring

src/pulsara_agent/conversation_kernel/mcp/sdk_facade.py
  官方 SDK session/transport 生命周期与 typed outcome 接线；删除自研 HTTP/SSE 状态机和 parser

src/pulsara_agent/conversation_kernel/mcp/wire.py
  区分依赖已承担的 frame/JSON 验证与 Pulsara schema/discovery 资源约束；不复制依赖 grammar

MCP 局部 HTTP client/transport policy adapter（必要时单独模块）
  统一 httpx2 注入；复用 DNS/IP/凭据策略与实际缺失的字节边界，不承担 MCP 协议解析/调度

src/pulsara_agent/conversation_kernel/mcp/oauth.py（新增窄模块，名称可按现有风格调整）
  SDK auth/TokenStorage ports、process-local login/refresh/callback owner；删除双 HTTP 库桥接与无证据的私有校验覆盖

src/pulsara_agent/plugins/importer.py（新增窄模块）
  selected-format/source observation、确定性 native candidate；不执行下载包

src/pulsara_agent/plugins/package_core.py
src/pulsara_agent/plugins/schemas/
  解析 dev.pulsara/mcp/connection-inputs.json；保留官方 portable schema，接通原生 SSE

src/pulsara_agent/capability/local_skill_management.py
src/pulsara_agent/capability/local_skill_publisher.py (或窄 removal owner)
src/pulsara_agent/capability/local_skills.py
  来源候选枚举、有限 frontmatter/目标目录归一化、完整资源安装；复用唯一 parser/publisher，
  typed loose Skill remove、descriptor-safe unbind/cleanup；不放宽 native 权限/激活语义

src/pulsara_agent/cli.py
  既有 Skill validate/install 接入同一有限归一化输入与诊断；不新增平行 importer/runtime

src/pulsara_agent/plugins/contracts.py
src/pulsara_agent/plugins/management.py
src/pulsara_agent/plugins/package_store.py
src/pulsara_agent/plugins/mcp_adapter.py
  instance connection overlay、native composition、remove stale guard；不复制 package GC

src/pulsara_agent/capability/builtin_catalog.py
src/pulsara_agent/capability/call_classifier.py
src/pulsara_agent/tool_permission.py
  注册唯一manage_capability descriptor；按closed action投影exact scope/effect并复用现有permission gate

src/pulsara_agent/conversation_kernel/tool_contracts.py
src/pulsara_agent/conversation_kernel/tool_runtime.py
src/pulsara_agent/conversation_kernel/tool_execution.py
src/pulsara_agent/conversation_kernel/host.py
  manage_capability preparation/authorization/execution、typed owner dispatch与automatic adoption；不复制mutation逻辑

src/pulsara_agent/conversation_kernel/runner.py
src/pulsara_agent/conversation_kernel/provider_dispatch.py
  将已登记 adoption 接入合法 preparation safe point；保留 model switch/compaction 的
  已取得 executable successor ownership，不重规划已 materialize 的 dispatch

src/pulsara_agent/conversation_kernel/interaction.py
src/pulsara_agent/conversation_kernel/live_control.py
src/pulsara_agent/ports/live_agent_event.py
src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto
src/pulsara_agent/terminal_protocol/v3_gateway.py
  process-local CAPABILITY_FORM projection/submission；复用唯一current interaction，不增加durable row/event
  同步 generated_v3/terminal_kernel_v3_pb2.py、现有 wire fixture/schema digest；
  沿用 CanonicalEntry.tool_result 精确关联和 latest_root_turn，不新增结果猜测器

src/pulsara_agent/web_app/http_server.py
src/pulsara_agent/web_app/session_controller.py
  user/workspace structured operations、secret request boundary、live adoption

frontend/components/capability-view.tsx
frontend/components/workbench-view.tsx
frontend/components/inspector-panel.tsx
frontend/lib/pulsara-types.ts
frontend/lib/runtime-adapter.ts
  shared add/edit/review form、独立 MCP/Skill import preview、conversation interaction、
  credential UX、三类删除与typed outcomes

src/pulsara_agent/bundled_skills/pulsara-mcp-installer/SKILL.md
src/pulsara_agent/bundled_skills/pulsara-skill-installer/SKILL.md
src/pulsara_agent/bundled_skills/pulsara-skill-installer/references/directory-contract.md
src/pulsara_agent/bundled_skills/pulsara-plugin-installer/SKILL.md
src/pulsara_agent/bundled_skills/pulsara-plugin-installer/references/conversion-contract.md
src/pulsara_agent/bundled_skills/pulsara-plugin-installer/references/codex-compatible.md
src/pulsara_agent/bundled_skills/pulsara-plugin-installer/references/pulsara-hook-extension.md
  user-owned credential handoff、独立 Skill 导入与完整资源使用、Plugin MCP配置指引
```

Capability mutation facade 若需要新模块，只创建承担实际 parser/owner 协调的窄模块，不创建空壳。
凭据持久化复用 settings.py，不新增 managed vault/Keychain adapter。Native MCP 通过注入 resolver
取得 exact 目标的值，`manage_capability` 只操作非秘密 intent。不得扩展为通用账号系统、独立 OAuth vault、
remote sync、database service、generic capability registry或第二套permission owner。

---

## 12. Hard-cut 实施顺序

**2026-09-06 的先行减法顺序：先完成下列 0.1–0.4，再继续 importer、表单与 live dogfood。当前已完成该减法，恢复后续验收。**
保留未提交的管理面工作，不 reset、不把部分实现当成已激活。网络替换不授权修改 DB、模型 provider、
memory、compaction 或 Skills/Plugin 的产品语义。

0.1 按 §6.3 逐项核验本机锁定 SDK/HTTP 依赖 API，以一个 keyless HTTP、一个 SSE、一个本地 OAuth
    fixture 验证官方 transport + 自定义 HTTP client；stdio 另验证精确环境和关闭。测试不能继续只覆盖
    旧 `_Bounded*` fake 后宣称 SDK 接线可用，也不重新实施整套 SDK conformance tests。
0.2 对字节/shape、stdio env/frame 与 auth replay 的实际缺口形成最小取舍，更新 §6.3；已有依赖能做
    的直接复用，缺扩展口的不靠复制协议补齐。不因尚未证明而先扩大自研实现。
0.3 完成单一路径替换：SDK transport、同一 MCP httpx2 policy adapter、SDK OAuth ports；删除本节
    列出的旧 parser/HTTP/SSE/bridge，复核资源限制、请求结算、unique ownership 与 healthy stream。
0.4 跑 §13.10 和受影响的 MCP/credential/supervisor/prefix tests，确认没有管理面回归后，才恢复下面
    尚未完成的功能顺序。完整激活仍需 §14 全量验证与 dogfood；不因减法阶段通过就宣称整篇完成。

先打通独立 MCP 与可移植 Skill 的纯 GUI 使用闭环，再让 Plugin 包装与模型入口复用它们。
这是实施优先级，不是分期兼容：以下范围仍在同一次 hard cut 内完成，不保留旧路径或 feature flag。

1. 冻结本文与 active spec同步点；确认数据库/event oracle不变。
2. 建立 MCP secret reference 并扩展唯一 LocalSettingsStore，先完成配置保留/凭据 CRUD/physical snapshot 纯本地 tests；不引入 Keychain 或 sidecar。
   同步实现 OAuth grant/client info codec、BoundSecretValue 的单一 resolver，不包含 pending flow 持久化。
3. 将 local MCP mutation 收敛为 complete typed create/update/remove/test；删除 Web/CLI 中手工拼接缩减 entry 的重复路径。
   复用前置减法已验证的 SDK HTTP/SSE 与 OAuth 接线，证明无绕过资源/网络边界或 tool replay。
   然后实现用户 authorize/cancel/logout、single-flight refresh 与 callback/late-write tests。
4. 打通独立 Skill 来源枚举、有限归一化、完整资源安装与 descriptor-safe removal，同步清理 exact enablement override。
   用真实来源 fixtures 验证可移植 Skill 无需模型改写；不增加 runtime roots 或第二个 publisher。
5. 扩展 Web controller、独立 MCP 配置导入和 MCP/Skill GUI；先完成手动/导入/补参/登录/使用/删除闭环。
   所有 first-party mutation 共用 automatic live-adoption owner，workspace 在下一次 provider dispatch 前的
   合法 safe point 采用；secret request 全链路不日志/回显。先跑纯 GUI focused tests 与本地协议 fixtures。
6. 在existing Plugin instance state中加入per-component non-secret connection overlay，并在`PluginMcpAdapter`中与immutable package definition exact join；不给portable schema增加伪slot。
   接着实现 source-format importer 与 client extension input schema；从真实包 fixtures 生成 native
   candidates，先验证 empty/default/component selection，再接 GUI；不照抄统计脚本扫描规则。
7. 给 Plugin removal增加 expected package install id stale guard与managed credential cleanup，继续复用existing state/anchor/GC；Plugin mutation 接入第5步同一个 adoption owner。
8. 在builtin catalog注册唯一ROOT-only `manage_capability` closed descriptor；实现prepare-first invocation与resolved physical effect projection，再接入existing call classifier/permission gate。先证明四种mode与Plugin/credential physical-location矩阵，禁止临时bypass-only special case。
9. 实现DIRECT路径，只dispatch到已有 MCP/Plugin typed owners；在工具调用栈内完成 origin future publication，不等待自身下一次 dispatch 或 borrow drain。mutation/adoption分层，一个tool call只产生一个合法 attempt/state 的最终ToolResult。
10. 扩展existing same-Host current interaction为typed `CAPABILITY_FORM`与process-local authorization disposition，明确READ_ONLY form-only actor边界，实现exact submit/cancel与controller-loss settlement；不改数据库/event oracle。
11. 将 Plugin instance 连接覆盖和 workbench interaction 接入第5步已有共享 editor/schema；
    不重做另一套 MCP 配置或认证流程。Package definition 保持只读。
12. 给user Skill/MCP/Plugin与已有workspace Skill/MCP management surface补齐删除入口和确认。
13. 更新 MCP、loose Skill、Plugin 三个 bundled installers 及 directory/conversion/Codex/Hook references，删除 OAuth 一律不支持、
    conversion 只能由模型完成、Plugin 普通连接参数不可覆盖、成功后例行 reload 等过时描述。
    保留不支持的真实宿主行为边界；外部格式别名只存在于 importer，不在 native codec/runtime 双读写。
14. 完成 MCP/Skill 纯 GUI、真实 MCP 认证/调用、Skill 正文及资源使用 dogfood，再完成 Plugin 与 model-assisted dogfood；运行完整 focused、Python/frontend、PostgreSQL clean-v0 verify。
15. 同步active specs，删除旧no-edit/no-delete/no-Plugin-credential、model只能口头指导、manual reload happy path契约，以及compat aliases、旧DTO fields与重复UI/API路径。

不得保留 v1/v2 form、old/new auth双读写、临时 feature flag、raw YAML fallback或“编辑其实是删除再添加”的前端伪实现。

---

## 13. Required semantic golden matrix

### 13.1 MCP add/edit

| 场景 | 必须结果 |
|---|---|
| 添加 keyless HTTP | 保存完整 native entry，连接状态来自 exact live config |
| 编辑 display name | server identity不变，whole-entry atomic replace |
| 编辑 endpoint/command | new physical config发布；old in-flight consumer安全drain |
| 尝试编辑 server_id/scope | UI不提供；API拒绝 |
| stale browser编辑 | conflict + fresh inspection；不得覆盖 current |
| test成功后取消 | 无 config、secret、client/process残留 |
| test失败后保存 | 允许保存，显示真实连接失败 |
| 改为 NoAuth / 删除 Header-env target / managed 改 environment | 只清理不再引用的 managed value，不保留幽灵凭据 |
| secret 写入后 config publish 失败 | 当前 owner 有限回滚本次值；已 FULL 或无法确定时明确 partial，不伪称未修改 |
| same-epoch tools变化 | 不重写 provider prefix；按existing DIRECT/META规则采用 |

### 13.2 Credentials

| 场景 | 必须结果 |
|---|---|
| managed Bearer | exact `Authorization: Bearer <value>`只进入HTTP physical request |
| managed custom Header | exact configured header注入；值不进response/log/trace |
| environment Bearer | Host env有值则连接；缺失则typed needs-credential |
| stdio managed secret env | 只进入exact child env；不进入command/args/output |
| ordinary edit未触碰secret | old binding/value保持 |
| replace secret | runtime physical identity变化并重连；不改semantic tool identity |
| clear required secret | config保留但needs-credential；不匿名fallback |
| 引用 `PULSARA_API_KEY` | admission拒绝 |
| OAuth server | 用户登录进入 §5.7；无有效 grant 显示需要登录，不伪装 keyless/Bearer |
| capability inspection | 只有presence/source kind，没有secret value字段 |
| local settings 保存/删除 MCP value | 复用唯一 writer，保留模型/DashScope/PostgreSQL/其他 MCP 配置，仍为 0700/0600 |
| 已解码返回值含 exact credential | transport owner 内替换文本；若命中 identity/schema key 则拒绝对应结果，不伪造可执行名称 |
| stdio stderr | 沿用丢弃，不为了新 scrub set 新增 stderr retention/logging |
| credential rotation时old request仍运行 | old/new physical client分别持对应 frozen header/env 与 scrub set直到各自drain，不逐次 request 偷换值 |

### 13.3 Plugin MCP credentials

| 场景 | 必须结果 |
|---|---|
| 安装含authenticated MCP的Plugin | package可先成功安装；缺凭据只影响exact MCP连接 |
| 连续安装向导填写key | 后台严格settle为install-disabled → connection overlay → enable；install request/validator从未接收secret |
| install失败或instance不存在 | 不创建credential binding，不进入后续配置/enable operation |
| Plugin尚未enable | MCP列表仍从installed inspection显示可配置行，但不发布runtime/model capability |
| 从Plugin详情配置 | 与MCP列表入口读写同一个instance overlay |
| 修改 instance endpoint/header/env | 按 §4.3/6.4 保存覆盖，原始定义仍可查看；package bytes 不变 |
| 修改 command/args/cwd/transport family 或包内定义 | instance API 拒绝；需要 package replace，不伪装普通配置 |
| 配置Bearer/custom Header/secret env | 只写non-secret overlay与local credential store |
| package没有slot声明 | UI不伪称已声明；用户显式选择auth kind和target name |
| USER与WORKSPACE同一Plugin | credential bindings隔离，不互相继承 |
| stale package install id | 不向replacement写入overlay，返回fresh inspection |
| 同一包上的 stale overlay | 完整 expected-old overlay 不匹配时 conflict；不增加 overlay hash/revision |
| enable review 等待期间 overlay/presence 变化 | SUBMIT 重读并要求刷新 review，不凭相同 package id 使用过时认证配置 |
| same component合法replace | overlay通过new definition重验后保留 |
| replace改变physical destination | new package保持disabled；enable review显示new destination与non-secret credential摘要，接受前不使用旧value |
| component消失/transport变化/target非法 | new runtime不采用old overlay，明确要求重新配置并清理obsolete managed value |
| overlay与package public Header/env同名 | UI明确提示覆盖；runtime只采用secret overlay value，package bytes不变 |
| disable/re-enable | overlay与managed value保留 |
| 仅清除 credential value | overlay/ref仍在，missing 时不匿名 fallback |
| 显式移除 connection overlay | package definition仍在，回到package-derived配置；若服务仍需认证则显示真实连接失败 |
| model/installer验证 | 模型只能看到presence与连接结果，不能看到value |

### 13.4 Skill deletion

| 场景 | 必须结果 |
|---|---|
| 删除 `${PULSARA_HOME}` Skill | exact visible child unbind并清理；source目录不变 |
| 删除 `~/.agents` Skill | 明确共享影响后exact删除；不触碰其他child |
| 删除 workspace Skill | 只影响同目录；user/inherited保持 |
| 删除disabled Skill | exact `skills.yaml` override一并清理 |
| 高优先级同名被删 | lower candidate按resolver自然出现 |
| target被换成symlink | no-follow拒绝/只unlink leaf，绝不越界 |
| cleanup中断 | visible capability已切除时报告partial cleanup，不伪造rollback |
| bundled/Plugin Skill | 不显示独立删除；指向parent source |

### 13.5 MCP deletion

| 场景 | 必须结果 |
|---|---|
| 删除user keyless MCP | exact user entry消失；所有live sessions收到refresh |
| 删除managed-secret MCP | config authority先切除，credential随后删除 |
| credential cleanup失败 | MCP仍已删除；明确partial outcome，不恢复连接 |
| 删除后同一 server/component 重新安装，再点击旧 cleanup 重试 | 检测新 owner/引用并停止，不删除新凭据 |
| 删除env-ref MCP | 不修改process env或env file |
| 删除workspace MCP | exact approval一并撤销，只影响同workspace |
| stale config identity | 不删除新replacement，返回conflict |
| running MCP call | old request按existing outcome owner settle；future admission unavailable |
| Plugin MCP row | 无独立删除；管理parent Plugin |

### 13.6 Plugin deletion

| 场景 | 必须结果 |
|---|---|
| 删除disabled Plugin | exact instance state删除 |
| 删除enabled Plugin | future Skill/MCP/Hook三类贡献同时消失 |
| Plugin含managed MCP credentials | instance authority先切除，managed values随后清理；Plugin data仍保留 |
| package刚被replace | old expected install id返回STALE，不删new state |
| old MCP/Hook仍运行 | package lock保持；GC报告IN_USE而非强删 |
| consumers drain后GC | unreferenced immutable package可回收 |
| USER instance删除 | 同名WORKSPACE instance不受影响，反之亦然 |
| Plugin data存在 | 默认保留且UI明确说明 |

### 13.7 UI

- Add 与 edit 必须共用同一 form component/schema projection。
- Secret input 不进入 React debug output、toast、error boundary、test fixture snapshot或browser persistence。
- Existing secret不回填；“已配置”与 replace/clear交互可键盘访问。
- Plugin package definition 只读，有限 connection overrides/credential 可编辑；两入口状态一致。
- Owned rows有真实删除；inherited/bundled/component rows没有假按钮。
- Delete confirmation清楚区分 Skill directory、MCP credential与Plugin data。
- Mutation settle前关闭/backdrop/重复提交入口按现有规则锁定。
- Backend stale/partial/attention必须有可理解文案，不能只显示异常名。
- 已展开项重复点击不重复读取；切换详情/表单的异步更新不清空整页、不自动滚动范围栏，过期响应不能覆盖当前选中项。

### 13.8 Model participation、permission与reload

| 场景 | 必须结果 |
|---|---|
| ROOT + READ_ONLY调用manage_capability | tool可调用但无模型DIRECT write；自动打开prefilled form，用户submit作为独立user-control-plane authority |
| ROOT + ASK_PERMISSIONS完整keyless candidate | 进入existing permission confirmation；Hook没有合法ALLOW/DENY时自动打开shared review/editor，用户确认后原tool call继续settle |
| ROOT + ACCEPT_EDITS创建workspace keyless HTTP MCP | candidate完整且无需existing review时DIRECT |
| ROOT + ACCEPT_EDITS创建user MCP | 按outside-workspace write语义打开form，不偷换成workspace write |
| ROOT + ACCEPT_EDITS安装/启停/删除任意scope Plugin | package/state实际位于PULSARA_HOME，按outside-workspace write打开form；WORKSPACE namespace不冒充workspace filesystem |
| ROOT + ACCEPT_EDITS操作WORKSPACE MCP managed credential | YAML可在workspace，但credential store effect为outside-workspace，create/replace/clear/cleanup均打开form |
| ROOT + ACCEPT_EDITS创建/启用stdio或Hook-bearing capability | 按existing terminal/executable effect打开form |
| ROOT + BYPASS_PERMISSIONS完整keyless MCP | DIRECT；不因MCP类别本身强制弹窗 |
| ROOT + BYPASS_PERMISSIONS缺Bearer value | 自动打开credential form；不得匿名安装、猜值或要求在chat粘贴 |
| SUBAGENT调用manage_capability | 明确拒绝；不能转发secret请求或借ROOT authority |
| 模型传simple/skip_confirmation/raw YAML/path/secret | schema或admission拒绝，不进入mutation |
| 模型prefill后用户修改公开字段 | 重新parse并执行用户实际提交的完整candidate，不执行旧草稿 |
| form target在等待时变化 | stale/conflict + fresh non-secret inspection；不覆盖current |
| 用户取消/拒绝/关闭form | 原tool call得到CANCELLED/REJECTED ToolResult，模型可继续自然语言收尾 |
| form 取消/参数拒绝发生在 attempt 前后 | 按 §6.5 分别映射 no-attempt / attempted result_state；业务 REJECTED/PARTIAL 不写成 canonical enum |
| controller缺失或interaction被替换 | 无mutation；pending owner关闭且tool call确定settle，不永久显示“操作未完成” |
| model安装完整Plugin | permission允许时可DIRECT安装为disabled；不隐式enable |
| 任意mode执行SET_PLUGIN_ENABLED(true) | 恒进入exact package review form；只有user SUBMIT生成call-local ExternalProcessAcceptance，模型参数/数据库没有acceptance |
| Plugin credential动作需要value | shared credential form；value不进入tool args/result/Hook/transcript |
| model tool origin mutation成功且origin adoption成功 | 同一turn下一次provider call前完成requested revision；ToolResult为APPLIED + RELOADED，可直接list/inspect/use |
| origin 当前 tool 仍持 surface borrow | inline future publication 正常完成，不等待该 borrow 或下一次 dispatch，ToolResult 只提交一次 |
| RELOADED 但远端无响应/认证失败 | source adoption 成功，connector 仍失败；不伪称已连接或工具一定可用 |
| 已冻结 compaction/model-switch executable successor 后收到 refresh | 不丢弃或重新 materialize 该 plan；revision 在下一合法 preparation point 采用 |
| mutation 已 FULL 后请求取消或断连 | join 实际 settlement；不返回“本次未更改”或自动重放写入 |
| model tool origin adoption失败 | capability truth保留；ToolResult为APPLIED + PARTIAL，不伪称新能力已可用 |
| Web mutation影响busy matching session | API可返回PENDING_SAFE_POINT；该session下一次provider dispatch前采用，pending不算失败 |
| automatic adoption为RELOADED | 模型不再调用reload_capabilities，直接list/inspect/use验证 |
| CLI/手工YAML/外部installer修改 | 不自动猜测；明确调用reload_capabilities后再验证 |
| automatic adoption为PARTIAL | capability truth不回滚；问题解除后允许显式reload补救 |
| same-epoch automatic/manual reload | SYSTEM/tools既有prefix不重写，messages只追加suffix |
| permission Hook观察manage_capability | 只见public candidate/effect；form secret永不进入Hook input |

---

### 13.9 外部格式与参数导入

| 场景/真实样本 | 必须结果 |
|---|---|
| 原生 Stripe/Cloudflare/Neon/Atlassian 包 | 选定原生发行目录后经唯一 loader；无需模型转换 |
| 同 repo 多宿主 manifest | 显式选择一个，不自动求并集 |
| Slack Codex `mcpServers:{}` / PostHog 空 hooks | 不加载相邻宿主的 MCP/Hook |
| Sentry manifest 内联 MCP / Datadog 自定义 MCP 路径 | 跟随选定声明读取，不只看 `.mcp.json` |
| MCP wrapper/bare map/OpenCode local command array | 完整保留 ID、顺序、cwd、env；映射后同一 native parser |
| 重复 JSON key、冲突 alias、未知行为字段 | 定位 exact field；不 last-write-wins 或静默丢弃 |
| Cursor variables / `${VAR:-default}` / `{env:VAR}` | 有限模板语义正确，无 eval/shell expansion |
| Cowork 空 URL | 本地预览等待填写；不安装假 endpoint，不向远端发 placeholder |
| 缺 key、公开定义完整 | 可安装 disabled Plugin/保存 MCP；连接需要认证，不把安装当登录 |
| 用户粘贴 JSON 含私有值 | 本地分类后仅写 private settings；原始 source/draft 不进普通日志/模型/包 |
| 未声明用途的 Header/env literal | 用户本地确认普通/私有，不能猜成 key，也不先发布到公共 projection |
| 私有值有前后缀、多变量拼接 | 单一 BoundSecretValue resolver；exact header/env 输出与 scrub-set 同步 |
| OpenCode `{file:path}` / headersHelper | 前者需显式具体文件读取；后者不执行，不伪装为静态值 |
| Auth/endpoint 改变后复用旧 key | 用户明确确认新目的地与凭据使用；模型必须走 USER_FORM |
| App/Channel/宿主专属 agent 或 Hook | 不宣称整包成功；可另选独立 MCP 安装，不静默裁剪 |
| Hook 可等价子集 | 原命令/脚本字节保持；事件/输入/控制均符合既有 Hook 契约 |
| source drift/symlink/路径逃逸 | 沿用既有 source admission，不能让转换绕过 |

### 13.10 OAuth 与 SSE

| 场景 | 必须结果 |
|---|---|
| 实际 transport ownership | fixtures 通过官方 streamable_http_client / sse_client；未调用旧自研 writer/listener/parser |
| SDK SSE event bound 与 Pulsara 原 bound 差异 | 按 §6.3 明确采用 SDK 的 1 MiB event/pending-line 限制，验证 GET/POST 路径；大于此值的单事件不再承诺接收，不复制 parser 扩大它 |
| 普通 JSON / 解压 / JSON shape | 已解码 body 在 SDK 解析前限制 16 MiB；结构检查在 SDK carrier 之后，不宣称 pre-allocation 或旧 per-slot raw buffer 等价；不累计健康长流总字节 |
| SDK client 注入与 URL policy | 首次请求、后续 POST/GET/DELETE、redirect、OAuth metadata/token 都走同一策略实现；不偷用默认 client |
| SDK GET + Last-Event-ID 续读 | 原工具 POST 次数不增加、结果仅交付一次；不误判成工具重执行，不创建 durable resume |
| SDK auth-flow 重新 yield 原 tools/call | 不重新发送可能有副作用的调用；与独立登录握手、metadata 请求区分 |
| client/SDK context 取消、配置替换、旧 borrow drain | 唯一关闭，无 orphan task；旧请求正常结算，不重写 provider prefix |
| SDK stdio 环境/超长未完成行/子进程关闭 | 精确 spawn 与原资源边界有实际接线证据；不以默认 env/无限未完成行缓冲冒充等价 |
| 后台 reload 遇到 OAuth challenge | 展示需要登录，不弹浏览器、不占 provider 执行等待 |
| 预注册 client / DCR / 配置有效 CIMD | 经通用 SDK protocol path 完成；没有 provider-name 分支 |
| 无 DCR/有效 client metadata | 用户可填写注册 client；不编造或窃用别的宿主 client |
| PKCE/state 错误、重复回调、错误回调路径 | 不接受 token，不影响其他 owner 的登录 |
| 用户取消/关闭/Host 重启 | pending process-local 状态释放；无 durable resume；原模型调用正常结算 |
| 删除/改目标/logout 后旧 callback 或 refresh 返回 | 不写回 grant，不恢复已注销连接 |
| 同 owner 并发 refresh / 两 owner 同 callback port | 前者单一 flight；后者正确关联或明确冲突，无错配 |
| metadata/resource/issuer/redirect 网络检查 | 合法不同 issuer 可接；恶意内网 redirect/越 audience 不放行 |
| token 到期/refresh token 被轮换/invalid_grant | 正常更新或提示重新登录，不能后台无限重试/匿名 fallback |
| 静态 header 与 OAuth | 同一请求只采用选定 auth；MCP credential 不发给 metadata/token 的其他 origin |
| refresh 时旧响应仍在回传 | old/new exact token 均在对应 physical owner scrub set，request header 不半途换值 |
| tools/call 可能已执行后 401/断连 | 原 unknown/failed outcome 保留，不自动 replay 工具 |
| test 需要交互登录 | 返回 needs-login；不为 disposable candidate 创建长期 grant |
| OAuth logout / capability 删除 | 前者仅本地注销，后者清理全部专属记录；远端不声称已 revoke |
| model AUTHORIZE_MCP | 用户确认后才开始授权；token 不进 canonical attempt/result/Hook；取消只结算一次 |
| SSE endpoint event + relative message URL | SDK 合法解析，Pulsara 网络校验与资源保护，单一 sessionful owner |
| SSE message URL 跨 origin | 不转发旧 credential，不用降低网络边界来兼容 |
| HTTP 404/500/429/401 | 不自动改 SSE；现有有证据的 discover/initialize 协商不受影响 |
| SSE 超帧/重复关闭/healthy 长流 | 既有字节与结算边界生效，无 double-close、无 stream 总寿命 cap |

### 13.11 Skill 与 OpenCode 来源互操作

| 场景 | 必须结果 |
|---|---|
| 从 `.opencode/skill(s)` / `.claude/skills` / `.agents/skills` 选定来源 | 枚举后复用现有安装 owner；不新增隐式 runtime roots |
| Skill 含 references/scripts/assets 与相对链接 | 安装副本完整，模型通过既有 Skill 工具读到正文并能找到引用资源 |
| description 缺省 / 来源目录名与 name 不同 | GUI 补齐或明确归一化后安装，source 不变；不要求模型手工改形状 |
| 一批 Skill 同时包含有效、待补描述与重名候选 | 逐项结果，失败不吞掉成功；不覆盖、不整批回滚；取消预览不发布 |
| 归一化后的安装副本 / 预览后来源变化 | 最终副本重新走 native 校验和既有 source/publisher 边界；preview 不冒充安装 authority |
| 附加展示 metadata / 真实权限字段 | 前者保留 inert；后者不静默降级，不按陌生字段统一拒绝 |
| 普通 Skill 中提到 OpenCode 工具示例 | 不基于产品名字自动拒绝；只有具体不可表达的必需行为才报告缺口 |
| JS Plugin 同仓库带独立 MCP/Skill | 用户可明确选择独立导入，不执行 JS Plugin，不宣称整包成功 |
| Node/TS 标准 MCP server / Skill JS 脚本 | 不按语言屏蔽；分别服从既有 stdio/工具执行与权限边界 |
| JS/TS Plugin entrypoint | 不 import、不执行、不建立 shim，清楚提示是宿主插件类型而非普通 Skill |
| 导入后的重名/启停/删除 | 复用现有 resolver/owner，删除安装副本不影响源目录 |

## 14. 验证与 dogfood

### 14.1 Focused tests

至少覆盖：

```text
tests/test_local_skill_management.py
tests/test_project_capability_management.py
tests/test_round6_mcp_production.py
tests/test_round9_3_agent_plugin_product.py
tests/test_round9_3_plugin_physical_semantics.py
tests/test_round9_unified_capability_semantics.py
tests/test_round4_plan_workflow.py
tests/test_stage2_conversation_runner.py
tests/test_stage2_protocol_v3.py
tests/test_local_web_http_surface.py
tests/test_memory_tool_result_rejection.py
tests/test_round3_1_provider_input_prefix_continuity.py
tests/test_bundled_skills.py
frontend/app/pulsara-app.test.tsx
frontend/lib/runtime-adapter.test.ts
```

另扩展 `tests/test_settings.py`、`tests/test_round5b_long_horizon_context_compaction.py` 与能力页 component tests；
新增 MCP/Plugin importer、Skill 导入归一化、OAuth、SSE focused tests 按实际 owner 命名；
Skill service/publisher 与前端都须覆盖 §13.11，不能仅靠既有删除测试代替导入、完整资源使用测试。
本规范不把这些待建文件当现成通过证据。
使用市场固定 commit 的最小真实 fixtures，保留来源/许可证；无需 vendoring 整个市场或新增内容 SHA。
不要把不存在的测试名当成已运行证据。Model-switch/compaction 的既有 focused tests 要覆盖新增 safe-point
接线；不是修改其语义。本地 MCP/Plugin fixtures 可复用，新增测试文件只按实际 owner 划分。

新增/扩展测试必须至少分层证明：

- `manage_capability`只有一个descriptor与一个execution owner，closed actions逐项schema/classifier覆盖；
- preparation先形成不可伪造的resolved effect projection；permission不从模型path/risk标签推导；
- ROOT四种permission mode矩阵、SUBAGENT拒绝；READ_ONLY只允许展示用户表单而不能把DENY升级成model permit，其他DENY仍走no-attempt；
- physical effect mapping覆盖USER/WORKSPACE local MCP YAML、两种scope的Plugin PULSARA_HOME state、managed credential store与stdio/Hook process；
- ACCEPT_EDITS下Workspace Plugin与Workspace MCP managed-credential create/clear/cleanup仍按outside write询问；
- DIRECT与USER_FORM汇入同一个MCP/Plugin typed mutation fake，既不shell out也不double apply；
- shared form prefill/edit/submit/cancel/stale/controller-loss与最终ToolResult settlement；
- Plugin enabled=true恒由exact review SUBMIT产生call-local ExternalProcessAcceptance；disable不携带acceptance；
- secret不出现在tool args/result、live projection、Hook input、protocol diagnostics或frontend state snapshot；
- model-origin first-party mutation在同一turn下一次provider call前settle requested adoption revision；old surface borrow可drain且无deadlock/double-close；
- Web-origin busy session返回PENDING_SAFE_POINT并在下一次provider dispatch前采用；pending与PARTIAL区分；
- first-party mutation只调用一次底层refresh；`RELOADED`后没有第二次model reload；
- CLI/manual source变更保持不可见直到explicit reload，reload仍不rebase same-epoch provider prefix；
- Plugin install-disabled、credential、enable三个settle保持独立。
- LocalSettingsStore 新凭据字段不会在保存 PostgreSQL/模型/DashScope 时丢失，反向也不会覆盖这些配置；
- 两个 same-Host 入口共用 mutation lane；cleanup retry 不误删重建 owner 的凭据；
- body 业务结果与 canonical attempt/result_state 分层，工具拒绝后模型继续回复且下一轮可运行；
- inline adoption 不与自身 borrow、mutation lock 或未来 dispatch 循环等待；fresh physical deadline 不含用户填表时间。

不得弱化现有 assertion、增加 skip/xfail或通过隐藏真实错误换绿。

### 14.2 Full verification

使用 repository-root `uv` 管理的 `.venv`：

```text
.venv/bin/pytest -q
cd frontend && npm test
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
cd frontend && npm run build:local
git diff --check
```

按当前 active database spec 对已核实的 loopback disposable PostgreSQL执行 clean-v0 migrate/deep verify。本文不应改变 schema；oracle任何增长都视为 blocker。

### 14.3 MCP、Skills 与 Plugin dogfood

先执行第12项中的独立 MCP 纯 GUI 流程及第16项的 Skill 流程，再执行 Plugin 与模型协作流程。
下面编号仅为场景标识，不是实现或执行优先级。至少完成：

1. 实际可用的 keyless HTTP MCP：模型调用`manage_capability` → bypass下DIRECT add → automatic reload → list → inspect → safe tool call；不得出现例行第二次reload。不预设 Firecrawl 或任何特定第三方永远提供免凭据 endpoint；外部不可用时报告阻塞，并以自管 HTTP fixture 证明本地链路。
2. 同一keyless candidate分别在ASK与ACCEPT_EDITS user/workspace scope运行，证明表单/直达分流符合13.8而非bypass-only；另以Workspace Plugin与Workspace MCP managed credential证明physical outside-write不会被逻辑scope放宽。
3. Authenticated HTTP MCP：模型发起typed public candidate，runtime自动打开shared form；用户只在表单输入Bearer，页面不回显，连接发现完整catalog并完成一个safe call。
4. Edit：从能力页与模型各变更一个non-secret setting，证明共用typed owner、old/new physical cut与prefix continuity。
5. Delete：删除连接后future call unavailable，managed credential不再存在。
6. stdio fixture：managed secret env只到child process；fixture主动回显secret时transport boundary拦截，trace/terminal/provider均不含值。
7. Skill user/workspace delete与duplicate fallback。
8. Authenticated Plugin MCP：模型在bypass下可先直接安装disabled Plugin，再由自动表单配置credential并完成exact-package enable review；只由user SUBMIT产生call-local acceptance，不改package bytes，automatic reload后完成一个safe call；disable/re-enable保持binding。
9. Replace Plugin：stable component保留并重验credential；new package在enable acceptance前不使用old value；removed/incompatible component不采用old binding。
10. Enabled Plugin delete：future contributions消失、managed MCP credentials清理、Plugin data保留，old physical consumer drain后GC。
11. Out-of-band CLI修改：变更本身不被runtime猜测；一次explicit`reload_capabilities`补救后采用，且不改provider prefix。
12. 纯 GUI：不启动模型，粘贴 MCP 配置 → 预览 → 填写私有值 → 保存/测试；再以本地外部 Plugin
    manifest → native candidate → disabled install → instance connection → review/enable 证明无需模型。
13. 使用真实 Firecrawl HTTP MCP 及其 stdio 形状验证 key 注入；另用自管 fixture 验证 Datadog 双 Header
    与普通参数，不把 fixture 声称为 Datadog 账号联通。Firecrawl CLI-only Plugin 不冒充 MCP Plugin。
14. 一项实际可授权的第三方 OAuth MCP，GUI 浏览器登录、连接、安全工具调用、注销再登录；账户/注册
    不可用时报告具体环境阻塞，并完成本地 OAuth server 的 PKCE/DCR/refresh/late-callback fixtures。
15. 自管真实 HTTP+SSE server 验证 discovery、safe call、断流/取消及删除；外部公开 SSE 可用时增加
    远端证据，不能以某第三方一直在线作为本地实现通过条件。
16. 从选定外部 Skill 目录通过 GUI 安装含 references 与无副作用脚本的真实样本；通过既有 Skill
    读取/工具路径验证正文、相对资源与一次安全使用，再验证启停/删除。另以同仓库 JS Plugin 未执行的
    fixture 证明独立导入，不把仅扫描出 SKILL.md 当完整 dogfood。

Real provider dogfood保留实际 prompt、provider-visible messages/tools、model reply、non-secret MCP result与失败详情；不得记录任何 MCP credential value，也不得输出 `PULSARA_API_KEY`。

若外部 API credential/无凭据服务不可用，remote dogfood精确报告environment blocker；synthetic authenticated HTTP/stdio fixture、全部本地测试与自管 keyless路径必须完成，但不得把它们宣称为第三方真实认证/远端可用性已通过。

---

## 15. Definition of Done

1. 用户能力页可以用同一 structured editor添加和直接编辑 MCP。
2. Current native HTTP/stdio/auth/exposure/effect/timeout/concurrency配置有完整但渐进披露的产品入口。
3. Plugin package definition 保持 immutable/read-only；MCP 列表和 Plugin 详情共用有限 instance
   connection override/credential 管理，支持参数化外部包，不改 executable/Hook/transport family。
4. 唯一ROOT-only `manage_capability` tool以closed actions覆盖本轮MCP/Plugin管理；它先从canonical owner形成不可伪造的resolved physical effect projection，不接收secret、raw config/path、shell wrapper或模型自报授权标签。
5. 该tool在READ_ONLY、ASK_PERMISSIONS、ACCEPT_EDITS、BYPASS_PERMISSIONS均可调用；现有permission按MCP YAML、PULSARA_HOME Plugin state、credential store与process的真实边界决定DIRECT或USER_FORM，不是bypass-only或逻辑scope猜测。
6. 能力页与model-triggered interaction复用同一form/schema/controller/typed owner；READ_ONLY只允许展示user-control-plane form而不产生model write permit，用户提交的exact candidate覆盖model prefill，cancel/stale/controller-loss均确定settle并让模型继续。
7. Plugin安装不以secret为输入或成功条件；连续UI在exact disabled instance创建后才调用connection overlay，再进行enable review。`enabled=true`恒由用户对current exact package提交review并生成call-local external-process acceptance；模型不能自报，系统不新增receipt。缺少凭据只使exact MCP需要配置，不使整个Plugin伪失败。
8. Managed secret由用户直接交给唯一 LocalSettingsStore，只有本地私有配置文件保存值；正式模型/API/Hook/live 投影不回传。无 Keychain、sidecar、DB 或 `.env` 双路径，不虚构 same-OS-user 文件隔离。
9. 已解码 HTTP/stdio 结果中的 exact credential 在 native MCP owner 内处理，old/new client 的 credential snapshot 与 scrub set一致；沿用 stderr 丢弃，不扩展为通用 DLP/lease/event 系统。
10. Environment reference仍是一等高级路径；reload与restart语义诚实。
11. First-party Web/tool mutation自动调用existing live adoption owner并返回独立mutation/adoption settlement；model-origin requested revision在同一turn下一次provider call前settle，其他session使用`PENDING_SAFE_POINT`而不是伪失败。`reload_capabilities`只保留为out-of-band或PARTIAL补救，不再是happy-path步骤。
12. 通用 OAuth 完成真实用户登录/refresh/logout；pending flow 非 durable，private grant 只在
    LocalSettingsStore；删除/重配后的晚回调不复活登录，无工具自动 replay。
13. User 与现有 workspace install surfaces 对 loose Skill、local MCP、Plugin instance满足“可安装即有删除”。
14. Skill删除只删除exact managed-root row并清理enablement override；同名候选自然重算。
15. MCP删除切除exact config/approval、清理managed credential、允许old physical request drain。
16. Plugin删除复用existing state/anchor/GC并以expected package install id防止stale delete；bundle contributions与instance MCP credentials一起消失，Plugin data保留。
17. Builtin、bundled、inherited与Plugin child contribution没有假删除按钮。
18. Same-epoch provider prefix不重写；canonical transcript/history不被删除操作回溯修改。
19. 无generic capability registry、第二套permission owner、database growth、event/job/receipt/tombstone/repair machinery、compat alias或双路径。
20. Focused/full/frontend/PostgreSQL/dogfood全部通过或对真实外部阻塞精确报告。
21. Active specs与bundled MCP/loose Skill/Plugin installer Skills只描述新单一路径。
22. 纯 GUI 可手动/导入 MCP、导入明确可等价外部 Plugin 并补参；不要求模型调用、无 per-service
    adapter registry。用户选定发行版的 active/default/empty 语义有真实 fixtures。
23. HTTP/SSE 由官方 SDK transport 与 httpx2 处理，Pulsara 只保留 §6.3 的集成边界；自研标准
    transport/parser 与 OAuth 双 HTTP 库桥接已删除，stdio 实际缺口明确收敛。保留必要协商，
    不泛化失败降级、不更改 provider adapter、不静默放宽或收紧资源/unknown-outcome 边界。
24. 外部格式 alias 仅在一次性 importer；转换后运行、更新、删除只有 native 路径。
    不能等价的 App/Channel/Hook 明确说明，不以静默裁剪换兼容率。
25. MCP 与 Skill 可独立导入、完整使用并管理，不被不支持的宿主 JS/TS Plugin 捆绑阻塞；
    Skill 的资源完整性与 native lifecycle 有实测，JS/TS 插件宿主明确不实现。

---

## 16. 最终产品文案基线

能力页说明：

> 管理这台设备上的插件、MCP 与技能。需要密钥的 MCP 请在这里完成认证，不要把密钥发送到对话里。

MCP 保存成功：

> 连接设置已保存。Pulsara 正在为已打开的会话刷新能力。

MCP 需要凭据：

> 连接已保存，但还需要凭据。完成认证后即可测试连接。

模型已准备完整配置、当前permission要求确认：

> Pulsara 已准备好这项能力设置。请检查后确认；你也可以在保存前修改。

模型需要用户补齐配置：

> Pulsara 已填写可以确认的公开设置。请补充标出的内容后保存；密钥只会交给这台设备上的凭据存储，不会发送给模型。

用户取消模型发起的表单后，tool结果对应的自然语言不得带责备或反复索取：

> 已取消，本次没有更改能力设置。

Automatic adoption partial：

> 设置已保存，但部分已打开的会话还没有刷新成功。你可以稍后重试刷新。

Plugin MCP 需要凭据：

> 这个连接由插件提供。你可以补充连接参数或更换凭据；插件原始文件不会被修改。

导入需要补参：

> 已读取配置。请补充标出的连接信息后继续。

整包存在不支持的组件：

> 这个插件依赖 Pulsara 尚未提供的功能，暂时不能完整安装。你可以查看具体组件，或单独添加其中的 MCP。

OAuth 需要用户动作：

> 这个连接需要登录。点击“登录”后将在浏览器中完成授权，不需要把令牌发送到对话里。

OAuth 注销：

> 已清除 Pulsara 保存的本地授权。连接配置仍保留；远端授权可在服务方账户中管理。

Skill 删除确认：

> 将删除这个 Skill 的安装副本及其中的文件。最初用于安装的来源目录不会受到影响。

MCP 删除确认：

> 将删除这个连接及 Pulsara 为它保存的本地凭据。远端 API key 不会被吊销，已有对话记录也不会被删除。

Plugin 删除确认：

> 将卸载这个插件；它提供的技能、MCP 与 Hook 将不再用于后续工作，Pulsara 为这些连接保存的凭据也会删除。插件数据默认保留。

任何内部 enum、reason code、fingerprint、package install id、credential binding、source priority 或 supervisor state都必须在前端翻译为上述用户语义，不得直接暴露实现术语。

---

## 17. 本次兼容性修订的代码证据与 subtraction 审计

本节记录最初的设计调研阶段：当时只修改规范，尚未实施新增功能、运行或激活下载包。
2026-09-06 的实施与真实验证见文件顶部状态及 `PULSARA_CAPABILITY_HARD_CUT_VALIDATION_2026_09_06.zh.md`。
本节原始市场样本是静态源码证据，不是
Pulsara compatibility certification。24 个 checkout/236 份 manifest 包含跨宿主重复，不能作兼容率分母。

### 17.1 可复核来源

- 2026-09-06 repository-root `.venv` 的 MCP `2.0.0` 初查及 `2.1.0` 升级复核：
  `mcp/client/streamable_http.py::streamable_http_client` 的 `http_client` 注入，
  `StreamableHTTPTransport._handle_sse_response` 的 `EventSource(response)`，
  `mcp/client/sse.py::sse_client` 的 client factory，`mcp/client/stdio.py::stdio_client` 的 env/reader，
  `mcp/client/auth/oauth2.py::OAuthClientProvider.async_auth_flow` 的原请求重新 yield。
  同环境 `httpx2/_sse.py::EventSource/_SSEEventDecoder` 已执行 event/pending-line 大小检查，
  `httpx2/_config.py::DEFAULT_MAX_EVENT_SIZE_BYTES` 为 1 MiB。这些是本机检查结果，不是新 SDK 固定版本门禁。
  公开接线说明见 [SDK transports](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md)
  与 [SDK OAuth clients](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/oauth-clients.md)；
  main 文档会更新，实际实施仍以项目锁定依赖源码与集成测试为准。
- OpenCode `f12e14cf1640cbf0dfb6b1ff425b2daaef459eec`：
  `packages/core/src/v1/config/mcp.ts` 的 Local/Remote/OAuth；
  `packages/opencode/src/mcp/index.ts` 的 connectRemote/authenticate/finishAuth；
  `mcp/oauth-provider.ts`、`mcp/auth.ts`；`plugin/loader.ts` 的 JS module import；
  `skill/index.ts` 的跨目录 Skill 发现；`packages/app/src/context/global-sync/mcp.ts` 的点击认证分流。
  [固定源码树](https://github.com/anomalyco/opencode/tree/f12e14cf1640cbf0dfb6b1ff425b2daaef459eec)
- Stripe 原生/宿主多发行版：
  [providers](https://github.com/stripe/ai/tree/6838252384/providers)；
  Cloudflare 原生根 manifest：
  [plugin.json](https://github.com/cloudflare/skills/blob/b8aeca6d7e/plugin.json)。
- Slack 显式 skills-only Codex 发行版：
  [README](https://github.com/slackapi/slack-mcp-plugin/blob/1579a07132/README.md)；
  PostHog 显式空 Codex Hooks：
  [codex-hooks.json](https://github.com/PostHog/ai-plugin/blob/19e4737aa8/hooks/codex-hooks.json)。
- Datadog 多字段与自定义路径：
  [.dd_claude-code_mcp.json](https://github.com/datadog-labs/claude-code-plugin/blob/195f570c00/.dd_claude-code_mcp.json)；
  Cursor variables：
  [GitHub manifest](https://github.com/cursor/plugins/blob/93b00b89ef/third_party/github/.cursor-plugin/plugin.json)；
  Cowork 空 URL：
  [Finance MCP](https://github.com/anthropics/knowledge-work-plugins/blob/1f517b9de4/finance/.mcp.json)。

来源 Git commit 只用于重现外部样本，不增加 production fingerprint 或插件兼容白名单。
维护方发布更新后只更新 fixtures/格式解析的有证据差异，不按服务名覆盖 runtime。

### 17.2 有必要增加与明确不增加

| 增量 | 必要性 / 唯一归属 |
|---|---|
| 一次性外部格式 importer | GUI 无模型导入；产物仍是唯一 native package/config |
| `dev.pulsara/mcp/connection-inputs.json` | 保存 immutable 的非秘密输入/target 语义，不篡改 portable schema |
| existing instance connection overlay | 用户实例普通参数/认证与 immutable 软件包分离 |
| local-settings OAuth grant/client info | 下次正常连接与 token refresh 必需，不做执行恢复 |
| process-local OAuth state/PKCE/callback | 防回调错配/CSRF 的真实协议用途，无跨重启恢复 |
| native SSE variant | 标准外部传输兼容，不是内部旧/新双读写 |
| SDK transport 的 MCP 局部 HTTP policy 注入 | 保留真实网络/凭据/资源边界，同时删除重复标准协议实现；不新增网络治理平台 |

不增加数据库列/表/约束、canonical event/subject/guard/relation/job、operation receipt、
fingerprint registry、per-provider/per-plugin runtime profile、通用 JS 插件引擎、后台市场同步、
secret helper executor、Hook 生命周期或 provider rebase exception。

新增 user-facing 状态通过现有 inspection/live projection 表达；OAuth grant 是最小必要持久化，
pending interaction/import draft 是可丢弃观察。若实施需要超出本文明确范围，必须报告具体代码/API
矛盾，不能临时加耐久性或降低协议边界掩盖问题。
