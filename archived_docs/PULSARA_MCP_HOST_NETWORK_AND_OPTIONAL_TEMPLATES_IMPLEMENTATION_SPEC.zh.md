# MCP 本机网络与可选资源模板契约

状态：当前实现规范。适用于独立 MCP、插件 MCP、连接测试和 OAuth HTTP。

## 网络所有权

用户授权的连接配置决定服务目标。MCP HTTPS 目标可以解析到公网、内网、回环或本机代理的 Fake-IP 地址。Pulsara 不提供目标 IP 范围隔离；有权配置连接的调用者也可以选择内网服务。

HTTPX 负责系统/环境代理、NO_PROXY、DNS、地址选择、TCP/TLS、SNI、连接池和传输关闭。Pulsara 向依赖传递原始域名 URL，不自行解析、固定 IP、枚举地址重试或进行公共 DNS/DoH 查询。代理连接失败不得由 Pulsara 改为直连重放。

Pulsara 的最小适配仍负责凭据边界、响应体资源界限、取消及请求结算。现有回环目标绕过代理规则保持有效。HTTPS 证书验证保持开启；普通 HTTP 仅允许显式 `allow_http_localhost` 的本机地址。该选项控制明文传输，不控制 HTTPS 目的网络范围。凭据目的源和 OAuth 跨源 POST 重定向约束继续生效。

HTTP transport 的配置字段为 `type`、`endpoint`、`allow_http_localhost`、`stateless_http_asserted`。解析、序列化、CLI、编辑器、插件适配和配置身份计算使用同一字段集合。未知字段直接拒绝，不自动修复旧配置，不引入兼容解析、双字段或隐式回退。

## 资源模板发现

先根据 server capabilities 发现工具和资源等目录。资源能力可以提供具体资源而未实现模板列表。对于 `resources/templates/list`：

- 第一次请求（无 cursor）明确返回 JSON-RPC `METHOD_NOT_FOUND`（-32601），表示该连接不支持资源模板列表，模板集合为空；其他已成功发现的能力正常发布。规则对支持的协议版本一致。
- 一旦接收到分页响应，后续任何错误均使本次完整目录发现失败，不能把不完整的模板集合当作空集合。
- 参数错误、认证错误、服务端内部错误、网络故障、取消、超时和畸形响应都不能归为“不支持”。
- `tools/list`、`resources/list`、`prompts/list` 不使用此例外。

SDK 负责 JSON-RPC 和协议解析，Pulsara 仅在其目录发现边界解释此明确的可选方法缺失。保持现有分页、schema、字节和物理并发界限，不增加总重试或会话寿命限制。

## 同 epoch 的启用与执行

新启用的 MCP 在现有 safe point 发布目录。已有 epoch 的 SYSTEM 与 provider tools 不变，messages 仅追加。新工具通过 `query_capabilities` 检索和检查，再通过 `use_new_mcp_tool` 执行。连接发现成功与真实调用成功分别验证。

## 配置来源与运行时服务的关联

MCP supervisor 在已有 `McpServerCatalogEntry` 中携带非秘密的 `runtime_source`。这是当前冻结目录中服务的来源事实，禁用、连接中、尚未发现及发现失败时同样存在；来源关联不依赖远端工具目录或物理 client 已建立。目录若保留旧 discovery candidate，其来源仍属于该 candidate，不能用正在重连的同名新配置替换。旧 runtime generation 的来源不随 supervisor 后续 reload 改写。

`query_capabilities` 在分页和模型结果序列化前，按 runtime server ID 与精确来源合并配置行和运行时行。本地来源必须匹配 USER／WORKSPACE；插件必须匹配 scope key、package owner 和 package install ID。同一来源只返回一条 `MCP_CONNECTION` 行，详情保留对应的 `MCP_SERVER` runtime target。用户级与项目级同名配置、HOST_OVERRIDE、不同插件安装版本不可按名称合并。无法证明关联的运行时服务仍独立展示。

来源关联只回答服务来自哪里，不证明磁盘配置已采用、不生成远端工具、不使服务可调用。目录未发现时，配置 parent 的远端子项查询返回空 items、`PARTIAL` 与 `MCP_CATALOG_NOT_DISCOVERED`；不得误报成没有远端能力。runtime 连接参数摘要继续只从精确匹配的已发现 candidate 获取，来源事实不扩大配置或秘密的可见范围。子代理继续按已有 scope 过滤目录，不通过内部来源字段增加公开详情。

该字段只存在于已有进程内目录事实，不进入 provider SYSTEM／tools、目录语义摘要或持久化 schema；不增加来源注册表、配置副本、fingerprint 或兼容回退。UI 直接消费同一个查询结果，不另设去重路径。关联回归入口为 `tests/test_mcp_query_association.py`，覆盖未发现状态、作用域冲突、插件安装版本、冻结来源和保留旧目录的重连。

## 验证

自动化验证覆盖域名完整传递、内网/Fake-IP、系统/环境代理与 NO_PROXY、真实 CONNECT 隧道、凭据门禁、TLS、HTTP 不重放、OAuth、旧字段拒绝，以及模板缺失/其他错误/分页中途失败。clean-v0 schema、事件、关系与持久化种类均不变。

真实验收使用 `require_pulsara_home()` 和 `LocalSettingsStore` 加载当前保存的配置及凭据，生产设置只读。使用独立临时 home 和经验证的本机临时 PostgreSQL 数据库。测试输入对已有 MCP 配置显式删除已取消的字段，再按新契约解析；这仅属于验收输入准备，不进入生产解析路径。保持当前 Clash TUN 开启，不改系统代理。

先在关闭 Firecrawl 的状态建立 epoch 并完成一轮对话，再通过普通能力管理入口启用相同连接；核实目录、前缀连续性和 META 路由。真实模型选择并执行一次只读最小 Firecrawl 调用，保存实际 prompt、模型答复、调用参数和工具结果，精确清除凭据值。不得用手写模型回复或直接 REST 请求代替 meta-tool 验收。

验收入口为 `uv run --locked python -m tests.dogfood.run_mcp_host_network_dogfood`。该脚本使用当前桌面数据库中的模型绑定和保存的 Firecrawl 连接，在临时数据库中执行验证；结束后删除临时数据库，证据保存到 `output/diagnostics/mcp-host-network/real-provider.json`。实际工具调用会产生服务商用量。自动化回归入口为 `tests/test_mcp_http_addresses.py`、`tests/test_mcp_runtime_proxy.py`、`tests/test_mcp_sdk_transport_probe.py` 和 `tests/test_mcp_rpc_errors_and_pagination.py`；OAuth、凭据、连接生命周期和前端编辑器测试继续约束各自边界。
