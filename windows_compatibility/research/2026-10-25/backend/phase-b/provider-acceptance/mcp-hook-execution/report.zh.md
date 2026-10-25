# 真实provider实际执行：MCP stdio / HTTP / PostToolUse hook

2026-10-25。与tasks的capacity/context及能力变更测试分工，本项单独补实际执行。正式installed Python/pulsara_agent优先，只读保存的同一deepseek-flash连接，独立UUID测试DB/短私有home/中文空格工程；没有调用外部MCP账号业务，没有改保存设置或正式PG生命周期。

## 原机制与有限适配

`mcp_hook_driver.py` 复用原 `run_round10_subagent_dogfood._run` 负责创建/关闭Core和Session，原ModelRuntime/recording adapter/SDK transport不替换。仅把context测试数据函数换成本项有限prompt，不运行或冒称last_n语义。配置在cold session前准备，批内SYSTEM/tools不变、messages只追加。

stdio与HTTP服务复用现 `round6_mcp_server.server`（维护中的MCP SDK MCPServer实例），通过公开tool decorator增加execution_echo，不复制协议。stdio调用无文件副作用的cmd.exe echo后写本次fixture journal并返回原fixture_echo；HTTP同一公开SDK app通过uvicorn受控预绑定127.0.0.1 socket启动，不开PG TCP。配置明确allow_http_localhost，仅该本机fixture；不改外部生产网络策略。

受信USER PostToolUse hook只在本次临时home配置，matcher=terminal；信任仍由原LocalHookSourceProvider/HookTrustStore处理。hook读取原stdin，执行cmd.exe echo并写本次journal，返回合法空JSON。原executor/SDK拥有进程和scope。被动constructor观察器只留实际scope/client对象，仍调用原构造器；未替换join/取消/物理结果。

## 真实结果与原失败

| 记录 | provider calls | 结果 |
| --- | --- | --- |
| first.result.json | 4 | 原stdio与hook已进入实际cmd.exe，但测试Python text=True按UTF8解码Windows中文UNC警告失败；hook非零，整体失败，保留完整结果 |
| bytes.result.json | 3 | 模型3个实际工具调用/副作用全过，4scope及两SDK owner都结算；fixture被SIGTERM后uvicorn按signal语义exit-15，严格exit0断言失败，保留 |
| eof.result.json | 3 | 原断言全部通过：2个MCP执行+1个terminal触发实际hook，原owner关闭，受控HTTP服务EOF自然0 |

修正只在fixture：Windows stdout/stderr捕获原始bytes、按ASCII标记断言，并保留base64原字节；不改生产编码或丢掉UNC提示。受控HTTP服务改由本次父stdin EOF设置原uvicorn.should_exit，serve正常返回0；客户端先按原owner关闭，随后才退休外部测试服务，不把HTTP客户端关连接等同于远端服务进程结束。没有降低原marker/次数/exit0/join断言，没有重写失败。

最终模型先调用stdio execution_echo(text=STDIO_REAL_PROVIDER_2026)，实际返回fixture:STDIO_REAL_PROVIDER_2026；HTTP同理返回fixture:HTTP_REAL_PROVIDER_2026，两者isError=false。journal各恰一行，stdio还记录MCP_STDIO_PE_DONE及原stdout/stderr字节，证明SDK服务器真正调用了Windows PE。

同一模型任务只执行一个terminal：`printf HOOK_MODEL_TASK`、tty=false、自然success/exit0；精确PostToolUse/terminal hook journal恰一行，记录HOOK_PE_DONE及原Windows字节。模型未通过terminal伪造journal或自行执行fixture脚本，真实canonical tool trace与journal交叉验证。cmd的UNC cwd提示保留，不据此宣称Windows程序可维持Linux cwd；echo无Windows文件写入。

最终4个原scope均physically_joined=true、directory退休、helper exit0、worker不活、failure=null，原joined_result均reason=settled；SDK_HTTP与_BoundedStdioTransport的原owner task均done。记录里的cancelled=true为实际scope封闭状态，不把它改写成未发生取消信号；长驻stdio服务器由原SDK正常close负责，Windows echo及hook命令已在各自调用中自然完成。HTTP没有伪造Windows ProcessScope，它是原网络transport；受控服务另由fixture owner在父EOF后自然exit0，并记录SDK session manager shutdown。

## 连续性、秘密与清理

`review-cleanup.json`对三批原始完整provider记录分别检查SYSTEM/tools精确相同、messages append-only，均通过。使用原LocalSettingsStore的credential_scrub_set逐字检查实际配置秘密，三批均无残留；保存prompt、实际响应、tool结果、PE原始输出及失败，不宽泛遮盖上下文。

三批UUID DB已原finally drop，后续只读确认均不存在，正式PG systemid仍7700523439330484737。原Core/SDK关闭后，无可读范围内匹配exact fixture argv或测试home的进程，失败的短home核无剩余scope目录后删除，private根 `.provider-accept-80k6ptzi`也已删。三个同UID PID355/357/154903环境不可读，明确保留该扫描限制，不声称全权限下全局无进程；不对这些PID操作。

已通知root：本项不再启动provider或依赖正式PG在线，可进行其真实GUI正常Quit/冷重开。没有停用、重置、删除正式PG、用户新session或保存配置。

本项补实际调用正控；超时/取消/失联、HTTP网络及Windows后代尾部的组合边界仍以此前28项原owner真实跨域矩阵为相邻证据，不冒称本项穷举所有错误。它也不代表尚待新正式包复验的ROOT stale-head/capacity/context已通过。
