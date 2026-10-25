// Read the installed application archive; never starts Electron or the product.
const fs=require('node:fs'),path=require('node:path');
const asar=require('../../../../desktop/node_modules/@electron/asar');
const archive=path.join(process.env.LOCALAPPDATA,'Programs/pulsara/resources/app.asar');
for(const file of ['package.json','dist/runtime.js','dist/main.js']){
 fs.writeFileSync(path.join(__dirname,'installed-'+path.basename(file)),asar.extractFile(archive,file),{flag:'wx'});
}
console.log(JSON.stringify({readOnly:true,archive,files:['package.json','dist/runtime.js','dist/main.js']}));
