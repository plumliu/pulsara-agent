# Results, errors and images

A tool resolves after its complete successful business value is saved. Each
`content` item has `type` and `value`. Keep its order and use the declared fields.
JS gets the complete value without native display timestamps or NEXT.

Tool calls can throw a `ToolError` with `code` and `message`. Inspect `err.result`
when present. It can contain a common error with `meta.status` set to `ERROR` or
`CANCELLED` and text
`content`, or the tool-specific failure on its API page. A save or delivery
error may carry a known `SUCCESS` value. Failed and cancelled calls may have
accepted effects. Check what happened before retrying.

Call `console.log` with the values ROOT needs. Actual image items are recognized
inside objects and arrays. A bare IMAGE_REF remains text; a failed image lookup
leaves a text explanation in place and preserves the surrounding output.

```js
const result = await tools.view_image({target:{kind:"PATH",path:"chart.png"}});
console.log({comparison:{selected:result.content.filter(p=>p.type==="image")}});
```

Cell output arrives separately from tool results and has no continuation or
export Target. Each call keeps ownership of accepted effects, even after the
Cell stops. Use `Promise.all` for independent calls; an error in one leaves other
submitted calls to settle.
