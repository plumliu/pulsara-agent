# Capability hard-cut 验证记录（2026-09-06）

状态：实施与验收进行中，不是整篇规范的激活证明。工作区未提交；没有 push。
基线 `a147ec39`。沿用仓库 uv `.venv`，MCP SDK 2.1.0 / httpx2 2.10.0。

## 已实现的关键减法

- 官方 SDK 承担 HTTP/SSE 协议、解析和会话。删除 `_BoundedHttpTransport`、
  `_BoundedLegacySseTransport`、自写 SSE event/frame 状态机和 `_SlotByteBudget`。
- OAuth 使用同一 httpx2 MCP policy client，不再有 httpx → httpx2 response 桥。
  OAuth 握手与 tools/call 分离，避免 SDK auth 对工具 POST 的透明重发。
- Pulsara 保留 DNS/IP/network policy、credential gate、已解码普通 HTTP body 上限、
  SDK 后的形状/结果语义、配置与运行时采用。SDK SSE 采用其现有 1 MiB event 边界；
  不声称保留旧 16 MiB SSE 或 pre-parse object-allocation 保证。
- stdio 仅保留已验证的精确环境、未完成帧和进程关闭缺口；不是另一套 HTTP 协议。
- GUI 外部发行版导入、参数分类、disabled install、MCP 连接编辑、Skill 独立导入已接通。

## 命令结果

- `.venv/bin/pytest -q`：最新一次 **1575 passed, 18 warnings, 338.37s**。
  同日此前完整运行分别为 1575 passed / 326.46s、1575 passed / 325.16s；更早为 1569 passed。
  最新运行包含市场 manifest、schema 边界与模型 Plugin review 的公开连接信息。
- `.venv/bin/pytest -q tests/test_plugin_source_import.py tests/test_bundled_skills.py`：
  **23 passed, 1.58s**。
- `.venv/bin/pytest -q tests/test_mcp_sdk_transport_probe.py tests/test_round6_mcp_production.py tests/test_mcp_sse.py tests/test_mcp_oauth.py tests/test_plugin_source_import.py tests/test_bundled_skills.py`：
  **122 passed, 14.39s**。
- `.venv/bin/pytest -q tests/test_capability_management_intent.py tests/test_bundled_skills.py`：
  **50 passed, 1.19s**，覆盖随后补充的工具说明与 installer 文案。
- `.venv/bin/pytest -q tests/test_mcp_schema_bounds.py tests/test_mcp_sdk_transport_probe.py tests/test_capability_management_intent.py tests/test_bundled_skills.py`：
  **72 passed, 1.33s**。
- `cd frontend && npm test`：最新 **116 passed, 8.25s**；之前为 116 passed / 7.30s、115 passed / 7.74s。
  补测 schema 失败不误报认证错误、批量 Skill 单项失败不中止其他项，以及表单提交完成后的
  shared editor close 不重复发 CANCEL。没有减少原有断言。
- `cd frontend && npm test -- components/skill-importer.test.tsx`：**2 passed, 0.91s**。
- `cd frontend && npx tsc --noEmit`：通过。
- `cd frontend && npm run build:local`：通过，更新本机内置静态资源。
- `cd frontend && npm run lint`：**2 个既有错误**，均在未修改的
  `components/memory-view.tsx`：render 中 ref 赋值与 effect 同步 setState。
  本轮文件无 lint 错误；未加 eslint 忽略。 --check`：通过。
- `.venv/bin/pulsara db migrate && .venv/bin/pulsara db verify --deep`：
  migrate `current/head=0/applied=[]/added_grants=[]`，deep verify `verified`；已核实本机
  `postgresql://pulsara:pulsara@localhost:5432/pulsara`。本轮没有 schema 变更或数据库重置。

此前全量运行曾有 11 个失败并因过时 shutdown fake 挂起而中断，后有
16 failed / 1553 passed，最后有 1 failed / 1568 passed。
修复的是 owner 移动后的 fake、遗漏的 shutdown owner、过时模型切换调用参数和
已删除测试节点的 oracle。没有弱化行为断言、添加 skip/xfail、扩大 DB/event/job oracle。

## 浏览器与真实服务

Pulsara 本机页面 `http://127.0.0.1:54128/`：

