# Phase A PG物理接缝契约提案（待root协作确认）

已读取AGENTS、冻结WSL规格§3.2/3.3/11.1/13.1及当前postgres.ts/postgres.test.ts/main调用点。当前只读生产，未运行baseline/测试/DB；待root确认共享接口和基线完成后再编辑。

目标：唯一TS BundledPostgres持有initialized/startedHere、start/stop/reset、错误仲裁、DSN、104-byte边界。POSIX操作实现只复用现Node fs/exec。无WSL/helper/新锁/未知结果协议或Python改动。

当前真实构造caller只有main.ts:36和postgres.test.ts:10。desktop负责main/locator/CLI，我只改postgres及独有operations/posix实现/tests；不交叉写文件。

最小操作候选：prepareLayout；读取PG_VERSION（ENOENT由start owner按原规则处理，reset不吞）；读取postmaster.pid事实；socket mode；reset所需realpath/uid/type；固定initdb/pg_ctl argv执行；删除该owner的data目录。绝不提供任意fs路径或任意binary执行入口。

装配建议：纯resource locator给postgresBin，工厂createPosixPostgresOperations(postgresBin, home)构造同一布局和实际操作对象，BundledPostgres必传该对象；owner不带默认POSIX fallback、资源选择或第二套路径实例。readonly directory/socket/log值可由同一operations携带供owner/主流程使用。root协调最终名称。

迁移须逐句保留：

- prepare desktop/mkdir socket/chmod/socket长度检查发生于readVersion前；byte length >=104拒绝。
- 只有ENOENT可被start版本读取吞；空版本仍按当前initdb路径；initdb拒非空data，不为失败擦目录。
- status只有numeric code3表示stopped，其他异常原样抛。start比较path.resolve(postmaster第二行)，reset比较原第二行，不能顺便统一两者。
- failed start再次status+精确instance验证才设置startedHere，但仍抛原start错误；probe失败不覆盖原错误。
- local exec逐次取process.env并覆盖LC_ALL=C，保4MiB maxBuffer；不增加timeout/重试/结果receipt。
- reset按原顺序检查data及父realpath→uid/目录→纯数字version→status→若running精确pid目录/socket→stop→rm；失败绝不提前清状态/删除。
- reset成功才startedHere=false；stop不自行清initialized/startedHere；DSN反斜杠/单引号转义不改。

拟补测试重点是实际新边界：注入操作仍保持原序列/异常/状态（特别status code3与其他launch error区别、failed-start原错误、reset拒绝不删）；同时保留现真实POSIX临时目录+fake pg_ctl可执行文件集成测试，验证leaf的argv/本地文件行为，而非给每个转发方法写镜像测试。socket边界、DSN与reset现有缺口可用产品级断言覆盖，不扩整个PG conformance suite。
