# Backend prospective ROOT 修复交叉审阅

结论：源码及现有定向证据未见冻结阻断；真实随包 context 重跑仍是激活前置，不能以源码审阅代替。

- 已通读 provider_dispatch.py 的 activate_prospective_root_input、_root_dispatch_read_has_only_later_head、runner.py 的同 accepted turn 普通 prepare 分支；同时核对 CanonicalModelInputIdentity、FrozenCanonicalCompileSnapshot、FrozenDurableProviderReplayManifestCut、FrozenCanonicalProviderDispatchRead 的 constructor 验证。
- 分支先要求同 model binding，严格大于的全局 head，prepared ROOT scope。归一化仅是未安装候选的比较值，不写 installed cohort、SYSTEM、tools、messages 或 durable state。每层 constructor 重新验证既有 fingerprint；完整 DTO 相等覆盖 payload/closure/late outcomes/permission/context/replay。它没有增加 fingerprint authority 或重建已安装 prefix。
- 资源在 take_resources 后由本地唯一 cleanup 持有；handle、borrow、hook、planning 在调用前清掉 ownership 位，finally 保证前面退役失败仍尝试后续退役；外层 except 的再次 cleanup 不重复已执行释放。单个退役异常原样传播。
- runner 已清 pending prospective 后收到 None，只 continue 现 accepted turn 的 ordinary prepare，不重新 accept；model_call_count 尚未增加。现真实 PG 回归直接在 ROOT accept 后 admit child，断言恰好一个 ROOT intent、同 turn、active handle 清空、既有 SYSTEM/tools 与 messages 前缀保持。
- 新负例覆盖同/倒退 head、binding、identity、payload、closure、late outcome、permission、context、replay；四个单一退役失败均验证只一次 cleanup 和原异常身份。未改低层 schema/receipt/retry cap。
- 本审阅未重跑 backend 已跑套件；其 before 失败、candidate 通过和原 context 全程结果由 backend/root 原始日志作为最终证据。execution-envelope 裸 Core fixture 的 maintenance=None 与现生产 constructor 事实一致，无放宽产品断言。

补充跨专题复核：已读 desktop/report.md、desktop/cross-review.md 及 startup-cover-smoke.cjs 最终diff。macOS showInactive 可能使被遮挡renderer维持 hidden，原脚本无条件等待fade不满足生产“仅可见时fade”前提；修正显式show/focus并等待visibility，原2秒与销毁/IPC断言保留，另独立hidden快路径禁止动画并要求物理destroy/放行。没有修改生产、伪造visible或放宽time bounds。frontend1372/83文件、type/lint与native startup三次证据是已执行范围，不能据此代替物理键盘IME或最终新包GUI。未见跨专题源码阻断。
