# Phase A PG物理操作实施记录

2026-10-24；backend；gpt-6.1-sol/high。

本专题完成代码与聚焦测试接入；整个Phase A是否完成仍由root汇总原生/安装/资源与交叉审阅证据。没有实施WSL/B/G0。

## 变更

- `desktop/src/postgres-operations.ts`：只读directory/socket/logPath/socketAddressBytes，以及当前真实消费的布局、版本、指定raw/resolved形式的instance观察、mode、reset路径/uid/type、固定PG执行和精确data删除。
- `desktop/src/postgres-posix.ts`：将原Node fs/exec物理动作移到一个POSIX工厂，固定initdb/pg_ctl集合、原LC_ALL=C和4MiB maxBuffer，原异常code直接保留，无超时/重试/默认平台fallback。
- `desktop/src/postgres.ts`：必传operations；保唯一initialized/startedHere及全部生命周期/错误文案/DSN/104byte门。启动验证用leaf resolved目录、reset验证用原raw目录；未顺便统一现有规则。owner无Nodefs/exec/path选择。
- `desktop/src/postgres.test.ts`：原真实临时目录+fake pg_ctl fixture保留并硬切工厂；新增resolved/raw差异、status数字3及原错误优先、首次initdb argv/state、stop/remove失败保state、socket门/DSN、真实symlink拒删。

main由desktop在唯一装配点使用 `new BundledPostgres(createPosixPostgresOperations(resources.postgresBin, home))`；没有默认构造、旧构造caller或第二PG状态机。

## 验证及审阅

- root改前基线完成后才开始生产编辑；本agent没有并发build覆盖dist。
- terminal统一 `npm --prefix desktop test`：tsc成功，45/45通过，0skip；含本专题7个PG测试。原始日志在 `terminal/phase-a-implementation/npm-test-candidate.log`。这是现macOS本地证据，不代表native Ubuntu/WSL/已安装产品通过。
- 本agent `git diff --check`通过；只读引用搜索确认main与tests所有BundledPostgres构造硬切。
- 本agent已跨审main/locator/CLI与runtime/host，无阻断，见 `cross-review.md`。terminal和desktop均回报PG源码跨审无阻断；root仍负责最终统一验证/冻结。

## 限制

未改Python/业务settings/数据库；没有WSLhelper/锁握手/TLS/OAuth wire/Windows构建。没有重置保存DB、签名、发布或运行provider。root安排的真实随包PG集成及安装/资源资格不由这里的fake executable/注入测试替代；native Linux环境缺项须继续标待验收。

## root等价性复审后的修订

首版readInstance会同时求raw/resolved，导致reset在调用进程cwd已删且PID记录malformed时额外path.resolve抛ENOENT，改变原中文instance-mismatch错误。已改成必传`readInstance('raw'|'resolved')`，只startup请求resolved，reset完全不执行规范化。新增第8条真实独立Node child回归，删除自己的cwd并保留真实data/PG_VERSION、使用status0可执行fixture，断言原错误与state/data保留；主测试进程不chdir。已通知terminal/desktop源码复核，等待root统一最终npm；首轮45/45是修订前证据，不能当修订后已通过。

最终回归首次暴露fixture前置问题：macOS `/tmp`是符号链接，因此测试先命中原reset路径保护；已只将该测试temp root realpath canonical后传child，未动production/弱化断言。只读检查root最终 `root/phase-a-implementation/verify-final.log`：46/46通过、0fail/skip/cancel；随后resource smoke通过PG17/vector0.8.1/pg_search0.26.1、应用import与搬移Chromium，release资源检查通过。terminal及desktop均复核readInstance mode修订无剩余阻断。此为root执行的macOS证据，仍不代表native Linux/整个A已验收。
