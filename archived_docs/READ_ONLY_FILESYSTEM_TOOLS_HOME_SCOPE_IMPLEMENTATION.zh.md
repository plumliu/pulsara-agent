# Read-only filesystem tools 解除 workspace 限制实施文档

_Created: 2026-07-04_

## 0. 结论

本轮要解决的问题不是 skill discovery，而是 read-only filesystem tools 的边界语义。

推荐 v1：

- `read_file` / `search_files` 作为只读工具，允许读取本机任意普通文本路径，包括用户家目录下的 `~/.agents/skills` / `~/.pulsara/skills`。
- `write_file` / `edit_file` 继续严格限制在当前 workspace root 内。
- 默认相对路径语义不变：`path="."` 或相对路径仍从 workspace root 解析。
- 绝对路径和 `~` 路径允许越出 workspace，但仍保留设备文件、二进制文件、单次读取大小、分页、重复读取保护。
- `search_files` 放开路径但不放开 broad recursive scan：workspace 外搜索必须指向具体文件或具体子目录，拒绝 `~`、用户根、`/`、系统根、`/tmp` 根等宽根目录。
- v1 不做 sensitive text denylist；这意味着 read-only / plan mode 将明确获得读取本机任意普通文本文件的能力，包括可能包含 secret 的文本文件。该语义必须在权限契约和 inspect 输出中显式呈现。
- tool result metadata 明确标记路径范围，例如 `access_scope: "workspace" | "home" | "temp" | "external_absolute"`，方便 inspector / audit 解释。

这是一条产品语义 hard cut：Pulsara 的只读文件工具提供本机只读文件系统视图，不再假装只能看到 workspace。

---

## 1. 我们是怎么发现这个问题的

真实 REPL trajectory 中，用户问“今天成都天气怎么样”，模型尝试使用 firecrawl skill。它先想读取 skill 说明：

```text
read_file firecrawl-search/SKILL.md
```

但 skill 实际位于用户目录的 agent skill roots，例如：

```text
~/.agents/skills/firecrawl-search/SKILL.md
~/.pulsara/skills/...
```

当前 `read_file` 是 workspace-bound 工具，因此读取 workspace 外的 skill 文件失败。模型随后退回 terminal：

```text
echo $HOME
pwd && ls -la
which firecrawl && firecrawl --help
firecrawl search ...
```

这暴露出一个体验和语义错位：

1. capability catalog 告诉模型有某个 skill；
2. skill 的真实说明文件在用户家目录；
3. 最自然的读取工具 `read_file` 却不能读；
4. 模型只能绕道 terminal，而 terminal 反而是更宽、更不可控的 open-world 能力。

换句话说，当前限制并没有真正提升产品安全性，反而把“读说明书”逼成了“执行 shell 探测”。

---

## 2. 当前代码语义

### 2.1 `WorkspaceTool` 是所有 workspace 文件工具的路径边界

代码入口：

- `src/pulsara_agent/tools/builtins/workspace.py`
- `src/pulsara_agent/tools/builtins/filesystem.py`

当前 `WorkspaceTool._resolve_path()` 语义是：

```python
path = Path(raw_path).expanduser()
if not path.is_absolute():
    path = self.workspace_root / path
resolved = path.resolve()
if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
    raise ValueError(f"path escapes workspace root: {raw_path}")
```

这意味着：

- 相对路径只能在 workspace 内；
- 绝对路径即使是 `~/...`，只要不在 workspace root 下，也会被拒；
- `read_file` / `search_files` / `write_file` / `edit_file` 当前共享同一个边界。

### 2.2 read-only 与 workspace-bound 当前混在一起

`ReadFileTool` / `SearchFilesTool` 的声明是：

```python
is_read_only = True
```

但它们继承了和写工具一样的 `_resolve_path()`。因此当前代码把两件事混在了一起：

- 权限语义：是否只读；
- 路径语义：是否只能访问 workspace。

对写工具来说，这个混合是合理的。对只读工具来说，它导致 skill / docs / home-scoped resources 无法用正道读取。

### 2.3 现有安全保护仍然有效

`read_file` 已有这些保护：

- 阻止设备文件：
  - `/dev/zero`
  - `/dev/random`
  - `/dev/tty`
  - `/dev/stdin`
  - 等；
