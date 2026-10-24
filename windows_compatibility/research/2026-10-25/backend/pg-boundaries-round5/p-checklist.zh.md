# P资格逐项清单

这是第五轮停止时的证据清单，验收对象为output候选机制，不代表生产实现/Windows激活。

| 条目 | 本轮结论 | 证据 |
| --- | --- | --- |
| PG17.9/vector0.8.1/pg_search0.26.1实际加载 | 已验证 | functional两次pg-facts/deep verify |
| 项目/随包Python3.12.15实际来源 | 已记录 | provenance.json；全部case node22.23.3 |
| rg/Playwright/Chromium及完整资源smoke | 引用已有通过 | 首轮root desktop:verify，版本/资源无本轮改动 |
| ext4/private Unix socket/角色/禁TCP | 已验证 | 中文空格home、device2096、生产validate/SQL事实 |
| clean-v0 initialize及复验 | 已验证 | production prepare_database + deep verifier verified |
| BM25中文/英文/代码和原文UTF8位置 | 已验证 | 实际生产predicate/tokenizer，verify_results逐span核对 |
| vector运算 | 已验证 | 实际vector扩展，同向量distance0 |
| 复用/重开/搬移后检索 | 已验证 | 真实owner复用；moved PG实际exe和第二次完整查询 |
| 错socket/raw目录/版本拒绝保数据 | 已验证 | functional中的实际owner分支和sentinel/查询 |
| 精确reset只删PGDATA/保外部与锁inode | 已验证 | functional running→stop→remove |
| ready前错误/单次operation准入 | 已验证 | 畸形ready/early request，不执行PG子命令 |
| 分段UTF8/codepoint跨chunk | 已验证 | fragmented输入输出及真实leading-tail splits |
| malformed/duplicate/partial/schema reply | 已验证 | framing11，发帧后unknown且不新认领 |
| malformed/duplicate/partial request | 已验证 | 单完整JSONL+EOF候选，执行前拒绝 |
| initdb/stop/remove操作前EOF | 已验证 | 各自readyEOF，未写operation/未继续后续动作 |
| initdb提交后EOF/死亡 | 已验证 | 成功结算/真实bootstrap继承锁 |
| stop死亡/已停丢回复 | 已验证 | 有限pg_ctl仍持kernel；reset不继续remove |
| remove前/中/后失联 | 已验证 | 未删/部分删/全删三种事实，unknown无重放 |
| 方法间另一次start与remove重验 | 已验证 | 独立真实start后PG EX busy，拒删保运行实例 |
| 真PG非零/readiness原readback | 已验证 | 真实code1+ready与code1+status3两条 |
| active kernel.lock阻maintenance/PG不阻kernel | 锁边界已验证 | 实际acquire_kernel_lock持有者；非完整kernel应用 |
| 当前真实随包kernel/main shutdown/宿主失联顺序 | 待root组合 | 另一个agent的kernel6研究；本轮不代证 |
| 每流4MiB超限压力与所有故障组合 | 未追加穷举 | 有限exec既有边界复用；不声称全组合覆盖 |
| 生产WSL helper/typed错误接口/GUI安装Quit | 未实施/未验 | Phase B保持未开始；不由研究candidate替代 |

可局部接受本轮独立PG机制与数据功能证据；整P须合并root控制链证据后决定。其他G0门以及Windows整体激活没有由此获得批准。
