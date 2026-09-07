# Pulsara Content Revision 行锚定编辑与 Create-Only Write Hard-Cut 实施规格

> 状态：**PROPOSED — 尚未实施、尚未激活**
>
> 记录日期：2026-09-08
>
> 目标：以一次无双轨 hard-cut，将现有 `old_text/new_text` 模糊替换改为“精确内容 revision + 原始行号操作”，并将 `write_file` 收敛为 create-only、atomic no-clobber 语义。
>
> 调研输入：Pulsara 当前 filesystem builtin、权限与 tool settlement 路径；`can1357/oh-my-pi` 的 `crates/pi-edit`、`crates/pi-natives`、coding-agent edit/read/write 集成及 hashline 测试。
>
> 上位约束：根目录 `AGENTS.md`、`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 及当前有效的 provider prefix、permission、long-horizon、tool settlement 规格。本文不得被解释为新增 provider-input rebase 边界、durable replay owner、权限 token 或通用 fingerprint 框架。

---

## 0. 执行结论

Pulsara 应借鉴 OMP 的**快照锚定编辑模型**，但不复制 OMP 的完整 hashline 实现。

本 hard-cut 的最终产品形态是：

~~~text
read_file
  -> exact raw-byte content_revision
  -> numbered visible lines
  -> process-local seen ranges

edit_file
  -> one canonical path
  -> required base_revision
  -> deterministic operations against original line numbers
  -> exact stale rejection
  -> in-memory stage
  -> atomic replace + post-write verification
  -> diff + new revision + changed windows

write_file
  -> create nonexistent text file only
  -> atomic no-clobber publication
  -> existing target always rejected
~~~

核心决策：

1. `content_revision` 是 read/edit 之间真实的内容完整性边界，不是 DTO fingerprint，也不是权限或 owner authenticity 证明。
2. `edit_file` 不再接收 `old_text`、`new_text`、`replace_all`，不再执行 exact/fuzzy text search。
3. 所有行操作都以 `base_revision` 对应的**编辑前原始文件**为坐标系；同一调用内后续 operation 不读取前序 operation 产生的新行号。
4. revision 不匹配时严格拒绝，不自动 remap，不尝试 whitespace recovery，不写任何字节。
5. 第一版保留一份 process-local、path-owned 当前 observation；不保存多版本历史，不增加 durable row/event/job/relation/subject/guard。
6. 第一版启用 seen-line guard；模型只能修改当前 revision 下由 Pulsara 实际展示过的行或相邻 gap。
7. `write_file` 只创建新文件。修改、清空或整文件替换已有文件统一进入 `edit_file`，并要求 `base_revision`。
8. 本变更只能在新 cold epoch 或显式 adopted compaction successor 中暴露新 tool schema；不得热改 existing epoch 的 provider tools。

---

## 1. 当前问题与变更理由

### 1.1 当前 `edit_file` 的正确性缺口

当前生产实现以：

~~~text
path + old_text + new_text + replace_all
~~~

执行定点替换，并依次尝试：

1. exact；
2. trimmed boundary；
3. line-trimmed；
4. whitespace-normalized。

该路径已有“默认拒绝多重匹配、同目录临时文件、`fsync`、`os.replace`、diff、写后验证”等优点，但仍存在以下产品缺口：

- `edit_file` 没有验证文件是否仍是模型读取过的版本；
- 外部修改后，只要旧文本仍存在，工具仍可能编辑模型未观察过的新内容；
- whitespace-normalized 匹配可能跨越格式变化并命中非预期区域；
- 模型必须重复提交旧原文，增加 provider token 和转义失败面；
- `errors="replace"` 会将非法 UTF-8 字节替换为 U+FFFD，随后写回可能造成不可逆数据损失；
- process-local path lock 只约束 Pulsara 当前进程，不能证明外部编辑器或 formatter 没有改写文件。

### 1.2 当前 `write_file` 的危险语义

当前 `write_file` 可以创建或完整覆盖文件。对于已有文件，它只在写入前计算一个 mtime-based stale warning，随后仍执行覆盖，并在成功结果中警告。

这意味着：

~~~text
stale detected
  -> overwrite still happens
  -> warning arrives after data loss boundary
~~~

warning 不能替代写入前置条件。已有文件的全量替换必须具备与局部修改相同的 exact revision 验证。

### 1.3 为什么选择行锚定，而不是加强 fuzzy text replacement

继续为 `old_text` 增加更多上下文、相似度阈值或 recovery 策略，不能关闭 stale-write 问题，只会扩大启发式状态空间。

行锚定编辑把契约改为：

~~~text
模型声明：我要修改我刚看到的版本 R 的第 N 到 M 行。
工具验证：当前文件仍精确等于 R，并且 N 到 M 确实被展示过。
工具执行：按唯一、确定的坐标应用操作。
~~~

这使“模型针对哪一版内容”“模型看过哪些区域”“工具修改哪些行”成为三个可独立验证的事实。

---

## 2. 从 OMP 借鉴什么、不借鉴什么

### 2.1 借鉴的产品原则

借鉴以下原则：

- read 产生文件版本锚点；
- edit 必须携带该锚点；
- 行号属于编辑前快照；
- edit 前重新读取并 stage，不把 preview 或旧内存视为当前磁盘真相；
- 模型只能锚定实际展示过的行；
- 多个 operation 在写入前统一验证；
- 错误结果直接告诉模型应重新读取哪个范围；
- 成功 edit 返回新 revision 和修改后上下文，允许下一次编辑继续 grounding。

### 2.2 明确不照搬的 OMP 机制

第一版不得实现：

- 4 位十六进制、16-bit 文件 TAG；
- 对尾随空白不敏感或对换行规范化后的弱 revision；
- 以 digest 为 key 的多版本 snapshot registry；
- stale snapshot 历史恢复和行号自动 remap；
- AST `N*` block anchor；
- `CUT`、匿名/命名 clipboard、跨文件 paste；
- 单次多文件 patch；
- move/delete 文件语义；
- 自动缩进、closer、boundary echo 或 syntax repair；
- 语法损坏后仍自动落盘；
- 为历史快照引入路径数、版本数、总内存等任意 lifetime cap；
- OMP raw patch grammar、Lark parser 或 streaming preview pump。

这些能力可在未来由独立产品规格证明必要性后加入；不得借本 hard-cut 顺带实现。

---

## 3. Authority 与 ownership

### 3.1 Filesystem owner

当前 canonical filesystem 仍是真实文件系统。Pulsara 不建立镜像文件、sidecar hash 文件、编辑日志或 durable snapshot store。

`content_revision` 不写入：

- 文件旁的 `.hash`/`.revision` 文件；
- PostgreSQL；
- canonical event；
- memory；
- provider prefix source；
- generic fingerprint registry。

### 3.2 Process-local observation owner

filesystem builtin runtime 增加一个 typed、process-local observation owner。它按 canonical path 保存至多一个**当前 observation slot**：

~~~text
FileObservationSlot
  canonical_path
  content_revision
  seen_line_intervals
~~~

约束：

- slot 由成功的 `read_file` 或成功的 `edit_file` 在 owner lock 下签发；
- 同一路径读到不同 revision 时，原 slot 被直接替换，不保留历史链；
- 同一路径、同 revision 的多次 paginated read 合并 seen intervals；
- edit 不通过 `revision -> snapshot` 全局查询对象，而是 exact join `canonical_path + slot revision + caller base_revision`；
- slot 不保存权限决定，不授予写权限；
- 进程退出、runtime 重建或 observation 丢失后，调用方必须重新读取；
- 不增加 crash recovery、receipt、checkpoint、repair graph 或 replay reducer。

### 3.3 Permission owner

现有 `PolicyPermissionGate` 继续是唯一写权限 owner。

`content_revision`：

- 不是 capability；
- 不是 permission token；
- 不是 workspace trust；
- 不是 host-local authorization；
- 不能跳过 symlink/path containment 检查；
- 不能把 read 权限提升为 write 权限。

每次 `edit_file`/`write_file` 仍按当前 descriptor、permission preset 和 frozen invocation context 完成授权。

---

## 4. `content_revision` 契约

### 4.1 定义

`content_revision` 是当前 canonical path 上**完整原始文件字节**的 SHA-256 content digest，provider-visible 编码为：

~~~text
sha256:<64 lowercase hex characters>
~~~

摘要覆盖 exact bytes，因此以下任一变化都必须改变 revision：

- UTF-8 BOM；
- CRLF/LF/CR；
- 尾随空格或 tab；
- 最终换行；
- 任意正文内容；
- 空文件与只含换行的文件之间的差异。

不得像 OMP 的短 TAG 那样先 trim 行尾或规范化换行后再计算。

### 4.2 为什么允许保留该 digest

该 digest 满足 fingerprint subtraction 规格的真实边界条件：

1. producer 是成功读取 exact bytes 的 `read_file`/post-edit verifier；
2. verifier 是后续独立执行并重新读取 exact bytes 的 `edit_file`；
3. edit 调用不携带完整旧文件，verifier不能直接逐字段比较旧 payload；
4. mismatch 有明确产品失败：`CONTENT_REVISION_MISMATCH`，并且零文件写入；
5. digest 覆盖的 bytes 在该次 read observation 内已冻结；
6. digest 不替代 permission、owner identity、path containment 或 physical liveness。

### 4.3 唯一 builder 与禁止扩散

实现中只允许 filesystem owner 提供一个私有 `_content_revision(raw_bytes)` builder。

不得新增：

- `FileRevision` 通用基类；
- revision registry；
- request/candidate/plan/result revision 副本；
- parent aggregate fingerprint；
- “revision verified” receipt；
- revision compatibility fallback；
- 用 revision 派生 permission 或 tool call identity。

tool result 可以把 revision 作为 public body 返回；canonical tool settlement 按既有 body 处理，不需要在 outer DTO metadata 再复制一次。

---

## 5. 文本文件资格与规范化

### 5.1 Exact bytes 与 logical lines 分离

两个层次必须分开：

~~~text
exact raw bytes
  -> content_revision / stale verification

strict decoded text + logical lines
  -> model-visible read / line operations
~~~

revision 对原始 bytes 计算；行操作在通过资格检查后的文本视图中执行。

### 5.2 文本资格

`read_file`、`edit_file` 和 create-only `write_file` 的文本路径必须：

- 不是 blocked device；
- 不是目录或非 regular file；
- 不命中现有 binary-extension policy；
- 是严格 UTF-8，可选 UTF-8 BOM；
- 不包含 NUL。

现有 `errors="replace"` 必须从可写路径移除。非法 UTF-8 返回 typed application error，不得生成 lossy 文本后写回。

### 5.3 换行与 BOM

局部行操作必须：

- 保留未修改行的原始正文；
- 保留已有 BOM；
- 保留文件最终换行状态；
- 对统一 LF 或统一 CRLF 文件，新增 logical lines 使用同一 newline style；
- 对无换行的单行文件，新增分隔符默认使用 LF；
- 第一版对 mixed line-ending 文件拒绝局部行操作，返回 `UNSUPPORTED_MIXED_LINE_ENDINGS`；
- mixed line-ending 文件仍可通过携带 exact `base_revision` 的 `replace_file` 明确完整替换。

不得为了编辑一个局部范围而隐式规范化整个文件的换行。

---

## 6. `read_file` 新契约

### 6.1 输入保持

`read_file` 继续接受当前：

- `path`；
- `offset`；
- `limit`。

现有已验证的单次读取行数、输出字符数和分页边界保持不变；本 hard-cut 不增加 total-history 或 worker-lifetime cap。

### 6.2 输出扩展

成功结果必须新增：

~~~json
{
  "status": "ok",
  "path": "src/app.py",
  "content_revision": "sha256:...",
  "offset": 120,
  "limit": 40,
  "total_lines": 900,
  "truncated": true,
  "content": "120|def load_user(...):\n121|    ..."
}
~~~

行号继续为 1-based。`content` 的展示格式可以保持 `N|text`，revision 必须作为独立 closed JSON field 返回；不得要求模型从自然语言中解析 revision。

### 6.3 Observation 更新

只有成功、未发生 lossy decode 的 read 才更新 observation：

~~~text
same canonical path + same revision
  -> union visible [offset, end]

same canonical path + different revision
  -> replace slot
  -> seen ranges = current visible interval only
~~~

折叠行、truncated column、未来可能加入的 elision 不算完整 seen line。若一行未被完整展示，该行不得加入 seen ranges。

第一版 `search_files` 不签发 edit observation。search 命中只能帮助定位；模型必须对目标范围调用 `read_file` 后才能编辑。

---

## 7. `edit_file` 新 schema

### 7.1 顶层 schema

hard-cut 后，`edit_file` 的唯一输入形态为：

~~~json
{
  "path": "src/app.py",
  "base_revision": "sha256:...",
  "operations": []
}
~~~

顶层：

- `path`：required non-empty string；
- `base_revision`：required、严格匹配 `sha256:<64 lowercase hex>`；
- `operations`：required non-empty array；
- `additionalProperties: false`。

以下旧字段必须同时从 descriptor、schema、executor、tests、prompt/docs 中删除：

- `old_text`；
- `new_text`；
- `replace_all`。

不得保留 old/new schema union，不得自动把旧调用转换为行操作。

### 7.2 Operation vocabulary

第一版只允许五种 operation：

#### `replace_lines`

~~~json
{
  "kind": "replace_lines",
  "start_line": 120,
  "end_line": 123,
  "lines": ["def load_user(...):", "    ..."]
}
~~~

- range 为 1-based inclusive；
- `start_line <= end_line`；
- `lines` 为 non-empty string array；
- 每项是不含 CR/LF/NUL 的单个 logical line；
- 如需删除，必须使用 `delete_lines`，不得以空 `lines` 暗示删除。

#### `delete_lines`

~~~json
{
  "kind": "delete_lines",
  "start_line": 120,
  "end_line": 123
}
~~~

- range 为 1-based inclusive；
- 删除最后剩余行可以产生空文件；
- 不产生 clipboard 或可复用寄存器。

#### `insert_before`

~~~json
{
  "kind": "insert_before",
  "line": 120,
  "lines": ["# explanation"]
}
~~~

- `line` 必须是存在的原始行；
- 插入 gap 由该行的可见性授权；
- `lines` 必须 non-empty。

#### `insert_after`

~~~json
{
  "kind": "insert_after",
  "line": 140,
  "lines": ["return result"]
}
~~~

- `line` 必须是存在的原始行；
- 插入 gap 由该行的可见性授权；
- 在最后一行之后插入是合法 EOF append；
- `lines` 必须 non-empty。

#### `replace_file`

~~~json
{
  "kind": "replace_file",
  "content": "complete desired UTF-8 text"
}
~~~

- 用于清空或完整替换已有文件；
- `content` 可为空字符串；
- `replace_file` 必须是 `operations` 中唯一一项；
- 仍必须验证 `base_revision`；
- 不要求整份旧文件全部 seen，因为其产品语义是用户/模型明确选择的完整替换；
- permission 层仍将其视为 destructive filesystem write；
- 输入 content 是完整目标文本，不是 patch；
- 若原文件有 UTF-8 BOM，默认保留；若原文件没有 BOM，不自动添加；
- content 中的 LF 按原文件统一 newline style 转换；mixed 文件由调用方给出的完整 content 终结旧混合布局。

### 7.3 Closed item schema

每种 operation item 必须 `additionalProperties: false`。实现应使用 JSON Schema `oneOf` 或仓库既有等价 closed-union 构造，使不同 kind 的字段不能混用。

不得接受：

- 未知 kind；
- 字符串行号；
- 0、负数或越界行号；
- body 中嵌入多行的 `lines[]` item；
- 空 operation list；
- no-op replacement；
- 重复 operation；
- 重叠 touched range；
- 一个 gap 被多个 operation 同时占用；
- `replace_file` 与任何其他 operation 混用。

---

## 8. 行坐标与 deterministic apply

### 8.1 原始坐标系

所有 operation 的 line/gap 坐标都针对 `base_revision` 对应的原始文件。

例如：

~~~text
replace_lines 10..12
insert_after 20
~~~

第二个 operation 仍指原始第 20 行，不受第一个 replacement 新增长度影响。

### 8.2 Range 与 gap 冲突

stage 阶段必须将每个 operation 降为 closed touched ranges/gaps，并拒绝歧义：

- 两个 replacement/delete range 不得相交；
- insert gap 不得落在被删除或替换 range 的内部；
- 同一 gap 只允许一个 insert；
- `insert_before N` 与 `insert_after N-1` 指向同一物理 gap，必须判为冲突；
- 完全重复也拒绝，不静默合并。

### 8.3 Materialization

通过全部验证后，按原始 offset 从后向前 materialize，或使用等价的单次线性 builder。实现不得逐项修改后再用变化后的 line number 解释后续 operation。

stage 产物必须至少包含：

- intended exact output bytes；
- unified diff；
- changed line windows；
- new content revision；
- no-op 判定。

这些值是同一次 tool call 的 process-local exact values，不增加 fingerprint 字段或 registry。

---

## 9. Seen-line guard

### 9.1 基本规则

普通行 operation 只有在以下 exact join 成功时才能 stage：

~~~text
caller path
  == observation canonical path

caller base_revision
  == observation content_revision
  == digest(current raw bytes)

every touched line / anchor line
  is contained in observation seen intervals
~~~

具体规则：

- `replace_lines`/`delete_lines`：inclusive range 中每一行都必须 seen；
- `insert_before N`/`insert_after N`：anchor line N 必须 seen；
- `replace_file`：不要求全文件 seen，但要求 exact revision；
- 只看见折叠范围的起止行不等于看见中间行；
- `search_files` 命中不等于 seen；
- caller 不能自行声明 `seen=true` 或传入 seen ranges。

### 9.2 Observation 缺失

如果 revision 与当前文件相同，但 process-local observation slot 因重启或 runtime 重建而缺失，返回：

~~~text
READ_OBSERVATION_REQUIRED
~~~

提示必须包含建议的最小 read range，不得自动从 canonical transcript、provider messages 或 durable events 重建 observation。

### 9.3 成功 edit 后的 observation

成功 edit 写后重新读取 exact bytes，并签发新 slot：

- revision 更新为 new revision；
- seen ranges 只包含成功结果中完整返回的 post-edit changed windows；
- 旧 revision 的 seen ranges 被丢弃；
- 未返回给模型的行不自动继承 seen 状态。

这样下一次 edit 可以直接锚定刚刚返回的修改区域，但不能借一次旧 read 永久获得整文件编辑资格。

---

## 10. `edit_file` 执行与 transaction boundary

### 10.1 执行顺序

一次调用的 required 顺序：

~~~text
1. schema preflight
2. canonical path resolution
3. existing permission authorization
4. acquire process-local canonical-path lock
5. verify regular text file eligibility
6. read exact raw bytes
7. compute current content_revision
8. exact-join observation + base_revision + current revision
9. validate seen ranges and all operations
10. stage complete intended bytes and diff in memory
11. reject no-op
12. immediately before publication, best-effort revalidate path identity/stat/content
13. same-directory temp write + flush + fsync
14. atomic os.replace publication
15. read exact persisted bytes and verify equality
16. publish success ToolExecutionResult
17. install new process-local observation slot
~~~

任何 schema、revision、seen、range、overlap、text eligibility 或 stage 错误都发生在第一个文件字节写入之前。

### 10.2 单文件边界

第一版一个 call 只修改一个 path，因此不引入多文件 transaction、rollback journal 或 partial-prefix settlement。

parent directory creation 不属于 `edit_file`；被编辑文件必须已存在。

### 10.3 Atomic replace 保持

继续复用当前同目录 temp + `fsync` + mode preservation + `os.replace` 的原子发布路径，但必须补齐：

- strict bytes/text handling；
- publication 前再次 best-effort stale check；
- exact post-write byte verification；
- binary/device/non-regular guard；
- mixed line-ending policy；
- directory `fsync` 在支持的平台上由唯一 filesystem adapter 负责。

不得复制另一套 writer framework。若标准库或现有依赖提供满足要求的 primitive，应优先复用。

---

## 11. `write_file` create-only 契约

### 11.1 新 schema

`write_file` 保留简单输入：

~~~json
{
  "path": "src/new_file.py",
  "content": "complete new UTF-8 content"
}
~~~

但语义 hard-cut 为：

- target 必须不存在；
- target 已存在时始终返回 `FILE_ALREADY_EXISTS`；
- 不接受 `overwrite` boolean；
- 不接受 optional `expected_revision`；
- 不提供 old/new compatibility mode；
- 修改已有文件必须使用 `edit_file`；
- 空 content 可以创建零长度文件；
- missing parent directories 继续按当前产品语义创建。

### 11.2 Atomic no-clobber

实现必须使用“同目录完整 staging + 原子 no-replace publication”的平台/依赖 primitive，使并发出现的目标不能被覆盖。

禁止：

~~~text
if not path.exists():
    os.replace(temp, path)  # check 与 replace 之间可被抢占并覆盖
~~~

实现前必须检查 Python 标准库、当前支持平台和已有依赖的 no-replace 能力。若某支持平台无法提供原子 no-clobber publication：

- 不得回退为可能覆盖的 `os.replace`；
- 不得静默降低保证；
- 返回 typed resource/platform boundary outcome；
- 在实施规格修订中明确支持矩阵和 dependency owner。

本 hard-cut 不授权自行复制一套跨平台 rename/link 状态机。

### 11.3 Parent directory 副作用

创建 missing parent directories 可能在文件 publication 失败时留下空目录。该行为沿用当前产品语义，但必须在测试中明确；不得为此引入 rollback graph 或 durable repair job。

---

## 12. 结果与错误契约

### 12.1 `edit_file` 成功结果

成功 public body 至少为：

~~~json
{
  "status": "ok",
  "path": "src/app.py",
  "base_revision": "sha256:...",
  "content_revision": "sha256:...",
  "operations_applied": 2,
  "diff": "--- a/src/app.py\n+++ b/src/app.py\n...",
  "changed_windows": [
    {
      "offset": 116,
      "end_line": 146,
      "content": "116|...\n117|..."
    }
  ],
  "files_modified": ["src/app.py"]
}
~~~

`changed_windows` 必须：

- 覆盖每个实际修改后的区域及少量确定的上下文；
- 使用新文件行号；
- 只包含完整展示的行；
- 作为新 revision 的 seen ranges；
- 服从既有单次 tool result/artifact projection bounds；
- 超出 projection budget 时可以缩小窗口，但不得谎报被省略行为已 seen。

outer canonical ToolResult、artifact lowering、provider projection、observation timing 和 settlement 继续复用现有统一路径。

### 12.2 `write_file` 成功结果

成功结果至少包含：

~~~json
{
  "status": "ok",
  "path": "src/new_file.py",
  "content_revision": "sha256:...",
  "bytes_written": 1234,
  "files_modified": ["src/new_file.py"]
}
~~~

创建成功不自动把整份新文件标为 seen。若结果没有返回正文，后续 edit 前仍需 `read_file`；不得把“模型提交过 content”自动等同于“provider 当前上下文完整保留该 content”。

### 12.3 Pre-write application errors

以下错误必须在零文件写入状态下返回 closed application error：

- `FILE_NOT_FOUND`；
- `FILE_ALREADY_EXISTS`；
- `NOT_A_REGULAR_FILE`；
- `UNSUPPORTED_TEXT_ENCODING`；
- `UNSUPPORTED_BINARY_FILE`；
- `UNSUPPORTED_MIXED_LINE_ENDINGS`；
- `INVALID_CONTENT_REVISION`；
- `CONTENT_REVISION_MISMATCH`；
- `READ_OBSERVATION_REQUIRED`；
- `UNSEEN_LINE_RANGE`；
- `INVALID_LINE_RANGE`；
- `OVERLAPPING_OPERATIONS`；
- `INVALID_OPERATION_COMBINATION`；
- `NO_OP`；
- `ATOMIC_NO_CLOBBER_UNAVAILABLE`。

每个错误 body 必须包含：

- stable error code；
- path；
- 人类可读 message；
- 模型可执行的 `_hint`；
- mismatch 时返回当前 revision；
- unseen/range error 时返回建议读取范围。

### 12.4 Effect-start 后失败

temp file 创建、write、`fsync`、publication、post-write verify 等 physical failure 继续服从现有 `bounded_write` effect 与 Round 5 long-horizon settlement：

- 不自动重试；
- 不伪造成功；
- 不用 revision mismatch 掩盖 unknown physical outcome；
- caller cancellation 后按现有 shield/drain 路径处理；
- 已发生 publication 但 post-write verify 失败时，明确报告“可能已经修改”，不声称 rollback。

---

## 13. 并发与可承诺边界

### 13.1 本进程内保证

在 canonical-path lock 下，Pulsara 保证：

- 同一 runtime 中对同一路径的 read observation 更新与 edit/write 串行；
- edit 只在当前 raw bytes revision 与 `base_revision` exact match 时 stage；
- pre-write application error 不落盘；
- 单文件 publication 使用原子 replace；
- 成功前执行 exact post-write verification。

### 13.2 不承诺跨进程 CAS

外部 IDE、formatter、git hook 或其他进程不受 process-local lock 约束。普通跨平台文件系统没有被本文证明可用的“比较 digest 后条件 replace”原语。

因此产品契约必须准确表述为：

> 在 Pulsara 持有的进程内 canonical-path lock 下，编辑只应用于已验证的 exact 文件版本；对不合作的外部并发写入执行尽力复验，但不承诺跨进程线性化 CAS。

不得把 content revision 宣称为：

- 跨进程锁；
- lease；
- filesystem generation；
- owner authenticity；
- publication acknowledgement；
- 无竞争证明。

如未来产品需要跨进程强 CAS，必须单独证明文件系统/平台原语、authority owner、失败路径和支持矩阵，并修订规格。

---

## 14. Provider input continuity 与 activation

### 14.1 Tool schema 是 provider prefix 的一部分

本 hard-cut 会改变：

- `edit_file` description；
- `edit_file` JSON schema；
- `write_file` description；
- read/edit/write provider-visible result contract；
- 可能的 base system tool guidance。

同一 installed epoch 中 `SYSTEM` 和 provider `tools` 必须 byte-identical，messages 只能 append suffix。

因此：

- existing epoch 不得热换新 schema；
- runtime discovery/reload 不得重写已安装 tools；
- 新契约只在 new cold epoch 或 explicitly adopted compaction successor 冻结；
- open attempt 继续消费它启动时冻结的 exact descriptor/executor binding；
- 不新增第三种 rebase boundary。

### 14.2 一次 hard-cut

实现和 activation 不得出现：

- old edit 与 line edit 双 descriptor；
- `edit_file_v2` 临时名称；
- old/new schema `oneOf`；
- feature flag；
- provider-specific fallback；
- old text fuzzy fallback；
- optional revision 过渡期；
- write 既可 overwrite 又可 create-only 的运行时模式；
- historical snapshot compatibility adapter。

开发可以按依赖顺序提交工作树修改，但激活终局只有：

~~~text
old single path removed
  -> new single path installed at approved root boundary
~~~

---

## 15. 实现边界与代码落点

实施时应优先修改和复用现有 owner，不建立平行 framework。

### 15.1 Filesystem builtin

主要落点：

- `src/pulsara_agent/tools/builtins/filesystem.py`
  - strict byte read/text eligibility；
  - unique content revision builder；
  - typed current observation slot；
  - line operation validation/staging/materialization；
  - exact atomic writer/post-write verifier；
  - create-only no-clobber writer。
- `src/pulsara_agent/tools/builtins/workspace.py`
  - 只在现有 path resolution 无法满足 exact canonical join 时做最小调整。

必须删除：

- `_fuzzy_replace`；
- `_match_exact`；
- `_match_trimmed_boundary`；
- `_match_line_trimmed`；
- `_match_whitespace_normalized`；
- `_find_literal_spans`；
- `_replace_spans`；
- edit 对 lossy `read_text(errors="replace")` 的依赖；
- write 的 post-hoc `_stale_warning` 产品路径。

如果 `_stale_warning` 无其他 consumer，应同次删除；不得留作“备用兼容”。

### 15.2 Builtin catalog

主要落点：

- `src/pulsara_agent/capability/builtin_catalog.py`
  - hard-cut edit/write descriptor 与 schema；
  - 保持 `filesystem_write` permission category；
  - 保持 `is_destructive=True`；
  - 保持现有 long-horizon `SYNTHESIS_MUTATION` 与 `bounded_write` 分类，除非独立规格证明必须变化。

### 15.3 Runtime 与 settlement

默认不修改：

- permission gate；
- attempt acceptance；
- tool effect classification；
- cancellation/drain；
- canonical ToolResult；
- artifact processor；
- provider lowering；
- durable schema/event/relation/job。

只有现有 extension point 无法携带 required result body 时才允许最小修改，并必须先记录真实 dependency/API gap。

### 15.4 Hook aliases

现有 Hook matcher 可继续把 external `apply_patch`/`Edit` 名称映射到 Pulsara `edit_file`，但 Hook stdin 中的 `tool_input` 必须是新的 Pulsara 原生 schema。

alias 不是协议转换器。不得接受 Codex unified diff 或 OMP raw hashline body 后偷偷转译为 operations。

---

## 16. 测试要求

### 16.1 Content revision

必须覆盖：

- 相同 bytes 得到相同 revision；
- 正文单字符变化；
- LF/CRLF 差异；
- BOM 差异；
- 尾随空白差异；
- final newline 差异；
- 空文件；
- revision 格式严格校验；
- path 与 revision exact join，不以 digest 全局回查文件。

### 16.2 Read observation

必须覆盖：

- 首次 read 安装 slot；
- 同 revision 分页 read 合并 seen intervals；
- 新 revision read 替换旧 slot；
- truncated/未完整展示行不进入 seen；
- search 命中不授予 edit eligibility；
- runtime observation 丢失后要求重新读取；
- 不创建 durable state。

### 16.3 Line operations

每种 operation 必须覆盖：

- 首行、中间行、末行；
- 单行与多行 replacement；
- 多个非重叠 operation；
- 坐标均基于原始文件；
- reverse materialization；
- blank logical lines；
- Unicode；
- 空文件边界；
- 保留 final newline；
- LF 与 CRLF；
- mixed newline 拒绝；
- invalid range；
- overlap；
- duplicate gap；
- no-op；
- `replace_file` 单独使用；
- `replace_file` 清空文件。

### 16.4 Stale 与 seen guard

必须覆盖：

- read 后外部修改导致 revision mismatch，零写入；
- 外部只改尾随空格也 mismatch；
- 外部只改换行也 mismatch；
- 内容恢复为原 exact bytes 时 revision 再次相同；
- 编辑未见行拒绝；
- replacement range 只有部分 seen 时拒绝；
- insert anchor seen 时允许；
- 成功 edit 只把返回窗口加入新 revision seen ranges；
- revision mismatch 不触发 fuzzy recovery。

### 16.5 Encoding 与 filesystem

必须覆盖：

- invalid UTF-8 拒绝且原 bytes 不变；
- NUL 拒绝；
- binary extension 拒绝；
- blocked device 拒绝；
- symlink containment 与 host-local permission 保持；
- mode preservation；
- temp 文件异常清理；
- post-write exact verification；
- physical failure 不自动重试；
- process-local competing edits 串行。

### 16.6 Create-only write

必须覆盖：

- 不存在目标成功创建；
- 已存在普通文件拒绝且 bytes 不变；
- 已存在 symlink/目录/特殊文件拒绝；
- 竞争创建只有一个成功且不覆盖；
- zero-length create；
- Unicode/strict UTF-8；
- missing parents；
- publication failure 不覆盖并清理 temp；
- unsupported atomic no-clobber 平台返回 typed boundary outcome。

### 16.7 Schema、continuity 与 real-provider dogfood

必须覆盖：

- descriptor/schema/executor exact binding；
- old fields 被拒绝；
- closed operation union；
- permission preset matrix 不变；
- Hook alias 只改 matcher candidate，不做协议转换；
- existing epoch `SYSTEM/tools` byte-identical；
- new cold epoch 安装新 schema；
- compaction successor 使用 approved root rebuild path；
- canonical ToolResult 与 provider projection 正确显示 revision、diff、changed windows；
- 真实 provider 完成 read -> edit -> edit 连续链；
- 真实 provider 遇到 stale mismatch 后重新 read 并成功；
- 真实 provider 创建新文件后按要求 read 再 edit；
- trace 只排除 `PULSARA_API_KEY` exact value，保留实际 tool arguments/results 和失败上下文。

不得通过放宽 assertion、skip/xfail、dual schema 或 fuzzy fallback 获得绿色结果。

---

## 17. Resource 与 long-horizon 约束

本 hard-cut 不增加：

- total files edited cap；
- total tool calls cap；
- total turns cap；
- worker lifetime cap；
- model-call count cap；
- retry count cap；
- arbitrary snapshot history cap。

继续使用现有单次 read/tool result/physical worker bounds。新增 staging 的资源边界必须按单次操作的真实内存或 provider 限制定义 typed outcome，不能演化为隐藏的会话 lifetime 限制。

process-local observation 每个 canonical path 只保留当前 slot，不保留版本历史。该结构是当前执行观察，不是 durability 或 replay machinery。

大文件若超出既有 read 能力，不能通过提供无 revision 的 overwrite fallback 绕过；应分页 read、使用现有 artifact 路径，或返回明确的 per-operation resource boundary。

---

## 18. 明确非目标

本 hard-cut 不负责：

- AST-aware code editing；
- formatter/linter 自动运行；
- 编译或语法正确性事务；
- multi-file atomic transaction；
- rename/move/delete tool；
- notebook cell editing；
- binary patch；
- non-UTF-8 encoding conversion；
- external-process file locking；
- Git index/worktree transaction；
- edit preview UI；
- patch history、undo 或 rollback；
- 远程 filesystem；
- 从旧 canonical transcript 恢复 seen provenance；
- OMP protocol compatibility。

若未来需要其中任何一项，必须先定义 dependency owner、产品语义、permission boundary、失败路径和测试，再独立修订。

---

## 19. 实施阶段

### Phase A：规格与 pure core

1. 冻结本文决策；
2. 定义 closed schemas 与 error vocabulary；
3. 实现 strict text decoding、exact revision、logical-line parser；
4. 实现 pure operation validation/staging/materialization；
5. 完成 pure unit tests。

### Phase B：owner 集成

1. 将 typed current observation 接入 `read_file`；
2. 将 edit exact join、seen guard 接入 filesystem runtime；
3. 复用现有 permission 和 path resolution；
4. 接入 atomic replace 与 post-write verification；
5. 实现 create-only atomic no-clobber writer；
6. 完成 focused integration tests。

### Phase C：hard-cut

1. 同次删除 fuzzy edit 与 stale-warning overwrite 路径；
2. 替换 builtin catalog descriptors/schemas；
3. 更新 prompt、README、Hook tests 和 active specs；
4. 确认没有 old/new 双轨、feature flag、compatibility adapter；
5. 运行完整回归。

### Phase D：activation

1. clean-v0 baseline；
2. new cold epoch/compaction successor continuity 证明；
3. real-provider dogfood；
4. architecture subtraction oracle；
5. 记录 exact commands、结果、工作树语义与 Git reference；
6. 只有全部 acceptance criteria 满足后，将本文状态改为 `ACTIVATED`。

---

## 20. Activation acceptance criteria

只有同时满足以下条件，才可宣告 hard-cut 完成：

1. `edit_file` provider schema 中不存在 `old_text/new_text/replace_all`。
2. 生产代码中不存在 fuzzy edit matcher 或兼容 fallback。
3. `edit_file` 的每次普通行修改都要求 exact `base_revision` 和 seen-line authorization。
4. stale revision 在任何写入前失败，并返回当前 revision 与可执行 re-read hint。
5. revision 覆盖 exact raw bytes，BOM/newline/trailing whitespace/final newline 变化都可检测。
6. revision 只存在于 filesystem content boundary，不扩散为 generic fingerprint topology。
7. process-local observation 不 durable、不保留历史、不通过 digest 全局查对象。
8. `write_file` 对任何已有 target 都 no-clobber 拒绝。
9. 已有文件的完整替换只能通过带 revision 的 `replace_file` operation。
10. invalid UTF-8/binary/device/mixed-newline 边界按本文 typed 语义处理，不发生 lossy rewrite。
11. permission、path containment、host-local scope 和 effect settlement 没有旁路。
12. 单文件 stage 在 publication 前完成，validation failure 零写入。
13. 成功结果返回 new revision、diff 和 bounded changed windows，并正确建立新 seen ranges。
14. physical unknown 不自动重试、不伪造成 application error 或成功。
15. 同一 epoch 的 SYSTEM/tools byte-identical，schema 变化只进入批准的 root rebuild boundary。
16. 没有新增 durable event、relation、job、subject、append guard、receipt、checkpoint 或 repair owner。
17. focused、full、continuity、wheel/isolated launcher 与 real-provider dogfood 全部通过。
18. 文档、clean-v0 baseline 和生产代码只描述新单一路径。

---

## 21. 最终产品不变量

激活后的长期不变量为：

~~~text
No observed exact revision
  -> no edit of an existing file

Revision mismatch
  -> no write

Unseen ordinary line anchor
  -> no write

Existing path
  -> write_file never overwrites

Existing-file full replacement
  -> edit_file + exact base_revision

content_revision
  -> exact bytes evidence only
  -> never permission, owner authenticity, lease, or cross-process CAS

same installed epoch
  -> SYSTEM/tools byte-identical
  -> messages append-only suffix
~~~

该终局保留 OMP hashline 最有价值的 grounding 与 stale-write 防护，同时符合 Pulsara 的小型 durability、真实边界 digest、单一权限 owner、单一 tool settlement 和 provider prefix continuity 原则。