- 阻止常见二进制扩展；
- UTF-8 text read，errors replace；
- line pagination；
- `MAX_READ_LINES = 2_000`；
- `MAX_READ_CHARS = 100_000`；
- repeated read dedup / warning。

这些保护应继续保留，并成为解除 workspace 限制后的主要安全地板。

---

## 3. 设计选择

### 3.1 为什么不做额外 trusted roots

一个看似更精细的方案是：

```text
workspace root + ~/.agents/skills + ~/.pulsara/skills + 内置 skill roots
```

但这个方案会引入长期维护债务：

- skill root 来源会变多；
- Codex / Pulsara / 用户自定义路径需要同步；
- MCP / plugin / future resource roots 还会继续扩展；
- 工具层和 capability provider 层会产生第二套 root registry；
- 用户看到“明明是只读，为什么某些路径能读、某些不能读”的解释成本更高。

因此 v1 不采用 trusted roots。我们直接把只读文件工具定义为本机只读文件系统视图。

### 3.2 这是 read-only / plan mode 的真实权限变更

Pulsara 默认 permission preset 是 `bypass-permissions`，terminal 默认可用。模型已经可以通过 terminal 执行：

```bash
cat ~/.agents/skills/foo/SKILL.md
```

因此在默认 `bypass-permissions` 下，`read_file` 允许读取 home text file 并不是引入一种全新的 open-world 能力，而是把已有能力从 shell fallback 拉回结构化、可审计、可分页的 read tool。

但这个论证**不适用于** `read-only` / plan mode：

- 当前 `read-only` preset 是 `terminal=off`；
- `read_file` / `search_files` 在 `READ_ONLY_ALLOWED_TOOL_NAMES` 中；
- plan mode 通过切到 read-only 获得强制力。

所以一旦 `read_file` / `search_files` 放开 workspace 路径，`read-only` / plan mode 会新增读取任意 home/external 普通文本文件的能力。这不是实现细节，而是权限契约变更。

本设计选择接受这个变更，但要求实现 PR 同步修改：

- `contracts/PERMISSION_POLICY_CONTRACT.zh.md`：read-only 不再表示 “workspace-only file read”，而是 “host-local ordinary text read, no write, no terminal”。
- `EffectivePermissionPolicy.to_dict()`：当前 `"file_tools": "workspace_only"` 必须改成能表达新语义的值，例如 `"local_text_read_write_workspace"`，或拆成 `read_file_scope` / `write_file_scope`。
- CLI / inspector 输出和测试：inspect 不能继续暗示 file tools 全部 workspace-only。
- plan mode 文案：plan mode 是 read-only execution，但不是 “只能读取 workspace 文件”。

同时，写工具仍保持 workspace-bound，不允许借此写用户家目录。

### 3.3 read-only 不等于无隐私风险

需要明确：读取 home text files 可能暴露隐私，且纯文本 secret 不会被二进制/设备文件保护挡住。例如：

- `~/.ssh/id_rsa`
- `~/.aws/credentials`
- `~/.npmrc`
- `~/.pypirc`
- `~/.netrc`
- `.env`
- 各类 token / config / credential text files

这里有两个可选产品姿态：

1. 加 sensitive-path denylist / confirmation。
2. 明确接受 “本机任意普通文本可读”，并在权限文案中如实呈现。

本设计按用户决策采用第 2 个姿态：**v1 不做 sensitive-path denylist，也不对 read-only text read 追加确认**。理由不是“没有风险”，而是当前 Pulsara 定位是本地 host agent，且我们希望避免第二套 trusted root / sensitive root 维护债务。

因此这个改动必须绑定当前 Pulsara 的产品定位：

- 本地 agent；
- 用户显式启动；
- 默认具有 terminal 能力；
- 只读工具比 terminal 更结构化、更可观测。

如果未来 Pulsara 提供更严格的 sandbox / enterprise profile，可以再定义 “workspace-only read profile” 或 sensitive path gate。但它不应成为当前默认路径的复杂度来源。

---

## 4. 目标语义

### 4.1 路径解析

`read_file` / `search_files`：

- 空路径仍拒绝；
- 相对路径从 workspace root 解析；
- `~` 展开到用户 home；
- 绝对路径允许；
- symlink 通过 `resolve()` 规范化；
- 不要求 resolved path 位于 workspace root 内；
- 仍拒绝危险设备文件；
- 仍拒绝二进制读取；
- 仍限制单次输出大小。

