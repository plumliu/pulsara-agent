Extract durable user preferences from the supplied evidence.

Return JSON only, using this exact shape:

{"schema_version":"compaction_memory_extraction_output.v1","candidates":[{"kind":"Preference","statement":"...","evidence_node_ids":["..."]}]}

Use only evidence node IDs supplied in the request. Produce at most three candidates.
Do not infer secrets, transient task state, assistant claims, tool output, summaries, or recalled memory.
Return an empty `candidates` list when the direct human evidence does not support a durable preference.