- 真实 Sentry Claude Plugin 预览：8 Skills + 1 MCP；安装成功且保持 disabled。
- Sentry `sentry-debug-issue` 独立 Skill GUI 预览、安装成功；完整资源保留的
  自动测试通过。真实 Skill 运行、启停/删除场景尚未全部完成。
- 本机 SDK FastMCP `http://127.0.0.1:54130/mcp`：GUI 导入、显式 localhost
  选择、连接测试成功（4 tools / 1 resource / 1 prompt）。测试服务名 `sdk-dogfood`。
- Firecrawl `https://mcp.firecrawl.dev/v2/mcp`：用户 key 经 GUI 存入本机凭据，
  Bearer HTTP 列出 25 tools，`firecrawl_scrape` 抓取 `https://example.com` 成功。
- Firecrawl 官方 `npx -y firecrawl-mcp`：仅 child env 注入同一已保存 key，列出
  27 tools，真实 scrape `isError=false/resultType=complete`，返回 Example Domain。
  key 不在本记录、测试源码或命令输出中。
- LangChain `https://docs.langchain.com/mcp`：3 tools，实际搜索文档成功。
- 真实模型 OpenRouter GPT-5.6 Luna，会话 `d55d7a67`：通过
  `list_mcp_servers → inspect_new_mcp_tool → use_new_mcp_tool` 调用 fixture_echo
  返回 `fixture:MCP-SDK-DOGFOOD-0906`，再调用 Firecrawl 抓取 Example Domain，
  最终自然语言回复成功。实际输入/输出保留于本机会话，而非伪造 provider 证据。
- 同一会话第二轮：真实模型以 `manage_capability` 新增临时 `sdk-model-dogfood`，
  自动采用后发现/检查并调用 echo，返回 `fixture:MODEL-MANAGE-0906`；随后改名、删除成功。
  未调用第二次 reload。模型为配置 schema 读取了不必要的源码，因此补充了公开工具说明与
  installer 的完整 native config 示例、whole-entry update 和缺信息时进入预填表单的指导。
- 从 Anthropic 官方已下载 Skill `skill-creator` 经 GUI 安装为
  `pulsara-dogfood-skill-creator`；真实模型读取安装后正文、`references/schemas.md`，
  使用仓库 `.venv/bin/python` 执行已审阅的只读 `scripts/quick_validate.py`，
  返回 `Skill is valid!` / exit 0。未运行其他脚本或创建训练/eval 任务。
- USER ASK 新增 `sdk-ask-dogfood` 进入共享表单；USER ACCEPT_EDITS 更新也进入表单，
  改名成功。首轮发现提交后旧 onClose 闭包重复 CANCEL，已用同步 call-local ref 修复并补测；
  浏览器复测只有一次提交与成功提示，没有“表单已变化或已关闭”的误报。
- WORKSPACE ASK 同名候选在表单取消，工具确定返回取消，模型自然语言继续回复；无工作区写入。
  随后 WORKSPACE ACCEPT_EDITS 无凭据配置直接安装，无表单、无手动 reload，实际 echo 返回
  `fixture:WORKSPACE-ACCEPT-0906`；USER 同名配置未被改变。
- 模型发起 USER `firecrawl` UPDATE（省略 config）进入已有连接的共享表单；原 secret 不回显。
  经表单重新填写用户授权的 Bearer 后，自动采用并发现 25 tools，真实模型再次抓取
  Example Domain / HTTP 200。工具参数不含 credential value，没有手动 reload。
- WORKSPACE Plugin INSTALL 的完整 candidate 在 ACCEPT_EDITS 下仍进入“安装插件（暂不启用）”
  用户确认；点击取消，没有安装 WORKSPACE 副本，也未修改 USER 同名实例。
  这项实际 UI 证明 Plugin 的本机存储写入不被逻辑 WORKSPACE scope 放宽。
- 带外 CLI 验证：`.venv/bin/pulsara mcp disable sdk-dogfood` 后，真实模型首先观察到
  READY / 4 tools，仅一次 `reload_capabilities` 后变为 DISABLED / 0 tools。
  第一组 WORKSPACE `sdk-ask-dogfood` 的 CLI disable 实验存在 USER 同名连接，reload 前后
  均为 READY / 4 tools，不能据此单独证明关闭；保留此结果，没有把它改写为成功。
