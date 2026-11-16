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
