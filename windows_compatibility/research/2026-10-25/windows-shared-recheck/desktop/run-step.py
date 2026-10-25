import subprocess,sys,json,datetime,time
from pathlib import Path
out=Path(__file__).resolve().parent; name=sys.argv[1]; args=sys.argv[2:]
start=datetime.datetime.now(datetime.timezone.utc).isoformat(); tick=time.monotonic()
with (out/(name+'.log')).open('wb') as stream:
 p=subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT)
result={'command':args,'start':start,'end':datetime.datetime.now(datetime.timezone.utc).isoformat(),'seconds':time.monotonic()-tick,'exitCode':p.returncode}
(out/(name+'.json')).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result));sys.exit(p.returncode)
