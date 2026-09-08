# Conversation Fork 验收记录

日期：2026-09-08。实现契约：[有效上下文复制规格](../../PULSARA_CONVERSATION_FORK_EFFECTIVE_CONTEXT_COPY_SPEC.zh.md)。
采用用户批准的 `retained_historical_requests` 有序 typed 列表；不保留旧单值协议。

## 真实 provider

```sh
.venv/bin/python tools/run_conversation_fork_dogfood.py \
  --output /tmp/pulsara-conversation-fork-dogfood-final.json
```

[最终报告](real-provider-final.json)：PASS，`openai/gpt-5.6-luna`，8 次真实调用。
报告保留 actual final-wire input、模型返回值及各分支检查，实际 API key 值已排除。

- 无压缩分叉：子会话回答 `FORK_ALPHA_812`，输入没有 `FUTURE_B_913`。
- 压缩后分叉：child 与 source 对应 snapshot 的 summary 完全相同；子会话回答
  `COMPACTED_ANCHOR_714`，输入没有后续 `FUTURE_D_615` 或已压缩的重复 filler。
- 父会话继续并压缩后，再从旧回复分叉：子会话仍回答 `FORK_ALPHA_812`，不混入三个未来标记。
- 每次 child 第一次发送前，检查无 copied turn/tool attempt/event 等执行 occurrence；
  后续通过普通 Host turn 和 provider 路径继续。

[保留的首次失败报告](real-provider-initial-failed.json)不是成功证据。最初夹具要求压缩后模型
精确复述早于摘要的 token；实际父摘要已遗漏该 token，父会话自身也回答 UNKNOWN，不能据此
断言 Fork 丢失了有效上下文。夹具改为核对父子摘要 exact equality、保留窗口中的标记和禁止的
未来内容，未放宽“复制分叉点有效上下文”的产品断言。失败输入与回复原样保留供复查。

每轮使用新建的本机 disposable clean-v0 database，并在 finally 中删除该确切测试数据库；
未重置用户配置的开发数据库。

## 浏览器与打包

使用 Playwright CLI，在本地真实 Web application + canonical PostgreSQL fixture 上验证：
最终回复入口、压缩基底分隔线、成功导航、重复分叉、Copy → Tab → Enter 键盘路径、
父草稿保留和 child 空输入框，以及 1200×813 / 390×844 布局。浏览器 fixture 不冒充真实模型测试；
真实模型证据由上面的独立 dogfood 提供。

- [桌面截图](../../output/playwright/fork-desktop.png)
- [窄屏截图](../../output/playwright/fork-mobile.png)

Copy 与 Fork 靠左相邻，间隔 6px；时间靠右。视觉验收发现并修正了原先 `space-between`
把 Fork 推到中间的问题；同时修复了单一 composer draft 跨 session 泄入 child 的问题。
浏览器日志有 `/favicon.ico` 404；切回已载入父会话时还观察到旧 connection 的 observe 409，
页面随后正常连接并恢复父草稿，新 child 也正常加载。没有将这些记录描述为 console-zero-error。

独立安装验证：

```sh
uv lock --check
uv build --wheel --out-dir /tmp/pulsara-fork-wheel-20260908
uv venv /tmp/pulsara-fork-wheel-env-20260908
uv pip install --python /tmp/pulsara-fork-wheel-env-20260908/bin/python \
  /tmp/pulsara-fork-wheel-20260908/pulsara_agent-0.1.0-py3-none-any.whl
cd /private/tmp
PULSARA_HOME=/private/tmp/pulsara-fork-wheel-home-20260908 \
  /private/tmp/pulsara-fork-wheel-env-20260908/bin/pulsara app --no-open --port 0
```

wheel 可从非源码目录 import；包含三张新表的 baseline、新 protobuf 字段、新 carrier parser
和最新静态资源。隔离 settings 没有 PostgreSQL 配置；launcher `/healthz` 返回
`{"status":"ready"}`，首页加载 `index-CIdlTS0R.js` 与 `index-Cx4ca-jO.css`，Ctrl-C 正常停止。
macOS `/tmp` 为 symlink，settings no-follow owner 要求使用真实 `/private/tmp`；未为夹具
增加 symlink fallback。CLI 参数是 `--no-open`，不是 `--no-browser`。

前端验证：`npm test` 为 128 passed；`npx tsc --noEmit`、修改文件 ESLint、
`npm run build:local` 均通过。完整 `npm run lint` 仍报告未改动的
`frontend/components/memory-view.tsx:46` 和 `:65` 两处 React hooks 规则错误；
没有禁用规则、增加 skip 或修改无关页面。构建继续显示既有大 chunk 提示。

## Python 与协议回归

```sh
CI=1 .venv/bin/python -m pytest -q
# 1681 passed, 19 warnings in 431.18s；没有跳过
CI=1 .venv/bin/python -m pytest -q tests/test_conversation_fork.py tests/test_round5b_long_horizon_context_compaction.py
# 92 passed
CI=1 .venv/bin/python -m pytest -q tests/test_round3_1_provider_input_prefix_continuity.py
# 10 passed
.venv/bin/python tools/generate_terminal_protocol_contract.py --check
# PASS
```

19 条 warnings 为已有 aiohttp `shutdown_timeout` 弃用提示。完整回归没有弱化断言、skip 或
xfail；64-call fixture 使用现有 writer lease renewal 修复裸 Runner 缺少 Host 续期的问题，
不改变测试要求的调用次数和长程可用性契约。
