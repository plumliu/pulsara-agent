"""Current project-memory fixtures without staging tools or relation graphs."""
from time import monotonic
from pulsara_agent.identity import random_id
from pulsara_agent.memory.scope import MemoryDomainContext,freeze_memory_read_context_binding,workspace_context_id


def project_memory_session(repo, workspace_root):
    workspace_id=workspace_context_id(workspace_root)
    lease=repo.acquire_host_writer(intent='NEW',session_id=random_id('session'),workspace_id=workspace_id,
        workspace_root=workspace_root,writer_owner_id=random_id('host'),lease_seconds=300,
        deadline_monotonic=monotonic()+30)
    binding=freeze_memory_read_context_binding(domain=MemoryDomainContext('u_local','project',stable_project_key=workspace_root),host_workspace_id=workspace_id)
    return lease,binding


def project_memory(repo,binding,title,statement):
    return repo.create_memory(memory_domain_id=binding.memory_domain_id,context_id=binding.current_project_context_id,
        title=title,statement=statement,kind='FACT',read_binding=binding,deadline_monotonic=monotonic()+30)
