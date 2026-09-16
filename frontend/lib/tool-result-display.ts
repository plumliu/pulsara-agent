import { createContext } from 'react';

// Browser-only presentation preference, shared by the main chat and task views.
export const ToolResultDisplayContext = createContext<{
  showBuiltinToolResults: boolean;
  onChange: (show: boolean) => void;
}>({
  showBuiltinToolResults: false,
  onChange: () => {},
});

export function readSavedToolResultDisplay(): boolean {
  try {
    return window.localStorage?.getItem('pulsara-show-builtin-tool-results') === 'true';
  } catch {
    return false;
  }
}
