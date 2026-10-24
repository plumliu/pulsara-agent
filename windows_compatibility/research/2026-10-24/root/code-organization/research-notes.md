# 代码组织修订工作记录

日期：2026-10-24。本轮用户明确要求把此前对话中的代码组织、平台适配、共用逻辑与具体接入顺序补进根Windows spec，并再次交叉审阅后冻结。沿用此前三位gpt-6.1-sol/high agent，个人本轮workspace各在code-organization/。

## 源码核对与汇总决定

- 重新读取terminal manager/environment、local_source_binding/exclusive_publish、desktop_runtime、DesktopApplication、Protocol-v3、MCP sdk_facade、hooks executor、screenshot owner、main/runtime/postgres、launcher、资源builder与mise。
- local_source_binding目前返回int fd；多个consumer和held DTO继续直接os.*，Windows HANDLE不能直接替代CRT fd。根§3.4明确typed owned bindings及全部直接/间接caller，包括source_import与file_preview_http；read_at、borrow/lock和cleanup不能退化。
- root采用相邻领域leaf及扁平process_scope小机制，不新增全局platform registry/service container。Registry/output/monitor、MCP/hook业务guard/frame/deadline、PG/main/runtime状态机保留共用。
- 三条内部IPC用途分开；Node不能调用Python对象。PG安全文件接缝由G0检查维护依赖后选唯一机制，必要helper只操作绑定文件，不能拥有PG/reset/credential authority；实际use期间binding lifetime是必须证明的条件，返回路径并非证明。
- 资源locator不读home/settings/启动进程；两个语言的只读布局由同一recipe和relocation smoke核对。不新增可编辑manifest或fingerprint registry。
- S0–S7映射原W0–W7并细化前后依赖/每步退出条件；共享接口和消费者完整替换，删除旧路径。原成本区间不重复加一份重构预算。
- root额外检查mise source声明只有旧prepare_resources入口；补S6叶子recipe/NSIS/CLI构建输入与真实test_build_cache失效验证义务，仍由原mise/build owners负责。

## 检查与限制

validation.json检查15主章节、3.1–3.5/11.1、Markdown fences/空白、18个明确现有consumer文件存在。这里只是文档与源码位置检查；本轮没有新增生产模块、运行G0、重跑产品测试、读写保存设置/数据库。先前24/49 macOS测试不计为本轮新证据。

根候选523行经terminal/backend全文批准；补唯一cache连带段后成为525行，已向所有审阅者通知，以525行为最终语义候选。冻结前必须读取并取得三位最终明确批准，之后仅改状态/记录。
