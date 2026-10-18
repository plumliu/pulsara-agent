import { createContext } from 'react';
import { uiPreferences } from './ui-preferences';

// Desktop presentation preference, shared by the main chat and task views.
export const ToolResultDisplayContext = createContext<{
  showBuiltinToolResults: boolean;
  onChange: (show: boolean) => void;
}>({
  showBuiltinToolResults: false,
  onChange: () => {},
});

export function readSavedToolResultDisplay(): boolean {
  return uiPreferences.getItem('pulsara-show-builtin-tool-results') === 'true';
}
