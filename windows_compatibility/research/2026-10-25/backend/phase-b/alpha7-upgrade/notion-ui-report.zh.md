# 正式 alpha7 UI Notion OAuth 的只读辅证

本轮使用正式安装的0.1.0a7，原GUI创建唯一用户级测试连接 `notion-oauth-alpha7-smoke`，保持禁用。root通过原登录入口发起，用户亲自完成授权；本分工没有操作认证UI、创建第二OAuth manager、调用业务工具或修改保存配置。

`alpha7-notion-authorized.json`：正式alpha7 Python经LocalSettingsStore读取指定local/user credential owner，grant存在、未过期，issuer/resource纯origin均为 `https://mcp.notion.com`；配置存在且禁用，endpoint匹配。没有记录token、client secret、client JSON、client ID、scope、callback code或完整授权URL。与reinstalled快照中的原非秘密模型连接字段、canonical session/model binding、三turn/28条entry counts及PG身份逐项相同；不冒充旧快照未记录的完整配置或secret值逐字节比较。

root已保存原GUI测试成功通知 `output/phase-b/root/alpha7-notion-test-success.txt`：65tools、8resources、0templates、2prompts。源码调用链为原 `testUserMcp → POST /api/capabilities/mcp/test → LocalMcpManagementService.test → BoundedMcpSdkClient.open → discover_mcp_catalog → finally client.aclose`。该路径只进行初始化和按advertised capabilities列目录，没有tools/call/resources/read；这些UI行为和计数来自root实际记录，本分工只读辅助核验，不把grant存在本身当成目录发现成功。

准备过 `cleanup_notion_api.py`，要求来自本次ready管道的完整公共certificatePem、标准CERT_REQUIRED/hostname验证及每次请求完整DER相等，然后才使用原cancel/logout/delete/current_identity。当前ready证书只在主进程内存，原临时文件已删除、日志无可信副本，因此该探针**未执行**；没有从未验证连接获取证书后自行信任，没有关闭TLS验证，也没有为此新增生产诊断。root改由同一原GUI清理唯一测试owner，等待其完成通知后才执行 `observe_notion.py cleared`。探针中的30秒只作用于单次诊断HTTP请求，不是产品新增寿命cap。

用户明确确认后，root通过原GUI依次“注销本地授权→未登录→移除服务→确认移除”，观察MCP0且无可见cleanup error。收到这次实际完成通知后，正式alpha7 Python执行 `observe_notion.py cleared`，exit0：指定config/grant均不存在，managed_credentials_for_owner=0；Linux127.0.0.1:17839 connect_ex返回111/ECONNREFUSED，回调端口已无监听。原非秘密模型连接字段、credential-present、canonical session/model binding、三turn/28条entry counts及PG身份逐项preserved=true。`alpha7-notion-cleared.json` 为独立证据，未覆盖authorized结果。

GUI成功与只读磁盘/PG/端口结果共同支持本轮本地清理完成；没有通过HTTP探针读取原响应，所以不声称实际捕获了`operation.cleanup_attention=false`响应或直接观察到owner内存pending集合。`cleanup_notion_api.py`始终未执行，当前没有`alpha7-notion-api-cleanup.json`。本地grant删除不代表Notion服务端授权撤销；本轮未发送远端revoke，也未进入账号设置操作。
