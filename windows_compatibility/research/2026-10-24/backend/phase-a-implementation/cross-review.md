# Phase A 候选跨专题审阅

范围：当前main/locator/CLI和runtime/host操作改动；2026-10-24，backend，gpt-6.1-sol/high。只读审阅其他agent文件，无交叉编辑。

结论：当前源码无阻断；等待统一编译/测试及root实际资源/安装证据，不将源码审阅当Phase A验收通过。

- main只装配一次DesktopResourcePaths与posixHostOperations，resolveHome用相同精确Python路径；runtime使用同一home/host，PG从同一postgresBin/home装配。boot/reset/requestQuit及start frame原门序未改。
- 原CLI脚本文本以pythonExecutable代替resources内部拼接，shell quoting、"$@"、marker/conflict/PATH/用户目录与Linux /opt包入口规则保持。所有生产/测试cliLauncherScript调用已硬切。
- locator只包含实际5字段，纯path.join，没有IO/settings/进程或新authority；main/runtime/PG/CLI调用闭包已接齐。新TS文件由既有tsconfig `src/**/*.ts`构建，未拆recipe/原生资源leaf，未为此添加无消费的cache authority。
- runtime仍拥有child、preparation、ready/JSONL、inspection、stop/EOF/exit与错误；host只承担现同步home解析、stream spawn、有限exec、目录准备与日志写入。env/cwd/stdio和原字节日志不变，保同一spawn/pipe/退出顺序。
- HostOperations有限exec无未消费maxBuffer参数，PG自己的LC_ALL/4MiB有限执行机制不耦合通用host port；两个领域各自调用维护Node SDK，没有第二生命周期或任意RPC。
- 无Windows/WSL adapter、helper/PG锁握手、TLS、OAuth新wire或frontend部署范围溢出。未动Python业务/DB/schema/providerprefix。
- main新增测试覆盖装配共享值与原启动门，runtime新增测试覆盖目录失败的not-started与日志写失败后原shutdown owner；原CLI实际可执行fixture与既有runtime生命周期测试保留。不是以转发方法数量建立镜像测试。

PG本专题实现另保原fake pg_ctl真实执行fixture与真实symlink reset拒绝，加owner原state/error/argv/boundary故障断言；由terminal统一首跑npm test避免并发覆盖dist。本文件未执行任何测试。
