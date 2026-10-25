const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const repo=process.cwd(),asar=require(path.join(repo,'desktop/node_modules/@electron/asar'));
const archive=path.join(repo,'desktop/release/win-unpacked/resources/app.asar'),version=JSON.parse(fs.readFileSync('desktop/package.json')).version;
assert.equal(version,'0.1.0-alpha.10');assert.equal(JSON.parse(asar.extractFile(archive,'package.json').toString()).version,version);
const files=['startup.html','startup.js','startup.css'];for(const name of files)assert.ok(asar.extractFile(archive,'assets/'+name).equals(fs.readFileSync(path.join(repo,'desktop/assets',name))));
const js=asar.extractFile(archive,'assets/startup.js').toString();assert.ok(js.includes('error-detail-text')&&js.includes('退出尚未完成')&&js.includes('retryStartup()')&&js.includes('showLogs()'));
fs.writeFileSync(path.join(__dirname,'package-ui-audit.json'),JSON.stringify({passed:true,version,actualUnpackedAsar:true,assetsEqualSource:files,friendlyFailureAndCollapsedDetails:true,originalRetryAndLogsHandlers:true,installerRun:false},null,2)+'\n');console.log('Actual alpha10 asar UI and original actions verified.');
