import { expect, it } from 'vitest';
import { decodeToolBusinessValue, toolBusinessText } from '../../frontend/lib/tool-business-value';

it('decodes the explicit full business wire without changing JSON values or item order', () => {
  const value={target:{kind:'PATH',path:'video.mkv'},meta:{status:'ERROR',source:'subtitle'},structuredContent:null,content:[
    {type:'text',value:'原文\r\n'}, {type:'object',value:{from:1,to:2,text:'没有改写'}}, {type:'target',value:{kind:'PATH',path:'/actual'}}]};
  expect(decodeToolBusinessValue(JSON.stringify(value))).toEqual(value);
  expect(toolBusinessText(decodeToolBusinessValue(JSON.stringify(value)))).toContain('原文\r\n');
});
it.each([{meta:{status:'SUCCESS'},content:[{type:'image',value:{kind:'IMAGE_REF',image_ref:'bad'}}]},
  {meta:{status:'SUCCESS'},content:[{type:'text',value:2}]},
  {meta:{status:'invented'},content:[]}])('rejects invalid typed result fields', value => {
  expect(()=>decodeToolBusinessValue(JSON.stringify(value))).toThrow();
});
