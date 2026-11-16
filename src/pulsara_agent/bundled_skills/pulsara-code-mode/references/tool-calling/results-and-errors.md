# Results and errors

A successful tools call resolves its declared complete saved business value.
For a result with content, inspect `part.type` and use `part.value`; object values
retain their complete typed fields. No native display page or display NEXT is
inserted into JS. A real service's own business pagination keeps its own meaning.

```js
try {
  const result = await tools.read_file({target:{kind:"PATH",path:"README.md"}});
  console.log(result);
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

A failure with an actual business value exposes that full value as err.result.
An earlier argument or transport failure may have no business value. Never
re-execute a write or remote action merely to obtain its response. Accepted effects
and their result settlement remain owned by the original tool call.

Every console.log is one formal output; a non-undefined top-level return is the
last successful output. Multiple log arguments keep their space separator.
Strings remain text. Ordinary objects keep their JSON structure and field order.
Actual image items are recognized recursively, including within object.value:

```js
const result = await tools.view_image({target:{kind:"PATH",path:"chart.png"}});
console.log({comparison:{selected:result.content.filter(p=>p.type==="image")}});
```

Only selected explicit output is scanned. Bare Targets, paths, URLs and strings
containing JSON do not load images. Each selected image occurrence remains in
position. An invalid reference or unreadable asset produces its reference and
actual reason as text; other output remains. Model image capability affects the
model projection, not the saved asset or complete intermediate JS value.

Cell output is delivered whole through the normal input queue. It has no
read_file, continuation, or export Target. Different outputs do not suppress an
image because another output already used that image. No original combination
JSON or second manual text body is saved.

Unexpected script failure preserves diagnostics and the established supplement
for accepted results that could not be delivered to JS. Explicit stop or Host
shutdown suppresses that supplement. Ordinary results that already reached JS
stay private until explicitly output. Stopping a Cell does not undo tool effects
or automatically stop Agent Works.
