# Pulsara Frozen Legacy REPL Retention Contract

> 状态：FROZEN LEGACY SURFACE；maintenance-only，无计划删除日期
>
> Requirement namespace：`REPL-RETENTION-*`
>
> 唯一owner：现有prompt_toolkit顺序式REPL的保留范围、禁止扩张和隔离规则

## 1. 定位

`pulsara host repl`保留为开发、诊断和低成本人工入口，但不是未来Web/Desktop client的
reference implementation，也不承担renderer-neutral Protocol v3的完整产品交互面。

它可以继续提供：

- open/resume/continue conversation；
- 顺序提交真实ROOT user message；
- 当前turn stop；
- 显式close；
- session列表与切换。

它不承诺：

- 并发读取输入和provider/tool live stream；
- 完整Plan、permission、MCP elicitation、TODO、subagent或artifact UI；
- live observation重放、GAP可视化或client-side cache；
- 与未来Web/Desktop界面feature parity。

## 2. 保留策略

### REPL-RETENTION-POLICY-001 显式入口

唯一入口是：

~~~text
pulsara host repl
~~~

Runtime或未来client失败时不得自动fallback到REPL。错误必须明确报告，不能让用户误以为
交互能力无损降级。

### REPL-RETENTION-POLICY-002 Maintenance-only

允许修改：

- security与canonical data-integrity修复；
- Host/session API演进所必需的机械适配；
- prompt_toolkit兼容与bounded resource修复；
- 现有命令的测试补强。

禁止扩张：

- 新建REPL专用authority、projection、receipt或recovery；
- 复制Protocol v3 command vocabulary；
- 为追求未来client parity增加复杂interactive state machine；
- 让REPL决定permission、Plan、MCP、tool或conversation真值。

### REPL-RETENTION-POLICY-003 No automatic discovery surface

REPL不承担静态capability inspector。Skill、MCP与tool exposure由正常Host/model-call planning
决定；不存在`host inspect` fallback或REPL内部第二套catalog renderer。

## 3. Authority边界

REPL只是`KernelHostCore`的调用者：

- session/turn/transcript由canonical repository拥有；
- provider/tool/terminal/subagent physical execution由各自process-local owner拥有；
- permission与Plan由typed Host policy拥有；
- REPL history只保存本地输入便利性，不是conversation truth；
- Ctrl-D只detach；`:close`才请求关闭conversation；
- REPL退出、渲染失败或history写入失败不得否定已接受的canonical operation。

## 4. 测试门

必须持续证明：

1. `host repl`只构造`KernelHostCore`；
2. open/resume/continue、stop、detach与close语义不漂移；
3. REPL没有独立数据库write port、Protocol reducer或client cache；
4. 非TTY、EOF、KeyboardInterrupt与prompt history failure均有bounded行为；
5. 新的Web/Desktop能力不会被反向要求复制到REPL；
6. repository中不存在bundled UI implementation或launcher。

## 5. 最终裁决

REPL保留的价值是低成本开发/诊断，而不是产品UI。未来client可以消费Protocol v3并提供更完整
体验，但二者不会形成fallback、feature-parity或shared client-state义务。
