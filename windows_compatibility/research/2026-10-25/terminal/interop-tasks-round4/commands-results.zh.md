# 第四轮命令与结果

从 `C:\python_workspace\pulsara-agent` 使用本地 uv 管理 `.venv`；Linux guest 明确 Ubuntu/plumwsl 与 clone cwd。首次需竞态分支 `output/windows-g0/interop_races_r3/round4/relay_fixture.py` 及前轮小型 Win32 fixture `output/windows-g0/interop_jobs/round2/job_probe.py`。不是生产 import。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py startup
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py forward
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py upstream
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py faults
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py minimum
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py watcher-fault
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py half-only
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py smoke
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/windows_probe.py tail-cancel
```

每次新 UUID。各运行所处源码阶段不同，不能用当前源码重跑旧名后声称原行为。最终选择及具体 sourceResult 一律由 final-matrix.json 给出：12 条修前成功、2 条 cold writer 修后限定复验、2 条 done-task/live 修后验证，共 16 条。原 faults 组含 watcher 失败，绝不称整组通过。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/verify_results.py
```

结果 verification.json：passed=true、selectedCaseCount=16，分版本计数 12/2/2。此命令只审阅已有记录，不重跑任务。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/recover_first_probe.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/cleanup_failed_watcher.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/cleanup_failed_cases.py normal-pty-06ea3e18
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/cleanup_failed_cases.py normal-pty-588862f5
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/verify_cleanup.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/settle_cleanup_evidence.py
```

前四条只针对具名历史失败。回查审计 254 Windows/73 Linux 身份及 25 私有目录：无当前活本次 Linux 身份/私有目录。verify_cleanup 严格回查因当前 PID33468 的 error5 返回 1，原失败保留；最后一条不再查询/操作当前 PID，而引用旧 creation 精确匹配且当时持续持有的 HANDLE wait0，输出 remainingExactOwned=[]，同时保留 currentPidUnobservable。不要把最后一条解释为“当前 PID 已不存在”。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round4/environment_probe.py
.venv/Scripts/python.exe -m ruff format output/windows-g0/interop_tasks_r3/round4
.venv/Scripts/python.exe -m ruff check output/windows-g0/interop_tasks_r3/round4
```

环境：两 clone HEAD adb3e1de、Windows Python3.12.13、Linux Python3.12.15、/home/plumwsl ext4。最终 Ruff 通过。没有运行 provider、MCP/GUI 或额外完整原测试矩阵。
