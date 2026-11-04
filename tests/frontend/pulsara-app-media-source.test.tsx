import { useContext, type ReactNode } from 'react';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import { OpenMediaNoteSource } from '../../frontend/components/media-annotation-view';
import { FakeAdapter, renderWorkbench } from './pulsara-app-fixture';

const note={resultId:'entry:source-result',entryId:'entry:source-result',sessionId:'session:archived-source',from:1,to:2,text:'Original annotation'};
function MediaTrigger({children}:{children:ReactNode}) {
  const open=useContext(OpenMediaNoteSource);
  return <><audio data-testid="active-player" src="/active-media"/><button onClick={()=>open?.(note)}>source trigger</button>{children}</>;
}
vi.mock('../../frontend/components/file-browser',async importOriginal=>({...await importOriginal<typeof import('../../frontend/components/file-browser')>(),FilePreviewProvider:MediaTrigger}));
vi.mock('../../frontend/components/media-note-source-dialog',()=>({MediaNoteSourceDialog:({onClose}:{onClose:()=>void})=><div role="dialog" aria-label="批注来源调用"><button onClick={onClose}>关闭来源</button></div>}));

it('source review leaves the current runtime/player mounted and consumes the source intent on close or navigation',async()=>{
  const adapter=new FakeAdapter();
  renderWorkbench(<PulsaraApp adapter={adapter}/>);
  await screen.findByLabelText('发送给 Pulsara');
  const connection=adapter.lastConnection,player=screen.getByTestId('active-player');
  const opened=adapter.connectCalls.length;
  fireEvent.click(screen.getByRole('button',{name:'source trigger'}));
  expect(screen.getByRole('dialog',{name:'批注来源调用'})).toBeDefined();
  expect(adapter.lastConnection).toBe(connection);
  expect(adapter.connectCalls).toHaveLength(opened);
  expect(screen.getByTestId('active-player')).toBe(player);
  fireEvent.click(screen.getByRole('button',{name:'关闭来源'}));
  expect(screen.queryByRole('dialog',{name:'批注来源调用'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'source trigger'}));
  fireEvent.click(screen.getByRole('button',{name:'设置',exact:true}));
  await waitFor(()=>expect(screen.queryByRole('dialog',{name:'批注来源调用'})).toBeNull());
  fireEvent.click(screen.getByRole('button',{name:'会话',exact:true}));
  expect(screen.queryByRole('dialog',{name:'批注来源调用'})).toBeNull();
  expect(adapter.connectCalls).toHaveLength(opened);
});
