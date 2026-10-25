# 正式安装包：真实 provider 的文件、memory、history 验收

本轮复用当前安装的 Python/package、用户保存的唯一 DeepSeek 连接，完成4个原 dogfood runner及1条经 root 明确授权的最小异常 Host 门，共88次真实 provider 调用。没有改生产代码、保存配置、正式GUI或PG生命周期。原始 provider SYSTEM/tools/messages、normalized response blocks、tool arguments/results 全量保留在 `*-result.json`；归档用标准 gzip 无损压缩，不改内容、不宽泛脱敏。只移除实际已配置 credential，并做 contains 后置检查。

## 边界

- Python/package：`/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/python/bin/python3`，生产包先导入并核安装根；只将 tests 与 output-only harness 拷入自己的 ext4 私有目录，不把 Windows/旧 Linux clone 的 src 加入 import path。
- 设置沿 `LocalSettingsStore` / `require_pulsara_home` 加载 `/home/plumwsl/.pulsara` 的保存连接，再以原 `_ReadOnlySettingsStore` 作内存注入，覆盖本次 UUID DB，且把应用数据 home 导向短 `.pa-*`；不导出 key、source .env 或另造模型连接。模型为保存的 `deepseek-flash` / `openai_chat_completions`，连接ID见报告。
- PG复用 pg_admission_r3 已审 `run_terminal_wsl.py` 的工厂：每次先核 systemid7700523439330484737、PG17.9、真实 data/socket/角色同实例、无TCP，再创建自己 UUID DB、原 clean-v0 两次 migrate；finally只DROP该进程创建的名字，不启停/reset集群。原有loopback根dbname限制没有被用于正式Unixsocket，也没有为测试开TCP。
- 长中文空格 workspace保留；home单独用短真实 ext4 路径，符合现有Unix socket路径边界。工厂适配只改测试settings/DB/temp/recording接口，原runner的模型循环、工具、断言与生产owner不复制。
- 与 interop_tasks_r3 划分：它负责 subagent/capacity/context/compaction跟进/MCP/hook；本分支不重复这些矩阵。history-query内一次真实force-compaction及原process-local最小reclaim注入，仅为原canonical历史分页查询fixture，不能称默认自动阈值验收。

## 实际结果

| 场景 | 真实调用 | 原结果与核验 |
| --- | ---: | --- |
| artifact_export candidate，原9场景 | 46 | 全部执行结算，9项主要合同独立核验；全程完整生产tool面，每次wire tools均含artifact_export；没有跑会删tool的baseline |
| direct-memory原runner | 20 | passed；真实read_file→CURRENT_PROJECT FACT、独立DECISION与BASED_ON、GLOBAL事实SUPERSEDES、Q4返回4条visible memory；正常Core.shutdown后fresh Core.resume_session真实memory_get读取B楼；原删除预览/关系级联断言通过 |
| session-content-query原runner | 14 | completed；真实search_sessions/search_session_content/read_session_content，真实compaction后用next_cursor补读原始ARCHIVE-MIDDLE/END，原查询、分页、工具面断言全部保留通过 |
| turn-interruption-history原runner | 3 | completed；在真实pwd已经物理执行、逻辑publication被测试门保留时原request_stop_turn接受；原NOTICE、tool closure与下一条明确新任务保留，不能称撤销已发生的pwd |
| idle Host OS异常退出→fresh memory/history | 5 | 真实remember已接受；无terminal process、Linux children为空时持pidfd核PID/starttime，仅SIGKILL本次自有子Host；fresh原Core.resume_session后真实memory_get+search_session_content+read_session_content成功，FACT仍只有1条，未重复remember/terminal/写文件 |

`verified-results.json` 区分了执行结算与语义核验。原runner的progress/失败不会被新结果覆盖。常规compiled-input观察共85次安装、69次同epoch比较，精确核SYSTEM/tools冻结值和messages前缀；异常子Host另有1次原始wire前缀比较。cold Host恢复/真实compaction使用原owner边界，不伪造provider-native replay或恢复process-local任务。

artifact细节：small补读准确R74X；JSON三个合计8097/8285/7194；多源join217/641/809对应owner-8/3/6；日志与不完整JSON原文按实际体读取；7次真实artifact_export，完整来源与65536-byte retained snapshot均保原coverage。每个producer只运行1次（multi各1次），没有通过重跑补丢失输出；occupied.txt一直保留，模型选择其他路径。