`write_file` / `edit_file`：

- 继续使用 workspace-bound resolver；
- 绝对路径如果逃出 workspace，继续拒绝；
- `~` 路径逃出 workspace，继续拒绝。

### 4.2 Tool result path 展示

当前 `_relpath(path, self.workspace_root)` 对 workspace 外路径会返回绝对路径。

推荐保留这个行为，但增加 metadata：

```json
{
  "path": "/Users/plumliu/.agents/skills/firecrawl-search/SKILL.md",
  "access_scope": "home",
  "workspace_relative": false
}
```

`access_scope` 建议使用互斥枚举，基于 `resolve()` 后的真实目标路径分类：

| scope | 含义 |
| --- | --- |
| `workspace` | path 位于 workspace root 内 |
| `home` | path 位于当前用户 home 内，但不在 workspace 内 |
| `temp` | path 位于系统临时目录（例如 `/tmp`、macOS `/var/folders/...`）内 |
| `external_absolute` | 其它绝对路径，不在 workspace/home/temp 内 |

这不是 permission gate，只是 audit / inspector 解释字段。

### 4.3 Search 行为

`search_files` 需要更谨慎，因为它可以递归扫描大目录。

推荐 v1 语义：

- `path` 默认仍是 `"."`，所以默认只搜 workspace；
- 如果模型显式给出 `~/.agents/skills` 或其它绝对路径，才搜那个路径；
- 继续使用 `limit` / `offset`；
- 保留 `MAX_SEARCH_LIMIT = 1_000`；
- Python fallback 的递归扫描应继续跳过二进制扩展；
- v1 必须拒绝 broad root 递归搜索，因为当前实现是先完整扫描/收集结果再分页，`limit` / `offset` 不是扫描上限：
  - `rg` 路径会把 stdout 全部拿回来后再分页；
  - Python fallback 会 `path.rglob("*")` 后遍历文件。

必须拒绝的 broad roots 至少包括：

- `/`
- 用户 home 根：`~`
- 用户集合根：`/Users`（macOS）或 `/home`（Linux）
- workspace 的父级根目录，如果它等价于大范围用户目录；
- 系统临时根：`/tmp`、`tempfile.gettempdir()`；
- 常见系统根：`/System`、`/Library`、`/Applications`、`/var`、`/usr`、`/bin`、`/sbin`、`/etc`。

允许：

- 具体文件；
- workspace；
- home 下明确子目录，例如 `~/.agents/skills`、`~/Desktop/project-notes`；
- temp 下明确子目录，例如某个已知 run artifact 目录。

这条 guard 是 performance + privacy 地板，不是 trusted root 机制。它不限制 `read_file` 直接读取某个明确文件，只限制 `search_files` 对宽根目录做递归枚举。

---

## 5. 代码落脚点

### 5.1 拆分 resolver

当前 `WorkspaceTool._resolve_path()` 同时服务读写。推荐新增两个 resolver：

```python
def _resolve_workspace_path(self, raw_path: str | None) -> Path:
    ...
    # 现有 workspace-bound 语义

def _resolve_read_path(self, raw_path: str | None) -> Path:
    ...
    # 允许 absolute / home / external
```

兼容方式：

- 保留 `_resolve_path()` 作为 `_resolve_workspace_path()` 的 alias，避免大范围改动；
- `ReadFileTool` / `SearchFilesTool` 改用 `_resolve_read_path()`；
- `EditFileTool` / `WriteFileTool` 继续用 `_resolve_path()` 或 `_resolve_workspace_path()`。

### 5.2 增加 scope classifier

在 `filesystem.py` 增加：

```python
def _path_access_scope(path: Path, workspace_root: Path) -> str:
    ...
```

并在 read/search result metadata 和 payload 中加入：

```json
{
  "access_scope": "home",
  "workspace_relative": false
}
```

### 5.3 descriptor / tool description / prompt 更新

更新两处文案，避免 registry / descriptor / 真实行为漂移：

- `src/pulsara_agent/capability/builtin_provider.py` 中 `read_file` / `search_files` descriptor；
- `src/pulsara_agent/tools/builtins/filesystem.py` 中 `ReadFileTool.description` / `SearchFilesTool.description` / schema description。

文案必须表达：

