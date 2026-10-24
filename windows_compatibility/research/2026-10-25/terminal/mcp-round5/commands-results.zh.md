# MCP 第五轮命令与结果

仓库根使用 `.venv/Scripts/python.exe`，guest显式 Ubuntu/plumwsl、Linux clone cwd及Linux `.venv`。本轮本地SDK server，不用saved settings/provider。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round5/windows_probe.py baseline
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round5/windows_probe.py normal
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round5/windows_probe.py failure
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round5/windows_probe.py guard
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round5/verify_results.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round5/verify_cleanup.py
```

结果：六条 observed=true，verification passed；一条原owner baseline实际遗漏Windows child，四条candidate物理close和一条真实guard拒绝各有断言。70 Windows/21 Linux身份及6目录的精确审计为空集，无unknown。

```powershell
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec .venv/bin/python -m pytest tests/test_mcp_transport_lifecycle.py tests/test_mcp_scheduling_isolation.py tests/test_mcp_credentials.py -q
.venv/Scripts/python.exe -m ruff format output/windows-g0/interop_tasks_r3/round5
.venv/Scripts/python.exe -m ruff check output/windows-g0/interop_tasks_r3/round5
```

原tests：21 passed in 6.68s。实际SDK inspect版本2.1.0、stdio_client/ClientSession/MCPServer API与安装源码，研究适配未复制parser或SDK owner。

前轮只读小依赖：`output/windows-g0/interop_tasks_r3/round4/{manager_probe.py,windows_probe.py}`、`output/windows-g0/interop_races_r3/round4/relay_fixture.py`、`output/windows-g0/interop_jobs/round2/job_probe.py`。复现须保留相应相对位置或按本机路径配置；没有复制venv/第三方源码或将探针import到生产。
