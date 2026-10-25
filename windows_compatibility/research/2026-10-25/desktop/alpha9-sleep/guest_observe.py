"""Read-only installed kernel/PG/lock/session/TLS observations. No settings reads."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import sys
from datetime import datetime, timezone


def identity(pid):
    process=Path(f'/proc/{pid}')
    fields=(process/'stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'starttime':fields[19],'pidNamespace':os.readlink(process/'ns/pid'),'executable':os.readlink(process/'exe')}


def lock(path):
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        facts=os.fstat(descriptor)
        assert stat.S_ISREG(facts.st_mode) and facts.st_uid==os.getuid() and facts.st_nlink==1
        busy=False
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:busy=True
        return {'path':str(path),'inode':facts.st_ino,'device':facts.st_dev,'uid':facts.st_uid,'mode':oct(stat.S_IMODE(facts.st_mode)),'busy':busy}
    finally:os.close(descriptor)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--session-id',required=True)
    parser.add_argument('--kernel-pid',type=int,required=True)
    args=parser.parse_args()
    assert 1<=args.port<=65535 and os.getuid()==1000
    import psycopg
    import pulsara_agent
    from pulsara_agent.capability.pulsara_home import require_pulsara_home
    home=require_pulsara_home();assert home==Path('/home/plumwsl/.pulsara')
    assert pulsara_agent.__file__.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.9/')
    kernel_pid=args.kernel_pid
    pg_pid=int((home/'desktop/postgres/postmaster.pid').read_text().splitlines()[0])
    descriptors=[os.pidfd_open(pid) for pid in (kernel_pid,pg_pid)]
    try:
        kernel=identity(kernel_pid);postgres=identity(pg_pid)
        assert kernel['executable'].startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.9/')
        owned_sockets=set()
        for path in Path(f'/proc/{kernel_pid}/fd').iterdir():
            try:link=os.readlink(path)
            except FileNotFoundError:continue
            if link.startswith('socket:['):owned_sockets.add(link[8:-1])
        listener=[]
        for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
            fields=line.split()
            if fields[1]==f'0100007F:{args.port:04X}' and fields[3]=='0A':listener.append(fields[9])
        assert len(listener)==1 and listener[0] in owned_sockets
        with psycopg.connect('host=/home/plumwsl/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_admin') as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            row=connection.execute("SELECT system_identifier::text,current_setting('data_directory'),current_setting('unix_socket_directories'),current_setting('listen_addresses'),inet_server_addr(),pg_postmaster_start_time()::text FROM pg_control_system()").fetchone()
            assert row[:5]==('7700523439330484737',str(home/'desktop/postgres'),str(home/'desktop/socket'),'',None)
            original=args.session_id
            session=connection.execute('SELECT id,title,workspace_id,latest_entry_sequence,writer_generation,writer_lease_owner_id,writer_lease_expires_at::text,writer_lease_expires_at > clock_timestamp(),clock_timestamp()::text FROM pulsara_v3.sessions WHERE id=%s',(original,)).fetchone();assert session
            turns=connection.execute('SELECT id,status FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id',(original,)).fetchall()
        # Read the public cert from the listener whose exact kernel fd was checked.
        context=ssl._create_unverified_context()
        with socket.create_connection(('127.0.0.1',args.port),timeout=10) as transport:
            with context.wrap_socket(transport,server_hostname=None) as tls:certificate=tls.getpeercert(binary_form=True)
        assert identity(kernel_pid)==kernel and identity(pg_pid)==postgres
        locks=[lock(home/'desktop'/name) for name in ('kernel.lock','pg-lifetime.lock')]
        assert all(value['busy'] for value in locks)
        result={'readOnly':True,'utc':datetime.now(timezone.utc).isoformat(),'version':pulsara_agent.__version__,'package':pulsara_agent.__file__,
            'bootId':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'kernel':kernel,'postgres':postgres,'pidfdsOpenedAndClosedForObservation':True,
            'pgFacts':{'systemid':row[0],'data':row[1],'socket':row[2],'listen':row[3],'inetAddress':row[4],'postmasterStarted':row[5],'dataInode':(home/'desktop/postgres').stat().st_ino},
            'locks':locks,'diagnosticsSocketInode':(home/'desktop/diagnostics.sock').stat().st_ino,'http':{'origin':f'https://127.0.0.1:{args.port}','listenInode':listener[0],'ownedByKernel':True,'certificatePem':ssl.DER_cert_to_PEM_cert(certificate)},
            'session':session[:4],'turns':turns,'writer':{'generation':session[4],'owner':session[5],'leaseExpires':session[6],'leaseValid':session[7],'databaseObservedAt':session[8]},'settingsRead':False,'kernelSpawned':False,'pgLifecycleCommands':False}
        print(json.dumps(result,ensure_ascii=False))
    finally:
        for descriptor in descriptors:os.close(descriptor)


if __name__=='__main__':main()
