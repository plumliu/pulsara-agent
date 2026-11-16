# Tool calling patterns

Use tools from the actual Cell directory. Native and JS entry points retain each
tool's documented parameters and permissions; Code Mode control tools themselves
are native-only. ROOT bypass-permissions is required for a Cell.

Read each tool's [API reference](api/index.md) for its input and complete return
type before composing a business Cell. Shared content items alone do not describe
an individual tool's result fields or error branches.

Sequential calls can feed later arguments directly. Use Promise.all for independent
calls; already submitted calls still settle if another call fails. Print only the
selected evidence or image items that ROOT should receive.

```js
const reads = await Promise.all(["README.md","pyproject.toml"].map(path=>
  tools.read_file({target:{kind:"PATH",path}})));
console.log({content:reads.flatMap(result=>result.content)});
```

For MCP, first obtain the needed input schema, available return description and
invocation reference through native query_capabilities or a preparation Cell:

```js
const matches = await tools.query_capabilities({kind:"MCP",query:"find issues"});
console.log(matches);
```

Once the model has received the relevant interface, write the business Cell with
that actual reference. It works across native calls and Cells in the same session;
a second query inside the business Cell is unnecessary. Query results in JS memory
become model-visible through explicit output.

Use tools.decide only for semantic predicates over supplied strings. Its
available() method takes no arguments, returns the Cell's frozen configuration
boolean, and creates no tool call. Exact counts and string matching use JS.
