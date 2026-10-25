# 用户批准的本地 alpha.7 构建

用户明确授权 `0.1.0-alpha.6 → 0.1.0-alpha.7`，只本地构建/安装，不tag或发布。本分支完成版本同步与制品构建；没有启动installer、关闭正式app、启停/重置正式PG或改用户数据。root已另行正常Ctrl+Q退出alpha.6并用已安装旧exe确认maintenance0，再接手alpha.7安装。

## 精确版本与材料

- Python project/init/uv.lock仅本包版本为 `0.1.0a7`；desktop/frontend package及lock根、packages空根为 `0.1.0-alpha.7`。改写时解析比较JS对象，仅允许这些版本字段差异，没有全局替换依赖、旧测试fixture或历史文档的alpha.6。
- `check_versions`输出alpha.7；`uv lock --check --offline --python .venv/Scripts/python.exe`通过，未更新依赖。9项原release/payload聚焦测试通过，未加skip/改断言。
- frontend `build:desktop`退出0。复用原private build-stage/native resources，源码快照2026-10-25 09:49:35–42 UTC，wheel安装09:49:45，payload完成09:50:43。包含冻结的ROOT四文件，runner.py最终源mtime09:27:57。
- 最终 installer：`C:/python_workspace/pulsara-agent/desktop/release/Pulsara-0.1.0-alpha.7-windows-x64.exe`，537005857 bytes。Windows electron-builder实际退出0，NSIS/blockmap与unpacked生成，未发布。
- Linux新目标：`/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7`；runtime/distribution版本均0.1.0a7，CPython3.12.15。构建本身没有部署该目录。
- 包内tar1595781120 bytes，manifest同版alpha.7，真实全内容SHA-256 `9d433825e7ec99d65138d4937c454d16ef52ddc4a5755fe34e707749ab44255a` 与source manifest一致；PE17920 bytes、`--version`真实exit0。

## 安装前来源/资源核验

`wheel-audit.json` 读的是private resources里实际安装的新wheel，不是Windows源码import：runner.py与该快照源文本相同；含`PreparedRootInputHeadStale`与while顶部`intent.cause` / `current_task.cancelling()`取消后禁止reprepare护栏。未把source观察冒充真实provider capacity通过。

该wheel内实际JS片段为 `platform===\`darwin\`?\`⌘\`:\`Ctrl\``，不是仅源码改过而static仍旧。包内Python/metadata版本均a7、迁移resource_bytes canonical CRLF0；原`prepare_resources.validate_resources`在制tar前真实运行，PG17.9、pg_search0.26.1、vector0.8.1、Python3.12.15/Linuxx86_64继续原pins。

许可所需PostgreSQL/pgvector/Python/AGPL说明、对应ParadeDB源tar及build说明均存在；沿原recipe的上游license/source固定材料pin复核通过，没有另造代码/证据SHA。原`smoke_resources.py`对复制搬移资源真实执行：独立临时PG/vector/pg_search搜索结果、Python import与headless Chromium均通过。它只拥有自己的临时cluster并在finally停止清理，不读正式settings或连接用户DB。未扩生产pgcrypto依赖，也没有重复未变的全部测试。

初次附加static审计使用双引号精确字面pattern，Vite实际产物使用backtick，故assert fragments失败；仅修审计识别实际quote形式后通过，产品static未变，失败摘要另存。不是缺Ctrl产物，更没有改断言去接受缺标签。

## 保存与执行边界

alpha.6 installer537003394 bytes及blockmap在release原路径保留，并另存本目录`previous-alpha6/`；原alpha.6材料manifest/结果也保留。旧失败的phase-b/provider原证据没有覆盖。没有再复制1.6GB旧tar；原alpha.6安装/runtime在本分支不动。

为避免写坏alpha.6历史记录，既有`build-final-payload.py`增加仅构建记录路径的`--record`参数，读取复制的原stage以复用同一private cache，将新记录/暂扣manifest放本目录；`record-windows-package.py --output`同样写本目录。默认旧路径语义保留。构建cache是可变工作目录，不是历史源码或执行authority；实际前后快照/材料按各报告记录。安装、更新保data、卸载/重装及新installed provider回归归root/其余agent后续验证。本报告不宣称新capacity或Windows激活已通过。
