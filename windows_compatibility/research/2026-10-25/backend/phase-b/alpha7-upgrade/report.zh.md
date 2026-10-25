# alpha7 升级前后只读数据资格

2026-10-25。用户已授权本机alpha7更新。root负责正常Quit、维护准入、NSIS与安装启动；本分工只读现有正式PG/settings及版本，不reset、不迁移、不启动/停止PG、不改配置、不调用provider。

升级前 `alpha7-upgrade-before.json` 已完成，使用正式alpha6 Python/site-packages，原deep verifier status verified：

- system_identifier=7700523439330484737；data=`/home/plumwsl/.pulsara/desktop/postgres`、inode171484；socket=`/home/plumwsl/.pulsara/desktop/socket`，无TCP。
- settings原LocalSettingsStore读取；只记录stat（inode197520/size440/mtime_ns1792918064891832010、uid1000/mode0600）和非秘密连接信息。未导出配置正文、API key或其它credential，也未生成配置hash。
- 保存连接deepseek-flash/chat_completions/catalog_standard，原会话model binding为effort/high；session:r3d6okwuq2trl3ribxzct6rbuy的三个ROOT turns均COMPLETED。记录各turn、initial/final entry ID、会话稳定字段和transcript entry counts，后验比较同值，不把会重开的writer lease/PID当稳定数据。

本目录独立于root此前 `installed-restart-before/after.json`，没有覆盖原冷重开证据。已通知root前快照完成、可正常Quit。

收到root正式alpha7安装并进入HTTPS工作台的通知后，已使用 `/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/python/bin/python3` 执行 `observe_upgrade.py after`，exit0。`alpha7-upgrade-after.json` 记录package version=0.1.0a7，新版本原deep verifier status=verified；同systemid/data/socket/inode、settings stat、保存连接与会话/三turn/entry counts全部逐项相等。postmaster start由17:32:00.940553+08变为17:55:42.936671+08。此次升级保数据验证通过；没有改配置或启停PG。已通知root可再次正常Quit，准备普通卸载/重装。

随后普通卸载/同alpha7重装的观察使用独立 `reinstalled` 标签与 `alpha7-upgrade-reinstalled.json`，仍逐项比较原before，同时核对after版本不变、postmaster start较after更新。三个标签均拒绝覆盖现有文件。两次后验都须等root明确阶段通知；准备阶段仅通过AST语法校验，未连接数据库。

root完成普通NSIS卸载/同alpha7重装并进入HTTPS工作台后，已使用同一正式alpha7 Python执行 `observe_upgrade.py reinstalled`，exit0。原deep verifier status=verified；相对before所有preserved/same_pg_identity字段均true，settings inode/size/mtime/uid/mode不变，原模型选择、会话、三个COMPLETED ROOT及28条entry counts保留。package仍0.1.0a7，新postmaster start=17:57:33.694648+08，较after更新。卸载/重装保数据后验通过；本脚本未执行卸载/安装、未修改配置或启停PG。卸载exit0、Windows appEntry消失及离线保留检查来自root独立记录，不冒充本脚本观测。
