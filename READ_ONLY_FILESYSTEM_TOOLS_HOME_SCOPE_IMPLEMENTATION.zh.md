# Read-only filesystem tools 解除 workspace 限制实施文档

_Created: 2026-07-04_

## 0. 结论

本轮要解决的问题不是 skill discovery，而是 read-only filesystem tools 的边界语义。

推荐 v1：

- `read_file` / `search_files` 作为只读工具，允许读取本机任意普通文本路径，包括用户家目录下的 `~/.agents/skills` / `~/.pulsara/skills`。
- `write_file` / `edit_file` 继续严格限制在当前 workspace root 内。
- 默认相对路径语义不变：`path="."` 或相对路径仍从 workspace root 解析。
- 绝对路径和 `~` 路径允许越出 workspace，但仍保留设备文件、二进制文件、单次读取大小、分页、重复读取保护。
- tool result metadata 明确标记路径范围，例如 `access_scope: "workspace" | "home" | "absolute" | "external"`，方便 inspector / audit 解释。

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

### 3.2 为什么这不是大幅放宽现有实际能力

Pulsara 默认 permission preset 是 `bypass-permissions`，terminal 默认可用。模型已经可以通过 terminal 执行：

```bash
cat ~/.agents/skills/foo/SKILL.md
```

因此 `read_file` 允许读取 home text file 并不是引入一种全新的 open-world 能力，而是把已有能力从 shell fallback 拉回结构化、可审计、可分页的 read tool。

同时，写工具仍保持 workspace-bound，不允许借此写用户家目录。

### 3.3 read-only 不等于无隐私风险

需要明确：读取 home text files 可能暴露隐私。因此这个改动必须绑定当前 Pulsara 的产品定位：

- 本地 agent；
- 用户显式启动；
- 默认具有 terminal 能力；
- 只读工具比 terminal 更结构化、更可观测。

如果未来 Pulsara 提供更严格的 sandbox / enterprise profile，可以再定义 “workspace-only read profile”。但它不应成为当前默认路径的复杂度来源。

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

`access_scope` 建议：

| scope | 含义 |
| --- | --- |
| `workspace` | path 位于 workspace root 内 |
| `home` | path 位于当前用户 home 内，但不在 workspace 内 |
| `absolute` | path 是绝对路径且不在 workspace/home 内 |
| `external` | 其它可读路径，例如 `/tmp` |

这不是 permission gate，只是 audit / inspector 解释字段。

### 4.3 Search 行为

`search_files` 需要更谨慎，因为它可以递归扫描大目录。

推荐 v1 语义：

- `path` 默认仍是 `"."`，所以默认只搜 workspace；
- 如果模型显式给出 `~/.agents/skills` 或其它绝对路径，才搜那个路径；
- 继续使用 `limit` / `offset`；
- 保留 `MAX_SEARCH_LIMIT = 1_000`；
- Python fallback 的递归扫描应继续跳过二进制扩展；
- 可新增一个防护：当搜索 home 或 external directory 且 path 是 home 根本身（`~`）时，要求更具体的子目录，避免误扫整个家目录。

最后这条可以作为 v1.1；如果本轮想最小实现，可以只靠 limit 和 timeout。

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

### 5.3 descriptor / prompt 更新

更新 `src/pulsara_agent/capability/builtin_provider.py` 中 `read_file` / `search_files` 描述：

- 不再说只能读 workspace；
- 明确相对路径从 workspace root 解析；
- 明确绝对路径 / `~` 可用于读取本机文本文件；
- 写工具描述仍强调 workspace。

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

### 6.2 search_files

- 默认 `path="."` 仍只搜索 workspace。
- 显式 workspace 外目录可搜索。
- 搜索 home 下 skill fixture 能找到 `SKILL.md`。
- `limit` / `offset` 仍生效。
- 内容搜索仍跳过二进制文件。
- `rg` path 参数不再强制 cwd 内，但 cwd 仍可保持 workspace root。

### 6.3 write/edit guard

- `write_file("~/outside.txt")` 仍拒绝。
- `edit_file("~/outside.txt")` 仍拒绝。
- workspace 内写/改行为不变。

### 6.4 real trajectory

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

