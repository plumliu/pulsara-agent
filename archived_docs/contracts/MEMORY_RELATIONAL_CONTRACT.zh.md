# PostgreSQL Relational Memory Contract

## 1. Authority

Memory fact、relation、candidate、governance decision与index generation均在
`pulsara_v3` canonical relations中。Oxigraph、SPARQL、JSON-LD mirror、surface worker与
projection delivery authority不存在。

Memory candidate必须先 durable accepted；governance始终由具名 durable job异步执行。
Ordinary foreground turn不等待 governance或index成功。

## 2. Query

Production query只使用 PostgreSQL：

- FTS lexical match；
- pgvector semantic match；
- exact direct/reverse relation；
- bounded 0/1/2-hop traversal。

3-hop-only path不得返回。不得为删除 SPARQL而新增 generic SQL graph DSL。

## 3. Index freshness

`memory_index_state`只保存 desired/applied generation与 handler-contract watermark。
Refresh job的 active/failed/exhausted truth只存在于 durable job rows。

Query用 stable target key做 bounded join，并返回 closed disposition：

- `COMPLETE`；
- `PARTIAL_STALE`；
- `PARTIAL_UNAVAILABLE`。

Desired/applied lost-wake scanner可以 enqueue exact `MEMORY_INDEX_REFRESH` job，但不得创建
第五类 handler、same-key repair owner或 projection receipt。

## 4. Mutation

Memory proposal随 model-visible ToolResult在 Host writer canonical transaction中接受；
governance与index写入必须携带 exact `JobAttemptClaimGuard`。Job worker不能修改 transcript或
subagent rows。

Stage 2不提供 delete/forget，也不扩大 two-hop。Large evidence通过统一 canonical blob
publication contract引用，不建立 per-domain hold/receipt graph。
