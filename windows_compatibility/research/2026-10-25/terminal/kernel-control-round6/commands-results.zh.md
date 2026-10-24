# 第六轮命令与结果

所有工作位于 `C:/python_workspace/pulsara-agent/output/windows-g0/interop_tasks_r3/round6`，不改生产/spec/commit。Windows编排Python3.12.13，WSL Ubuntu/plumwsl开发Python3.12.15。实际kernel使用bundle Python3.12.15。

```powershell
& 'C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe' output/windows-g0/interop_tasks_r3/round6/generate.cjs
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/windows_probe.py smoke
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/windows_probe.py child-clear
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/windows_probe.py protocol
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/windows_probe.py hosts
```

前15例12observed/3保留loss观测失败。Esc期间只运行checkpoint.py安全审计，未继续新案例。用户确认误按后继续：

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/windows_probe.py loss-recheck
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/windows_probe.py clean-eof
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/finalize.py
```

loss-recheck中5例observed、1例同步观测阻塞失败；clean-eof仅该例修后重验observed。最终22case、18observed/4保留harness失败、18条窄断言；remainingExactHosts/Kernels/Homes空，当前carrier PID均不存在。所有实际结果与关键raw统一保留final-matrix，不逐例再复制stdout/stdin日志。

```powershell
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec .venv/bin/python -m pytest tests/test_desktop_runtime.py -q
.venv/Scripts/python.exe -m ruff format output/windows-g0/interop_tasks_r3/round6
.venv/Scripts/python.exe -m ruff check output/windows-g0/interop_tasks_r3/round6
```

原测试12passed in1.53s；Ruff格式/检查通过。owner.cjs/generate.cjs仅使用本地TypeScript printer做最终源码格式整理；没有因此重跑完整矩阵。只读空间/WSL/resource/ldd观测与官方Ubuntu/Microsoft网页用于distro方案，未下载/import/terminate。计划文件精确列来源与观测数字。

## 获授权后的独立distro / SIGHUP / 窗口 / 人工睡眠补充

前段“未import”描述当时检查点，随后root明确审阅授权，实际执行：

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/distro_manage.py create
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/distro_build.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/distro_prepare.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/distro_probe.py normal
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/distro_probe.py distro-loss
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/distro_finish.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/signal_build.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/signal_probe.py observe-default
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/signal_probe.py ignore-hup
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/signal_probe.py ignore-hup electron
& 'C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe' output/windows-g0/interop_tasks_r3/round6/browser_build.cjs
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/browser_probe.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/sleep_build.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/sleep_probe.py
```

镜像418495746bytes、SHA256与Ubuntu签名gpgv均通过；distro正控和真实terminate/new-namespace观测完成后精确注销、删除VHD/镜像，原Ubuntu Running/default不变。3条实际SIGHUP对照通过；窗口首次final-result缺失保留，补原main既有window-all-closed政策后只重跑该项成功。

sleep_probe打印ready后保持静置，root收到用户“实际睡眠1–2分钟且已唤醒”通知才打开精确 `sleep-076aee0b.inspect` 门；同kernel/PG身份、namespace、两锁和真实SQL一致，随后原Quit正常收尾并精确清理。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/supplement_verify.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round6/supplement_audit.py
```

补充8case、7项有限正证、1条原窗口fixture失败保留；7条窄断言通过，8host/6共享kernel-home及2已注销namespace scope全部精确结算。离线验证首轮glob误含sleep的host-result，assert拒绝，改为显式排除后复核；没有为此重跑物理实验或修改原数据。

最终Ruff全目录检查通过；固定Node22 `--check` 校验本目录11个cjs全部通过。归档57项约1.53MB；全部JSONL逐行解析通过。未因格式/静态检查重新运行完整物理矩阵。
