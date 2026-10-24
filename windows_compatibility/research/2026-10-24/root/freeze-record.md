# Windows 兼容规格冻结记录

日期：2026-10-24。root工作区。交付规格：根`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`。

## 冻结决定

root已读三份研究报告、第一轮审阅与第二轮审阅。三位`gpt-6.1-sol / high` agent均重新通读修订后的同一388行候选，并明确给出“可冻结，无剩余阻断”；第二轮记录分别位于`../terminal/review-round2.md`、`../backend/review-round2.md`、`../desktop/review-round2.md`。

root同意冻结。此后根文档仅更新文首状态和审阅记录，未修改已获认可的产品语义；agent已明确纯状态变化无需重审。没有生成文档/代码SHA或额外证据authority。

## 第一轮问题关闭

- 内部Protocol-v3单独适配为Windows loopback stream，保留原capability/frame/attachment，独占listener与close/connect生命期；不同于named-pipe diagnostics与宿主JSONL。
- 采用标准PS7 -Command退出码，不自造native退出数值保留政策；不全局NonInteractive；Job范围空查询和ConPTY EOF真实不确定性明确。
- 私有PG auth文件的bootstrap、DDL/文件非原子、失败保留、无自动轮换与确认reset顺序；纠正physical/role/schema owners；client require_auth。
- Windows source executable不适用值、native与cmd/显式解释器分开；MCP/hook独立env和credential最后临界区。
- 安装/卸载采用NSIS已有customCheckAppRunning扩展替换默认kill；不杀已知/未知进程绕过收尾。
- UTF-8无BOM宿主wire与真实匿名pipe适配分别列G0；custom OAuth URI继续只focus。
- WSL完整替代路线、interop、两端路径/home、生命周期与成本明确，不自动实现。

## root最后检查

- 根正文15个章节、Markdown fence平衡、本地报告链接存在、无TODO/TBD/尾随空白。
- 19个关键参考仓库源码路径实存；完整Git checkout身份见repository-checkouts.json。
- 预算8个包合计88–154人日；25%–35%不确定性后约110–208人日；上游长期fork不吸收在该区间。
- 本轮仅根实施规格成为新增未跟踪交付，output为仓库既有忽略目录；没有生产实现、设置、数据库变更。
- terminal基线24 passed、backend基线49 passed仅是macOS合同回归，不是Windows通过；没有Windows实机/构建/provider验收。

## 冻结边界

原生Windows11 25H2 x64、本地NTFS、PS7.6 LTS目标已冻结；G0必须先实证原生PG扩展、ConPTY/Job/EOF、NTFS安全原语、runtime/IPC才可全面实施。G0失败须修订产品合同并重新审阅，不自动WSL、外置DB、删搜索、无锁/无Job降级。规格冻结不代表G0通过、Windows支持已完成或可以发布。
