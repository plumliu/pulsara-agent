# Results, errors and images

A tool resolves after its complete successful business value is saved. Each
`content` item has `type` and `value`. Keep its order and use the declared fields.
JS gets the complete value without native display timestamps or NEXT.

Tool calls can throw a `ToolError` with `code` and `message`. When present,
`err.result` contains the available business value. Read its `meta.status` and
`content` for failure, cancellation or usable results. A save or delivery error
may carry a known `SUCCESS` value. Check the actual effects before retrying.

Use `console.log` for progress and a top-level `return` for a final value.
Pass typed image items as values, including inside an object or array.
A bare IMAGE_REF or a JSON string is delivered as text. A failed image lookup
leaves a text explanation and preserves the surrounding output.

```js
const result = await tools.view_image({target:{kind:"PATH",path:"chart.png"}});
console.log({comparison:{selected:result.content.filter(p=>p.type==="image")}});
```

Cell output arrives separately from tool results and has no continuation or
export Target. Use sequential `await` when one call needs another's result or
effect. Use `Promise.all` for independent calls. If one rejects, the other
submitted calls still settle and may have effects.