- 自建参数化 Claude 格式 Plugin `pulsara-firecrawl-dogfood`（不是冒称官方 Plugin）：
  GUI 预览 public endpoint default/private header 参数，disabled install，用户连接表单填写
  credential，exact enable review 后真实模型经 Plugin MCP 抓取 Example Domain / HTTP 200。
  模型随后替换 1.0.0 → 1.0.1，替换后 disabled，重新 review 后启用并再次抓取成功，
  再 disable/re-enable。stable component 的凭据保留；两个版本的 6 个 package 文件均不含凭据值。
  真实模型 enable form 原先只列 server ID，已补充当前 exact public endpoint/command、认证类别与
  credential presence，仍复用同一 typed review owner，不增加 receipt/registry。
  测试 Plugin data 留有一个明确标记，供删除后验证保留；尚未删除，等待用户确认测试副本清理。

### Notion OAuth：原 schema 阻塞已修复，全目录与重新登录通过

从 GUI 导入 `https://mcp.notion.com/mcp` + OAuth；官方 SDK 完成 discovery/DCR，
Chrome 复用既有 Notion 登录态。用户本人完成首次工作空间授权；本机 callback 收到 code，
Pulsara 显示“已完成授权”。未记录 token/code。

重新创建服务/SDK client 后可使用已保存授权完成 discover，协议 `2026-07-28`，
远端列出 **42 tools**。全目录采用失败点明确为：

```text
tool: notion-query-data-sources
input schema: 68,467 bytes（json.dumps 默认格式），5,770 nodes，depth 33
Pulsara existing schema node bound: 4,096
supervisor.discover_mcp_catalog → wire.validate_schema → McpWireBoundExceeded
```

修复前没有擅自放宽此界限，也没有保存过滤后的配置。仅在临时测试对象中使用既有
`include_tool_names=['notion-search']`，通过正常 catalog validation 后，实际执行：

```json
{"query":"PULSARA-OAUTH-DOGFOOD-0906-NO-SUCH-PAGE","query_type":"internal","page_size":1,"max_highlight_length":0}
```

远端返回 `isError=false, resultType=complete`，结果为用户可见的 Student Planner
页面标题/链接，无正文 highlight。没有修改任何 Notion 页面。
上述初次实验当时只证明 OAuth 与单工具调用，不代表全目录兼容。

随后用户明确批准删除独立 schema 4096 节点限制。现在 schema 复用现有 wire JSON
65,536 nodes，保留 256 KiB/depth 64；没有 Notion 特例或保存工具过滤。
新进程、新 client 在 ALL exposure/FAIL_SERVER 下通过完整发现：
**42 tools / 6 resources / 2 prompts**，真实 `notion-search` 返回 complete/isError=false。
GUI 已显示“已连接，42 工具，6 资源”。schema 资源超限有独立产品错误，不再混为登录失败。
GUI 注销本地授权后，重新通过 Chrome 同一工作空间与原有权限完成 OAuth，Pulsara
显示“已完成授权，可以测试连接”。没有修改 Notion 页面。
长期自然过期 refresh 未等待真实 token 到期；本地 PKCE/DCR/refresh/logout 竞态 tests 已通过。

## 仍需收尾

### 后续 GUI 发行版自动识别修订（同日）

- 删除 GUI 的预先手选格式/默认 native 路径。预览 API 仅收 source_path，复用已有 source
  admission 与各发行版 parser；唯一 manifest 自动预览，多个 manifest 分列组件且不预选。
  Native 也先预览。错误发行版保持可见；选择切换清除参数；安装仍传 exact format 并 fresh validate。
- `.venv/bin/pytest -q tests/test_plugin_source_import.py tests/test_local_web_http_surface.py tests/test_round9_3_agent_plugin_product.py tests/test_bundled_skills.py`：
  **42 passed, 5 warnings, 2.01s**。新增自动识别/多版本非并集/无效 sibling/native/missing tests 与 HTTP 请求契约。
- `cd frontend && npm test`：**120 passed, 7.82s**；`npx tsc --noEmit`、`npm run build:local` 通过。
  本轮涉及 TS/TSX 文件的 targeted eslint、Ruff 与 diff whitespace check 通过。
  没有重新跑整库 Python 全量；上面的 1575 是本修订前的完整结果。
