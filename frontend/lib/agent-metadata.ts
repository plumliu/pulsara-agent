import { useEffect, useState } from 'react';
import { agentCellApi, AgentCellError, type AgentRow } from './agent-cell-api';

export interface AgentMetadata { agent?: AgentRow; unavailable?: boolean; error?: string }
/** One serial reader per visible Agent tab; its pane consumes the same snapshot. */
export function useAgentMetadata(root: string, agent: string): AgentMetadata {
  const [state, setState] = useState<AgentMetadata>({});
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setState({});
    const refresh = async () => {
      try {
        const result = await agentCellApi.agent(root, agent, controller.signal);
        if (!controller.signal.aborted) setState({agent: result.agent});
      } catch (cause) {
        if (!controller.signal.aborted) {
          const unavailable = cause instanceof AgentCellError && (cause.status === 404 || cause.status === 410);
          setState(previous => unavailable ? {unavailable:true} : {...previous, error:cause instanceof Error ? cause.message : '暂时无法读取子代理。'});
        }
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void refresh(), 3000);
      }
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, agent]);
  return state;
}
