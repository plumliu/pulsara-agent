import { createContext } from 'react';

/** Transient UI actions; annotation content remains in the Tiptap document. */
export const DraftAnnotationContext = createContext<{
  editable: boolean;
  activeId?: string;
  edit: (id: string, anchor: HTMLElement) => void;
  preview: (id?: string) => void;
}>({ editable: false, edit: () => {}, preview: () => {} });
