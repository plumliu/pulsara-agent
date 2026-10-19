import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import { FakeAdapter, projection, renderWorkbench, textPrompt, typeComposer } from './pulsara-app-fixture';

const review = {id: 'review-steer', kind: 'plan-draft' as const, workflowId: 'workflow-steer', workflowRevision: 7};
const pendingReview = () => ({...projection(''), messages: [], isRunning: false, planMode: true,
  activeTurnId: undefined, interaction: review});

function reviewAdapter() {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pendingReview();
  adapter.interactionContent = {kind: 'plan-draft', body: '# A plan for review'};
  return adapter;
}

describe('blocking input production conversation path', () => {
  it('queues an ordinary composer send during review without resolving the review', async () => {
    const adapter = reviewAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('A plan for review');
    const connection = adapter.lastConnection!;
    connection.submitPrompt.mockImplementationOnce(async commandId => ({commandId, status: 'pending', promptDelivery: {
      queueItemId: 'normal-queue', queueStatus: 'PENDING', deliveryMode: 'new-turn',
    }}));
    await typeComposer('do this after the plan');
    fireEvent.click(screen.getByRole('button', {name: '排队发送'}));
    await waitFor(() => expect(connection.submitPrompt).toHaveBeenCalledOnce());
    expect(connection.resolveInteraction).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', {name:'引导', exact:true})).toBeNull();
    expect(screen.getByText('A plan for review')).toBeTruthy();
    expect(screen.getByRole('region', {name: '等待处理的输入'}).textContent).toContain('do this after the plan');
  });

  it('queues the composer input and only steers after the queue send is clicked', async () => {
    const adapter = new FakeAdapter();
    const current = {...projection(''), control: {active_turns: [{turn_id:'turn-exact', scope_kind:'ROOT', status:'RUNNING'}]},
      interaction: {id:'permission', kind:'tool-confirmation' as const, prompt:'Allow write?', options:[], expiresAtUtc:'2099-01-01T00:00:00Z'}};
    adapter.connectionValue = current;
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    const connection = adapter.lastConnection!;
    connection.submitPrompt.mockImplementationOnce(async commandId => ({commandId, status:'pending', promptDelivery:{
      queueItemId:'permission-queue', queueStatus:'PENDING', deliveryMode:'new-turn',
    }}));
    const steer = vi.spyOn(connection, 'steerQueuedPrompt').mockImplementation(async commandId => ({commandId, status:'pending', promptDelivery:{
      queueItemId:'converted-steer', queueStatus:'PENDING', deliveryMode:'steer',
    }}));
    await typeComposer('keep the files unchanged');
    expect(screen.queryByRole('button', {name:'引导', exact:true})).toBeNull();
    fireEvent.click(screen.getByRole('button', {name:'排队发送'}));
    await waitFor(() => expect(connection.submitPrompt).toHaveBeenCalledOnce());
    expect(steer).not.toHaveBeenCalled();
    const commandId = connection.submitPrompt.mock.calls[0][0];
    act(() => connection.emit({...current, queuedCount:1, queuedPrompts:[{
      queueItemId:'permission-queue', commandId, sequence:1, status:'pending', inputOrigin:'HUMAN_MESSAGE',
      deliveryMode:'new-turn', content:textPrompt('keep the files unchanged'),
    }]}));
    const queueSend = within(screen.getByRole('region', {name:'等待处理的输入'})).getByRole('button', {name:'发送', exact:true});
    await waitFor(() => expect((queueSend as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(queueSend);
    await waitFor(() => expect(steer).toHaveBeenCalledWith(expect.any(String), 'permission-queue', 'turn-exact'));
    expect(connection.submitPrompt).toHaveBeenCalledOnce();
    expect(connection.resolveInteraction).not.toHaveBeenCalled();
  });

  it('converts an existing queued input to review revision without sending a copy', async () => {
    const adapter = reviewAdapter();
    const source = {queueItemId:'review-queue', commandId:'original-command', sequence:1, status:'pending' as const,
      inputOrigin:'HUMAN_MESSAGE', deliveryMode:'new-turn' as const, content:textPrompt('revise from queued input')};
    adapter.connectionValue = {...pendingReview(), queuedCount:1, queuedPrompts:[source]};
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('A plan for review');
    await typeComposer('unsent composer draft');
    expect(screen.queryByRole('button', {name:'引导', exact:true})).toBeNull();
    fireEvent.click(within(screen.getByRole('region', {name:'等待处理的输入'})).getByRole('button', {name:'发送', exact:true}));
    const connection = adapter.lastConnection!;
    await waitFor(() => expect(connection.resolveInteraction).toHaveBeenCalledWith(review,
      {kind:'plan-draft', decision:'revise', sourceQueueItemId:source.queueItemId}, expect.any(String)));
    expect(connection.submitPrompt).not.toHaveBeenCalled();
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('unsent composer draft');
    const commandId = connection.resolveInteraction.mock.calls[0][2]!;
    act(() => connection.emit({...projection(''), queuedPrompts:[], messages:[{id:'revision-input', turnId:'turn-plan-next', role:'user', body:'revise from queued input',
      time:'now', inputSource:{commandId, deliveryMode:'new-turn'}}], interaction:undefined}));
    await waitFor(() => expect(screen.getAllByText('revise from queued input')).toHaveLength(1));
    expect(screen.queryByRole('region', {name:'等待处理的输入'})).toBeNull();
  });
});
