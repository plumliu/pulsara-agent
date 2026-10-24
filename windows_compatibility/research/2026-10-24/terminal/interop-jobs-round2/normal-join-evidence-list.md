# 正常join补充小清单

- `normal-join-report.zh.md`、本清单。
- 新探针 `normal-join-probe.py`（复用冻结 `job_probe.py`，没有改动它）。
- `normal-join-4a197629.jsonl`：完整Windows角色/精确identity/EOF/Job范围及cleanup事实。
- `normal-join-4a197629.jsonl.linux.jsonl`：anchor/runner PID+starttime、runner结束、anchor EOF。
- `normal-join-4a197629-summary.json`：最终ActiveProcesses0、全部8个持有process HANDLE wait=0、Linux范围不存在。
- `normal-join-4a197629.jsonl.carrier-output`（本轮空文件）及 `normal-join-run.log`（probe exit0的最终summary输出）。

未产生二进制/新依赖，未覆盖旧证据；不添加文件内容hash。
