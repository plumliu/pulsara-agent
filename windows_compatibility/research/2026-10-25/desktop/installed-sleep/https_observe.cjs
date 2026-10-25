// A read-only probe: CA validation plus exact full DER, no retries or business calls.
const https=require('node:https'),{X509Certificate}=require('node:crypto');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',value=>input+=value);
process.stdin.on('end',()=>{
 const options=JSON.parse(input),url=new URL('/healthz',options.origin),certificate=new X509Certificate(options.certificatePem);
 if(url.protocol!=='https:'||url.hostname!=='127.0.0.1')throw Error('Exact loopback HTTPS required');
 const result={origin:options.origin,at:Date.now(),method:'GET',path:'/healthz'};
 const request=https.get(url,{ca:options.certificatePem,rejectUnauthorized:true,agent:false},response=>{
  let body='';response.setEncoding('utf8');response.on('data',part=>body+=part);
  response.on('end',()=>{result.httpStatus=response.statusCode;result.health=JSON.parse(body);if(response.statusCode!==200||result.health.status!=='ready'||!result.fullCertificateSame)process.exitCode=1;console.log(JSON.stringify(result));});
 });
 request.on('socket',socket=>socket.once('secureConnect',()=>{result.fullCertificateSame=socket.getPeerCertificate().raw.equals(certificate.raw);result.authorized=socket.authorized;result.localPort=socket.localPort;result.remotePort=socket.remotePort;}));
 request.setTimeout(10000,()=>request.destroy(Error('Finite read-only observation I/O deadline')));
 request.on('error',error=>{result.error={name:error.name,message:error.message,code:error.code??null};console.log(JSON.stringify(result));process.exitCode=1;});
});
