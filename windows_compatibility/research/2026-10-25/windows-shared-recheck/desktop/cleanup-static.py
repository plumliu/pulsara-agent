from pathlib import Path
import subprocess,json
root=Path.cwd().resolve(); prefix='src/pulsara_agent/desktop_app/static'; static=(root/prefix).resolve()
out=Path('/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/desktop')
review=json.loads((out/'static-review-all.json').read_text()); assert review['sameSourceContentAfterCRLF'] and review['payloadAlreadyWritten']
raw=subprocess.check_output(['git','status','--porcelain=v1','-z','--untracked-files=all','--',prefix]).decode().split('\0'); entries=[x for x in raw if x]
assert entries
untracked=[x[3:] for x in entries if x.startswith('?? ')]
for name in untracked:
 p=root/name; assert p.resolve().is_relative_to(static) and p.is_file() and not p.is_symlink()
subprocess.run(['git','restore','--source=HEAD','--worktree','--',prefix],check=True)
for name in untracked: (root/name).unlink()
remaining=subprocess.check_output(['git','status','--porcelain=v1','--',prefix]).decode(); assert not remaining,remaining
record={'review':'static-review-all.json','payloadPreserved':True,'trackedRestoredFromHEADOnlyIn':prefix,'generatedUntrackedFilesRemoved':untracked,'beforeStatus':entries,'afterStaticStatus':remaining,'noOtherDirtyTouched':True}
(out/'static-cleanup.json').write_text(json.dumps(record,indent=2)+'\n')
print('Reviewed static restored only:',len(entries),'entries;',len(untracked),'exact generated files removed')
