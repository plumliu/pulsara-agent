const {app}=require('electron'),net=require('node:net'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{rootCertificates}=require('node:tls');
const {createBackendSession}=require('../../../desktop/dist/backend-session');
app.setPath('userData',path.join(__dirname,'readiness-sdk-profile'));app.on('window-all-closed',()=>{});
const result={};let owner;
app.whenReady().then(async()=>{
 const temporary=net.createServer();await new Promise(resolve=>temporary.listen(0,'127.0.0.1',resolve));const port=temporary.address().port;await new Promise(resolve=>temporary.close(resolve));
 owner=createBackendSession({origin:'https://127.0.0.1:'+port,certificatePem:rootCertificates[0]});
 try{await owner.session.fetch('https://127.0.0.1:'+port+'/',{method:'HEAD',redirect:'error'});assert.fail('closed port answered');}
 catch(error){result.error={name:error.name,message:error.message,code:error.code??null,cause:error.cause??null};assert.equal(error.message,'net::ERR_CONNECTION_REFUSED');}
 result.passed=true;
}).catch(error=>{result.failure=error.stack;}).finally(async()=>{if(owner)await owner.retire(async()=>{});fs.writeFileSync(path.join(__dirname,'readiness-sdk-error.json'),JSON.stringify(result,null,2));app.exit(result.passed?0:1);});
