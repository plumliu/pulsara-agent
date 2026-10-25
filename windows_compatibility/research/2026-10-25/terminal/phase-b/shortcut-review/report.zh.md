# 快捷键 Ctrl/AltGraph/IME 窄修

2026-10-25。仅审原 `PulsaraApp` 全局快捷键和 `SessionSearchDialog` 关闭处理器；没有扩大到 UI 重构、键盘布局管理或用户配置。正式安装版 alpha7 尚未包含本修复，未改版本、未构建/替换已装静态资源。

## 原语义与具体反例

原会话搜索规格约定 ⌘K/Ctrl+K 开关搜索，保留独立 ⌘N/新会话；搜索输入保留 IME 合成。原全局处理器只检查 `(metaKey || ctrlKey)` 和逻辑 `key`，不检查 Alt 或组合期；搜索弹窗已有组合期/229保护，但 Ctrl+Alt+K 仍满足其关闭条件。

新增原组件测试的初次8项结果为 **3通过、5失败**：

- 普通 Ctrl+K/N 正常，两项通过。
- Ctrl+Alt+K/N 均被 `preventDefault` 并触发应用快捷键；两项失败。
- 带 `isComposing=true` 的 Ctrl+K/N 同样被取消默认行为；两项失败。这是原组件对有效 DOM 输入状态的实测，不声称用户已经物理复现了该 IME 组合。
- 搜索输入中的 Ctrl+Alt+K 被当作关闭快捷键；一项失败。
- 映射 AltGraph 字符正例（逻辑 `key='ń'`、物理 `code='KeyN'`、AltGraph状态true）原本通过，不能将其写成原 AltGr 字符输入必然失败。

`before.log` 保留初次原实现失败，未改断言以获得通过。测试用 jsdom 的 DOM 事件和原 React 组件，不是 Windows 物理键盘事件抓取；它验证应用是否吞掉事件/打开或关闭对话框，不把 jsdom 的默认字符插入行为当实机文本输入证明。

## Chromium 与实际 AltGr 边界

本仓库锁定 Electron44.9.0；[该版本官方 DEPS](https://raw.githubusercontent.com/electron/electron/v44.9.0/DEPS) 对应 Chromium152.0.7977.167。[同版本 Windows key map 源码](https://raw.githubusercontent.com/chromium/chromium/152.0.7977.167/ui/events/keycodes/platform_key_map_win.cc) 的 `DomKeyFromKeyboardCodeImpl` 使用当前布局生成逻辑 key；找到 Ctrl+Alt 对应的可打印映射时，把修饰状态规范化为 AltGraph。没有可打印映射时会尝试较少修饰键的逻辑 key。因此不能只按 `code=KeyN` 或右Alt键帽推断 `key='n', ctrlKey=true`，也不能把所有 AltGr 字符都当作 Ctrl+Alt+字母错误。

[UI Events 的 key 指引](https://w3c.github.io/uievents/#keys-guidelines) 同样区分布局/修饰后的逻辑 key 与物理 code。这里保留真实可打印 glyph 的正例，只将已复现的普通 Ctrl+Alt chord 错识别和组合期事件吞键判为缺陷；没有声称捕获了当前用户的 AltGr 硬件误触。当前用户 HKL0804及“有右Alt”本身不构成实际 AltGr 布局资格。

## 最小生产变化

- `frontend/app/pulsara-app.tsx`：原处理器先忽略 `isComposing`/229，再让原 Ctrl/Meta K/N 条件排除 Alt 与 AltGraph。
- `frontend/components/session-search-dialog.tsx`：保留既有 composition/Escape/焦点行为，只给 Ctrl/Meta K 关闭条件加入同样的 Alt/AltGraph排除。

生产 diff 共5增/3减。没有修改快捷键平台选择、Shift行为、工作区/会话owner、composer提交策略或浏览器快捷键。没有新增全局键盘状态/快捷键抽象。

## 验证与交付

- 新13项覆盖普通 Ctrl/Meta K/N、Ctrl+Alt、组合期、229、真实 glyph 语义正例及搜索关闭；加原搜索9项，共22/22通过。
- 随后原主体 App、搜索、composer draft、prompt suggestions及新增快捷键5文件共 **254/254** 通过，原完整断言保留。
- `tsc --noEmit` exit0，两生产文件 ESLint exit0，`git diff --check`通过。首次把测试文件顺带传给前端ESLint时，该文件因在config base之外被忽略，有1条warning；不冒称该测试文件已被ESLint检查。测试本身由Vitest执行。
- 没有创建DB、scope、后台进程或修改正式GUI/设置。首次日志目录放错位置只创建了空 `frontend/output/...`，已核空后逐个精确删除；原构造式清理命令被执行策略拒绝后，使用经核验的单一literal非递归删除完成，没有泛删。

日志和命令均在本目录。此修复是当前源码结果，不是当前alpha7已装版资格；需root获得新候选版本授权后，沿原前端静态构建→wheel/payload→NSIS→正常维护更新部署。实际 AltGr 布局和新候选实机快捷键仍应按真实环境分别观察，不能拿上述合成 DOM 测试或用户原alpha7 IME反馈冒充。
