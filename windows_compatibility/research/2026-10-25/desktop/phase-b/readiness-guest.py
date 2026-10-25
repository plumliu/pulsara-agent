"""Finite real TLS listener gate for the production BackendSession owner."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading

root=Path(tempfile.mkdtemp(prefix='.pulsara-readiness-',dir=Path.home()))
lock=threading.RLock()
mode='normal'
active=0
thread=None
server=None
def emit(event,**facts):
 with lock: print(json.dumps(dict(event=event,**facts)),flush=True)
class Handler(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args): pass
 def do_HEAD(self):
  global active
  with lock: active+=1
  emit('head-start',path=self.path,mode=mode)
  try:
   if mode=='pending':
    self.connection.settimeout(.1)
    while True:
     try:
      if not self.connection.recv(1):return
     except TimeoutError:pass
   self.send_response(401 if mode=='unauthorized' else 200)
   self.send_header('Content-Length','0');self.end_headers()
  except (ssl.SSLError,ConnectionResetError,BrokenPipeError):pass
  finally:
   with lock: active-=1
   emit('head-closed')
try:
 key,cert=root/'key.pem',root/'cert.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
 public=cert.read_text();key.unlink();cert.unlink()
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler,bind_and_activate=False)
 server.server_bind();server.socket=context.wrap_socket(server.socket,server_side=True)
 emit('prepared',origin=f'https://127.0.0.1:{server.server_address[1]}',certificatePem=public,pid=os.getpid(),starttime=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],home=str(root))
 for raw in sys.stdin:
  command=raw.strip()
  if command=='stop':break
  if command.startswith('serve '):
   assert thread is None
   mode=command.split(' ',1)[1]
   server.server_activate();thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   emit('serving',mode=mode)
  elif command=='inspect':
   with lock:emit('inspect',active=active)
finally:
 if server is not None:
  if thread is not None:server.shutdown();thread.join()
  server.server_close()
 with lock:assert active==0,active
 assert root.parent==Path.home() and root.name.startswith('.pulsara-readiness-') and root.stat().st_uid==os.getuid()
 shutil.rmtree(root);emit('cleaned',exists=root.exists())