- 实际浏览器：Sentry 自动识别 Claude（8 Skills/1 MCP）；Neon postgres 展示 Claude/Cursor
  两个版本（各 7 Skills/1 MCP），未选时不能安装，选 Cursor 后显示其独立预览并可安装。
  本轮仅只读预览，没有重复安装现有 Sentry，也没有安装/授权 Neon。
- Slack 完整市场目录确实识别了 Claude/Codex/Cursor，但现有 converter 对其中的 `commands`
  宿主行为拒绝整包；三个版本各自显示原因。不是导入成功，也不等同于前述最小 manifest fixture。

### 能力管理视觉复核（同日）

- 本轮只调整呈现：Plugin/Skill/MCP 导入、MCP/插件连接编辑、启用审阅共用内容留白与
  内部滚动，标题和操作栏保持可见；移除预览卡片对所有 label 的横向 flex 规则。
  多发行版使用保留原生 radio 语义的暖色选择卡片，名称不拆词，组件清单按需展开。
- 修正四列凭据输入、插件 MCP 列表按钮的列宽；详情操作按钮按完整按钮换行，
  不把按钮文案挤进窄列。窄屏模态层高于底部导航，不遮挡保存/取消。
- Chrome 实际复核：Neon Claude/Cursor 预览与选择、Anthropic skill-creator 预览、
  MCP JSON 配置预览、秘密 Header/OAuth 表单、Sentry 插件连接默认值及启用确认。
  仅预览和取消，没有安装、启用、保存凭据、授权或调用真实工具。
- 480×820 复核：发行版名称 nowrap；body 与两张卡片均 scrollWidth = clientWidth，
  无横向溢出；固定操作栏未被底部导航覆盖。检查后恢复默认 viewport。
- `cd frontend && npm test -- --run`：**120 passed**；`npx tsc --noEmit` 与
  `npm run build:local` 通过。涉及组件与 Plugin importer 测试的 targeted eslint、
  `git diff --check` 通过。测试补充组件列表展开、正文/操作栏分离断言。
  本轮未改 Python 或协议，也未重跑数据库/真实 provider dogfood；既有整篇剩余项不变。

### OAuth 回调页视觉修订（同日）

- 纯文本回调替换为无脚本、无外部资源的中文 HTML 页面，使用 Pulsara 暖纸色、轨道标识、
  居中卡片与返回指引；兼顾窄屏及系统深色模式。收到回调不宣称 token exchange 已成功。
  无效/过期、缺少 code 与服务商拒绝使用相同版式，保留原有 HTTP 状态和 OAuth settlement。
- `.venv/bin/pytest -q tests/test_mcp_oauth.py tests/test_mcp_oauth_callback_page.py`：**7 passed**。
  包含真实本地回调 HTTP 的成功/错误/缺码/拒绝、无 query 值回显、无授权写入及 HTML escaping。
  新增拒绝用例首轮对 owner 暴露异常类型的假设不符：owner 已将异常转换为带产品文案的
  ValueError；按现有契约修正测试并增加 failed 状态断言，未修改异常处理生产逻辑。
- 相关四个 Python 文件 Ruff 和 `git diff --check` 通过；Chrome 用同一响应生成器在独立本机
  预览服务检查桌面及 390×844 版式，结束后恢复 viewport 并关闭临时服务。
  未触发真实 OAuth 或重启用户的主服务；主服务重启后新回调使用该页面。

### 整篇验收剩余项

- 完成 §14.3 尚余 Workspace managed-credential outside-write 的真实表单、Plugin/Skill 删除等场景；
  自动 fixtures 不能替代全部交互验收。
  Plugin enable/replace/disable/re-enable、Skill 真实资源/脚本与 keyless permission 基本路径已经通过。
- 全部关联 active 文档已经同步主要路径，仍需最终核对未激活/历史段落。
- 清理测试用 `sdk-dogfood` 连接/进程，保留用户的 Firecrawl、Notion 登录配置。
- 已向用户请求确认删除本轮测试副本；答复前保留 `pulsara-firecrawl-dogfood`、
  `pulsara-dogfood-skill-creator` 与 `sdk-dogfood` / `sdk-ask-dogfood`。
  前者 Plugin data 与来源目录不属于删除范围。Notion、独立 Firecrawl、Sentry 保留。
- 前端全量 lint 的 memory-view 基线问题尚未处理，未隐藏其失败。

未把剩余工作说成完成，没有新增 fingerprint registry、数据库限制或持久化执行恢复。
