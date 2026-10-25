"""Installed owners + finite test routes. Session/Core/model/DB are NOT under test."""
import asyncio,json,os,sys,tempfile,shutil
from pathlib import Path
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch
os.umask(0o077)
import pulsara_agent
assert str(Path(pulsara_agent.__file__).resolve()).startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/')
sys.path.extend(['/home/plumwsl/workspaces/pulsara_agent','/home/plumwsl/workspaces/pulsara_agent/.venv/lib/python3.12/site-packages'])
from aiohttp import web
from cryptography import x509
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.desktop_app.tls_identity import create_desktop_tls_identity
from tests.test_desktop_http_surface import _model_server_dependencies
home=Path(tempfile.mkdtemp(prefix='.pbf-network-',dir=Path.home()));os.environ['PULSARA_HOME']=str(home)
def emit(event,**facts):print(json.dumps(dict(event=event,**facts),ensure_ascii=False),flush=True)
active={};opened=[];closed=[];requests=[];counter=0
async def main():
 global counter
 static=home/'static';static.mkdir();(static/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Production owner network test</title><p>finite fixture</p>')
 workspace=home/'workspace';workspace.mkdir()
 async def availability(_sid):return {'path':str(workspace),'outcome':'AVAILABLE'}
 async def summary(_sid,**kwargs):return SimpleNamespace(workspace_root=str(workspace))
 sessions=SimpleNamespace(workspace_availability=availability,core=SimpleNamespace(read_resumable_session=summary),memory_domain_id='private-fixture',_lock=asyncio.Lock(),_operations={},host_writer_unavailable=lambda _id:False)
 bridge=RendererBridge(sessions=sessions,protocol_server=object());sid='session:tpsqggcbte7fwanzpf642ljwli'
 bridge._connections['owner']=SimpleNamespace(connection_id='owner',host_session_id='test-host',session_id=sid,role='controller',generation=7,is_open=True)
 bridge._controller_by_session[sid]='owner'
 # Only shorten standard X509 builder dates for this validity-boundary test.
 original_before=x509.CertificateBuilder.not_valid_before;original_after=x509.CertificateBuilder.not_valid_after
 now=datetime.now(timezone.utc);end=now+timedelta(seconds=15)
 with patch.object(x509.CertificateBuilder,'not_valid_before',lambda self,_date:original_before(self,now-timedelta(minutes=1))),patch.object(x509.CertificateBuilder,'not_valid_after',lambda self,_date:original_after(self,end)):
  identity=create_desktop_tls_identity(home)
 assert not list(home.glob('.tls-*'))
 async def stream(request):
  global counter
  kind=request.match_info['kind'];token=str(id(request));active[token]=kind;opened.append(dict(token=token,kind=kind));transport=request.transport
  try:
   if kind=='pending':
    while transport and not transport.is_closing():await asyncio.sleep(.02)
    raise asyncio.CancelledError
   headers={'Content-Type':'text/event-stream'} if kind=='sse' else {'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="private-controlled.bin"','Content-Length':str(64*1024*1024)}
   response=web.StreamResponse(headers=headers);await response.prepare(request);sent=0
   while True:
    counter+=1;data=(f'data: {json.dumps(dict(pid=os.getpid(),count=counter))}\n\n'.encode() if kind=='sse' else b'N'*65536)
    await response.write(data);sent+=len(data)
    if kind!='sse' and sent>=64*1024*1024:break
    await asyncio.sleep(.05)
   return response
  except (ConnectionResetError,BrokenPipeError):raise asyncio.CancelledError
  finally:active.pop(token,None);closed.append(dict(token=token,kind=kind,transportClosing=not transport or transport.is_closing()))
 def server_at(port,identity):
  server=LocalHttpServer(sessions=sessions,bridge=bridge,static_root=static,requested_port=port,is_ready=lambda:True,is_draining=lambda:False,ssl_context=identity.context,**_model_server_dependencies())
  server._app.router.add_get('/probe/{kind:sse|download|pending}',stream)
  async def observe(request,response):requests.append(dict(path=request.path,status=response.status,method=request.method))
  server._app.on_response_prepare.append(observe);return server
 server=server_at(0,identity);await server.start();port=server.port
 emit('ready',pid=os.getpid(),starttime=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],home=str(home),origin=server.origin,certificatePem=identity.certificate_pem,validTo=end.isoformat(),packagePath=pulsara_agent.__file__,privateKeysRemain=list(map(str,home.glob('.tls-*'))),fixtureScope='real HTTP/RendererBridge/import; synthetic sessions/Core and control streams')
 try:
  while raw:=await asyncio.to_thread(sys.stdin.buffer.readline):
   value=json.loads(raw);op=value['op']
   if op=='stop':break
   if op=='rebind':
    assert not active,active
    await server.aclose();foreign=create_desktop_tls_identity(home);server=server_at(port,foreign);await server.start();emit('rebound',origin=server.origin,certificatePem=foreign.certificate_pem,pid=os.getpid());continue
   if op=='inspect':
    imports=[]
    for file in (home/'imports').glob('*/*'):
     if file.is_file():s=file.stat();imports.append(dict(path=str(file.relative_to(home)),bytes=s.st_size,mode=oct(s.st_mode&0o777),uid=s.st_uid))
    emit('inspect',pid=os.getpid(),active=list(active.values()),opened=list(opened),closed=list(closed),imports=imports,stages=[p.name for p in (home/'imports').glob('.import-*')],requests=list(requests))
 finally:await server.aclose();assert not active,active;emit('server-stopped',active=list(active.values()))
try:asyncio.run(main())
finally:
 assert home.resolve().parent==Path.home() and home.name.startswith('.pbf-network-') and home.stat().st_uid==os.getuid()
 shutil.rmtree(home);emit('cleaned',home=str(home),exists=home.exists(),pid=os.getpid())
