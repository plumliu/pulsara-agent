# Mac startup smoke：visibility前提失败与测试修正

2026-10-25。root在本次nativeMac统一回归中发现 `test:startup` exit1，委派本专题定位。**最终判断是smoke的动画场景缺renderer可见性前提；没有证明StartupCover生产挂起或需要生产修复。** 已按root明确批准，仅修改 `desktop/scripts/startup-cover-smoke.cjs`，没有改StartupCover、startup.js、main或产品时限。

## 原始失败与直接证据

1. `../root/startup.log`保留首个真实nativeElectron失败：前四生命周期通过，最终 `closing: fade never started`，原2秒within触发。
2. 私人 `startup-cover-diagnostic.cjs`复制原smoke并定位当前仓库dist/assets，profile独立；保留全部原断言、原2秒门，只在同次executeJavaScript观察document.hidden、visibilitychange、动画实际computed opacity/playState及native finish/destroy。首轮 `startup-diagnostic.log` exit0，visible时真实animate开始，opacity1/running，minimize样本还观测到finished opacity0。
3. 同一诊断三个有限重复样本全部exit1，原错误为minimize、replace、minimize的fade never started；每次紧前的原renderer任务都观察到hidden=true/visibility=hidden。原材料为 `startup-diagnostic-repeat-{1,2,3}.log`、`startup-diagnostic-repeats.json`。这些是fixture观察样本，未改产品寿命/重试cap。
4. 为区分“无动画”和“IPC真的挂起”，私人 `startup-cover-diagnostic-branch.cjs`仅给原finish的executeJavaScript同字符串注入console（没有额外等待/改变分支），并观察原ready Promise与contents销毁。`startup-diagnostic-branch.log` exit1顺序为：

   ```text
   PROBE page state hidden=true/visibility=hidden
   PROBE finish document.hidden=true
   PROBE observed cover destroyed
   PROBE ready resolved minimize
   Error: minimize: fade never started
   ```

   原gate已放行、原cover已物理销毁，之后只是测试仍在等一个按生产合同不应发生的fade。

## 机制与历史归属

`StartupCover.finish()`的native hidden/minimized和renderer `if(document.hidden)return`快路径在a57ce3b1父提交已存在；其目的就是不依赖后台动画完成来解锁交互。新代码没有移除此合同。原smoke只调用showInactive，并假设后续一定能动画，不能用native isVisible替代Page Visibility事实。

[Electron官方BrowserWindow说明](https://www.electronjs.org/docs/latest/api/browser-window#page-visibility)明确macOS页面状态还反映完全被其他窗口覆盖，showInactive只展示、不聚焦，因而不保证动画前提。文档用于解释已捕获的实际观察，不代替本机证据；本轮未将失败笼统归因为CPU负载或“flaky”。a57加入更多show/retire场景可能改变时序，但现有证据只支持测试前提缺失，没有证明产品生命周期缺陷。

## 唯一测试修正

- 原 `fadeStarted` helper显式show/focus本次fixture，然后使用原2秒within，等待同renderer真实visibilitychange到!document.hidden，才安装原animate观察器。
- 原fade-start、concurrent通知只一次fade、IPC释放inert、contents destroyed、替换不移除新cover、finalclosing等全部断言保留；未加sleep、未延长timeout、未禁backgroundThrottling、未伪造document.hidden或改变生产animation。
- 新增独立hidden快路径：hide本次host并等真实document.hidden；给本次测试页animate安装误调用即throw的探针，经原preload→IPC调用finish，验证交互gate解除和原contents destroyed/移除。此项明确不算动画覆盖。
- foreground fixture经root通知授权序列执行，show/focus只用于本次测试窗口；执行结束立即交回CUA，未操作candidate窗口、用户原应用或OS sleep/wake。

## 验证与保留限制

首个修后harness在新增hidden观察器中直接返回了赋值后的函数，Electron executeJavaScript structured-clone拒绝并exit1：`startup-fixed-first-harness.log`保留。将测试表达式包为IIFE返回undefined后，未更改断言/产品逻辑。

最终用现有dist，直接 `mise exec -- desktop/node_modules/.bin/electron desktop/scripts/startup-cover-smoke.cjs`，不npm/tsc写共享dist，**完整三次连续exit0**：`startup-fixed.log`、`startup-fixed-repeat-{1,2}.log`与`startup-fixed-repeats.json`。每次四生命周期、独立hidden快路径、最后closing/geometry/trust/error-controls均PASS。

这次修的是当前nativeMac fixture资格，不能把此结果表述成已重新执行Windows smoke；Windows原场景断言仍全部保留。root可将startup-fixed.log作为本次Mac结果，原startup.log仍是本轮实际首次失败。没有启动业务main/PG、读写settings、注册CLI或修改/Applications。私有诊断profile仅保留ignored output供审阅，不作为产品状态。
