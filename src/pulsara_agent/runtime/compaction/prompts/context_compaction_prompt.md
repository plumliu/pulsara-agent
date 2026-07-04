CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use terminal, file, memory, browser, network, or any other tool.
- You already have all context needed in the compaction input.
- Tool calls will be rejected and this compaction attempt will fail.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

You are performing a Pulsara CONTEXT COMPACTION.

Create a detailed handoff summary for a later LLM that will continue the same runtime session after older context has been compacted.

This is not a user-facing final answer. This is a continuity artifact used to reconstruct model-visible context while the full canonical event log remains stored elsewhere.

Before writing the final summary, write an <analysis> block. In that analysis, chronologically inspect the provided compaction input and verify:

1. What the user explicitly asked for.
2. What the assistant actually did.
3. Which files, commands, tools, artifacts, tests, plans, memory results, or runtime events matter for continuing.
4. Which facts are canonical user/tool/event facts, and which are derived projections.
5. Which problems were solved, which failed, and which remain pending.
6. Whether the proposed next step is directly grounded in the most recent user request.

Then write a <summary> block with the exact sections below.

<summary>
1. Current User Intent and Active Objective
   - State the user's latest active request and any stable preferences relevant to continuing.
   - Include only user intent that is explicitly present in the compaction input.
   - If the latest task was completed, say so and do not invent a new objective.

2. Important Decisions and Constraints
   - List architectural or product decisions already made.
   - Include constraints from contracts, user instructions, repository policy, or runtime mode.
   - Preserve exact decision boundaries where they matter.

3. Files, Code, and Artifacts
   - List files inspected, created, or modified, with absolute or repository-relative paths as provided.
   - For each important file, explain why it matters for the current work.
   - Reference artifact ids instead of inlining long artifact contents.
   - Include short snippets only when they are essential for continuation.

4. Tools, Commands, and Runtime State
   - Summarize important tool calls, terminal commands, tests, and their outcomes.
   - Preserve terminal/session/host lifecycle facts needed to continue safely.
   - If a terminal call yielded a long-running or background process, preserve the exact process_id, command, status, owner/session clues if available, and the safe continuation action. Tell the next agent to continue with terminal_process (poll/log/wait/submit/kill as appropriate) rather than restarting the command.
   - If a run was aborted, failed, detached, resumed, or recovered, state that as runtime state, not as a user preference.

5. Memory and Projection Boundary
   - Distinguish durable user-provided facts from memory recall results, working-context projection, and recovery notes.
   - If memory recall or working context influenced the conversation, describe it as recalled/projection context.
   - Do NOT convert recalled/projection content into a claim that the user just said it.
   - Do NOT write any compact-summary content as a durable memory candidate.

6. Errors, Corrections, and User Feedback
   - Record important errors, failed assumptions, user corrections, and how they were fixed.
   - Pay special attention when the user said the assistant's behavior was wrong or should be changed.

7. Pending Tasks and Open Questions
   - List only tasks still pending or explicitly requested.
   - List design questions only if they remain unresolved and materially affect implementation.
   - Do not resurrect old completed tasks.

8. Current Work State
   - Describe precisely what was being worked on immediately before compaction.
   - Include the latest relevant file/path/test/command state.
   - If recent messages are preserved verbatim after this summary, say what this summary intentionally leaves to the recent tail.

9. Next Step
   - Provide the next step only if it is directly aligned with the latest active objective.
   - Quote or paraphrase the grounding recent user request.
   - If the safe next step is to wait for user approval or input, say that clearly.
</summary>

Rules:

- Be precise and complete enough for continuation, but do not include irrelevant old history.
- Prefer stable identifiers: run_id, turn_id, artifact_id, memory_id, file path, command, test name.
- For live terminal processes, process_id is a stable runtime identifier. Preserve it when present and say whether it can be resumed via terminal_process.
- Do not overclaim. If the compaction input only says a tool was requested but not completed, preserve that uncertainty.
- Do not claim a file was modified unless the event/timeline says it was modified.
- Do not claim a test passed unless the test output says it passed.
- Do not treat memory recall, working context, recovery notes, or inspector diagnostics as direct user statements.
- Do not include hidden chain-of-thought. The <analysis> block is a private drafting checklist and will be stripped before reinjection.
- Do not ask the user questions in the summary unless the current workflow is genuinely blocked on user input.