必须保留的限定：snapshot主要“只在retained范围未发现、前部丢失无法推出全流不存在”的结论诚实，coverage/reason/字节范围有真实tool证据；模型最终副叙述把末条行号写成01397，而原可见tail包含01399，这一点不准确，原回复未改写。因此只判9项主要合同通过，不声称所有自然语言逐字无误。existing场景证明原文件保留且另选目标，没有刻意诱发no-clobber错误，不能把它写成collision拒绝负测。readonly场景模型未再调用写工具；不把它冒充已尝试写入后的拒绝实测。

异常Host是独立UUID DB里的Linux KernelHostCore进程失联，SIGKILL exit=-9，不是用户正式Electron GUI被杀，也不证明任何终端/Windows PE任务可以从崩溃后恢复。记忆/历史来自原canonical DB，过程没有清writer lease、伪造历史、重放旧remember或重启正式PG。原父进程仍持创建DB的内存归属，故可在fresh Core正常关闭后清理自己的DB。

## 正式GUI三turn只读交叉

读取单一授权会话 `session:r3d6okwuq2trl3ribxzct6rbuy`，没有open/resume其owner或改变writer。三个turn均COMPLETED，13条tool事实：12条SUCCESS，另1条是首turn读取不存在的AGENTS.md得到原FILE_NOT_FOUND；此原失败保留，之后smoke写/读成功。不把这条可恢复工具错误删掉或写成全部tool成功。

实际文件核对：smoke.txt31bytes/LF/0600；preview.html3018、preview.png17701、preview.pdf592；两个Windows实际选择/拖放导入副本均285bytes、uid1000、0400、父0500。root的PNG下载逐字节17701相同、HTML HTTPS脚本执行且require/bridge undefined、真正PDF单页可见等实际GUI元数据交叉引用在 `gui-three-turn-read-only.json`，这些是root操作，不声称由本分支截图/点击。

原模型带空格的Markdown链接仍逐字保存在第二turn消息中；尖括号格式修正是第三个新用户turn，未重写原历史。保存配置仅经原store读取；记录只含连接ID/model/wire及读前后inode/size/mtime一致，不打印配置文档或credential。初次smoke只读记录也保留。

## 原失败、清理与剩余范围

首次测试facade缺少PG适配器当前要求的PromptContent字段，在settings校验之后、DB/provider调用之前失败；原stderr/stdout/exit保留。补齐测试factory字段后未修改生产API或原runner断言，之后5组成功。没有为了通过而删tool、加skip、换模型或改保存配置。

`cleanup-audit.json` 已真实确认5个本次UUID DB均不存在、各自scratch及短home不存在、各WSL执行carrier退出0；异常子Host的精确PID/starttime已不活，private tests/harness目录已删除。正式PG postmaster启动时间仍是2026-10-25 16:43:52.967046+08:00、systemid不变、无TCP，授权正式GUI会话仍存在。本分支没有停止、reset或删除正式PG/settings/workspaces/imports。

没有逐个另存本轮所有临时Windows HANDLE库存；interop物理结算依赖已验正式scope/owner返回，结合原core shutdown、carrier退出与目录清理，不扩大成全系统零进程审计。原runner、其所用helper及实际PG adapter按清单保存，生产源码仍由Git/安装材料边界负责，不新增代码或证据SHA。

本分支文件、memory、history所选门完成；automatic history mixed-Decision路线需要额外保存retrieval配置，本轮未擅造配置，也不以显式查询替它宣称通过。整体Windows激活仍由root结合另一agent的长程/subagent结果、正式GUI正常Quit/重开及其余§13矩阵决定。本轮没有原生macOS实测声明、GUI操作、配置改写、重打包或提交。

归档后续边界：另一agent在独立capacity场景发现ROOT旧head无限reissue，正修4个生产文件并加确定性回归。上述88次是当前正式安装be6af材料的实测，不自动冒称修后全部provider场景通过。当前不重建/安装；待共同冻结后由root决定新候选与受影响subset复验。已有raw全部保留，不因新bug改写它们。
