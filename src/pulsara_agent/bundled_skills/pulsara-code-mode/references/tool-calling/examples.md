# Tool examples

## Read and select evidence

```js
const result = await tools.read_file({target:{kind:"PATH",path:"README.md"}});
console.log({content:result.content});
```

## Combine selected images and text

```js
const image = await tools.view_image({target:{kind:"PATH",path:"chart.png"}});
console.log({content:[{type:"text",value:"Check this chart.\n"},...image.content]});
```

## Save selected text

```js
await tools.write_file({target:{kind:"PATH",path:"notes.txt"},content:"Evidence collected.\n"});
return "Saved notes.txt";
```

## Search sources and read a selected page

```js
const sources = await tools.web_search({
  query:"Python asyncio task cancellation",
  include_domains:["docs.python.org"], count:5
});
console.log(sources);
```

After ROOT has seen the records, use a selected URL Target in a later Cell:

```js
const page = await tools.web_fetch({
  target:{kind:"URL",url:"https://docs.python.org/3/library/asyncio-task.html"}
});
console.log({target:page.target,title:page.meta.title,content:page.content});
```

Search records are object items with `target`, optional `title`, `snippet` and
`date`. Fetch returns text items containing the actual extracted Markdown.
Each call returns its complete saved value; native NEXT is not a JS argument.

## Judge explicit material

```js
const inputs = ["Export did not create a file.","The file opens correctly."];
if (tools.decide.available()) {
  const result = await tools.decide({condition:"Does this describe an export failure?",inputs});
  console.log(inputs.filter((_,index)=>result.content[0].value.decisions[index]));
} else {
  console.log({condition:"Does this describe an export failure?",inputs});
}
```
