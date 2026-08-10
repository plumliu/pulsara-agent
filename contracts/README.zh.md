# Pulsara 当前长期契约索引

本目录只描述 conversation-kernel 终局。根目录中的 research、incident、review
与 hard-cut 文档保留设计历史，但不构成 production compatibility contract。

当前有效契约只有：

- [CONVERSATION_KERNEL_DURABILITY_CONTRACT.zh.md](CONVERSATION_KERNEL_DURABILITY_CONTRACT.zh.md)
  — canonical conversation、selective journal、turn/tool/job 事务与 crash 语义。
- [POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md](POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md)
  — clean migration universe、binding v2、extension、grant 与 reset-required。
- [TERMINAL_CLIENT_PROTOCOL_V3_CONTRACT.zh.md](TERMINAL_CLIENT_PROTOCOL_V3_CONTRACT.zh.md)
  — Python/Go ownership、snapshot、observation、live plane 与 GAP。
- [MEMORY_RELATIONAL_CONTRACT.zh.md](MEMORY_RELATIONAL_CONTRACT.zh.md)
  — PostgreSQL memory、governance、index freshness 与 bounded two-hop。
- [PROCESS_LOCAL_EXECUTION_CONTRACT.zh.md](PROCESS_LOCAL_EXECUTION_CONTRACT.zh.md)
  — provider stream、terminal、subagent、live bus 与 close/join。
- [CAPABILITY_AND_POLICY_CONTRACT.zh.md](CAPABILITY_AND_POLICY_CONTRACT.zh.md)
  — skills、builtin catalog、permission 与 extension hook boundary。
- [PACKAGE_BOUNDARY_CONTRACT.zh.md](PACKAGE_BOUNDARY_CONTRACT.zh.md)
  — production imports、facade、CLI、test support 与已删除 package 禁区。

其他同目录文件是历史链接的退役落点。它们不是 active contract，也不能作为
恢复旧 API、旧 schema 或旧 authority 的依据。

若代码与上述 active contract 冲突，应修代码或显式修订 active contract；不得从
退役文件、旧 test fixture 或 Git 历史中恢复 compatibility surface。
