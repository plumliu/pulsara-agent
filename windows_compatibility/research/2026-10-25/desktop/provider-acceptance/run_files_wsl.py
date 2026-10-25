"""Reuse existing provider dogfood with reviewed UUID-DB/short-home factories."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pg_adapter


def driver(api, kind, args):
    from pulsara_agent.llm.runtime import ModelRuntime
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
    from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
    from tests.dogfood import run_artifact_export_dogfood as artifact
    from tests.dogfood import run_direct_memory_dogfood as memory
    from tests.dogfood import run_session_content_query_dogfood as history
    from tests.dogfood import run_turn_interruption_history_dogfood as interruption
    import run_crash_recovery as crash
    from tempfile import TemporaryDirectory

    module = {'artifact':artifact,'memory':memory,'history':history,'interruption':interruption,'crash':crash}[kind]
    facade = SimpleNamespace(LocalSettingsStore=api.LocalSettingsStore,
        _create_database=api.shared._create_database,_drop_database=api.shared._drop_database,
        _find_connection=api.shared._find_connection,_ReadOnlySettingsStore=api.shared._ReadOnlySettingsStore,
        ModelRuntime=ModelRuntime,PromptContent=PromptContent,TemporaryDirectory=TemporaryDirectory,save=None)

    async def run(runner_args,report):
        report.update(runner=module.__name__,full_production_tool_surface=True)
        previous_inputs={}
        checks=[]
        original=ConversationKernelRunner.__init__
        def initialized(owner,*pos,**kw):
            previous=kw.get('provider_input_installed_observer')
            def observe(request):
                compiled=request.compiled_input
                identity=compiled.canonical_input_identity
                scope=ProviderInputContinuityScope(session_id=request.session_id,
                    scope_kind=identity.conversation_scope_kind,scope_subagent_task_id=identity.scope_subagent_task_id)
                view=owner._continuity.current_view(scope)
                assert view is not None
                key=(scope,view.epoch_nonce)
                old=previous_inputs.get(key)
                if old is not None:
                    assert compiled.system_prompt==old.system_prompt
                    assert compiled.tools==old.tools
                    assert compiled.messages[:len(old.messages)]==old.messages
                previous_inputs[key]=compiled
                checks.append({'session':request.session_id,'scope':identity.conversation_scope_kind.value,
                    'epoch':view.epoch_nonce,'revision':view.epoch_revision,'compared':old is not None,
                    'messages':len(compiled.messages)})
                if previous is not None:previous(request)
            kw['provider_input_installed_observer']=observe
            original(owner,*pos,**kw)
        report['prefix_checks']=checks
        with ExitStack() as stack:
            for name,value in {'LocalSettingsStore':facade.LocalSettingsStore,
                '_create_database':facade._create_database,'_drop_database':facade._drop_database,
                '_ReadOnlySettingsStore':facade._ReadOnlySettingsStore,
                'TemporaryDirectory':facade.TemporaryDirectory}.items():
                stack.enter_context(patch.object(module,name,value))
            stack.enter_context(patch.object(ConversationKernelRunner,'__init__',initialized))
            if kind=='artifact':
                stack.enter_context(patch.object(module,'_find_connection',facade._find_connection))
                stack.enter_context(patch.object(module,'save',facade.save))
                runner_args.phase='candidate'
                await module.run(runner_args,report)
            elif kind in {'memory','crash'}:
                stack.enter_context(patch.object(module,'ModelRuntime',facade.ModelRuntime))
                if kind=='crash':await module.run(args,report)
                else:
                    result=await module._run(model_id=args.model,wire_api='openai_chat_completions')
                    report['cases'].append(result)
                    report['status']='completed' if result['passed'] else 'semantic_failure'
            else:
                result=await module.run()
                report['provider_calls'].extend(result.pop('calls'))
                report['cases'].append(result)
                report['status']='completed'
    facade.run=run
    return facade


def main():
    parser=pg_adapter.parser()
    parser.add_argument('--kind',choices=('artifact','memory','history','interruption','crash'),required=True)
    args=parser.parse_args()
    api=pg_adapter.imports()
    api.terminal=driver(api,args.kind,args)
    if args.check_imports:
        import pulsara_agent
        assert pulsara_agent.__file__.startswith('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/')
        print('Installed package and original runners imported; no settings/PG/provider accessed.')
        return 0
    required=('saved_home','connection_id','model','distribution','user','expected_uid','interop_helper','socket','port',
        'admin_role','runtime_role','root_database','expected_data','expected_system_id','scratch_root','output')
    missing=[name for name in required if getattr(args,name) is None]
    if missing:parser.error('execute requires '+', '.join(missing))
    os.umask(0o077)
    scrub=api.LocalSettingsStore(args.saved_home/'local-settings.yaml').read().credential_scrub_set()
    stdout,stderr=io.StringIO(),io.StringIO()
    try:
        with redirect_stdout(stdout),redirect_stderr(stderr):return pg_adapter.run(args,api)
    finally:
        out,err=scrub.scrub_text(stdout.getvalue()),scrub.scrub_text(stderr.getvalue())
        assert not scrub.contains(out) and not scrub.contains(err)
        print(out,end='')
        __import__('sys').stderr.write(err)

if __name__=='__main__':raise SystemExit(main())
