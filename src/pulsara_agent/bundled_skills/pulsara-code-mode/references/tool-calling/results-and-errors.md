# Results and errors

## Select the output that helps the next decision

`console.log` accepts one or more values. Strings retain their text; other values
use JSON serialization, and multiple arguments are separated by spaces. Each
call creates one ordered output record. Output remains available if the script
later fails. Logging `error.message` exposes the error text; logging an Error
object alone can produce an empty object.

Ordinary successful tools resolve their public JSON value or text. Tool failures
reject with a `ToolError`, carrying `code`, `message`, and available result or
saved-body details. For example, a script can expose these details for ROOT to
interpret:

```js
try {
  const result = await tools.read_file({
    target: { kind: "PATH", path: "README.md" },
    limit: 120
  });
  console.log(result);
} catch (error) {
  console.log({ message: error.message, code: error.code, result: error.result });
}
```

A successful query can describe a failed activity: a Work with `status: "stopped"`
is a normal query result. Likewise, waiting for a terminal process does not imply
its exit code was successful. The activity's state and output describe its outcome.

Arguments must be JSON-compatible values. Pass concrete values rather than
functions or `undefined`; batch parameter functions are the specific exception
described in [Coordination](../agent-orchestration/coordination.md). Non-finite
numbers, BigInt, cycles, and integers outside JavaScript's safe integer range
cannot cross the tool boundary. For an unsafe-number response, use the provided
exact saved-body read route instead of rounding it.

## Preserve the tool's actual coverage

A JS result has the full public value for that call, including its own pagination
or truncation. It is not necessarily the whole file, history, or process output.
Use the tool's actual cursor, offset, or target to read further when needed.

Source pages provide a complete `next` call or null. Copy it to continue the
source. It does not read omitted text from an earlier result. A saved result's
`read` call uses `read_file` with its exact SESSION_ENTRY target; its own `next`
continues that frozen body. A complete body covers the reported selection, which
may be less than a process lifetime or an entire remote dataset.

Each log is actively delivered as its own input; ROOT does not use `exec_cell`
or `manage_cell.wait` to fetch output pages. Large formally published records
retain their body under the original entry. Use the supplied read call when ROOT
needs more text, or use its target as `write_file.source` to save the whole body.
Only a real output omission needs a read call; a complete displayed page needs no
second body read.

A tool value that exceeds the bridge boundary rejects with `value_boundary` while
preserving the real business state and result IDs. Inspect `error.read`, body
availability and coverage. Read or save that accepted body; do not repeat a write
or remote action to recover its response. JavaScript cannot access content an
upstream source never returned.

A Cell that fails unexpectedly reports a combined diagnostic preserving its
specific cause. Tools already accepted but not delivered to JS still settle and
report their actual `kind: "tool_result"` records; they do not execute again.
Explicit Stop keeps real effects readable without using late results to wake the
stopped execution. Ordinary results already delivered to JS stay inside the script
unless it explicitly outputs them.

Agent work belongs to the collaboration owner, not the Cell. After a script fails
or stops, use native `query_agents` to inspect actual Agent/Work state, native
`send_agent_message` to supplement active work, or native `stop_agent` to stop it.
These controls work independently of Cell slots and the Code Mode runtime. Resume
assignments or waits in a new script using the existing targets; reuse completed
results rather than dispatching them again.

## Decision availability and failures

`tools.decide.available()` is a local configuration check frozen for the Cell,
not a network health check. A false value leaves the semantic question unanswered:
send the condition, material and relevant Agent/Work references to ROOT, and
continue the task through the existing tools. Configuration is optional; explain
the Jev-like Decision setting when the user explicitly wants to use or configure it.

An actual `tools.decide` call can fail through the normal `ToolError` path even
after an available check, for example if settings change or the service fails.
Preserve its concrete error, or adjust the call when the cause is understood.
Never substitute a false array for an error. A successful result covers every
input in order; failure does not supply a usable partial decisions array.
Resource diagnostics identify an oversized input when applicable: reduce that
material or condition, or process appropriate source pages in separate calls.

## Show an image through the native tool

JavaScript does not have `tools.view_image`. Use tools to create the image file,
then log its path or an existing image reference. After reading that output,
ROOT can call native `view_image`, for example:

```json
{"target":{"kind":"PATH","path":"/workspace/output/chart.png"}}
```

The native call supplies the actual image to the model. A logged path alone does
not. This is separate from presenting a finished visualization to the user.
