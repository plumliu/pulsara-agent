const fs=require('node:fs'),path=require('node:path');
const asar=require('../../../../desktop/node_modules/@electron/asar');
const archive=path.join(process.env.LOCALAPPDATA,'Programs/pulsara/resources/app.asar');
const directory=path.join(__dirname,'baseline-source/desktop/assets');fs.mkdirSync(directory,{recursive:true});
for(const name of ['startup.html','startup.js','startup.css']){
 fs.writeFileSync(path.join(directory,name),asar.extractFile(archive,'assets/'+name),{flag:'wx'});
}
