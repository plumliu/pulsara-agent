# 消息启动延迟与送达展示

## 范围与真源

保留 `submit_prompt → enqueue_prompt → _prompt_delivery_loop → prepare_prospective_root_input → consume_prepared_prompt_head` 单一路径、FIFO、命令确认、取消、权限与输入预算校验。队首获得处理资格不等于已开始调用模型。

本次只优化物理准备顺序及展示，不改变记忆召回语义，不新增响应截止时间、持久事件、表、任务或 provider prefix 重建边界。

## 测量

`tests/dogfood/probe_prompt_startup.py` 通过现有 saved settings / `require_pulsara_home` 所属读取路径复用实际模型及 embedding 配置；配置只读，创建并清理独立本地数据库和临时 home。诊断包装器只记录各调用开始时刻与耗时，不装入生产服务，也不改变接受事实。测试 watchdog 只约束诊断。

基线样本：短输入准备约 142 ms；触发自动召回的输入准备约 1687 ms，其中召回 1523 ms、远程 embedding 1250 ms，首次中文分词约 250 ms。网络样本不能作为稳定 p95；以可控阻塞测试证明实际重叠，以真实请求核对最终回复。

## 实施边界

- `KernelMemoryToolPort` 继续拥有自动检索、远程调用取消与关闭；`KernelSessionIO` 继续拥有中文分词物理线程。分词和 embedding 独立执行，在原有检索入口合流。显式搜索、召回数量、可见域、排序、精确回读和失败降级不变。
- `ProviderDispatch` 在已冻结 prospective ROOT family 上让初次输入编译与记忆 source 读取重叠；最终 wire 测量和队列消费必须等待二者完成。结果仍只绑定该 family 一次，后续候选复用相同观察。退出、编译失败、取消时取消并等待未完成的召回，不残留后台任务。
- SDK 继续拥有 embedding 请求；本次不修改 SDK transport、凭据生命周期或远程超时。不得为了更快而删除召回、缩减结果或复用过期记忆。
- 前端使用已确认 receipt / canonical queue / active turn 显示：未确认时“正在发送”；确认待处理时用户消息“已送达”，助手侧“准备开始…”；已有运行时不显示第二个准备助手。未知、拒绝、取消仍保留各自状态；后台同步延迟不得冒充模型已收到请求。
- 不添加“等待模型响应”这一更细阶段：目前 UI 的 active turn 只能证明已接受运行，不能精确证明 provider 已发出请求。沿用助手“正在处理”。

## 验证

用阻塞栅栏证明分词期间远程请求已开始、输入编译期间召回已开始；验证取消/失败会排空关联任务、禁用召回不会请求远端、结果及降级保持不变。回归队列与 prospective admission / compaction 测试；前端覆盖发送、已送达准备、未知/失败、运行中排队以及 canonical 消息替换后的去重。实际浏览器检查布局，构建生产静态文件。

## 结果

2026-09-27 完成：

- 后端队列、记忆、输入编译和 compaction 回归 167 项通过；额外记忆降级/并行单测通过。阻塞测试证明网络请求不等待分词、召回不等待首次编译；编译失败和用户取消后没有遗留召回任务。
- 前端 Workbench / App / adapter 共 247 项通过，TypeScript、ESLint、相关 Python Ruff、生产构建通过。
- 真实模型对照使用相同提示、独立空数据库及临时 home，均得到 `PULSARA_OK`。准备总耗时分别为 1686.7 / 2333.6 ms，其中远端请求分别为 1249.9 / 2190.3 ms。扣除远端等待后的开销为 436.8 / 143.3 ms，减少约 294 ms。后一次网络更慢，因此不能将这两次样本声称为端到端加速或稳定 p95。
- 桌面与 390px 窄屏检查准备提示、TODO 和排队卡片布局；本地服务已优雅重启，4 个原有会话的 transcript 序号一致，页面 HTTP 200。
- 可复查证据在 `output/prompt-startup/`：`before.json`、`after.json`、`backend-tests.log`、`frontend-build.log` 与桌面/窄屏截图。此目录是本地诊断输出，不作为新的产品状态真源。

远程 embedding 延迟仍会影响首次响应。下一步若要对自动召回施加更短的产品响应预算，需要明确召回质量与等待时间取舍；本次没有隐式缩短既有 deadline 或跳过召回。
