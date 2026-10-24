# WSL-only 最终语义候选二审

2026-10-24；backend；gpt-6.1-sol / high。

**最终结论：当前 394 行根规格可冻结，无剩余阻断。** 第一轮387行全文审阅结论仍成立；本轮重新通读394行完整候选，并只读核对OAuth注入实际源码。root随后对§3.3操作帧前取消及§10.3跨eventloop取消作最终同段精确化后，已再次读取确认。后续仅文首/§15冻结元数据更新不改变本批准覆盖的实施语义。

## 修订复核

- **PG helper**：所有operation帧前取消（含ready已到但尚未发帧）均结束stdin并join helper；发帧后持同kernel flock到真实child/readback结算，不能转成not-started。wait-owner为独立等待模式，不能先拿操作锁再嵌套等同锁。NB busy、ready-before-operation、CLOEXEC不泄给PG server、helper异常死/pg_ctl活则G0失败等合同一致，未引新registry/receipt/lease。
- **OAuth实际扩展点**：`capability/mcp_management.py:329/:349/:367`已有async open_browser注入并传给McpOAuthManager；默认路径实际调用Linux `webbrowser.open`。`desktop_app/application.py:91`、`conversation_kernel/host.py:7841/:7877/:7967`当前未贯穿参数，最终§3.2/10.3明确这些真实修改点，不再假称Windows浏览器可自动打开。
- **OAuth唯一owner**：标准SDK仍负责PKCE/state/iss/token；Pulsara原Linuxcallback listener先启动；仅经过现可信JSONL向当前live Node请求有限HTTP(S)/无userinfo的system-browser动作。授权code/token/client-secret不经host通道，没有新OAuth协议/通用RPC或第二授权状态。Windows shell.openExternal仅实际打开动作，不裁决authorized。
- **取消与未知**：Node只在真实open sink检查本地已知close/retire；Linux请求已发后的取消不能跨另一个eventloop保证撤销排队/已执行打开，原flow拒晚callback/result且保unknown、不自动再开。最终文本已删除不可能保证的跨域取消承诺，同时没有为此新增cancel协议/registry。pending future/listener随原owner清理，不因晚回复重新启动flow。
- **OAuth故障门**：PKCE S256、state/issuer/一次性验证、Windows callback端口冲突/rebind/取消明确进入实机门。复用实际SDK，无自制PKCE/回调代理。
- **TLS长程**：“短命”限定为本次网络物理生命周期；不得用到期重启kernel/换epoch或添加任务总寿命cap。G0包含时间变化/长连接/新连接，与原exact cert/session pin门一致。没有以此提前声称TLS集成已通过。
- **构建目标**：Ubuntu x64产guest/rootfs/Linuxpayload，Windows原生runner产Electron/NSIS并装同版本制品，cache/recipe输入、release集合与publish never明确；构建平台Python不被混作Windows执行内核。

## 仍保留的诚实限制

本批准不免除G0：WSL relay/EOF、helper异常物理锁、exact TLS pin/cache/退役、Windows回调与Linuxext4/mount/UNC仍需真实证据；任一不可满足先修规格，不换fallback。A只做Node实际接缝与受影响POSIX验收，Windows激活仍要求全部G0/B/native-installed/provider/长程与受影响回归。workspace仍唯一Linux-backed用户级picker，Windows全局CLI和全库typed binding/ProcessScope迁移均不属于本次首版。

本轮只写本个人审阅文档；未改根规格或production，未改保存配置/数据库，未运行测试、G0、provider、部署或构建。
