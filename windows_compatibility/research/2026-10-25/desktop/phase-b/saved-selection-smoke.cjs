const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
const {WslSetup}=require('../../../desktop/dist/wsl-setup');
const file=path.join(__dirname,'invalid-selection-smoke.json'),result={cases:[]};
(async()=>{
 const setup=new WslSetup(file);
 try {
  for(const text of ['not JSON',JSON.stringify({distro:'Ubuntu',user:'root',extra:true}),JSON.stringify({distro:'Ubuntu',user:'-bad'})]){
   await fs.writeFile(file,text);const before=await fs.readFile(file,'utf8'),found=await setup.discover();
   assert.ok(found.distros.some(v=>v.name==='Ubuntu'&&v.version===2));assert.equal(found.saved,undefined);assert.ok(found.savedError);assert.equal(await fs.readFile(file,'utf8'),before);
   result.cases.push({invalidInput:text,result:found,unchangedUntilExplicitSelection:true});
  }
  const selected=await setup.select({distro:'Ubuntu',user:'plumwsl'}),reopened=await setup.discover();
  assert.equal(selected.uid,1000);assert.deepEqual(reopened.saved,{distro:'Ubuntu',user:'plumwsl'});assert.equal(reopened.savedError,undefined);
  result.explicitSelection={selected,reopened};result.passed=true;
 } finally {await fs.rm(file,{force:true});}
})().catch(error=>{result.failure=error.stack;process.exitCode=1;}).finally(()=>fs.writeFile(path.join(__dirname,'saved-selection-results.json'),JSON.stringify(result,null,2)));
