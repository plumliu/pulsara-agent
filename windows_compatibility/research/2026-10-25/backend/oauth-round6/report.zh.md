# R6 OAuth：真实Windows浏览器→原Linux owner→Notion

本轮在用户明确授权并现场完成Notion认证后，**原OAuth owner真实取得token响应HTTP200，原MCP SDK握手协商2026-07-28，`list_tools`返回65项且无next cursor。** 没有调用业务工具或读取/修改Notion页面。原listener/pending、本地grant和隔离Linux home已清理，进程正常退出。

这是G0 OAuth有限机制和真实服务互操作证据，不是生产WSL适配、完整N门或Windows激活。生产、规范、依赖和提交未改；interop/Hiddify TUN未更改。

## 所有权和范围

- 使用实际`LocalMcpManagementService(open_browser=...)`、`McpOAuthManager`和锁定mcp2.1.0。原manager先安装aiohttp listener，再调用open_browser；SDK生成PKCE/state、校验issuer、DCR和token exchange，没有另写客户端grant/PKCE/state协议。
- 隔离home为`/home/plumwsl/.g0oauth6-*`，当前UID1000/0700/ext4 device2096；LocalSettingsStore和mcp.yaml明确指向该home，不读写生产settings或凭据。
- Windows实际Electron44.9.0，内嵌Node24.22.0；这是Electron自身版本，没有用全局Node替代。Electron主进程仅通过HTTP(S)/无userinfo/当前live的OAuth-only请求调用`webContents`之外的系统` shell.openExternal`。
- 有限JSONL请求只有`type/requestId/url`，结果只有`type/requestId/opened`。authorization URL只存在私有pipe和OS打开sink中，不转发到host日志，不输出query。code/token/client secret不进入这个host协议。
- Linux原callback、SDK和settings继续拥有认证裁决；Windows只回实际系统打开结果。真实Notion认证由用户手动完成，没有Computer Use、自动点击登录或授权。

原`BoundedMcpSdkClient.open()`拥有SDK discover/initialize协商和必要协议bookkeeping；探针随后只调用`client.session.list_tools()`，没有调用management.test（它还会枚举resources/prompts），也没有调用tools/call/resources/read。这里的“握手”不冒称抓包证明实际发送了legacy initialize RPC；实际协商版本来自原SDK字段。无业务工具调用的依据是受限调用面和该原owner源码，不是Notion服务端审计或完整网络抓包。

## Mock跨Windows回调：8项

`mock.host-results.json`记录实际Electron→系统浏览器→WSL原listener，`verify.py`对保留结果执行离线断言。

| case | 实际结果 |
| --- | --- |
| success | S256 verifier/challenge由mock AS实际核对，token exchange一次，原owner authorized并存grant |
| wrong state | 浏览器真实回调400；不交换token、不存grant，原flow取消并关listener |
| wrong issuer | SDK拒绝callback iss，未请求token；flow failed，callback展示error |
| PKCE/invalid_grant | 原SDK S256证明实际正确，mock AS随后返回标准invalid_grant；SDK拒绝存grant |
| callback reuse | 在token交换门上保持首次callback未结算，第二次同code/state回调被400拒绝；一次token请求，随后首次成功200 |
| cancel | 系统浏览器已到本地授权页但不发callback，原manager.cancel取消flow、不存grant、listener关闭 |
| port conflict | 原callback端口已占用，bind失败；没有发送host打开请求，没有token请求或grant |
| pipe EOF | 浏览器已打开、原flow等待callback时真实关闭host→guest控制pipe；原service关闭/flow取消，无grant、listener/pending收齐，不重开浏览器 |

PKCE负控精确定义为“正确S256经过验证后AS拒绝兑换invalid_grant”，没有篡改SDK verifier，也不宣称测试了Notion服务端的错误PKCE实现。服务端PKCE本身的完整conformance不由Pulsara研究客户端重写。

7次系统打开（port conflict没有打开）都先从Windows对无query的探针路径HEAD确认原listener可达；8项结算后Windows HEAD都不可达。原owner的listener字段均清空，pending=0。取消/EOF发生在打开ACK之后、等待用户callback阶段；未把这组证据扩大为所有in-flight open-result取消交错。

端口占用和结算后关闭已实测。随后在独立`rebind/` snapshot补了一项：原flow取消/关listener/pending清后，陌生mock服务真实绑定原端口；Windows HEAD200并经系统浏览器把完整旧callback送到错服务，其返回成功页200。原owner仍cancelled、token请求0、无grant；双方listener最终关闭。该单项安全事件、首次可达性失败及清理见`rebind/report.zh.md`，没有重跑原8项或访问Notion。

