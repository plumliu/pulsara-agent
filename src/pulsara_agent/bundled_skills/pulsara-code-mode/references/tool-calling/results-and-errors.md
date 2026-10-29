# Results and errors

## Select the output that helps the next decision

`console.log` accepts one or more values. Strings retain their text; other values
use JSON serialization, and multiple arguments are separated by spaces. Each
call creates one ordered output record. Output remains available if the script
later fails. Logging `error.message` exposes the error text; logging an Error
object alone can produce an empty object.

Ordinary successful tools resolve their public JSON value or text. Tool failures
reject with a `ToolError`, carrying `code`, `message`, and available result or
artifact details. For example, a script can expose these details for ROOT to
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
raw-text or artifact route instead of rounding it.

## Preserve the tool's actual coverage

A JS result has the full public value for that call, including its own pagination
or truncation. It is not necessarily the whole file, history, or process output.
Use the tool's actual cursor, offset, or target to read further when needed.

Terminal results can include `output_artifact` for a readable archive. Use its
actual ARTIFACT target with `artifact_read`; check its disposition and coverage.
An artifact marked complete covers the reported selection, which may be less
than the process's entire lifetime.

Large individual log records are delivered through an artifact. An output page
with `has_more: true` has further records to read through `wait_exec`. A script
can log summaries, full responses, or selected fields, depending on what ROOT
needs. File or artifact locations also give access to the saved content.

If a stopped or failed script still has pending calls, later `wait_exec` output
can contain their real outcomes as `kind: "tool_result"` records. These describe
already submitted operations; the script is not executing again. These outcomes
can help clarify whether an interrupted operation took effect.

## Show an image through the native tool

JavaScript does not have `tools.view_image`. Use tools to create the image file,
then log its path or an existing image reference. After reading that output,
ROOT can call native `view_image`, for example:

```json
{"target":{"kind":"PATH","path":"/workspace/output/chart.png"}}
```

The native call supplies the actual image to the model. A logged path alone does
not. This is separate from presenting a finished visualization to the user.
