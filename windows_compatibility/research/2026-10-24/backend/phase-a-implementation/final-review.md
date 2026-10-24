# Phase A最终交叉审阅与真实PG失败边界实验

2026-10-24；backend；gpt-6.1-sol/high。

**结论：当前Phase A代码无剩余阻断，本地macOS证据通过；原生Linux/已安装GUI等最终资格须以root汇总为准。** 不将本实验升级为Phase A全部平台已验收，不进入WSL/B/G0。

## 实际命令与隔离

在仓库根执行：

```sh
.venv/bin/python output/windows-compat-20261024/backend/phase-a-implementation/run-real-pg-failures.py
```

该repo uv Python runner执行当前Node probe，直接import最终 `desktop/dist/postgres.js` 与 `postgres-posix.js`，使用实际随包 `desktop/resources/postgres/bin/{pg_ctl,psql,initdb,postgres}`。没有build或修改dist、生产、tests、CLI、saved settings。

传给Node的环境仅PATH/TMPDIR/LANG/LC_ALL，probe把HOME设为自己刚创建并realpath后的 `/private/tmp/pga-WOCf4r`。没有读取生产LocalSettingsStore或连接用户数据库；全部DSN/SQL均明确指向自己socket、postgres库及pulsara_admin。

原始证据：`real-pg-failures.log`（命令、实际状态/错误/清理、exit0）、`real-pg-failures.json`（passed=true）、`real-pg-failures.cjs`、`run-real-pg-failures.py`。它们都只在本个人目录，不作为生产依赖/authority。

## 实际结果

| 场景 | 观察与断言 |
| --- | --- |
| 真实初次启动 | PG17.9，PID70673，initialized=true、startedHere=true；listen_addresses为空，Unix socket在本home，pg_search预加载。SQL读回server_version、data_directory、unix_socket_directories和postmaster start time |
| 自有实例socket元数据错配 | 仅修改本实验postmaster.pid第5行；新owner.start报“已有数据库进程的目录或 socket 与应用专用实例不一致。”，不认领startedHere；reset报“运行实例与专用目录不匹配，拒绝重置。”。真实pg_ctl仍status0/同PID，SQL读回实例全部字段与初始值完全相同，sentinel保留；随后恢复原pidfile |
| raw/resolved既有差异 | 仅将本实验pidfile目录行改成同目录`/.`；新owner.start按resolved复用，startedHere=false；reset按raw拒绝。真实实例仍同PID/同SQL start time，可查询且未被停止；随后恢复原pidfile |
| 真实cluster版本不兼容 | 正常stop并确认status3后，将自有cluster PG_VERSION从17改为16；start报原完整中文版本拒绝。initialized/startedHere=false；16原文、sentinel及global/pg_control同inode/size保留。没有自动initdb、删除或起库；恢复自己修改的版本文件 |
| 精确清理 | 用同一确切PGbin与data路径观察；最终真实pg_ctl status=3、stdout为“pg_ctl: no server running”。确认临时home仍原realpath且是本次创建的pga前缀后仅删除该home；原始证据temporaryHomeRemoved=true、exit_code=0 |

实验操纵的是自己PG的pidfile/版本文件，未改任何其他PG实例。清理若无法确认真实stopped则probe会保留临时目录并报失败；本次没有走该分支。

## 代码与统一验证复核

- owner唯一保initialized/startedHere、错误仲裁、initdb/status/start/stop/reset顺序、DSN/104byte边界；physical leaf只承担实际Node文件/命令操作。
- 最终readInstance必传raw/resolved模式；reset不额外查询cwd。root发现的等价性问题已修，独立Node child删cwd测试确实覆盖目标边界；macOS `/tmp`符号链接fixture已realpath，不弱化断言。
- main/locator/runtime/CLI所有实际构造caller同片硬切；无默认adapter/旧构造/fallback，无Python业务或WSL helper/锁协议范围混入。详细交叉审阅在 `cross-review.md`。
- 只读复核root `verify-final.log`：最终46/46，0fail/skip/cancel；随后真实搬移PG/vector0.8.1/pg_search0.26.1、Pythonimport、Chromium与release资源检查通过。
- terminal与desktop对最终PG模式修订均明确无阻断；本独立真实PG故障实验进一步证明拒绝路径没有误停/误删自己的真实运行实例，不能由mock或单纯API调用断言替代。

本轮没有provider、保存配置/用户DB修改、签名发布，也没有为机械抽取增加持久事件/receipt/hash/锁协议或cap。源码/本机功能证据允许推进Phase A收口；未有的原生Linux/安装资格仍须如实标待验收，WSL产品支持仍未实现。