- 不再说只能读 workspace；
- 明确相对路径从 workspace root 解析；
- 明确绝对路径 / `~` 可用于读取本机文本文件；
- 明确 `search_files` 对 workspace 外 broad root 递归搜索会拒绝；
- 写工具描述仍强调 workspace。

### 5.4 权限契约与 inspect 输出

实现 PR 必须同步：

- `contracts/PERMISSION_POLICY_CONTRACT.zh.md`
  - read-only 允许 `read_file` / `search_files` 读取 host-local ordinary text files；
  - read-only 仍拒绝 file write / terminal / durable memory write；
  - plan mode 继承该 read-only 文件读取能力。
- `src/pulsara_agent/runtime/permission.py`
  - `EffectivePermissionPolicy.to_dict()["filesystem"]` 不得继续写 `"file_tools": "workspace_only"`；
  - 推荐拆分为：

```json
{
  "filesystem": {
    "read_file_scope": "host_local_text",
    "search_files_scope": "host_local_text_guarded_broad_roots",
    "write_file_scope": "workspace_only",
    "terminal": "off"
  }
}
```

- CLI inspect / tests
  - inspect 输出必须能解释 read 与 write 的 scope 不同；
  - read-only / plan mode 文案不能继续暗示 “只能读 workspace 文件”。

---

## 6. 测试矩阵

### 6.1 read_file

- 相对路径仍从 workspace root 读取。
- workspace 内绝对路径可读。
- `~/...` 下临时文本文件可读。
- workspace 外绝对文本文件可读。
- `/dev/zero` 等设备文件仍拒绝。
- 二进制扩展仍拒绝。
- 超过 `MAX_READ_CHARS` 仍拒绝并提示分页。
- 重复读取 dedup 行为不变。
- payload / metadata 包含 `access_scope`。
- read-only policy 下读取 workspace 外普通文本文件被允许。
- plan mode 下读取 workspace 外普通文本文件被允许，terminal / write 仍被拒绝。
- known sensitive text path（例如 `.env` fixture）如果被显式 `read_file` 指向，v1 行为是允许；测试应锁定这是有意产品语义，而不是 accidental bypass。

### 6.2 search_files

- 默认 `path="."` 仍只搜索 workspace。
- 显式 workspace 外目录可搜索。
- 搜索 home 下 skill fixture 能找到 `SKILL.md`。
- 搜索 `~`、`/Users`、`/`、`/tmp` 根等 broad roots 被拒绝。
- 搜索 home/temp 下具体子目录允许。
- `limit` / `offset` 仍生效。
- 内容搜索仍跳过二进制文件。
- `rg` path 参数不再强制 cwd 内，但 cwd 仍可保持 workspace root。

### 6.3 write/edit guard

- `write_file("~/outside.txt")` 仍拒绝。
- `edit_file("~/outside.txt")` 仍拒绝。
- workspace 内写/改行为不变。

### 6.4 permission / inspect

- `EffectivePermissionPolicy.to_dict()` 输出 read/write scope 拆分，不再使用误导性的 `"file_tools": "workspace_only"`。
- CLI inspect 显示 read tools scope 是 host-local text，write tools scope 是 workspace-only。
- `PERMISSION_POLICY_CONTRACT` 中 read-only / plan mode 语义与实现一致。

### 6.5 real trajectory

用 REPL 或 real LLM dogfood 验证：

1. 用户询问某个 skill 能力；
2. 模型看到 catalog；
3. 模型使用 `read_file` 读取 `~/.agents/skills/<skill>/SKILL.md`；
4. 不再退回 terminal `cat` / `ls` 来读取 skill 说明。

---

## 7. 验收标准

- `read_file` / `search_files` 对 home-scoped skill 文件可用。
- 写工具不能越出 workspace。
- read-only 工具仍不能读取设备文件或二进制文件。
- read-only / plan mode 明确获得 host-local ordinary text read 能力；这在契约、inspect、测试中都有体现。
- `search_files` 对 workspace 外 broad roots fail-closed。
- capability descriptor 与工具真实行为一致。
- inspector / event metadata 能解释一次 read 是 workspace 内还是 home/external。
- 真实 REPL 中读取 skill 说明不再失败。

---

## 8. 非目标

本设计不做：

- 不新增 `skill_view` / `skill_get` 工具；
- 不维护 trusted skill roots allowlist；
- 不改变 terminal 权限；
- 不让写工具越出 workspace；
- 不解决 artifact preview 预算问题；那是另一份文档的主题。
