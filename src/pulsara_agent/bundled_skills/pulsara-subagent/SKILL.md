---
name: pulsara-subagent
description: Delegate Pulsara tasks, collect results and choose between lightweight result references and inherited worker history. Use for parallel work, follow-up questions, continued review, dependencies and failure recovery.
---

# Pulsara Subagents

## Task and subagent

A task is one delegation with an objective, execution status and outcome. A
subagent is the worker that executes that task. Pulsara shows this delegated work
as a **task** in the UI. `task` is the instruction text; `task_id` identifies the
delegation; optional `task_name` gives it a readable name.

The main Agent assigns tasks; workers execute them and return results. Workers
cannot delegate further. A follow-up creates a new task and worker execution,
even when it inherits an earlier task's conversation. Use the returned task ID
to wait, send guidance or cancel that specific execution. Use the tools available
and authorized in your current scope; this guide does not grant permissions.

## Choose the next action

| What you need | Use |
| --- | --- |
| One independent piece of work | `spawn_agent`; omit `context` for a self-contained task. |
| Correct or guide a running worker | `send_agent_message` with its exact `task_id`. Queued means accepted, not yet read. |
| Ask a finished worker a follow-up, keeping its working background | `spawn_agent` with `context: {"mode":"worker_history","task_id":"<old task_id>"}`. |
| Give a new task an old finding, structured result or failure diagnosis | `material_task_ids: ["<old task_id>"]`; omit `context` unless other history is needed. |
| Schedule tasks together, with some requiring others to succeed | `create_agent_tasks`; add `depends_on` only for actual prerequisites. |

Finished tasks stay finished. A history-based follow-up gets a new task ID;
describe it to the user as continuing from the previous worker's history.
Do not conclude that further work is impossible merely because the old task ended.
Two new tasks can branch from the same source independently; to build on one
branch later, use that branch's returned task ID. Branches share the working
directory, so conversation isolation does not isolate file changes.

## Supply just enough context

Write the objective, deliverable, constraints and necessary locations in `task`.
Choose the smallest context that supports the work:

- **Result reference / `material_task_ids`**: supply finished tasks' result
  summaries, structured `data` when present, or public failure diagnoses. Use
  this to combine findings, implement a reported fix or diagnose a failure when
  the outcome is enough. It does not bring their conversation or tool history.
- **Default / `none`**: only the task and any explicitly selected materials.
- **`last_n`**: add 1–3 recent main-conversation turns using `turns`. This does
  not copy their tool calls/results; put required evidence in the task.
- **`worker_history`**: use the exact `task_id` of a finished worker in this
  conversation. It must have started and have readable public history. Do not
  supply `turns`. The new branch continues the source
  conversation, including public messages and tool calls/results; compacted
  parts may be summaries. Use this when the next question needs the earlier
  investigation, evidence or working background beyond its final result.
  Historical calls are records, not work to repeat. It does not copy private
  reasoning, old tools, permissions or running processes. The new worker uses
  its current environment.

For example, combining a review's findings needs a result reference; checking a
new case against the files and tool outputs examined during that review may need
`worker_history`. Write the new objective without repackaging the old transcript
into `task`. Do not also reference the same task as result material when its
history already supplies what you need.

`material_task_ids` accepts finished tasks in this conversation, including
failures and tasks that never started. Use it for recovery when a result or
diagnosis is enough.
`depends_on` waits for success and supplies the prerequisite's result; a failed
prerequisite blocks the dependent task. Do not make recovery depend on a failed
task succeeding. Material/history references do not wait for running tasks.

Omit `model` to inherit the assigning turn's model and reasoning. For a requested
different model, call `list_agent_models` and copy a returned `connection_id` and
supported reasoning choice. Omitted reasoning uses that target's default.
`profile` changes working style, not the model; omit it for ordinary work.

## Common calls

One independent review, using `spawn_agent`:

```json
{"task":"Review src/parser.py for incorrect handling of empty input. Report concrete defects with locations; do not edit files.","task_name":"parser-review"}
```

Follow up after the parser review, using `spawn_agent`.
Replace `<old task_id>` with the exact returned ID:

If only the findings are needed:

```json
{"task":"Use the supplied review findings to prioritize fixes for src/parser.py. Explain the order; do not edit files.","material_task_ids":["<old task_id>"]}
```

If the earlier investigation and evidence are needed:

```json
{"task":"Continue from your earlier review of src/parser.py. Check missing-field handling and compare it with your empty-input findings. Report concrete defects with locations; do not edit files.","context":{"mode":"worker_history","task_id":"<old task_id>"}}
```

Recover from a failed task, using `spawn_agent`:

```json
{"task":"Inspect the supplied failure diagnosis and identify an in-scope fix. Check whether earlier operations had effects before repeating them.","material_task_ids":["<failed task_id>"]}
```

Within one `create_agent_tasks` call, dependencies use another item's `task_key`. Names and keys preserve case; copy dependency keys exactly (`A` and `a` are different).
For a prerequisite from an earlier call, use `"task:" + its exact task_id`.
Task labels are not IDs. Recover lost IDs with `list_agents`.

## Collect the outcome

Dispatch returns before completion; extra tasks can queue for execution capacity.
Continue independent work, then use `wait_agent` if your answer needs the results.
Results arrive as separate conversation messages, not inside the wait response.
Read whether each task succeeded; a satisfied wait only means its finish condition
was met. A timeout leaves tasks running. Avoid polling task lists for delivery.

Use `stop_agent` to cancel unwanted work; cancellation does not undo side effects.
Workers can finish with a final reply or `report_agent_result`. The latter must
be the only tool call in that response. Make the summary understandable without
the worker transcript; use optional `data` only when structured output helps.
