# 建议归档清单

- `report.zh.md`、`environment.json`、`cleanup-final.json`、本清单。
- 六个研究入口：`socket_probe.py`、`pinned_socket_probe.py`、`same_chain_probe.py`、`relay_probe.py`、`runtime_environment.py`、`verify_cleanup.py`。
- `fe6645af-*`：独立A/B路由、父先退持pipe、取消隔离、裸env缺失socket fallback。
- `pin-36e45fe3-*`（不含零字节`.go`）：O_PATH正测与anchor死后拒绝新PE。
- `relay-4e1c8baf-*`：最终25秒B负载、relay死亡前后B仍live、之后自然finish，A/B全部原始record。
- `relay-ca30a7c3-*`：保留首轮6秒B负载及时序限制，不能替代最终B断言。
- `4613808b-*`：保留首个未显式继承pipe导致的断言失败与精确清理记录。
- `same-chain-66dccd3c*`、`same-chain-3c90111a*`（不含`.go`）：分别保留显式setsid逃逸fallback与普通同PGID子被回收/等待done超时，二者边界不可混合。

`sources/`均为报告链接的官方tag源码，可重取；无需归档它、`__pycache__`或零字节控制文件。没有二进制、DB、settings或凭据。

这些入口的原始输出路径就是本次实验的事实；`socket_probe.py`从原output布局导入jobs代理的`job_probe.py`。若归档到不同目录后希望直接复现，应只调整归档副本的fixture定位到归档jobs小fixture（或在原output布局运行），不能修改原始records里的路径、时间或状态。报告中的固定宿主/guest路径均须复现者核对。
