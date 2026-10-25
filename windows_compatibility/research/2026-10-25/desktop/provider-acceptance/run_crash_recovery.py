"""One owned Host loss; original canonical memory/history are read by a fresh Host."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.windows_interop import configure_interop
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_model_switch_handover_dogfood import _ReadOnlySettingsStore, _RecordingModelRuntime, _binding, _create_database, _drop_database, _find_connection


async def child(config):
    assert require_pulsara_home()==Path(config['saved_home'])
    saved=LocalSettingsStore().read()
    scrub=saved.credential_scrub_set()
    selected=next(value for value in saved.model_connections if value.id.value==config['connection_id'])
    os.environ['PULSARA_HOME']=config['home']
    configure_interop(**config['interop'])
    settings=_ReadOnlySettingsStore(replace(saved,postgres=LocalPostgresConfig(config['runtime'],config['admin'])))
    catalog=ModelCatalogOwner(ModelsDevCatalogClient());await catalog.refresh()
    native=ModelRuntime.production(settings=settings,catalog=catalog)
    calls=[]
    core=KernelHostCore.production(model_runtime=_RecordingModelRuntime(native,calls,None))
    try:
        session=await core.open_session(HostWorkspaceInput(workspace_kind='project',workspace_root=Path(config['workspace']),trust_workspace_mcp_config=False))
        assert await session.attach_controller('crash-recovery-child')
        await session.update_model_call_binding(_binding(native,selected))
        outcome=await session.run_turn(PromptContent.text('请只调用 remember，把这条 GLOBAL FACT 保存：项目 '+config['marker']+' 的测试地点是 C 楼。不要调用终端、文件或其他服务；工具成功后简短确认。'),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        trace=await asyncio.to_thread(_tool_trace,session)
        assert trace and any(item['tool_name']=='remember' and item['result_state']=='SUCCESS' for item in trace)
        assert all(item['tool_name'] in {'remember','memory_get','memory_search'} and item['result_state']=='SUCCESS' for item in trace)
        tools=session._runner._tools
        processes=tools._terminal.list_processes(owner_host_session_id=tools._host_owner_id,include_running=True,include_finished=True)
        assert not processes
        descendants=set()
        for task in Path('/proc/self/task').iterdir():
            descendants.update((task/'children').read_text().split())
        assert not descendants
        with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,deadline_monotonic=monotonic()+30) as connection:
            facts=connection.execute('SELECT id,statement FROM pulsara_v3.memory_facts WHERE statement LIKE %s',('%'+config['marker']+'%',)).fetchall()
        assert len(facts)==1 and 'C 楼' in facts[0][1]
        seed={'session_id':session.session_id,'memory_id':facts[0][0],'statement':facts[0][1],'marker':config['marker'],
            'pid':os.getpid(),'starttime':Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],
            'terminal_processes':0,'linux_children':list(descendants),'tools':trace,'reply':outcome.final_text,'calls':calls}
        encoded=json.dumps(scrub.scrub_json(seed),ensure_ascii=False,indent=2)
        assert not scrub.contains(encoded)
        Path(config['seed']).write_text(encoded+'\n')
        print(json.dumps({'event':'accepted-and-idle','pid':os.getpid(),'session_id':session.session_id}),flush=True)
        # The parent either closes this finite test normally or kills its exact pidfd.
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await core.shutdown()


async def run(args,report):
    saved=LocalSettingsStore().read()
    scrub=saved.credential_scrub_set()
    database,_,admin,runtime=_create_database(saved)
    prior=os.environ.get('PULSARA_HOME')
    process=None
    core=None
    try:
        with TemporaryDirectory(prefix='crash-history-') as temporary:
            root=Path(temporary);(root/'home').mkdir()
            workspace=root/'中文 空格异常恢复';workspace.mkdir()
            settings=_ReadOnlySettingsStore(replace(saved,postgres=LocalPostgresConfig(runtime,admin)))
            actual_home=require_pulsara_home()
            seed_path=args.output.with_name('crash-seed.json')
            assert not seed_path.exists()
            config={'saved_home':str(args.saved_home),'home':str(actual_home),'workspace':str(workspace),
                'connection_id':args.connection_id,'admin':admin,'runtime':runtime,'marker':'LOSTHOST_'+uuid4().hex[:12].upper(),
                'interop':{'helper_path':str(args.interop_helper),'distribution':args.distribution,'user':args.user},'seed':str(seed_path)}
            config_path=root/'child-config.json';config_path.write_text(json.dumps(config))
            bootstrap="import sys,runpy,pulsara_agent;assert pulsara_agent.__file__.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/');sys.path.insert(0,sys.argv.pop(1));sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
            env=dict(os.environ);env['PULSARA_HOME']=str(args.saved_home);env.pop('PYTHONPATH',None)
            process=subprocess.Popen([sys.executable,'-I','-c',bootstrap,str(Path(__file__).parent),__file__,'--child',str(config_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
            line=await asyncio.wait_for(asyncio.to_thread(process.stdout.readline),STAGE2_LIMITS.foreground_io_timeout_ms/1000)
            assert line and json.loads(line)['event']=='accepted-and-idle'
            seed=json.loads(seed_path.read_text())
            assert seed['pid']==process.pid and seed['terminal_processes']==0 and not seed['linux_children']
            descriptor=os.pidfd_open(process.pid)
            try:
                observed=Path(f'/proc/{process.pid}/stat').read_text().rsplit(')',1)[1].split()[19]
                assert observed==seed['starttime']
                assert 'run_crash_recovery.py' in Path(f'/proc/{process.pid}/cmdline').read_bytes().replace(b'\0',b' ').decode()
                signal.pidfd_send_signal(descriptor,signal.SIGKILL)
                code=await asyncio.to_thread(process.wait)
                assert code==-signal.SIGKILL
                report['host_loss']={'pid':process.pid,'starttime':observed,'pidfd':True,'signal':'SIGKILL','exit':code,'terminal_processes':0,'linux_children':[],'ordinary_windows_tasks_submitted':False}
            finally:os.close(descriptor)
            child_out,child_err=process.communicate()
            report['child_output_after_gate']=scrub.scrub_text(child_out)
            report['child_stderr']=scrub.scrub_text(child_err)
            report['provider_calls'].extend(seed.pop('calls'))
            catalog=ModelCatalogOwner(ModelsDevCatalogClient());await catalog.refresh()
            native=ModelRuntime.production(settings=settings,catalog=catalog)
            selected=_find_connection(saved,args.model)
            core=KernelHostCore.production(model_runtime=native)
            session=await core.resume_session(seed['session_id'],workspace_input=HostWorkspaceInput(workspace_kind='project',workspace_root=workspace,trust_workspace_mcp_config=False))
            assert await session.attach_controller('crash-recovery-parent')
            await session.update_model_call_binding(_binding(native,selected))
            prompt='这是异常退出后的全新Host。请先调用 memory_get 读取 '+seed['memory_id']+'，再用 search_session_content 在当前会话查找 '+seed['marker']+' 并用 read_session_content 读取命中原文。不要重新 remember，不执行旧任务，不写文件或调用终端。最终报告原来保存的地点及 RECOVERED_'+seed['marker']+'。'
            outcome=await session.run_turn(PromptContent.text(prompt),requested_permission_mode=PermissionMode.READ_ONLY)
            trace=await asyncio.to_thread(_tool_trace,session)
            after=[row for row in trace if row['request_sequence']>max(item['request_sequence'] for item in seed['tools'])]
            names={row['tool_name'] for row in after}
            assert {'memory_get','search_session_content','read_session_content'}<=names
            assert all(row['result_state']=='SUCCESS' for row in after)
            assert not (names & {'remember','terminal','write_file','artifact_export'})
            assert 'C 楼' in outcome.final_text and 'RECOVERED_'+seed['marker'] in outcome.final_text
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,deadline_monotonic=monotonic()+30) as connection:
                count=connection.execute('SELECT count(*) FROM pulsara_v3.memory_facts WHERE statement LIKE %s',('%'+seed['marker']+'%',)).fetchone()[0]
            assert count==1
            report['cases'].append({'passed':True,'seed':seed,'resume_prompt':prompt,'resume_reply':outcome.final_text,'resume_tools':after,'fact_count_after':count,'boundary':'OS-killed idle test Host -> original fresh Core resume; no terminal recovery or side-effect replay'})
            await core.shutdown();core=None
            report['status']='completed'
    finally:
        if process is not None and process.poll() is None:
            out,err=await asyncio.to_thread(process.communicate,'quit\n')
            report['child_fallback_stdout']=scrub.scrub_text(out)
            report['child_fallback_stderr']=scrub.scrub_text(err)
        elif process is not None and 'child_stderr' not in report:
            out,err=process.communicate()
            report['child_fallback_stdout']=scrub.scrub_text(out)
            report['child_fallback_stderr']=scrub.scrub_text(err)
        if core is not None:await core.shutdown()
        if prior is None:os.environ.pop('PULSARA_HOME',None)
        else:os.environ['PULSARA_HOME']=prior
        _drop_database(saved,database)


if __name__=='__main__':
    assert sys.argv[1]=='--child'
    asyncio.run(child(json.loads(Path(sys.argv[2]).read_text())))
