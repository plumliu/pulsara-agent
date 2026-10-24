# PG 第四轮真实 owner 与 typed guard 独立审查

2026-10-25，interop_races_r3；只读 `pg_admission_r3/round4` 的 candidate.diff、owner.cjs、helper.py、机械候选 postgres.ts、最终 report.zh.md、transport-spawn-error.json，并抽查对应结构化结果。未运行 PG、未修改该分支或生产文件。

**可归档。真实 owner 的未提交他者误归属负例、输出候选的有限对照，以及完整 G0 未通过的限定，与源码和证据一致；没有把 typed 草案写成生产已有合同。**

- `owner.cjs` 直接加载编译出的当前 `BundledPostgres` 并构造实际对象，观察其原 `initialized/startedHere`；适配层只实现现有 `PostgresOperations` 叶子调用，没有第二份 lifecycle owner。
- foreign负例中，本次start没有operation-written，收到真实no-operation并等carrier正常结束。之后在原catch readback之前，独立有限helper实际启动同一准确私有PG；该helper不是另一个影子 startedHere。原owner catch按当前running/data/socket便设startedHere=true，因而认领了本次未提交的启动。这与“自己的start丢回复后PG可能运行”的歧义分开，因果明确。
- `candidate.diff` 只有读取 `error.postgresExecution` 和在其等于 `not-submitted`/`unknown` 时原样throw两行。它阻止本次错误诱发新的归属更新，没有主动清空调用前已有startedHere，也未改原正常复用/stop/reset逻辑。
- helper的真实PG非零结果作为完整 `result.ok=false`、numeric code和原stdout/stderr返回；对应adapter在sent=true且carrier/result完整时不追加上述两类tag，因此仍进入原start catch的status/readInstance readback。只有传输/未提交类别提早退出。这个保留结论本轮来自源码和原/候选各8项同一测试；报告明确**未新增真实PG非零/延迟readiness专门实测**，没有扩大证据。
- 两个候选对照中，独立PG真实query仍成功，而not-submitted或unknown保留、startedHere=false。它没有通过取消独立启动或自动重发本次start获得结果。`mainQuitWouldStop`被准确限定为由真实字段和main条件推导，未冒称GUI Quit实测。
- 早期spawn失败自检只启动不存在的Node child路径，不启动PG；保原ENOENT Error对象、code、cause，添加not-submitted。最终 `owner.cjs` 早期error和close分支按本次sent状态分类，完整真实PG非零路径不受guard影响。

报告准确保留：原late-ready fixture错误、原owner生产归属问题仍在、完整协议schema/资源边界和全部取消交错未覆盖、PermissionError审计限制、无GUI/完整G0/Phase B资格。报告中的13个物理case与实际正常/协议/死亡/foreign/候选分类一致。无需增加真实PG故障矩阵来归档本次有限证据。