## 真实Notion

官方入口采用[Notion客户端指南](https://developers.notion.com/guides/mcp/build-mcp-client)的Streamable HTTP `https://mcp.notion.com/mcp`。本轮实际安全事件：

1. 原SDK probe收到401，protected resource metadata及AS metadata均200，动态注册201。
2. Linux原listener已安装，Windows HEAD404确认可达；Electron shell.openExternal返回opened=true，root通知用户现场授权。
3. 用户报告成功回调，原token endpoint实际200。
4. 原SDK握手`protocol_version=2026-07-28`，list_tools实际65项、next_cursor_present=false。
5. client正常aclose；原manager.logout清本地record，service和bridge关闭，listener_closed=true、pending_count=0、local_grant_removed=true。
6. 本次home删除，guest和wsl carrier正常exit0，stderrBytes=0、无partial frame。

原始safe事件见`real.host-results.json`。仅记录工具数量和协议元数据，没有导出工具schema、页面内容、用户授权范围内容或业务结果。

**本地清理与远端撤销不同。** 原McpOAuthManager.logout只删除本地授权记录，不调用远端revoke。本轮没有撤销Notion服务端授权或DCR注册，也没有浏览/操作用户账户设置。不能由本地token/home已删除推导远端授权已撤销。

## 秘密与诊断

真实code/token/clientsecret仅留在Linux原owner/SDK/隔离store和必要的HTTPS交换中；Windows系统浏览器自然处理OAuth重定向，Electron应用层不接收callback code/token。host的唯一URL数据是授权URL，未持久化到日志。

为保留诊断而不把秘密送到host，guest用现`ProcessCredentialScrubSet`观察实际callback code、请求表单secret、响应token/clientsecret，按值精确scrub；原bounded HTTP请求和response均调用原实现一次。callback的只读middleware与response-factory spy只用于取秘密值/状态观察，没有修改OAuth判断。私有stderr先保存在Linux 0600临时文件，结算后以实际secret集合scrub并断言后置条件，才允许诊断跨host；原SDK私有auth日志过滤仍保留。

`verify.py`另外检查所有归档host事件没有URL/authorization_url/access_token/refresh_token/client_secret/code_verifier carrier字段，也没有code/state/challenge等auth query。这个离线检查是结构和authquery扫描，不是事后把真实秘密重新导出做比较。没有生成secret hash或receipt。

## 原失败与源码阶段

- `first.mock.host-results.json`：首个动态localhost listener立即HEAD为ECONNREFUSED，未调用系统打开；flow/home已清理。保留原Electron源码。随后仅作最多5秒只读presence观察，验证WSL动态转发就绪，不重放授权、不改网络/TUN。这是诊断观察边界，不是产品用户等待寿命cap。
- `second.mock.host-results.json`：前四项通过；reuse fixture用两个完全相同iframe URL，没有取得独立的第二次callback，保持门导致真实10秒HTTP请求超时，原断言失败。保留原guest源码。改为第二HTTP URL加无OAuth语义的duplicate marker，code/state/iss仍相同，400断言保留，最终通过。未把该fixture现象说成生产OAuth bug。
- mock最终成功后新增real分支及安全HTTP事件观测；没有在含这些新增只读观察的最终源码上重跑全部8项。已有成功JSON对应其实际阶段，不改写成全矩阵最终源码重跑。

第一次mock、第二次mock、最终mock和真实Notion四个home全部已清理。`cleanup-audit.json`核四个精确home不存在、当前用户可观察范围无本guest进程；补项`rebind/cleanup-audit.json`另核两个精确home同样干净。PermissionError仍跳过，不声称全权限系统审计。没有终止发行版或其他用户进程；系统浏览器属于明确OS交接，不承诺关闭其他浏览器窗口。

可选Electron临时profile/cache递归删除被自动审批拒绝，工具只给`blocked by policy`，没有换tool重试；`profile-cleanup.json`确认其仍存在于本轮ignored output，不归档。它不是Linux授权store，也没有BrowserWindow认证内容。本地grant/home/listener等必要清理已经完成。

## 运行与结论

Windows入口`run.py mock`启动实际Electron，Linux代码须同步到同clone round6。`run.py real --approved`只在root已协调用户后使用；人机授权没有增加总等待时限。原request的HTTP10秒来自既有owner；mock有限观察超时不成为产品cap。

该有限OAuth链取得mock安全负控、独立陌生服务rebind负控与真实Notion互操作正证，可以归档为N门子项。host崩溃/半帧/取消与打开结果更多交错、真实安装GUI以及其余N门仍不能由这些case自动覆盖。没有修改生产或进入Phase B。
