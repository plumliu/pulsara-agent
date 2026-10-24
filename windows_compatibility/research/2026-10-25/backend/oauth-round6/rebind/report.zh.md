# OAuth callback端口重绑单项补证

本项通过。独立snapshot只跑一个mock flow，没有访问Notion、重跑原8项或调用业务工具。生产、规范、依赖均未改。

原LocalMcpManagementService/McpOAuthManager先安装listener，Windows实际Electron对旧端口HEAD404后，用shell.openExternal打开本地mock AS。AS保留原state/code/issuer于Linux内存并返回等待页。探针随后调用原manager.cancel和service.aclose，真实flow为cancelled、task done、listener None、pending=0、无grant。

另一个本次mock aiohttp服务真实绑定同一callback端口。Windows HEAD得到该错服务200，再用shell.openExternal打开不含query的mock AS `/rebind` 路径；AS在浏览器内重定向完整旧callback，synthetic code/state不进入host协议或日志。错服务核对旧code/state/issuer匹配，收到一次实际 `/callback`，返回HTTP200及“Authorization successful”页面。页面显示不能替代原owner事实：旧flow仍cancelled、token请求0、grant不存在、原listener仍None、pending=0。随后foreign listener也关闭，Windows再HEAD不可达，原guest/carrier正常exit0。

`rebind.host-results.json`保留安全原始事件，`verify.py`离线核上述结果、正常close/无partial frame和host carrier字段/authquery扫描；`verified-results.json`通过。fixture的无query导航是跨Windows送达旧callback的研究工具，不新增生产OAuth协议或由host裁决授权。

第一次重绑后立即HEAD发生ECONNRESET，未导航旧callback；有限25秒fixture观察后正常清理但case失败，原结果与Electron源码保存在`first.*`。随后沿用原主矩阵已使用的最多5秒只读presence观察再运行本项，通过；没有重放授权或让host参与PKCE/state/token判断。这些观察时限属于有限fixture，不是产品OAuth等待寿命cap。

运行：Windows项目venv执行`rebind/run.py rebind`，它启动真实Electron44.9.0/Node24.22.0；Linux代码运行当前clone项目venv/mcp2.1.0。两个精确隔离home均已删除，`audit.py`在当前UID可观察范围未见本guest残留；PermissionError仍跳过。双方listener、pending、本地grant已清，不关闭共享发行版或其他浏览器窗口。可选Electron profile/cache沿用父报告的清理限制，不重试被自动审批拒绝的递归删除。
