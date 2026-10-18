import { createContext } from 'react';
export const FileLinkContext = createContext<{
  imageOwnerKey?: string;
  readImageReference?: (ref: string, signal: AbortSignal) => Promise<import('../lib/runtime-adapter').SavedImageData>;
  open: (path: string, opener: HTMLElement, base?: string, newTab?: boolean) => void;
  basePreview?: string;
  imagesUrl?: string;
} | undefined>(undefined);
