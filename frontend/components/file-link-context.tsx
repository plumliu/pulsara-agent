import { createContext } from 'react';
export const FileLinkContext = createContext<{
  imageOwnerKey?: string;
  readImageReference?: (ref: string, signal: AbortSignal) => Promise<import('../lib/runtime-adapter').SavedImageData>;
  open: (path: string, opener: HTMLElement, base?: string, newTab?: boolean) => void;
  basePreview?: string;
  imagesUrl?: string;
} | undefined>(undefined);

/** File previews own read leases; the gallery owns no separate media service. */
export const FileMediaContext = createContext<{
  api: import('../lib/file-preview').FilePreviewApi;
  ownerKey: string;
  sessionId: string;
  open: (path: string, time?: number, version?: {modified_ns: string; size: number}) => void;
} | undefined>(undefined);
