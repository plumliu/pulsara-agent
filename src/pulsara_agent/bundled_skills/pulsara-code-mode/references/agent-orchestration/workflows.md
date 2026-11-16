# Investigation and review workflow

1. Read the relevant project material with ordinary tools.
2. Submit concrete independent assignments in one run_agent array. Include ROOT
   history only through explicit CONTEXT_MATERIAL selection.
3. Output accepted Targets that later Cells or native controls need.
4. Wait for exact Works, or wait for Agents to become idle when that is the actual
   condition. Check meta.wait_reason and every returned record.
5. Query retained WORK Targets for full results. Reuse an AGENT Target for a new
   Work when its accumulated context is useful.
6. Use depends_on for successful prerequisite results; use messages for independent
   discussion. Do not wait for a reply from a Work blocked on your own completion.
7. Print the selected evidence, including actual image content items, for ROOT.

Optional tools.decide can route explicit text through a semantic yes/no condition.
Its complete decisions array is result.content[0].value.decisions. Guard calls
with available(), preserve real failures, and use JS for exact comparisons.

Stopping or failing a Cell does not undo accepted Work or tool effects. Inspect
actual state with native controls before resuming; do not redispatch to recover a
missing result. A new Cell uses fresh variables and the same retained Targets.
