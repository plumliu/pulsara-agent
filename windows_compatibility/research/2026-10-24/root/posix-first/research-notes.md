# POSIX先行Phase A汇总与root复核

2026-10-24。本轮用户要求先抽接口、Linux/macOS回归，再加入Windows；当前只修实施规格，不执行生产实施/G0/provider/数据库操作。

## 依据与取舍

- 复用三位gpt-6.1-sol/high专题agent，在各自posix-first目录写提案；root先汇总完整候选，再要求三人全文交叉审阅，不以原代码组织版本批准覆盖新条款。
- 从有效AGENTS、当前生产代码及现行spec开始。重点复核local_source_binding/exclusive_publish、settings提交cut、Plugin metadata/PhysicalLifetimeAnchor、Preview read_at/borrow、terminal Registry/环境attempt/异步spawn与Node资源重复。
- A为可独立交付的POSIX hard cut；B仍须A验收及G0-D/T/F/R全过。纠正§1/3.1/3.3/3.5/4/11/11.1/12/13旧全局前置，不能只追加新章节保留冲突。
- A必选：typed安全文件全caller；scope+terminal physical+POSIXshell/probe；MCP/hooks/screenshot实际创建/cleanup；Node多caller资源locator。Unix IPC/PGleaf/recipe重组以实际需求决定，不搭空框架。
- 文件身份/权限不统一重写：完整typed POSIX观察，各产品保留原投影/canonical值；三种publish分开；known/unknown仍原owner。显式POSIX mode比未证实universal ACL诚实，B可根据G0改私有接口且删旧路径。
- 进程机制不接管Registry状态/输出/monitor/槽；root/range/drain/join区分；非终端不强制套terminal完成policy。补Popen后包装构造失败交接，沿用原隔离而非新registry。
- 实测门覆盖macOS arm64/native Ubuntu x64+arm64、wheel/资源/分层installedGUI/真实provider。容器不替native；资源smoke不替GUI/provider。按改动影响重建native资源，不能靠force隐藏cache输入缺失。
- baseline/candidate不共享candidate editable环境；artifact runner baseline故意删tool，改前后都须完整生产tool surface。saved DB helper限制须preflight，不为runner改保存设置或开TCP。

## 成本讨论

专题粗估A terminal/scope/shell/asynccaller净8–14人日；typed文件全caller/对应故障回归10–16；desktop资源/条件IPC/build/安装7–14。三者范围仍含A0待选条件片，不能机械相加变成新承诺，也不叠加到原88–154人日。正文保留唯一总预算与A/B归属，A0按实际闭包/条件片/native环境拆账；确有二次交付额外验收时单列原因。

## 文档检查

684行首轮候选：11.2.1–11.2.8齐全；代码fence配对；没有原“W0先行/同一工作包分支内”阻断A的句子；所有literal test_*.py引用均在仓库存在（test_build_cache在desktop/scripts）。结果见validation.json。这些是文档/位置校验，不是任何产品行为通过。

## 完成情况

三位agent对684行全文候选交叉审阅，均明确无剩余阻断；root独立认可后只更新冻结元数据为695行最终文本。三位又分别复核最终元数据并在review-round1.md追加明确批准。流程已完成，见freeze-record.md；没有用结束状态/无人反对替代批准。validation.json记录最终695行文档检查。
