# 第七轮命令与结果

Windows仓库根，项目 `.venv` Python3.12.13编排；固定Windows Node22.23.3；新独立distro实际bundle Python3.12.15/PG17.9。没有provider调用、生产改动、共享Ubuntu终止或PhaseB。

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round7/distro_manage.py create
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round7/distro_build.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round7/distro_prepare.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round7/recovery_build.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round7/recovery_probe.py
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round7/distro_finish.py
```

唯一实际恢复链recovery-dee640cd observed=true、failure=null。真实canonical POST201→本次distro terminate→原样旧pidfile/PGDATA→原wait-owner+TS start/initializeFalse→PG实际crash recovery/canonicalGET200和SQL→原Quit/PGstop1/home清理通过。

最后finish脚本Running断言失败；此时新distro注册/VHD/镜像已删除，原Ubuntu实际Stopped。该原失败保留，不重新启动共享Ubuntu改绿。随后仅只读核验：

```powershell
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round7/distro_manage.py audit-retired
.venv/Scripts/python.exe output/windows-g0/interop_tasks_r3/round7/verify_final.py
```

passed=true：两host旧精确身份已退、本次home删除、本次注册/目录不存在、原default/Ubuntu注册项逐值不变，原Stopped状态如实保留。官方镜像摘要与Ubuntu CD签名gpgv已通过；大型镜像/VHD/PGDATA不归档。

最终Ruff format/check全目录通过；固定Node22对7个cjs作`--check`通过。全部JSONL逐行parse通过；manifest35文件约276KB。格式化后只重读已有原始结果/作静态检查，未再跑物理恢复链。PG agent独立只读审阅通过。
