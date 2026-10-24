# 命令与结果

仓库根 `C:\python_workspace\pulsara-agent`，Windows 本地 uv 管理 `.venv`，Linux 明确 Ubuntu/plumwsl clone。所有记录使用新 uuid；再次运行会产生不同前缀。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round3/task_probe.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round3/task_probe.py --patched
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round3/task_probe.py --ab
```

最终 baseline-7cc458d9 六项、patched-c6de6806 六项、shared-ab-8e194a20 两项 completedObservation=true，failure 字段不存在，cleanup Job ActiveProcesses=0、members=[]。baseline 本身的“完成”是观察实验完成，其中四项记录真实 owner 的负例；不是产品合同通过。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round3/verify_results.py baseline-7cc458d9 patched-c6de6806 shared-ab-8e194a20
```

结果：checkedCases=14、allMeasuredAssertionsPassed=true，见 verified-results.json。

```powershell
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec .venv/bin/python -m pytest tests/test_round2_terminal_environment.py tests/test_terminal_cognitive_subtraction.py tests/test_round2_terminal_output.py -q
```

结果：47 passed in 6.40s。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round3/environment_probe.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round3/verify_cleanup.py
.venv/Scripts/python.exe -m ruff format output/windows-g0/interop_tasks_r3/round3/guest_probe.py output/windows-g0/interop_tasks_r3/round3/task_probe.py output/windows-g0/interop_tasks_r3/round3/verify_results.py output/windows-g0/interop_tasks_r3/round3/verify_cleanup.py output/windows-g0/interop_tasks_r3/round3/environment_probe.py
```

环境结果见 environment.json；首次过长 socket 路径直接重现 AF_UNIX path too long。清理结果：222 Windows 身份、62 Linux 身份，remainingWindows/unknownWindows/remainingLinux/privateChildren 全空。格式化不改生产文件。

归档重点：report.zh.md、五个 Python 探针、environment.json、verified-results.json、cleanup-final.json、最终三个 matrix 及相同前缀原始记录，以及报告列出的首轮失败。没有生成代码/文档 SHA 或另建证据指纹体系。
